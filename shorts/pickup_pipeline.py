#!/usr/bin/env python3
"""
botたん 朝の作品紹介 Shorts 自動投稿パイプライン

JST 6:00 に起動し、その曜日のテーマ（pickup_sources.WEEKDAY_THEMES）の作品を1本選んで、
約30秒の紹介動画を生成して YouTube に投稿する。2026-10-17 にクイズから切り替えた
（クイズのコードは quiz_pipeline.py に残してある。run_quiz.sh の MORNING_FORMAT を参照）。

構成（尺は区間ごとに発話長で決まる）:
  HOOK   掴み           作品名を入れた一言（顔のアップ。サムネもここから撮る）
  WHAT   どんな作品か   あらすじ・概要
  PUSH   推しポイント   山場（強く寄る・ちびキャラ）
  INFO   公開日など     「ぜひ観てみてね」
  END    エンディング   季節の挨拶 + 固定クロージング（quiz_data と共用）

画面の上部にはパネル（アートワーク・作品名・日付・画像の出典）を最初から最後まで出す。

事実の扱いは pickup_prompts の冒頭を参照。作品名・日付はデータベースの値を画面に出す。

環境変数（core.py のものに加えて）:
  PICKUP_THEME        : テーマを固定する（手動確認用。pickup_sources.THEME_LABELS のキー）
  PICKUP_USED_CSV     : 紹介済み台帳のパス (デフォルト: ./data/pickup_used.csv)
  PICKUP_NO_CONSUME   : true で台帳に記録しない（テスト用）
  TMDB_API_KEY        : 映画・ドラマ
  IGDB_CLIENT_ID / IGDB_CLIENT_SECRET : ゲーム（Twitch の開発者アプリ）
  SKIP_YOUTUBE / KEEP_TEMP / YOUTUBE_PRIVACY : quiz_pipeline と同じ
"""

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import core
import chibi
import english
import pickup_layout
import pickup_prompts
import pickup_research
import pickup_sources
import quiz_data
import quiz_layout
import quiz_pipeline
import rich_text
from common import bgm

# 候補を何本まで試すか。調べもので検索が走らなかった作品は使わずに次へ進む。
# 1本につき Gemini 1〜2回なので、最悪でも10回で止まる
MAX_CANDIDATES = 5
# 台本の作り直し（ローカル LLM なので課金は無い）
SCRIPT_TRIES = 2

PAD_AFTER = {"HOOK": 0.35, "WHAT": 0.30, "PUSH": 0.40, "INFO": 0.30, "END": 0.0}
PART_KEYS = (("HOOK", "hook"), ("WHAT", "what"), ("PUSH", "push"), ("INFO", "info"))
# 生成モーションを敷くパート。冒頭から最後までを1本の連続モーションで埋める
VRMA_PARTS = ("HOOK", "WHAT", "PUSH", "INFO", "END")
# HOOK のあと（WHAT の頭）でカメラを引く。HOOK はアップのまま＝サムネもアップで撮れる
PULLBACK_PART = "WHAT"
EMPHASIS_PARTS = {"PUSH"}
CAPTION_PARTS = {"HOOK", "WHAT", "PUSH", "INFO", "END"}
DURATION_WARN_SEC = 36.0

EMOTION_VARIANTS = {
    "HOOK": [(0.80, 0.70), (0.65, 0.85), (0.90, 0.55)],    # わくわく呼びかけ
    "WHAT": [(0.60, -0.30), (0.50, -0.50), (0.70, -0.15)],  # 落ち着いて説明
    "PUSH": [(1.00, 0.60), (0.90, 0.80), (0.95, 0.45)],     # 全力で褒める
    "INFO": [(0.80, 0.20), (0.75, 0.05), (0.85, 0.35)],     # 明るくお知らせ
    "END":  quiz_pipeline.EMOTION_VARIANTS["END"],
}


# ──────────────────────────────────────────────
# Step 1: 作品選びと調べもの
# ──────────────────────────────────────────────

def pick_work(theme: str, day, tmp_dir: Path, used: set[str] = None) -> tuple[dict, dict, Path]:
    """検索で裏の取れた作品を1本選ぶ。返り値は (作品, 調べもの, アートワーク)。"""
    tried = 0
    for item in pickup_sources.candidates(theme, day, used=used):
        tried += 1
        if tried > MAX_CANDIDATES:
            break
        print(f"[作品] 候補{tried}: {item['title']}（{item['date']} / {item['key']}）")
        found = pickup_research.research(item)
        if found is None:
            continue
        try:
            art = pickup_sources.download_artwork(item, tmp_dir)
        except Exception as e:
            # 画像の無いカードにはしない（イメージがわかないので）。次の候補へ
            print(f"[作品] アートワークを取れないので使いません: {e}")
            continue
        return item, found, art
    raise RuntimeError(f"{theme}: 使える作品が見つかりませんでした（試した候補 {min(tried, MAX_CANDIDATES)}本）")


# ──────────────────────────────────────────────
# Step 2: 台本
# ──────────────────────────────────────────────

def _sources_text(item: dict, research: dict) -> str:
    """台本の検証で照合する元の文。データベースの値と調査メモ。"""
    return json.dumps(item, ensure_ascii=False) + "\n" + research["text"]


def generate_script(item: dict, research: dict) -> dict:
    """台本を書かせ、問題があれば1回だけ作り直させる。字数はコードで合わせる。"""
    sources = _sources_text(item, research)
    problems = []
    script = None
    for attempt in range(1, SCRIPT_TRIES + 1):
        script = core.llm_json(pickup_prompts.SYSTEM_PROMPT,
                               pickup_prompts.build_user_prompt(item, research["text"], problems),
                               pickup_prompts.SCRIPT_SCHEMA)
        problems = pickup_prompts.validate_script(script, sources)
        for p in problems:
            print(f"[LLM] 警告（{attempt}回目）: {p}")
        if not problems:
            break

    for key in pickup_prompts.PARTS:
        script[key] = quiz_pipeline._normalize_sentences(script.get(key))
    for text in pickup_prompts.drop_bad_sentences(script, sources):
        print(f"[LLM] 問題が残った文を落としました: {text}")
    pickup_prompts.apply_fixed_hook(script, item)
    for key in pickup_prompts.fill_missing(script, item):
        print(f"[LLM] {key} が空になったので固定文で埋めました")
    empty = [key for key in pickup_prompts.PARTS if not script.get(key)]
    if empty:
        # 中身のパートを固定文では作れない。事実を曲げた台本は出さない
        raise RuntimeError(f"台本の問題を直せませんでした（空のパート: {empty}）")

    pickup_prompts.ensure_peak(script)
    for text in pickup_prompts.fit_length(script):
        print(f"[LLM] 尺に収めるため落としました: {text}")
    print(f"[LLM] 台本 {pickup_prompts.total_chars(script)}字（上限 {pickup_prompts.TOTAL_CHARS}字）")
    return script


_EN_PARTS = ("hook", "what", "push", "info", "ending")


def translate_script(item: dict, script: dict) -> dict | None:
    """台詞を英訳し、各文に "en" を書き込む。返り値は {"title"}。訳せなければ None。"""
    sentences = [s for key in _EN_PARTS for s in script.get(key) or []]
    en = english.translate([s["text"] for s in sentences], script.get("title_hook") or item["title"])
    if en is None:
        return None
    for s, line in zip(sentences, en["lines"]):
        s["en"] = line
    return {"title": en["title"]}


# ──────────────────────────────────────────────
# Step 3: 音声・字幕・表情・モーション
# ──────────────────────────────────────────────

def build_audio(script: dict, wav_path: str, tmp_dir: Path, prefix: str) -> list[dict]:
    """パートごとに合成・結合して、各パートと各文の実測区間を返す。"""
    specs = [(pid, script[key]) for pid, key in PART_KEYS] + [("END", script["ending"])]
    segments, all_wavs, t = [], [], 0.0
    for pid, sentences in specs:
        # 掴みだけ声色を変える（夜版のサムネの一言・クイズの掛け声と同じ）
        wavs = core.synthesize_sentences(sentences, tmp_dir, f"{prefix}_{pid}",
                                         core.HOOK_VOICE_PARAMS if pid == "HOOK" else None)
        start, spans = t, []
        for sent, (path, dur) in zip(sentences, wavs):
            spans.append({"start": round(t, 3), "end": round(t + dur, 3),
                          "motion": sent.get("motion")})
            all_wavs.append(path)
            t += dur
        segments.append({"id": pid, "sentences": sentences, "spans": spans,
                         "start": round(start, 3), "end": round(t, 3),
                         "duration": round(t - start, 3)})
        pad = PAD_AFTER.get(pid, 0.0)
        if pad > 0:
            all_wavs.append(core.make_silence_wav(tmp_dir / f"{prefix}_sil_{pid}.wav", pad))
            t += pad

    core.concat_wavs(all_wavs, wav_path)
    actual = core.get_wav_duration(wav_path)
    print(f"[音声] 合計 {actual:.3f}秒 (計算値 {t:.3f}秒)")
    if actual > DURATION_WARN_SEC:
        print(f"[警告] 尺が長すぎます: {actual:.1f}秒（警告閾値 {DURATION_WARN_SEC}秒）")
    for s in segments:
        print(f"  {s['id']:5s} {s['start']:6.2f}s 〜 {s['end']:6.2f}s ({s['duration']:5.2f}s)")
    if abs(actual - t) > 0.15:
        raise RuntimeError(f"タイムライン不整合: 計算{t:.3f}s vs 実測{actual:.3f}s")
    for p in all_wavs:
        Path(p).unlink(missing_ok=True)
    return segments


def build_emotions(segments: list[dict], rng) -> list[dict]:
    """パートの頭に表情を置く（quiz_pipeline.build_emotions と同じ作り）。"""
    out = []
    for seg in segments:
        v, a = rng.choice(EMOTION_VARIANTS[seg["id"]])
        out.append({"time": round(seg["start"] + rng.uniform(0.0, quiz_pipeline.TIME_JITTER), 2),
                    "valence": round(quiz_pipeline._clamp(v + rng.uniform(-0.12, 0.12)), 2),
                    "arousal": round(quiz_pipeline._clamp(a + rng.uniform(-0.12, 0.12)), 2)})
    return sorted(out, key=lambda e: e["time"])


def build_vrma_blocks(segments: list[dict], script: dict) -> list[dict]:
    """冒頭から最後までを1本の連続モーションで埋める（quiz_pipeline と同じ考え方）。

    END は固定文でペルソナが書いた文が無いので、台本の ending_motions を区間に等分する。
    """
    seg = {x["id"]: x for x in segments}
    start = float(seg["HOOK"]["start"])
    end = float(seg["END"]["end"]) + core.VRMA_OUTRO_SEC
    spans = []
    for pid in VRMA_PARTS:
        if pid != "END":
            spans += seg[pid]["spans"]
            continue
        texts = [t.strip() for t in script.get("ending_motions") or [] if (t or "").strip()]
        part = seg["END"]
        if not texts:
            spans.append({"start": part["start"], "end": part["end"], "motion": None})
            continue
        step = (part["end"] - part["start"]) / len(texts)
        spans += [{"start": part["start"] + step * i, "end": part["start"] + step * (i + 1),
                   "motion": text} for i, text in enumerate(texts)]
    spans.sort(key=lambda sp: sp["start"])
    # パート間の無音を前の文に含めて、生成モーションの隙間を無くす
    for i, sp in enumerate(spans):
        sp["end"] = spans[i + 1]["start"] if i + 1 < len(spans) else max(sp["end"], end)
    seg_list = core.plan_vrma_from_sentences(spans, start, end)
    if not seg_list:
        print("[モーション] 埋められる区間がありません")
        return []
    print(f"[モーション] {start:.1f}〜{end:.1f}秒 → {len(seg_list)}セグメント")
    return [{"name": "body", "time": round(start, 2), "segments": seg_list}]


# ──────────────────────────────────────────────
# main
# ──────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="朝の作品紹介Shorts生成パイプライン")
    p.add_argument("--theme", default="", help="テーマを固定する（PICKUP_THEME と同じ）")
    p.add_argument("--stage", choices=["all", "script", "voice"], default="all",
                   help="どこまで実行するか（script: 台本まで / voice: 音声まで）")
    p.add_argument("--preview", action="store_true",
                   help="Unity録画の代わりに単色背景を使う（レイアウト確認用・高速）")
    p.add_argument("--keep-temp", action="store_true", help="一時ファイルを残す")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.theme:
        os.environ["PICKUP_THEME"] = args.theme
    keep_temp = args.keep_temp or core.env_flag("KEEP_TEMP")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = Path(tempfile.gettempdir())
    prefix = f"pickup_{ts}"
    wav_path       = str(tmp_dir / f"{prefix}.wav")
    webm_path      = str(tmp_dir / f"{prefix}.webm")
    mp4_path       = str(tmp_dir / f"{prefix}.mp4")
    emotion_path   = str(tmp_dir / f"{prefix}_emotions.json")
    timeline_path  = str(tmp_dir / f"{prefix}_timeline.json")
    frame_path     = str(tmp_dir / f"{prefix}_frame.png")
    thumbnail_path = str(tmp_dir / f"{prefix}_thumbnail.png")

    core.cleanup_old_temp_files(patterns=("bottan_*", "quiz_*", "pickup_*"))
    total_start = time.time()
    cleanup_targets = []

    try:
        if args.stage == "all" and not args.preview:
            from common.unity_license import ensure
            ensure(core.UNITY_EXE)

        # ── Step 1: 作品選びと調べもの
        day = pickup_sources.today_jst()
        theme = pickup_sources.theme_of(day)
        print(f"[作品] {day}（{'月火水木金土日'[day.weekday()]}）のテーマ: "
              f"{pickup_sources.THEME_LABELS[theme]}")
        item, research, artwork = core._timed("Step1 作品選び", pick_work, theme, day, tmp_dir)
        cleanup_targets.append(str(artwork))
        print(f"[作品] {item['title']} を紹介します")

        # ── Step 2: 台本
        script = core._timed("Step2 台本生成", generate_script, item, research)
        script["ending"] = quiz_data.build_ending_sentences()

        en = None
        if args.stage != "script":
            en = core._timed("Step2.5 英訳", translate_script, item, script)

        Path(timeline_path).write_text(json.dumps(
            {"item": item, "research": research, "script": script},
            ensure_ascii=False, indent=1), encoding="utf-8")
        if args.stage == "script":
            print(json.dumps({"item": item, "research": research, "script": script},
                             ensure_ascii=False, indent=2))
            return 0

        # ── Step 3: 音声とタイムライン
        segments = core._timed("Step3 音声生成", build_audio, script, wav_path, tmp_dir, prefix)
        cleanup_targets.append(wav_path)
        subtitles = core._timed("Step3.5 字幕生成", quiz_pipeline.build_subtitles, segments,
                                core.TOP_SUB_MAX_CHARS)
        Path(timeline_path).write_text(json.dumps({
            "item": item, "research": research, "script": script,
            "segments": segments, "subtitles": subtitles, "webm": webm_path,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[タイムライン] 保存: {timeline_path}")
        if args.stage == "voice":
            cleanup_targets.clear()
            print(f"\n✅ 音声生成まで完了: {wav_path}")
            return 0

        # ── Step 3.7: BGM（失敗したら固定曲）
        bgm_path = None
        if not args.preview:
            bgm_path = core._timed("Step3.7 BGM生成", bgm.generate, "pickup", tmp_dir)
            if bgm_path:
                cleanup_targets.append(str(bgm_path))

        # ── Step 3.8: AI生成モーション（失敗しても動画は作る）
        vrma_motions = []
        if core.VRMA_MOTION_DIR and not args.preview:
            ardy_proc = None
            try:
                blocks = build_vrma_blocks(segments, script)
                if blocks:
                    ardy_proc = core.ardy_start(reuse=False)
                if ardy_proc is not None and core.ardy_wait_ready():
                    vrma_motions = core._timed(
                        "Step3.8 モーション生成", core.build_vrma_motions, blocks,
                        core.VRMA_MOTION_DIR, f"{day.isoformat()}-{item['key'].replace(':', '_')}")
            except Exception as e:
                print(f"[モーション] 生成に失敗しました（動画は続行）: {e}")
            finally:
                core.ardy_stop(ardy_proc)
            if not vrma_motions:
                from common import notify
                notify.warn("朝の作品紹介: ARDY のモーション生成に失敗しました。"
                            "Mixamo の固定モーションだけで公開します（logs/quiz_*.log を確認）")

        # ── Step 4: 表情
        rng = random.Random(f"{day.isoformat()}-{item['key']}")
        quiz_pipeline.build_emotion_file(segments, build_emotions(segments, rng),
                                         emotion_path, vrma_motions)
        cleanup_targets.append(emotion_path)

        # ── Step 5: Unity録画
        pullback_at = None
        if args.preview:
            print("[Unity] --preview のためスキップ（単色背景に音声を載せる）")
            source = str(tmp_dir / f"{prefix}_preview.mkv")
            _preview_source(wav_path, source)
            cleanup_targets.append(source)
        else:
            extra = ["-cameraOffsetY", f"{quiz_pipeline.CAMERA_OFFSET_Y}",
                     "-mouthCloseOnSilence", f"{quiz_pipeline.MOUTH_CLOSE}"]
            if vrma_motions:
                extra += ["-skipIntroClip", "1"]
                if quiz_pipeline.VRMA_PULLBACK > 0:
                    pullback_at = next(s["start"] for s in segments if s["id"] == PULLBACK_PART)
                    extra += ["-cameraPullbackAt", f"{pullback_at:.2f}",
                              "-cameraPullbackZ", f"{quiz_pipeline.VRMA_PULLBACK}"] + core.vrma_unity_args()
            core._retry("Step5 Unity録画", core.record_with_unity,
                        wav_path, webm_path, emotion_path, extra_args=extra,
                        catch=(RuntimeError, TimeoutError), delay=15)
            source = webm_path
            cleanup_targets.append(webm_path)

        # ── Step 6: 合成
        panel = pickup_layout.panel_image(item, artwork, random.choice(pickup_layout.THEMES), tmp_dir)
        cleanup_targets.append(str(panel))
        core._timed("Step6 MP4合成", _render, segments, subtitles, source, mp4_path, panel,
                    bgm_path, pullback_at)

        # ── Step 7: サムネイル
        thumb_ok = False
        if not args.preview:
            try:
                import thumbnail
                hook = next(s for s in segments if s["id"] == "HOOK")
                thumbnail.capture_frame(source, frame_path, max(hook["start"] + 0.5, hook["end"] - 0.5))
                thumbnail.generate_quiz_thumbnail(
                    frame_path, thumbnail_path, item["title"],
                    corner_text=pickup_sources.THEME_LABELS[theme], tagline="")
                thumb_ok = True
                cleanup_targets.append(frame_path)
            except Exception as e:
                print(f"[サムネイル] 生成に失敗しました（投稿は続行）: {e}")

        # ── Step 8: 投稿
        if core.env_flag("SKIP_YOUTUBE") or args.preview:
            print("[YouTube] スキップ")
        else:
            _upload(item, script, mp4_path, thumbnail_path if thumb_ok else "",
                    bgm_generated=bgm_path is not None,
                    en_cues=quiz_pipeline.build_en_cues(segments, en) if en else None, en=en)

        print(f"\n✅ パイプライン完了: {mp4_path}  (合計: {time.time()-total_start:.1f}秒)")
        return 0

    except Exception as e:
        print(f"\n❌ エラー発生: {e}")
        raise

    finally:
        if not keep_temp:
            for p in cleanup_targets:
                if p and Path(p).exists():
                    Path(p).unlink()
                    print(f"[Cleanup] 削除: {p}")


def _preview_source(wav_path: str, out: str) -> None:
    """--preview 用の入力。Unity の録画の代わりに単色背景へ音声を載せる。"""
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x35507a:s={core.W}x{core.H}:r=30",
        "-i", wav_path, "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", out,
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)


def _render(segments, subtitles, source, mp4_path, panel, bgm_path=None, pullback_at=None):
    """パネル・字幕・ちびキャラを重ねて MP4 にする（quiz_pipeline._render と同じ順で重ねる）。"""
    seg = {s["id"]: s for s in segments}
    work = Path(mp4_path).parent
    video_end = round(seg["END"]["end"] + quiz_pipeline.QUIZ_TAIL_SEC, 3)
    captions = [dict(s) for s in subtitles if s.get("part") in CAPTION_PARTS]

    reaction_spans = []
    for s in segments:
        for sent, sp in zip(s.get("sentences") or [], s.get("spans") or []):
            reaction_spans.append({"start": sp["start"], "end": sp["end"], "part": s["id"],
                                   "reaction": chibi.reaction_of(sent)})

    spans = [{"start": s["start"], "emphasis": s["id"] in EMPHASIS_PARTS} for s in segments]
    if pullback_at is not None:
        cuts = core.plan_cuts(spans, video_end,
                              forced=[pullback_at + quiz_pipeline.PULLBACK_MOVE_SEC],
                              wide_at=[pullback_at])
    else:
        cuts = core.plan_cuts(spans, video_end)
    print(f"[カット] {len(cuts)}カット: "
          + " ".join(f"{c['start']:.1f}s×{c['zoom']}/{c['angle']:+g}°" for c in cuts))
    face_at = (lambda t: quiz_pipeline.FACE_WIDE if pullback_at is not None and t >= pullback_at
               else quiz_pipeline.FACE_CLOSE)

    inserts = chibi.plan_inserts(reaction_spans, captions)
    print("[ちびキャラ] " + (" ".join(f"{i['start']:.1f}-{i['end']:.1f}s {i['reaction']}"
                                     for i in inserts) or "なし"))
    # 重ねる順: ちびキャラ → パネル → 字幕（あとのものが上）
    overlays = (chibi.build_overlays(inserts, work, width=quiz_pipeline.QUIZ_CHIBI_W)
                + [{"path": panel, "start": 0.0, "end": video_end + 1.0, "x": 0, "y": 0}]
                + rich_text.caption_overlays(captions, quiz_pipeline.CAPTION_TOP, work))
    core.run_ffmpeg_finalize(source, mp4_path,
                             core.base_vf_parts() + core.build_cut_filters(cuts, face_at),
                             timeout=300, bgm_path=bgm_path, overlays=overlays,
                             sfx=chibi.build_sfx(inserts), max_duration=video_end)


def _upload(item, script, mp4_path, thumbnail_path, bgm_generated=False, en_cues=None, en=None):
    from youtube import upload_to_youtube, save_youtube_upload_to_db, notify_discord
    from description import build_pickup_title, build_pickup_description, _credits

    title = build_pickup_title(item, script.get("title_hook", ""))
    description = build_pickup_description(item, bgm_generated=bgm_generated)
    tags = [item["title"], pickup_sources.THEME_LABELS[item["theme"]]]
    if item.get("artist"):
        tags.append(item["artist"])

    en_upload = None
    if en:
        en_upload = {
            "title": english.build_title(
                f"{item.get('original_title') or item['title']}: {en['title']}", "Bot-tan's Pick"),
            "description": english.build_description("pickup", _credits(bgm_generated)),
            "srt": english.build_srt(en_cues or []),
        }

    yt_url = core._timed("Step8 YT投稿", upload_to_youtube,
                         mp4_path, title, description, thumbnail_path, en_upload, tags)
    if yt_url:
        if os.getenv("YOUTUBE_PRIVACY", "public") == "public":
            save_youtube_upload_to_db(yt_url, title, [
                {"corner_name": "PickupCorner", "theme": item["theme"], "key": item["key"],
                 "title": item["title"]},
            ])
            notify_discord(yt_url, title)
        if core.env_flag("PICKUP_NO_CONSUME"):
            print("[作品] PICKUP_NO_CONSUME=true のため台帳に記録しません")
        else:
            pickup_sources.mark_used(item, yt_url)


if __name__ == "__main__":
    sys.exit(main())
