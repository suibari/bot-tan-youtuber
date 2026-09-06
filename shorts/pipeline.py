#!/usr/bin/env python3
"""
botたん YouTube Shorts 自動投稿パイプライン

環境変数:
  GEMINI_API_KEY      : Gemini APIキー (USE_LOCAL_LLM=false時)
  YOUTUBE_CLIENT_ID   : YouTube OAuth2 クライアントID
  YOUTUBE_CLIENT_SECRET: YouTube OAuth2 クライアントシークレット
  USE_LOCAL_LLM       : true でOllama(Gemma 4 26B)、false でGemini (デフォルト: true)
                        num_ctx は common/llm.py の定数。env では変えられない
  LOCAL_LLM_MODEL     : Ollamaで使うモデル名 (デフォルト: hf.co/unsloth/gemma-4-12B-it-qat-GGUF:UD-Q4_K_XL)
  GEMINI_MODEL        : Geminiのモデル名 (デフォルト: gemini-2.0-flash)
  VOICEVOX_URL        : VOICEVOXのURL (デフォルト: http://localhost:10101)
  VOICEVOX_SPEAKER    : VOICEVOXのスピーカーID (デフォルト: 8)
  UNITY_EXE           : UnityエディタのパスまたはビルドされたPlayerのパス
  UNITY_PROJECT       : Unityプロジェクトのパス
  BGM_PATH            : BGM音声ファイルのパス (省略可)
  YOUTUBE_PRIVACY     : YouTube動画の公開設定 (public/private/unlisted, デフォルト: public)
  VRMA_PULLBACK       : 生成モーション開始以降カメラを引く量[m] (既定 0.7)
  （生成モーションの調整値 VRMA_GAIN / VRMA_HIPS_Y などは core.py 側を参照）
"""

import os
import random
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile

from dotenv import load_dotenv
load_dotenv()

from prompts import SYSTEM_PROMPT, build_user_prompt
from description import build_description, build_title

from thumbnail import capture_thumbnail_frame, generate_thumbnail

# 共通処理は core.py に集約されている。
# 既存の `from pipeline import ...` を壊さないよう、ここで再エクスポートする。
from core import (  # noqa: F401
    VOICEVOX_URL, VOICEVOX_SPEAKER, UNITY_EXE, UNITY_PROJECT, BGM_PATH,
    USE_LOCAL_LLM, LLM_MODELS, LLM_MODEL, DB_CONFIG,
    W, H, FONT_PATH,
    _timed, _retry, _llm_create, parse_script_json, llm_json,
    _rescale_va, enforce_variance, build_emotion_timeline,
    get_wav_duration, _synthesize, valence_arousal_to_voicevox_params,
    synthesize_sentences, make_silence_wav, concat_wavs, generate_voice,
    _query_mora_times, split_sentences, generate_subtitle_timing, _find_subtitle_time,
    _dedupe_subtitle_overlaps,
    _start_xvfb, record_with_unity, VRMA_MOTION_DIR,
    ardy_start, ardy_wait_ready, ardy_stop, build_vrma_motions,
    VRMA_SEG_MIN_SEC, VRMA_TAIL_PAD,
    VRMA_GAIN, VRMA_HIPS_Y, VRMA_SEG_TARGET_SEC, VRMA_MAX_SEGMENTS_TOTAL,
    VRMA_BODY_TILT, VRMA_YAW_LIMIT, VRMA_HEAD_YAW, VRMA_HEAD_COUNTER,
    plan_vrma_from_sentences, vrma_unity_args, env_flag,
    esc_drawtext, base_vf_parts, build_subtitle_filters, build_target_filters,
    run_ffmpeg_finalize, cleanup_old_temp_files,
)

# Animatorの既定ステート Blow A Kiss は、トリガー無しで冒頭から4.57秒流れる
# （Assets/Animations/Blow A Kiss.fbx, firstFrame:0 lastFrame:137 @30fps）。
# 生成モーションを被せると尻切れになるので、これが終わるまでは置かない
HOOK_MOTION_SEC = 4.6
# 台本が長すぎたときに警告を出す閾値[秒]。目標は30秒。
# 無人実行なので生成は止めず、ログに実測尺と文字数を残してプロンプト調整の材料にする
NIGHT_DURATION_WARN_SEC = 35.0
# 引き開始以降カメラを引く量[m]
VRMA_PULLBACK   = float(os.getenv("VRMA_PULLBACK", "0.7"))

# Gemini response_schema: 台本をJSONとして構造化出力させるスキーマ
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "sentences": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text":    {"type": "string"},
                                "valence": {"type": "number"},
                                "arousal": {"type": "number"},
                                # その文を話している間の体の動き（英語）。
                                # 文に紐づけることで、台詞と動きが必ず対応する
                                "motion":  {"type": "string"},
                            },
                            "required": ["text", "valence", "arousal"],
                        },
                    },
                },
                "required": ["section", "sentences"],
            },
        },
        "meta": {
            "type": "object",
            "properties": {
                "first_greeting_status": {"type": "string"},
                "nagi_themes":           {"type": "array", "items": {"type": "string"}},
            },
            "required": ["first_greeting_status", "nagi_themes"],
        },
    },
    "required": ["sections", "meta"],
}

# ──────────────────────────────────────────────
# Step 1: ラズパイDBからデータ取得
# ──────────────────────────────────────────────

from psycopg2.extras import RealDictCursor

from common.db import connect_raw


def fetch_from_bot_db() -> dict:
    """ラズパイDBからNagiのポストとMood履歴を取得する"""
    print("[DB] ラズパイDBに接続中...")

    conn = connect_raw()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            # 今日のNagiの高得点ポスト
            cur.execute("""
                SELECT
                    p.did,
                    p.text                AS post_text,
                    s.score               AS score,
                    p.record_created_at   AS created_at
                FROM nagi.post_scores s
                JOIN nagi.posts p ON s.post_uri = p.uri
                WHERE s.score >= 88
                  AND p.record_created_at >= NOW() - INTERVAL '1 days'
                  AND p.deleted_at IS NULL
                  AND p.kossori = false
                ORDER BY s.score DESC
            """)
            interactions = cur.fetchall()

            # 今週エネルギー
            cur.execute("""
                SELECT
                    status,
                    mood,
                    mood_en,
                    energy,
                    created_at
                FROM affirmative_bot.biorhythm_history
                WHERE created_at >= NOW() - INTERVAL '1 days'
                ORDER BY RANDOM()
            """)
            moods = cur.fetchall()

    finally:
        conn.close()

    print(f"[DB] Nagiポスト: {len(interactions)}件, Mood履歴: {len(moods)}件")
    all_moods = [dict(r) for r in moods]
    # statusごとに1件ずつランダムサンプリング
    seen_status = {}
    for m in all_moods:
        s = m.get("status")
        if s not in seen_status:
            seen_status[s] = m
    sampled_moods = list(seen_status.values())
    random.shuffle(sampled_moods)

    return {
        "interactions": [dict(r) for r in interactions],
        "moods":        sampled_moods,
    }


def pick_closing_mood(moods: list[dict], corner_context: dict = None) -> dict | None:
    """③締めで使う botたん自身のエピソードを1件だけ選ぶ。

    **LLM に選ばせない。** 以前は Mood を20件並べて「この中から選んでください」と
    書いていたが、同じプロンプトに他人の Nagi 投稿の一覧も並んでおり、どちらも
    「一人称の日本語で書かれた具体的な出来事」の箇条書きだったので、LLM が
    投稿のほうを botたんの体験として使ってしまった（2026-08-25 18:00 の
    「資格取得のために警察署に行って」は、その日の Nagi 投稿そのもの）。
    1件に確定して渡せば「選ぶ」余地が無くなり、混同そのものが起きなくなる。

    直近2日で使った status（corner_context）は Python 側で避ける。避けた結果
    候補が無くなったら、除外を無視して選ぶ（無人実行なので候補ゼロで落とさない）。
    """
    if not moods:
        return None
    excluded = set((corner_context or {}).get("excluded_first_greeting_statuses") or [])
    candidates = [m for m in moods if m.get("status") not in excluded] or list(moods)
    chosen = random.choice(candidates)
    print(f"[Mood] ③締め: status={chosen.get('status')} / {str(chosen.get('mood'))[:40]}"
          f"（候補{len(candidates)}件 / 除外status={sorted(excluded) or 'なし'}）")
    return chosen


# ──────────────────────────────────────────────
# Step 2: LLMで台本生成
# ──────────────────────────────────────────────

def generate_script(data: dict, comments: list[dict] = None, corner_context: dict = None,
                    closing_mood: dict = None) -> str:
    """DBデータから台本を生成する"""
    print(f"[LLM] 台本生成中...")

    user_prompt = build_user_prompt(data, comments=comments, corner_context=corner_context,
                                    closing_mood=closing_mood)
    # スキーマは経路によらず response_format で渡す。Gemini はそのまま
    # OpenAI 互換で受け、Ollama は common/llm.py が native の format へ写す。
    # （extra_body に response_mime_type/response_schema を渡すと Gemini は400になる。
    #   逆に Ollama へ extra_body で options を渡しても黙って捨てられる）
    extra_kwargs: dict = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name":   "script",
                "schema": SCRIPT_SCHEMA,
            },
        }
    }

    response = _llm_create(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        **extra_kwargs,
    )
    print(f"[DEBUG] finish_reason: {response.choices[0].finish_reason}")

    script = response.choices[0].message.content.strip()
    print(f"[LLM] 台本生成完了:\n{script}\n")
    return script


def generate_corner_timing(
    script: str,
    subtitles: list[dict],
    intro_duration: float = 0.0,
    section_starts: dict[str, str] = None,
    has_comments: bool = False,
) -> list[dict]:
    """セクションタグで確定したタイミングでコーナーラベルを生成する"""
    # label/color は画面に出さなくなった（左上のコーナーテロップは
    # ターゲットテロップに置き換えた）が、corners はカメラ引き・サムネ撮影・
    # DoThankful の探索開始位置を決めるのに使い続けるので残してある。
    if has_comments:
        corner_meta = [('CommentCorner', "コメントコーナー", "#ff9f43")]
    else:
        corner_meta = [('NagiCorner', "今日のNagi", "#00A88A")]
    corner_meta.append(('Closing', "全肯定メッセージ", "#7ec8e3"))
    total_duration = subtitles[-1]["end"] if subtitles else 90

    resolved_starts = []
    search_from = intro_duration
    for tag, _label, _color in corner_meta:
        keyword = (section_starts or {}).get(tag)
        t = _find_subtitle_time(subtitles, keyword, start_from=search_from) if keyword else None
        if t is not None:
            print(f"[コーナー] {tag}: '{keyword}' → {t}s")
            search_from = t + 0.01
        else:
            print(f"[コーナー] {tag}: キーワード未検出、フォールバック使用")
        resolved_starts.append(t)

    # フォールバック: 見つからないセクションはコーナー数で等分して補完
    body_duration = total_duration - intro_duration
    share = body_duration / len(corner_meta)
    corners = []
    for i, (tag, label, color) in enumerate(corner_meta):
        start = resolved_starts[i] if resolved_starts[i] is not None else round(share * i + intro_duration, 3)
        next_start = next((resolved_starts[j] for j in range(i + 1, len(resolved_starts)) if resolved_starts[j] is not None), None)
        end = next_start if next_start is not None else total_duration
        # tag は section 名。生成モーションの割り当てに使う（ffmpeg側は label/color しか見ない）
        corners.append({"start": round(start, 3), "end": round(end, 3),
                        "label": label, "color": color, "tag": tag,
                        "detected": resolved_starts[i] is not None})

    return corners

def build_vrma_blocks(sentences: list[dict], durations: list[float],
                      intro_duration: float, vrma_end: float) -> list[dict]:
    """文ごとのモーションを、その文が読まれる時刻に置く連続モーションを組む。

    sentences: 本編の文（"motion" を持ちうる）。generate_voice に渡したものと同じ順。
    durations: generate_voice が返した各文の実測尺[秒]。
    intro_duration: 冒頭一言の尺[秒]。本編はここから始まる。
    vrma_end: 生成モーションを終わらせる時刻[秒]。DoThankful に食い込ませないこと。

    夜版でMixamoが走るのは 冒頭の Blow A Kiss（4.57秒）と Closing の
    DoThankful・DoWave だけ。その間を1本の連続モーションで埋める。

    NOTE: 以前はコーナー単位の motions を尺で按分していたが、どの文に対応するかを
          見ていなかったため、最後の文のために書いた動きがコーナーの中盤で再生されていた。
          「ただ動いている」だけで話している内容と合わない状態だったので、文に紐づけた。
    """
    w_start, w_end = HOOK_MOTION_SEC, float(vrma_end) - VRMA_TAIL_PAD
    if w_end - w_start < VRMA_SEG_MIN_SEC:
        print("[モーション] 埋められる区間がありません")
        return []

    spans, t = [], float(intro_duration)
    for sent, dur in zip(sentences, durations):
        spans.append({"start": t, "end": t + dur, "motion": sent.get("motion")})
        t += dur

    segments = plan_vrma_from_sentences(spans, w_start, w_end)
    if not segments:
        print("[モーション] 埋められる区間がありません")
        return []

    n_auth = sum(1 for sp in spans
                 if (sp.get("motion") or "").strip() and sp["end"] > w_start and sp["start"] < w_end)
    print(f"[モーション] {w_start:.1f}〜{w_end:.1f}秒 → {len(segments)}セグメント "
          f"（モーション指定のある文 {n_auth}/{len(spans)}）")
    return [{"name": "body1", "time": round(w_start, 2), "segments": segments}]


# ──────────────────────────────────────────────
# Step 5: FFmpegでMP4に変換・仕上げ
# ──────────────────────────────────────────────

def finalize_video(input_webm: str, output_mp4: str,
                   subtitles: list[dict] = None,
                   target_text: str = "") -> None:
    """FFmpegで縦型Shorts用MP4に変換・字幕合成する（夜版レイアウト）

    target_text は「誰に向けた動画か」を示す一言（Thumbnail セクション＝サムネに
    焼くのと同じ文言）。以前は左上にコーナー名（「今日のNagi」等）を出していたが、
    Shorts はスワイプで途中から入るので、コーナー名より「自分向けの動画か」が
    分かるほうが効く。
    """
    print(f"[FFmpeg] MP4変換中...")

    vf_parts = base_vf_parts()
    if subtitles:
        vf_parts += build_subtitle_filters(subtitles)
    if target_text:
        vf_parts += build_target_filters(target_text)

    run_ffmpeg_finalize(input_webm, output_mp4, vf_parts)


# ──────────────────────────────────────────────
# Step 6: YouTubeにアップロード
# ──────────────────────────────────────────────

from youtube import (
    fetch_youtube_comments,
    upload_to_youtube,
    fetch_recent_corners,
    get_recent_video_stats,
    should_enable_comment_corner,
    fetch_nagi_corner_context,
    save_youtube_upload_to_db,
    notify_discord,
)

def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = Path(tempfile.gettempdir())

    wav_path       = str(tmp_dir / f"bottan_{ts}.wav")
    intro_wav_path = wav_path.replace(".wav", "_intro.wav")
    webm_path      = str(tmp_dir / f"bottan_{ts}.webm")
    mp4_path    = str(tmp_dir / f"bottan_{ts}.mp4")
    screenshot_path = str(tmp_dir / f"bottan_{ts}_thumbnail.png")

    total_start = time.time()
    try:
        # Step 1: DBからデータ取得
        data = _timed("Step1 DB取得", fetch_from_bot_db)
        if not data["interactions"] and not data["moods"]:
            print("[ERROR] データが取得できませんでした")
            return

        # Step 1.5: コメントコーナー有効化判定 → コメント取得
        recent_stats = get_recent_video_stats(n=3)
        use_comment_corner = should_enable_comment_corner(recent_stats)
        print(f"[コメント] コメントコーナー有効化条件: {'満たした' if use_comment_corner else '未達 → スキップ'}")

        comments = fetch_youtube_comments() if use_comment_corner else None
        if not comments:
            comments = None
        has_comments = bool(comments)
        print(f"[コメント] CommentCorner: {'あり (' + str(len(comments)) + '件)' if has_comments else 'なし → スキップ'}")

        # corner_context取得（直近status除外・Nagi参考/除外リスト）
        corner_context = {}
        try:
            recent_corners = fetch_recent_corners(limit=2)
            nagi_context = fetch_nagi_corner_context()
            corner_context = {**recent_corners, **nagi_context}
        except Exception as e:
            print(f"[corner_context] 取得失敗（スキップ）: {e}")

        # ③締めで使う botたん自身のエピソードは Python 側で1件に確定する。
        # LLM に一覧から選ばせると、隣に並ぶ他人の Nagi 投稿を自分の体験として
        # 使うことがある（README「他人の投稿を自分の体験にしない」）
        closing_mood = pick_closing_mood(data["moods"], corner_context)

        # ARDYサーバーを先に起動しておく。
        # モデル読み込みに4〜5分かかるので、台本生成と音声合成の裏でロードさせる。
        # 裏で走るぶん優先度は下げる。CPU 版 VOICEVOX と食い合うと合成が数十秒に
        # 伸びて Step3 で落ちる（2026-08-31）。読み込みは遅れても待てる
        ardy_proc = None
        if VRMA_MOTION_DIR:
            ardy_proc = ardy_start(low_priority=True)

        # Step 2: 台本生成
        script_cache = os.getenv("SCRIPT_CACHE", "")
        if script_cache and Path(script_cache).exists():
            raw_script = open(script_cache).read()
            print(f"[LLM] キャッシュから台本読み込み: {script_cache}")
            # 台本は別の Mood で書かれているので、いま選んだものは捨てる
            closing_mood = None
        else:
            raw_script = _timed("Step2 台本生成", generate_script, data, comments,
                                corner_context, closing_mood)

        # Step 2.5: JSONパース、クリーン台本作成
        script_data = parse_script_json(raw_script)
        sections = {s["section"]: s["sentences"] for s in script_data["sections"]}
        script_meta = script_data["meta"]
        # first_greeting_status は Python 側で確定した Mood のものを正とする。
        # LLM の自己申告は、実際に使った Mood とズレることがある
        closing_status = (closing_mood or {}).get("status") or script_meta.get("first_greeting_status", "")
        print(f"[META] first_greeting_status={closing_status}, "
              f"nagi_themes={script_meta.get('nagi_themes')}")
        thumbnail_sentences = sections.get("Thumbnail", [])
        thumbnail_text = thumbnail_sentences[0]["text"][:20] if thumbnail_sentences else "今日も全肯定だよ！"
        print(f"[サムネイル] 一言: {thumbnail_text}")
        # Thumbnail以外の全文をフラットなリストに
        main_sentences = [
            sent
            for s in script_data["sections"]
            if s["section"] != "Thumbnail"
            for sent in s["sentences"]
        ]
        main_sentences = enforce_variance(main_sentences)
        clean_script = "".join(s["text"] for s in main_sentences)
        # コーナータイミング用: 各セクション先頭テキストの先頭8文字
        section_starts = {k: v[0]["text"][:8] if v else "" for k, v in sections.items()}
        print(f"[セクション] 検出: {list(sections.keys())}")

        # Step 3: 音声生成。各文の実測尺を受け取り、モーションを文に紐づけるのに使う
        sentence_durations = _timed("Step3 音声生成", generate_voice,
                                    main_sentences, wav_path, thumbnail_text)

        # 冒頭一言の音声時間を取得
        intro_duration = get_wav_duration(intro_wav_path) if thumbnail_text and Path(intro_wav_path).exists() else 0.0

        # 本編音声の実際の長さを取得（字幕タイミングのスケーリング基準に使用）
        # 各文個別合成+concatの場合はtotal_sentが実際の音声長さに近い（フルスクリプト
        # 1回クエリより正確）ため、実際のWAV長さを渡すことで字幕ずれを防ぐ
        actual_main_duration = get_wav_duration(wav_path) - intro_duration if Path(wav_path).exists() else None
        print(f"[字幕] 本編音声長さ: {actual_main_duration:.3f}s" if actual_main_duration else "[字幕] 本編音声長さ取得失敗、フォールバック使用")

        total_sec = intro_duration + (actual_main_duration or 0.0)
        print(f"[尺] 合計 {total_sec:.1f}秒 (冒頭一言 {intro_duration:.1f}s + 本編 "
              f"{(actual_main_duration or 0.0):.1f}s / 本文{len(clean_script)}文字)")
        if total_sec > NIGHT_DURATION_WARN_SEC:
            print(f"[警告] 台本が長すぎます: {total_sec:.1f}秒 "
                  f"(目標30秒 / 警告閾値{NIGHT_DURATION_WARN_SEC}秒, 本文{len(clean_script)}文字)")

        # Step 3.5: 字幕・コーナータイミング生成
        subtitles = generate_subtitle_timing(clean_script, time_offset=intro_duration, actual_duration=actual_main_duration)
        if intro_duration > 0:
            # 冒頭一言も本編チャンクと同じ扱いにする（+0.05 の余韻を付け、
            # 本編1枚目との間隔を dedupe に通す）。以前は dedupe のあとに
            # 足していたので、冒頭字幕だけ言い終わる前に消えることがあった
            subtitles = [{"start": 0.0, "end": round(intro_duration + 0.05, 3),
                          "text": thumbnail_text}] + subtitles
            _dedupe_subtitle_overlaps(subtitles)
        corners   = generate_corner_timing(clean_script, subtitles, intro_duration, section_starts, has_comments)

        # 感情タイムライン生成
        emotions, wave_time = build_emotion_timeline(main_sentences, subtitles, intro_duration)

        # トリガー発火時刻を字幕から算出
        # DoThankful: 締めセクション内に限定（corners末尾がClosing）
        closing_start = corners[-1]["start"] if corners else 0.0
        thankful_time  = _find_subtitle_time(subtitles, "高評価",   start_from=closing_start) or 0.0
        print(f"[トリガー] waveTime: {wave_time}s, thankfulTime: {thankful_time}s")

        # AI生成モーション。失敗しても動画は作る
        # 生成モーションは DoThankful の直前まで敷く。「高評価」が字幕から見つからず
        # thankful_time が 0 のときは DoThankful/DoWave と衝突しうるので Closing の手前で止める
        vrma_end = thankful_time if thankful_time > 0 else closing_start
        vrma_motions = []
        if VRMA_MOTION_DIR:
            try:
                blocks = build_vrma_blocks(main_sentences, sentence_durations,
                                           intro_duration, vrma_end)
                if blocks and ardy_wait_ready():
                    vrma_motions = _timed(
                        "Step3.8 モーション生成", build_vrma_motions,
                        blocks, VRMA_MOTION_DIR, datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d"))
            except Exception as e:
                print(f"[モーション] 生成に失敗しました（動画は続行）: {e}")
            finally:
                ardy_stop(ardy_proc)
                ardy_proc = None
            if not vrma_motions:
                # 動画は完成するのでパイプラインは成功のまま（notify.error ではない）。
                # 黙らせておくと「動画は出ているが棒立ち」に何日も気づけない。
                # 2026-08-31 の夜は exit 0 で公開まで通り、誰も気づかなかった
                from common import notify
                notify.warn("夜のShorts: ARDY のモーション生成に失敗しました。"
                            "モーション無しで公開します（logs/pipeline_*.log を確認）")

        # 感情JSONファイル保存
        emotion_path = str(tmp_dir / f"bottan_{ts}_emotions.json")
        # greetingTime1 は書き出さない。corners[0].end - 2.0 で発火するが、NagiCorner が
        # 毎回未検出のため常に「締めの2秒前」になり、Standing Greeting の手振り(5.10秒)が
        # コーナー境界を分断していた。省くと生成モーションが最後まで連続する
        payload = {
            "emotions":      emotions,
            "waveTime":      wave_time,
            "thankfulTime":  thankful_time,
        }
        if vrma_motions:
            payload["vrmaMotions"] = vrma_motions
        with open(emotion_path, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"[感情] 保存: {emotion_path}")
        for m in vrma_motions:
            print(f"[モーション] 生成: {m['file']} @{m['time']}s")

        # Step 4: Unity録画（Mono GC 競合による確率的クラッシュへの対策でリトライあり）
        # 生成モーションがあるときだけ、フックの次のコーナーからカメラを引く。
        # 冒頭は Blow A Kiss（手を顔に持っていく動き）なのでアップのままが合う
        extra = []
        if vrma_motions and VRMA_PULLBACK > 0:
            pullback_at = max(corners[0]["start"], HOOK_MOTION_SEC) if corners else HOOK_MOTION_SEC
            extra = ["-cameraPullbackAt", f"{pullback_at:.2f}",
                     "-cameraPullbackZ", f"{VRMA_PULLBACK}"] + vrma_unity_args()
        _retry("Step4 Unity録画", record_with_unity, wav_path, webm_path, emotion_path,
               extra_args=extra or None,
               catch=(RuntimeError, TimeoutError), delay=15)

        # サムネ作成
        thumbnail_path = str(tmp_dir / f"bottan_{ts}_thumbnail.png")
        # 生成モーションを使うときは、カメラが引く前（フック内）からサムネを撮る
        thumb_before = (max(corners[0]["start"], HOOK_MOTION_SEC)
                        if (vrma_motions and corners and VRMA_PULLBACK > 0) else None)
        capture_thumbnail_frame(webm_path, screenshot_path, emotions, before=thumb_before)
        generate_thumbnail(screenshot_path, thumbnail_path, thumbnail_text)

        # Step 5: MP4変換
        _timed("Step5 MP4変換", finalize_video, webm_path, mp4_path, subtitles, thumbnail_text)

        # Step 6: YouTubeアップロード
        title = build_title(thumbnail_text)
        description = build_description()
        if not env_flag("SKIP_YOUTUBE"):
            yt_url = _timed("Step6 YT投稿", upload_to_youtube, mp4_path, title, description, thumbnail_path)
            if yt_url and os.getenv("YOUTUBE_PRIVACY", "public") == "public":
                corners_metadata = [
                    {"corner_name": "Thumbnail", "theme": thumbnail_text},
                    {"corner_name": "Closing", "status": closing_status},
                ]
                nagi_themes = script_meta.get("nagi_themes", [])
                if isinstance(nagi_themes, list) and nagi_themes:
                    corners_metadata.append({"corner_name": "NagiCorner", "theme": nagi_themes})
                save_youtube_upload_to_db(yt_url, title, corners_metadata)
                notify_discord(yt_url, title)
        else:
            print("[YouTube] スキップ (SKIP_YOUTUBE=true)")

        elapsed = time.time() - total_start
        print(f"\n✅ パイプライン完了: {mp4_path}  (合計: {elapsed:.1f}秒)")

    except Exception as e:
        print(f"\n❌ エラー発生: {e}")
        raise

    finally:
        #pass
        # 一時ファイル削除
        for path in [wav_path, intro_wav_path, webm_path]:
            if Path(path).exists():
                Path(path).unlink()
                print(f"[Cleanup] 削除: {path}")


if __name__ == "__main__":
    main()
