"""配信ループの分岐（掘り下げ・クロージング直前の沈黙）。

コメント > クロージング直前は黙る > 掘り下げ > フリートーク の順で、
「何も喋らない時間」が伸びないこと。run_loop は LIVE_CLOSING_HHMM で抜けて
しまい、それ以降に届いたコメントには一切反応できないので、終了間際に長い
独り言を始めさせない。
"""

import importlib.util
import sys
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def load_live():
    """live.py を、外の世界に触るものを全部差し替えて読み込む。"""
    config_stub = _stub(
        "config",
        DRY_RUN=True, ENERGY_REFRESH_SEC=30,
        FILLER_IDLE_SEC=25.0, FILLER_STOP_LEAD_SEC=120.0,
        FOLLOWUP_IDLE_SEC=8.0, FOLLOWUP_MAX_DEPTH=2, FOLLOWUP_TTL_SEC=120.0,
        IDLE_ENABLED=True, LIVE_CLOSING_HHMM="21:55", LIVE_END_HHMM="22:00",
        LIVE_GO_LIVE_RETRY_SEC=300, LIVE_START_HHMM="21:00",
        LIVE_TESTING_LEAD_SEC=120, BOT_CONTEXT_TTL_SEC=20.0,
        BOT_MOOD_SERVE_LIMIT=1, FPS_LOG_SEC=60.0,
        LIVE_HEALTH_CHECK_SEC=60.0, LIVE_HEALTH_STALL_SEC=120.0,
        MEMORY_LOG_SEC=300.0,
        LIVE_MIN_FPS=20.0, LIVE_FPS_PROBE_SEC=12.0,
        LIVE_FPS_GATE=True, LIVE_FPS_STALL_SEC=180.0, LIVE_DISPLAY=":99",
        LIVE_HISTORY_TURNS=6, LIVE_HISTORY_USER_TURNS=3, SKIP_ARDY=True,
        SUBTITLE_LEAD_SEC=0.0, UNITY_PROJECT="/tmp/unity",
        UNITY_RESTART_MAX=2, UNITY_RESTART_TIMEOUT_SEC=300.0,
        UNITY_RESTART_COOLDOWN_SEC=30.0,
        WORK_DIR=Path("/tmp/bottan-live-test"), ensure_dirs=lambda: None,
    )
    stubs = {
        "config": config_stub,
        "chat": _stub("chat", CommentQueue=object, make_poller=lambda *a, **k: None),
        "conversation": _stub("conversation", ConversationLog=object),
        "energy": _stub("energy", get_energy=lambda: 50.0),
        "filler": _stub("filler", FillerPlanner=object),
        "idle": _stub("idle", IdleAnimator=object),
        "llm": _stub("llm", generate_reply=lambda *a, **k: {}),
        "memory": _stub("memory", BotMemoryWriter=object),
        "gauge": _stub("gauge", write=lambda *a: None),
        "motion": _stub("motion", MotionPool=object, ArdyWorker=object),
        "notify": _stub("notify", warn=lambda *a: None, error=lambda *a: None),
        "persona": _stub("persona", build_system_prompt=lambda: ""),
        # recall は BotMemoryClient と DB を掴むので差し替える
        "recall": _stub("recall", CommentRecall=object,
                        subject_of=lambda _text: ""),
        "safety": _stub("safety"),
        "schedule": _stub("schedule"),
        "subtitle": _stub("subtitle", SubtitleScheduler=object),
        # topics は依存ゼロなので実物を読ませる（mood の消費判定を通したい）
        "unity_client": _stub("unity_client", UnityError=RuntimeError),
        "unity_live": _stub("unity_live", UnityLive=object),
        "voice": _stub("voice"),
        # live.py は `from common import grounding` で読む（live/ に同名モジュールは
        # 無い）。実物は import しただけでは外に触らないが、needs_lookup / lookup は
        # ollama と Gemini を叩くので、ここでも差し替えておく
        "common.grounding": _stub(
            "common.grounding", SKIP="skip", UNKNOWN="unknown", FACTS="facts",
            WEB="web", SELF="self", NONE="none",
            classify=lambda _text: ("none", ""),
            needs_lookup=lambda _text: False,
            lookup=lambda _q: {"status": "skip", "facts": "", "queries": []},
            warmup=lambda: None),
        # log_gpu_state は nvidia-smi と ollama を叩く
        "common.ardy": _stub("common.ardy", log_gpu_state=lambda *a: None),
    }
    previous = {key: sys.modules.get(key) for key in stubs}
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(
            "live_loop_test_module", LIVE / "live.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


live = load_live()


class FakePlanner:
    def __init__(self):
        self.themes = []

    def set_followup_theme(self, theme):
        self.themes.append(theme)


def bare_session():
    """__init__ を通さずに、分岐に要るものだけ持った LiveSession を作る。"""
    session = object.__new__(live.LiveSession)
    session._thread = None
    session.last_speech_at = time.monotonic()
    session.planner = FakePlanner()
    session._mood_state = {"text": "", "served": 0}
    session._stopping = False
    session._broadcast_dead = False
    session._last_frames = -1
    session._frames_at = time.monotonic()
    session._inactive_count = 0
    session.broadcast = None
    session.obs = None
    return session


MOOD = ("全肯定botたんは、お風呂で「FLASHBULB」を聴きながら、"
        "今日の出来事を思い出しているよ。")


class MoodServingTest(unittest.TestCase):
    """mood を全プロンプトに載せ続けない。

    mood は biorhythm 由来で 20〜90分ごとにしか変わらないので、素で入れると
    1時間の配信の全発話に同じ文が乗る。2026-08-24 の配信で「FLASHBULB」の話が
    61発話中14回出たのがこれ。
    """

    def test_the_mood_is_served_once(self):
        session = bare_session()
        self.assertEqual(session._bot_for_prompt({"mood": MOOD})["mood"], MOOD)
        self.assertEqual(session._bot_for_prompt({"mood": MOOD})["mood"], "")

    def test_it_stays_dropped(self):
        session = bare_session()
        session._bot_for_prompt({"mood": MOOD})
        for _ in range(5):
            self.assertEqual(session._bot_for_prompt({"mood": MOOD})["mood"], "")

    def test_a_new_mood_starts_over(self):
        """20〜90分ごとの更新で復活する。配信中に2〜3回は近況を話せる。"""
        session = bare_session()
        session._bot_for_prompt({"mood": MOOD})
        self.assertEqual(session._bot_for_prompt({"mood": MOOD})["mood"], "")
        other = "モルフォと公園を散歩していた"
        self.assertEqual(session._bot_for_prompt({"mood": other})["mood"], other)

    def test_the_original_mood_is_kept_for_the_memory_block(self):
        """載せないと決めたあとも、記憶ブロックからは同じ話を落としたい。"""
        session = bare_session()
        session._bot_for_prompt({"mood": MOOD})
        bot = session._bot_for_prompt({"mood": MOOD})
        self.assertEqual(bot["mood"], "")
        self.assertEqual(bot["mood_raw"], MOOD)

    def test_peeking_does_not_spend_the_budget(self):
        """雑務スレッド（RAG 先読み）は消費せずに覗くだけ。"""
        session = bare_session()
        for _ in range(10):
            session._bot_for_prompt({"mood": MOOD}, consume=False)
        self.assertEqual(session._bot_for_prompt({"mood": MOOD})["mood"], MOOD)

    def test_an_empty_mood_is_harmless(self):
        session = bare_session()
        self.assertEqual(session._bot_for_prompt({})["mood_raw"], "")
        self.assertEqual(session._bot_for_prompt({"mood": ""})["mood_raw"], "")


class ClosingSoonTest(unittest.TestCase):
    def test_it_is_quiet_inside_the_lead(self):
        session = bare_session()
        closing_at = datetime.now() + timedelta(seconds=30)
        self.assertTrue(session._closing_soon(closing_at))

    def test_it_still_talks_well_before_closing(self):
        session = bare_session()
        closing_at = datetime.now() + timedelta(seconds=600)
        self.assertFalse(session._closing_soon(closing_at))


class ReplyMemoryUsagePolicyTest(unittest.TestCase):
    def test_usage_is_after_speech_and_excludes_fallbacks(self):
        source = (LIVE / "live.py").read_text(encoding="utf-8")
        speech_guard = source.index("if not spoken_text:")
        usage_guard = source.index('if remembered and not reply.get("_fallback"):')
        usage_call = source.index("self.recall.record_usage(remembered, output_ref)")
        self.assertLess(speech_guard, usage_guard)
        self.assertLess(usage_guard, usage_call)


class ThreadTest(unittest.TestCase):
    def test_beginning_a_thread_hands_the_theme_to_the_planner(self):
        session = bare_session()
        session._begin_thread("モルフォに起こされた話")
        self.assertEqual(session.planner.themes, ["モルフォに起こされた話"])
        self.assertEqual(session._thread["depth"], 0)

    def test_an_empty_theme_clears_the_thread(self):
        session = bare_session()
        session._begin_thread("なにか")
        session._begin_thread("   ")
        self.assertIsNone(session._thread)

    def test_a_planner_failure_does_not_break_the_stream(self):
        session = bare_session()

        def explode(_theme):
            raise RuntimeError("bot memory is down")

        session.planner.set_followup_theme = explode
        session._begin_thread("なにかの話")           # 例外が漏れないこと
        self.assertIsNotNone(session._thread)


class FollowupDueTest(unittest.TestCase):
    def test_nothing_to_dig_into_without_a_thread(self):
        self.assertFalse(bare_session()._followup_due())

    def test_it_waits_for_the_silence_to_open_up(self):
        session = bare_session()
        session._begin_thread("なにかの話")
        session.last_speech_at = time.monotonic()          # いま喋り終わった
        self.assertFalse(session._followup_due())
        session.last_speech_at = time.monotonic() - 9      # 9秒空いた
        self.assertTrue(session._followup_due())

    def test_it_stops_at_the_depth_limit(self):
        session = bare_session()
        session._begin_thread("なにかの話")
        session.last_speech_at = time.monotonic() - 30
        session._thread["depth"] = live.FOLLOWUP_MAX_DEPTH
        self.assertFalse(session._followup_due())

    def test_a_stale_theme_is_dropped(self):
        """古い話題を蒸し返さない。"""
        session = bare_session()
        session._begin_thread("ずっと前の話")
        session.last_speech_at = time.monotonic() - 30
        session._thread["at"] = time.monotonic() - live.FOLLOWUP_TTL_SEC - 1
        self.assertFalse(session._followup_due())
        self.assertIsNone(session._thread)

    def test_the_silence_never_gets_longer_than_the_filler_threshold(self):
        """掘り下げはフリートークより先に出ること。

        逆だと、掘り下げられるテーマがあるのに FILLER_IDLE_SEC だけ黙る。
        """
        self.assertLess(live.FOLLOWUP_IDLE_SEC, live.FILLER_IDLE_SEC)


class UnityRecoveryTest(unittest.TestCase):
    class FakeUnity:
        def __init__(self, start_error=None):
            self.alive = False
            self.started = []
            self.start_error = start_error

        def is_alive(self):
            return self.alive

        def start(self, ready_timeout):
            self.started.append(ready_timeout)
            if self.start_error:
                raise self.start_error
            self.alive = True

    class FakeObs:
        def __init__(self):
            self.bound = []

        def bind_window_capture(self, project):
            self.bound.append(project)

    def make_session(self, unity=None):
        session = bare_session()
        session.unity = unity or self.FakeUnity()
        session.obs = self.FakeObs()
        session._unity_restart_count = 0
        session._unity_restart_after = 0.0
        return session

    def test_restart_rebinds_obs_to_the_new_window(self):
        session = self.make_session()
        self.assertTrue(session._recover_unity())
        self.assertEqual(session.unity.started, [live.UNITY_RESTART_TIMEOUT_SEC])
        self.assertEqual(session.obs.bound, [live.UNITY_PROJECT])
        self.assertEqual(session._unity_restart_count, 1)

    def test_a_live_process_is_not_restarted(self):
        session = self.make_session()
        session.unity.alive = True
        self.assertTrue(session._recover_unity(force=True))
        self.assertEqual(session.unity.started, [])

    def test_restart_budget_prevents_an_endless_crash_loop(self):
        session = self.make_session(self.FakeUnity(RuntimeError("crashed")))
        for _ in range(live.UNITY_RESTART_MAX):
            session._unity_restart_after = 0.0
            self.assertFalse(session._recover_unity(force=True))
        self.assertFalse(session._recover_unity(force=True))
        self.assertEqual(len(session.unity.started), live.UNITY_RESTART_MAX)


class RenderGateTest(unittest.TestCase):
    """絵が動いていないホストで live へ入らないこと。

    2026-09-10 は 0.1〜1.9fps のまま14分配信した。unity.is_alive() も
    xrandr のリフレッシュレートも OBS の出力フレーム数も素通りしている
    （tests/test_render_gate.py の冒頭を参照）。
    """

    class FakeUnity:
        def __init__(self, fps):
            self.fps = fps
            self.probed = []

        def measure_fps(self, probe_sec):
            self.probed.append(probe_sec)
            return self.fps

    def make_session(self, fps):
        session = bare_session()
        session.unity = self.FakeUnity(fps)
        return session

    def test_a_healthy_host_passes(self):
        session = self.make_session(60.0)
        self.assertEqual(session.check_render(), 60.0)
        self.assertEqual(session.unity.probed, [live.LIVE_FPS_PROBE_SEC])

    def test_a_stalled_host_stops_the_stream(self):
        session = self.make_session(1.0)
        with self.assertRaises(RuntimeError) as caught:
            session.check_render()
        # 復旧手順まで通知に載せる。人が見るのは Discord の1行だけ。
        # 復旧は :99 の作り直しで、その係は reset_display.sh（Xorg と openbox の
        # 両方を入れ直し、実測まで見る）。__GL_SYNC_TO_VBLANK も必ず添えること。
        # これを落として測ると健全なホストでも 1fps に見え、切り分けが振り出しに戻る
        message = str(caught.exception)
        self.assertIn("reset_display.sh", message)
        self.assertIn("__GL_SYNC_TO_VBLANK=0", message)

    def test_the_gate_can_be_turned_off(self):
        session = self.make_session(1.0)
        original = live.LIVE_FPS_GATE
        live.LIVE_FPS_GATE = False
        try:
            self.assertEqual(session.check_render(), 1.0)
        finally:
            live.LIVE_FPS_GATE = original


class LowFpsNoticeTest(unittest.TestCase):
    """ゲートを抜けたあとに絵が止まったら、一度だけ知らせること。"""

    def make_session(self):
        session = bare_session()
        session._fps_low_since = 0.0
        session._fps_low_warned = False
        return session

    def test_a_brief_dip_is_not_reported(self):
        session = self.make_session()
        session._note_fps(1.0)
        session._note_fps(60.0)
        session._note_fps(1.0)
        self.assertFalse(session._fps_low_warned)

    def test_a_sustained_stall_is_reported_once(self):
        session = self.make_session()
        session._note_fps(1.0)
        # 最初の記録から LIVE_FPS_STALL_SEC 経ったことにする
        session._fps_low_since -= live.LIVE_FPS_STALL_SEC + 1
        session._note_fps(1.0)
        self.assertTrue(session._fps_low_warned)
        session._note_fps(1.0)
        self.assertTrue(session._fps_low_warned)

    def test_recovery_clears_the_countdown(self):
        session = self.make_session()
        session._note_fps(1.0)
        session._note_fps(60.0)
        self.assertEqual(session._fps_low_since, 0.0)


class FakeBroadcast:
    def __init__(self, status="live"):
        self.status = status

    def lifecycle_status(self):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


class FakeStreamObs:
    def __init__(self, **stats):
        self.stats = {"active": True, "reconnecting": False,
                      "total_frames": 1000}
        self.stats.update(stats)

    def stream_stats(self):
        return dict(self.stats)


class BroadcastWatchdogTest(unittest.TestCase):
    """配信が途切れたことに気づいて畳む。

    2026-09-09 は 21:08 に YouTube が enableAutoStop で枠を閉じたのに、
    こちらは 23分間それに気づかず喋り続けた。ホストが swap で詰まって OBS の
    送出が止まったのが元で、Unity はプロセスとしては生きていたので既存の
    死活監視（_housekeeping の unity.is_alive）には何も掛からなかった。
    """

    def make_session(self, broadcast=None, obs=None):
        session = bare_session()
        session.broadcast = broadcast
        session.obs = obs
        return session

    def test_a_completed_broadcast_stops_the_stream(self):
        session = self.make_session(FakeBroadcast("complete"), FakeStreamObs())
        session._check_broadcast()
        self.assertTrue(session._broadcast_dead)
        self.assertTrue(session._stopping)

    def test_a_revoked_broadcast_stops_the_stream(self):
        session = self.make_session(FakeBroadcast("revoked"), FakeStreamObs())
        session._check_broadcast()
        self.assertTrue(session._stopping)

    def test_a_live_broadcast_keeps_going(self):
        session = self.make_session(FakeBroadcast("live"), FakeStreamObs())
        session._check_broadcast()
        self.assertFalse(session._broadcast_dead)
        self.assertFalse(session._stopping)

    def test_a_youtube_api_failure_does_not_end_the_stream(self):
        """点検が転んだくらいで配信を畳まない。次の点検で見直せばよい。"""
        session = self.make_session(
            FakeBroadcast(RuntimeError("quota")), FakeStreamObs())
        session._check_broadcast()
        self.assertFalse(session._stopping)

    def test_obs_going_inactive_stops_the_stream(self):
        session = self.make_session(FakeBroadcast("live"),
                                    FakeStreamObs(active=False))
        session._check_broadcast()
        self.assertFalse(session._stopping, "1回では畳まない")
        session._check_broadcast()
        self.assertTrue(session._broadcast_dead)

    def test_one_odd_sample_does_not_end_the_stream(self):
        """配信を早く終わらせてしまう損のほうが、気づくのが遅れる損より大きい。"""
        obs = FakeStreamObs(active=False)
        session = self.make_session(FakeBroadcast("live"), obs)
        session._check_broadcast()
        obs.stats["active"] = True
        obs.stats["total_frames"] = 2000
        session._check_broadcast()
        session._check_broadcast()
        self.assertFalse(session._stopping)

    def test_frozen_frames_stop_the_stream_after_the_grace_period(self):
        obs = FakeStreamObs(total_frames=1000)
        session = self.make_session(FakeBroadcast("live"), obs)
        session._check_broadcast()                 # 1回目は基準を取るだけ
        self.assertFalse(session._stopping)
        session._frames_at -= live.LIVE_HEALTH_STALL_SEC + 1
        session._check_broadcast()
        self.assertTrue(session._broadcast_dead)

    def test_advancing_frames_reset_the_grace_period(self):
        obs = FakeStreamObs(total_frames=1000)
        session = self.make_session(FakeBroadcast("live"), obs)
        session._check_broadcast()
        session._frames_at -= live.LIVE_HEALTH_STALL_SEC + 1
        obs.stats["total_frames"] = 1900           # 出ている
        session._check_broadcast()
        self.assertFalse(session._stopping)

    def test_reconnecting_gets_twice_the_grace_period(self):
        """OBS が自分で戻そうとしている間は待つ。"""
        obs = FakeStreamObs(total_frames=1000, reconnecting=True)
        session = self.make_session(FakeBroadcast("live"), obs)
        session._check_broadcast()
        session._frames_at -= live.LIVE_HEALTH_STALL_SEC + 1
        session._check_broadcast()
        self.assertFalse(session._stopping)
        session._frames_at -= live.LIVE_HEALTH_STALL_SEC
        session._check_broadcast()
        self.assertTrue(session._broadcast_dead)

    def test_an_unreachable_obs_does_not_end_the_stream(self):
        class Broken:
            def stream_stats(self):
                return {"error": "websocket is closed"}

        session = self.make_session(FakeBroadcast("live"), Broken())
        session._check_broadcast()
        self.assertFalse(session._stopping)


class ClosingSkipTest(unittest.TestCase):
    """配信が途切れているならクロージングは喋らない。

    聞いている人は居らず、そこが呼ばれるときは VOICEVOX と Unity も詰まって
    いる。締めの合成を試みても1文ごとにタイムアウトを待つだけで、teardown が
    数分伸びる。
    """

    def source(self):
        return (LIVE / "live.py").read_text(encoding="utf-8")

    def test_an_in_flight_speech_is_cut_short(self):
        """途切れたあとの残りの文を合成しない。

        そのときは VOICEVOX も詰まっているので、1文につき最大32秒
        （読み取り15秒 × 2回 + 待ち1秒）待たされて片付けが伸びる。
        """
        source = self.source()
        loop = source.index("for i, line in enumerate(lines):")
        guard = source.index("if self._broadcast_dead:", loop)
        synth = source.index("voice.synthesize_lines(", loop)
        self.assertLess(guard, synth)

    def test_the_guard_sits_before_the_closing_speech(self):
        source = self.source()
        guard = source.index("if self._broadcast_dead:")
        closing = source.index('"配信のクロージングです。')
        self.assertLess(guard, closing)
        # ガードから発話までのあいだに return があること
        self.assertIn("return", source[guard:closing])


if __name__ == "__main__":
    unittest.main()
