"""botたん AITuber ライブ配信のオーケストレータ。

  20:40  各サービスを起動（ARDY は読み込みに4〜5分かかるので最初に投げる）
  20:50  YouTube の配信枠を作り、OBS から送出を始める
  21:00  live へ遷移してオープニング
  〜21:55 コメントに返事。途切れたらフリートーク
  21:55  クロージング
  22:00  complete して片付け

配信中は何が落ちても配信自体は続ける方針。詳細は _speak と各 except を参照。
"""

import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta

import chat
import energy
import filler
import idle
import llm
import memory
import gauge
import motion as motion_mod
import notify
import persona
import safety
import schedule as live_schedule
import subtitle
import unity_client
import unity_live
import voice
from config import (
    DRY_RUN, ENERGY_REFRESH_SEC, FILLER_IDLE_SEC, IDLE_ENABLED, LIVE_CLOSING_HHMM,
    LIVE_END_HHMM, LIVE_GO_LIVE_RETRY_SEC, LIVE_START_HHMM, LIVE_TESTING_LEAD_SEC,
    BOT_CONTEXT_TTL_SEC, FPS_LOG_SEC, SKIP_ARDY, SUBTITLE_LEAD_SEC, UNITY_PROJECT,
    WORK_DIR, ensure_dirs,
)

# LLM が落ちたときに使う定型。無言になるよりはよい
FALLBACK_LINES = [
    [{"ja": "ありがとう、うれしいよ。", "en": "Thank you, that makes me happy."}],
    [{"ja": "そうなんだね、聞かせてくれてありがとう。",
      "en": "I see. Thanks for telling me."}],
    [{"ja": "コメントくれてうれしい。", "en": "I'm glad you commented."}],
]


def _today_at(hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)


def _sleep_until(when: datetime, label: str) -> None:
    wait = (when - datetime.now()).total_seconds()
    if wait > 0:
        print(f"[live] {label} {wait:.0f} 秒待ちます")
        time.sleep(wait)


class LiveSession:
    def __init__(self):
        self.unity = unity_live.UnityLive(log_path=WORK_DIR / "unity.log")
        self.pool = motion_mod.MotionPool()
        self.ardy = motion_mod.ArdyWorker(self.pool)
        self.queue = chat.CommentQueue()
        self.planner = filler.FillerPlanner()
        self.subs = subtitle.SubtitleScheduler()
        self.idle = idle.IdleAnimator(self.pool, enabled=IDLE_ENABLED)

        self.broadcast = None
        self.obs = None
        self.poller = None
        self.memory_writer = memory.BotMemoryWriter()

        self.system_prompt = persona.build_system_prompt()
        self.recent_replies = []
        self.replied_count = 0
        self.last_speech_at = 0.0
        self.started_at = None
        self._stopping = False
        # 直近の発話が実際に鳴り始めた時刻。反応までの内訳を出すのに使う
        self._speech_started_at = 0.0
        # fps を最後に出した時刻
        self._fps_at = 0.0
        # botたんの状態のキャッシュ（_bot_context 参照）
        self._bot_cache = None
        self._bot_cache_at = 0.0
        # メインループを止めない雑務スレッド（_start_chores 参照）
        self._chores = None

    # ── 準備 ──────────────────────────────────────────

    def prepare(self) -> None:
        ensure_dirs()
        subtitle.clear()
        subtitle.write_clock()

        print("[準備] DB を確認します")
        memory.ensure_schema()
        self.memory_writer.start()
        bot = memory.get_biorhythm()
        print(f"[準備] botたんの状態: {bot['status']} / energy={bot['energy']:.1f} / {bot['mood'][:40]}")

        print("[準備] 読み辞書を確認します")
        voice.preload_pronunciations()
        print("[準備] VOICEVOX を確認します")
        speaker = voice.health_check()
        print(f"[準備] 話者: {speaker}")

        # ARDY は読み込みに4〜5分かかる。待っている間に他を進めたいので
        # ここで投げて、Unity の起動と並行させる
        if SKIP_ARDY:
            print("[準備] SKIP_ARDY のため ARDY は起動しません")
            ardy_ok = False
        else:
            print("[準備] ARDY を起動します（読み込みに4〜5分かかります）")
            ardy_ok = self.ardy.start(wait=True)
        print(f"[準備] ARDY: {'利用可' if ardy_ok else '利用不可（プールのモーションで配信します）'}")
        print(f"[準備] モーションプール: {self.pool.summary()}")
        if ardy_ok:
            self.ardy.prewarm(per_category=3)

        print("[準備] Unity を起動します")
        self.unity.start(ready_timeout=420)

        self.planner.refresh_memory()

        # 喋っていない間の身振りと表情。Unity の /status を見て動くので、
        # 配信ループとは独立に回してよい
        self.idle.start()

    def connect_obs(self):
        if DRY_RUN:
            print("[準備] DRY_RUN のため OBS には接続しません")
            return None
        import obs as obs_mod
        obs_mod.launch()
        self.obs = obs_mod.Obs().connect()
        # Unity は prepare() で起動済み。窓の ID もタイトルも起動のたびに変わるので、
        # シーンに保存されたキャプチャ先をここで今の窓へ合わせる
        self.obs.bind_window_capture(UNITY_PROJECT)
        # 字幕は websocket で直接流し込む。ファイル経由だと OBS が読みに行くのが
        # 約1秒に1回で、そのぶん発話より字幕が遅れる
        if self.obs.bind_text_sources():
            subtitle.set_sink(self.obs.set_text)
        return self.obs

    # ── 配信枠 ────────────────────────────────────────

    def create_broadcast(self) -> None:
        if DRY_RUN:
            print("[YouTube] DRY_RUN のため配信枠は作りません")
            return

        import broadcast as broadcast_mod
        scheduled_start, scheduled_end = live_schedule.today_schedule()
        title, description = live_schedule.broadcast_text(scheduled_start)

        prepared = memory.get_prepared_broadcast(scheduled_start)
        if prepared:
            self.broadcast = broadcast_mod.Broadcast.load(prepared["broadcast_id"])
            if self.broadcast:
                print(f"[YouTube] 4時に準備した配信枠を再利用: {self.broadcast.url}")

        # DB保存だけ失敗した場合もYouTube上の同日枠を拾い、重複作成しない。
        if self.broadcast is None:
            self.broadcast = broadcast_mod.Broadcast.find_scheduled(scheduled_start)
            if self.broadcast:
                print(f"[YouTube] YouTube上の当日配信枠を再利用: {self.broadcast.url}")

        if self.broadcast is None:
            print("[YouTube] 準備済み枠がないため、その場で作成します")
            self.broadcast = broadcast_mod.Broadcast().create_event(
                title, description, scheduled_start,
            )

        try:
            broadcast_mod.cleanup_stale(
                preserve_ids=[self.broadcast.broadcast_id],
                scheduled_before=scheduled_start,
            )
        except Exception as e:
            print(f"[YouTube] 古い枠の片付けに失敗（続行します）: {e}")

        memory.save_prepared_broadcast(
            self.broadcast.broadcast_id,
            self.broadcast.url,
            self.broadcast.title or title,
            scheduled_start,
            scheduled_end,
        )
        self.broadcast.create_stream_and_bind()

        self.obs.set_stream_target(
            self.broadcast.ingestion_address, self.broadcast.stream_name)
        self.obs.start_stream()

        if not self.broadcast.wait_for_ingestion(timeout=180):
            raise RuntimeError("OBS からの映像が YouTube に届きません")

        memory.start_broadcast(self.broadcast.broadcast_id,
                               self.broadcast.url, title)

    def start_testing(self) -> None:
        """配信開始の前にモニターストリームを立ち上げておく。

        ここで testing まで入れておかないと、開始時刻に投げる live が
        testStarting の途中に当たって 403 で弾かれる。入れなかった場合も
        go_live() 側が投げ直すので、ここでは止めない。
        """
        if self.broadcast is None:
            return
        if not self.broadcast.start_testing():
            print("[YouTube] testing まで入れませんでした（開始時刻に投げ直します）")

    def go_live(self) -> None:
        if self.broadcast is not None:
            if not self.broadcast.go_live(LIVE_GO_LIVE_RETRY_SEC):
                raise RuntimeError("live への遷移に失敗しました")
            notify.live_started(self.broadcast.url, self.broadcast.title)

        live_chat_id = self.broadcast.live_chat_id if self.broadcast else None
        def remember_comment(comment):
            self.memory_writer.ingest_comment(
                comment.message_id,
                self.broadcast.broadcast_id if self.broadcast else "dry-run",
                comment.channel_id,
                comment.author,
                comment.text,
                {"isSuperChat": comment.is_super_chat,
                 "isMember": comment.is_member,
                 "isOwner": comment.is_owner},
            )

        self.poller = chat.make_poller(
            live_chat_id, self.queue,
            on_comment=remember_comment,
            on_delete=self.memory_writer.tombstone,
        )
        self.poller.start()
        self.started_at = time.monotonic()

    # ── 発話 ──────────────────────────────────────────

    def _bot_context(self, max_age: float = BOT_CONTEXT_TTL_SEC) -> dict:
        """いまの気分・行動・energy。コメント返信の直前で必ず呼ばれる。

        DB は別マシン（`common/db.py` の DB_HOST）にあり、接続はプールせず
        毎回張り直す。ここが詰まるとメインループごと止まってコメントに反応
        できなくなるので、

          - `energy.get_energy()` は呼ばない。`memory.get_biorhythm()` が
            返す energy と同じ行なので、以前は同じ行を2回引くために接続を
            2本張っていた
          - 数秒ぶんはキャッシュを使い回す。energy は分単位でしか動かない

        引けなかったときは直前の値を使う。無ければ当たり障りのない既定値。
        """
        now = time.monotonic()
        if self._bot_cache is not None and now - self._bot_cache_at <= max_age:
            bot = dict(self._bot_cache)
            bot["now"] = datetime.now().strftime("%H:%M")
            return bot

        try:
            bot = memory.get_biorhythm()
            self._bot_cache = dict(bot)
            self._bot_cache_at = now
        except Exception as e:
            print(f"[live] biorhythm を引けません（直前の値で続けます）: {e}")
            bot = dict(self._bot_cache or {"energy": 50.0, "mood": "", "status": ""})
        bot["now"] = datetime.now().strftime("%H:%M")
        return bot

    def _generate(self, user_prompt: str) -> dict:
        """LLM に投げる。落ちたら定型で返す（無言にしない）。"""
        try:
            return llm.generate_reply(self.system_prompt, user_prompt)
        except Exception as e:
            print(f"[live] LLM が応答しません、定型で返します: {e}")
            import random
            return {
                "lines": random.choice(FALLBACK_LINES),
                "valence": 0.5, "arousal": 0.2,
                "motion_category": "neutral", "motion_en": "",
                "_fallback": True,
            }

    def _speak(self, reply: dict, tag: str = "", interruptible: bool = False) -> bool:
        """1回ぶんの発話。音声・モーション・字幕をまとめて行い、喋り終わるまで待つ。

        文は1つずつ合成して Unity のキューへ流し込む。全文の合成を待ってから
        送ると、そのぶん喋り出しが遅れる（コメントへの反応が体感で数秒遅くなる）。

        interruptible なら、文と文の切れ目でコメントの有無を見て切り上げる。
        フリートークを最後まで喋りきってからでないとコメントに反応できない、
        という往復の遅さが初回の配信でいちばん効いていた。

        どこかが落ちても配信は続ける。返り値は実際に喋れたかどうか。
        """
        lines = safety.sanitize_reply_lines(reply.get("lines"))
        if not lines:
            print("[live] 読み上げる文が残らなかったのでスキップします")
            return False

        valence = reply.get("valence", 0.0)
        arousal = reply.get("arousal", 0.0)
        category = reply.get("motion_category", "neutral")

        # 1) モーション。合成を待たずに先に動かす。プールから即座に引く。
        #    投げるのは idle スレッドの仕事（/motion の送出者を1つにしておかないと、
        #    フェードイン途中のクリップを打ち切って Idle が一瞬混ざる）。
        #    発話が終わるまで、クリップが切れる手前で継ぎ足し続けてくれる
        self.idle.speak_begin(category, valence, arousal)

        # 2) ARDY へ非同期で投げる。次の発話以降で使えるようになる
        motion_en = safety.sanitize_motion(reply.get("motion_en", ""))
        if motion_en:
            self.ardy.submit(motion_en, category)

        prefix = f"utt_{int(time.time() * 1000)}"
        began = time.monotonic()
        spoken = 0
        self.subs.begin()

        for i, line in enumerate(lines):
            if i > 0:
                # いまの文が終わる少し手前まで待つ。ここが割り込みの窓になる
                if self._wait_line(last_dur, interruptible):
                    print(f"[live] コメントが来たので{tag or '発話'}を"
                          f"{i}/{len(lines)}文で切り上げます")
                    break
            try:
                wav, durs = voice.synthesize_lines(
                    [line], WORK_DIR, f"{prefix}_{i:02d}", gap_sec=0.0,
                    tail_gap=0.0 if i == len(lines) - 1 else 0.25)
            except Exception as e:
                print(f"[live] 音声合成に失敗: {e}")
                if spoken == 0:
                    notify.warn(f"音声合成に失敗しました: {e}")
                break

            try:
                unity_client.speak(wav, valence, arousal)
            except Exception as e:
                print(f"[live] Unity へ発話を送れません: {e}")
                if spoken == 0:
                    notify.warn(f"Unity へ発話を送れません: {e}")
                break

            pushed = False
            if spoken == 0:
                # 字幕は Unity が実際に鳴らし始めてから走らせる（WAVのロード待ちを吸収）。
                # SUBTITLE_LEAD_SEC で前後にずらせる（config.py の解説を参照）
                if SUBTITLE_LEAD_SEC < 0:
                    self.subs.push(line, durs[0])
                    pushed = True
                self._wait_speech_start()
                self._speech_started_at = time.monotonic()
                print(f"[live] 喋り出しまで {self._speech_started_at - began:.1f}秒")
                if SUBTITLE_LEAD_SEC > 0:
                    time.sleep(SUBTITLE_LEAD_SEC)
            if not pushed:
                self.subs.push(line, durs[0])
            last_dur = durs[0]
            spoken += 1

        self.subs.finish()
        if spoken == 0:
            self.subs.stop()
            self.idle.speak_end()
            return False

        self._wait_speech_end()
        self.idle.speak_end()
        self.last_speech_at = time.monotonic()

        text = " ".join(l["ja"] for l in lines[:spoken])
        self.recent_replies.append(text)
        self.recent_replies = self.recent_replies[-8:]
        print(f"[live] 発話{f'({tag})' if tag else ''}: {text[:60]}")
        return True

    def _wait_line(self, duration: float, interruptible: bool,
                   lead: float = 0.7) -> bool:
        """いまの文が鳴り終わる少し手前まで待つ。割り込むべきなら True。

        次の文の合成には 1秒かからない（実測 0.05〜0.7秒）ので、lead ぶん
        手前で戻れば音は途切れない。
        """
        deadline = time.monotonic() + max(0.0, duration - lead)
        while time.monotonic() < deadline:
            if interruptible and self.queue.has_pending():
                return True
            time.sleep(0.1)
        return interruptible and self.queue.has_pending()

    @staticmethod
    def _wait_speech_start(timeout: float = 5.0) -> None:
        """Unity が実際に鳴らし始めるまで待つ。字幕を出す基準時刻になる。

        刻みが粗いとそのぶん字幕が音声より遅れるので細かく回す。
        /status は Unity のメインスレッドが書いた値を返すだけなので安い。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if unity_client.status().get("speaking"):
                    return
            except unity_client.UnityError:
                return
            time.sleep(0.02)

    def _wait_speech_end(self, timeout: float = 120.0) -> None:
        unity_client.wait_until_idle(timeout=timeout)

    # ── ループ ────────────────────────────────────────

    def reply_to_comment(self, comment) -> None:
        first = self.queue.is_first_time(comment.channel_id)
        self.queue.mark_seen(comment.channel_id)

        # 反応までの内訳を測る。これが無いと「15秒かかる」の原因が
        # 待ち行列なのか DB なのか LLM なのか切り分けられない。
        # comment.received_at は ChatPoller が受け取った瞬間（＝コメント欄に
        # 出した瞬間）なので、視聴者から見た体感とほぼ一致する
        t_pop = time.monotonic()
        bot = self._bot_context()
        t_ctx = time.monotonic()

        prompt = persona.build_comment_prompt(
            comment.author, comment.text, bot, self.planner.cache,
            is_first_time=first, is_super_chat=comment.is_super_chat,
            recent_replies=self.recent_replies,
        )
        reply = self._generate(prompt)
        t_llm = time.monotonic()
        if not self._speak(reply, tag=comment.author):
            return

        started = self._speech_started_at
        print(f"[live] 反応まで {started - comment.received_at:.1f}秒 "
              f"(待ち {t_pop - comment.received_at:.1f} / "
              f"DB {t_ctx - t_pop:.1f} / LLM {t_llm - t_ctx:.1f} / "
              f"合成 {started - t_llm:.1f})")

        self.replied_count += 1
        # energy はここでは足さない。下の memory.save_comment() が入れた行を
        # biorhythm_server が拾って加算する（live/energy.py の説明を参照）
        response_text = " ".join(l["ja"] for l in reply["lines"])
        self.memory_writer.update_response(comment.message_id, response_text)
        try:
            memory.save_comment(
                self.broadcast.broadcast_id if self.broadcast else "dry-run",
                comment.author, comment.channel_id, comment.text,
                response_text, bot.get("energy", 0),
            )
        except Exception as e:
            print(f"[live] 配信ログを保存できません（無視します）: {e}")

    def speak_filler(self) -> None:
        bot = self._bot_context()
        topic = self.planner.next_topic()
        prompt = persona.build_filler_prompt(
            topic["hint"], bot, self.planner.cache,
            recent_replies=self.recent_replies,
        )
        # フリートークはコメントが来たら途中でやめる。最後まで喋りきってから
        # でないと反応できないのが、往復の遅さのいちばん大きな要因だった
        reply = self._generate(prompt)
        spoken = self._speak(reply, tag="フリートーク", interruptible=True)
        if spoken and topic["memory_ids"] and not reply.get("_fallback"):
            output_ref = self.broadcast.broadcast_id if self.broadcast else "dry-run"
            self.planner.record_usage(topic["memory_ids"], output_ref)

    def speak_scripted(self, instruction: str, tag: str) -> None:
        reply = self._generate(persona.build_scripted_prompt(instruction, self._bot_context()))
        self._speak(reply, tag=tag)

    def run_loop(self) -> None:
        closing_at = _today_at(LIVE_CLOSING_HHMM)
        last_housekeeping = 0.0

        self._start_chores()
        self.speak_scripted(
            "配信のオープニングです。挨拶をして、今日も来てくれた人にお礼を言って、"
            "コメントで話しかけてねと伝えてください。",
            "オープニング")

        while datetime.now() < closing_at and not self._stopping:
            comment = self.queue.pop()
            if comment is not None:
                try:
                    self.reply_to_comment(comment)
                except Exception as e:
                    print(f"[live] コメント処理でエラー（続行します）: {e}")
                    traceback.print_exc()
            elif time.monotonic() - self.last_speech_at > FILLER_IDLE_SEC:
                try:
                    self.speak_filler()
                except Exception as e:
                    print(f"[live] フリートークでエラー（続行します）: {e}")
            else:
                time.sleep(0.5)

            now = time.monotonic()
            if now - last_housekeeping > 2:
                self._housekeeping()
                last_housekeeping = now

        self.speak_scripted(
            "配信のクロージングです。「botたん」という自分の名前を必ず言って、"
            "高評価とチャンネル登録がうれしいことを伝えて、「また明日ね」で締めてください。"
            "日付は言わないこと。",
            "クロージング")

    def _start_chores(self) -> None:
        """DB や LAN を触る雑務を別スレッドへ追い出す。

        以前は energy ゲージ（30秒ごと）・RAG 先読み（30秒ごと）・記憶の
        引き直し（600秒ごと）を run_loop の中で直接呼んでいた。DB は別マシンで
        接続をプールしておらず `connect_timeout` は数秒あるので、そのホストが
        不調だと**コメントが1件も無くてもメインループが十数秒止まる**。
        2026-08-21 の配信で実際に起きた（同じホストの 3200/3204 も全滅していた）。
        """
        def run():
            gauge_at = 0.0
            rag_at = 0.0
            memory_at = time.monotonic()
            while not self._stopping:
                now = time.monotonic()
                if now - gauge_at > ENERGY_REFRESH_SEC:
                    gauge_at = now
                    try:
                        gauge.write(energy.get_energy())
                    except Exception:
                        pass
                if now - memory_at > 600:
                    memory_at = now
                    try:
                        self.planner.refresh_memory()
                    except Exception as e:
                        print(f"[live] 記憶を引き直せません（無視します）: {e}")
                if now - rag_at > 30:
                    rag_at = now
                    try:
                        self.planner.prefetch_rag(
                            self._bot_context(),
                            recent_comments=list(self.poller.recent) if self.poller else [],
                            recent_replies=self.recent_replies,
                        )
                    except Exception as e:
                        print(f"[live] RAG 先読みを始められません（無視します）: {e}")
                time.sleep(1.0)

        self._chores = threading.Thread(target=run, daemon=True, name="chores")
        self._chores.start()

    def _housekeeping(self) -> None:
        """数秒ごとの雑務。落ちても配信に影響しない処理だけ置くこと。

        **ここには DB・LAN を触る処理を置かないこと。** メインループから
        呼ばれるので、詰まるとコメントへの反応がそのぶん遅れる。
        重いものは _start_chores の別スレッドへ。

        コメント欄は ChatPoller が受信と同時に書くのでここでは触らない
        （ここで書くと、視聴者から見て画面に出るまでがこの間隔ぶん遅れる）。"""
        try:
            subtitle.write_clock()
        except Exception:
            pass

        # ARDY が作り終えたモーションを拾う。プールに入っているので
        # 次に pick したときから使われる
        ready = self.ardy.poll_ready()
        if ready:
            print(f"[live] モーションができました: {ready[1]} / {self.pool.summary()}")

        # Unity のフレームレート。/motion を受けた Unity はメインスレッドで
        # .vrma をパースするので、モーションを投げる頻度を上げたときに
        # フレームが落ちていないかはここでしか分からない
        if FPS_LOG_SEC > 0 and time.monotonic() - self._fps_at > FPS_LOG_SEC:
            self._fps_at = time.monotonic()
            try:
                st = unity_client.status()
                print(f"[Unity] fps={st.get('fps', 0):.1f} "
                      f"motion={st.get('motion_queue', 0)} "
                      f"rss={self.unity.rss_mb():.0f}MB")
            except unity_client.UnityError:
                pass

        if not self.unity.is_alive():
            print("[live] Unity が落ちています")
            notify.error("Unity", "プロセスが終了しました")
            self._stopping = True

    # ── 片付け ────────────────────────────────────────

    def teardown(self) -> None:
        print("[終了] 片付けます")
        self.idle.stop()
        self.subs.stop()
        subtitle.clear()

        if self.poller is not None:
            self.poller.stop()
        self.memory_writer.stop()

        if self.broadcast is not None:
            self.broadcast.finish()
            try:
                memory.end_broadcast(self.broadcast.broadcast_id, self.replied_count)
            except Exception as e:
                print(f"[終了] 配信記録を残せません: {e}")

        if self.obs is not None:
            # OBS を配信中に手で起動し直すとここの websocket は死んでいる。
            # その後の Unity と ARDY の後始末まで巻き込まれないよう握りつぶす
            try:
                # 字幕ソースをファイル読み込みに戻す。戻さないと、次に
                # プログラムを通さず OBS を使ったとき字幕が出ないままになる
                subtitle.set_sink(None)
                self.obs.restore_text_files()
                print(f"[終了] OBS 統計: {self.obs.stream_stats()}")
                self.obs.stop_stream()
                self.obs.disconnect()
            except Exception as e:
                print(f"[終了] OBS の後片付けに失敗（続行します）: {e}")

        self.unity.stop()
        self.ardy.stop()

        if self.broadcast is not None and self.started_at:
            notify.live_ended(self.broadcast.url, self.replied_count,
                              (time.monotonic() - self.started_at) / 60.0)
        print(f"[終了] 返事したコメント: {self.replied_count}件 / "
              f"モーションプール: {self.pool.summary()}")


def main() -> int:
    session = LiveSession()

    def on_signal(signum, frame):
        print(f"\n[live] シグナル {signum} を受け取りました。片付けます")
        session._stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        session.prepare()
        session.connect_obs()
        session.create_broadcast()

        # 開始の少し前に testing まで入れておき、21:00 ちょうどに live へ入る
        start_at = _today_at(LIVE_START_HHMM)
        _sleep_until(start_at - timedelta(seconds=LIVE_TESTING_LEAD_SEC),
                     "testing へ入るまで")
        session.start_testing()
        _sleep_until(start_at, "配信開始まで")

        session.go_live()
        session.run_loop()
        return 0

    except Exception as e:
        traceback.print_exc()
        notify.error("配信", f"{type(e).__name__}: {e}")
        return 1
    finally:
        session.teardown()


if __name__ == "__main__":
    sys.exit(main())
