#!/usr/bin/env python3
"""
botたん 夜の動画 自動投稿パイプライン（毎日 18:00・Bluesky）

Bluesky と Nagi の投稿から1件を紹介する縦動画を作り、Bluesky に投稿する（shorts/bluesky.py）。
その夜のおやすみポスト（bsky-affirmative-bot）が RP とコメントで紹介する。
YouTube には出さない（2026-10、YouTube は朝のクイズだけに絞った）。

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
  BSKY_DID, BSKY_APP_PASSWORD : 投稿する bot のアカウント
  SKIP_BLUESKY        : true で投稿せず、mp4 と投稿用 JSON を残して止まる（SKIP_YOUTUBE も同じ意味）
  VRMA_PULLBACK       : 生成モーション開始以降カメラを引く量[m] (既定 0.7)
  KEEP_TEMP           : true で一時ファイル（音声・録画）を残す
  NIGHT_MOUTH_CLOSE   : 無音時に表情の口成分を打ち消す強さ 0〜1 (既定 1.0)
  （生成モーションの調整値 VRMA_GAIN / VRMA_HIPS_Y などは core.py 側を参照）
"""

import os
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile

from dotenv import load_dotenv
load_dotenv()

from prompts import SYSTEM_PROMPT, build_user_prompt, usable_interactions
import english
import chibi
import rich_text

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
    VRMA_SEG_MIN_SEC, VRMA_TAIL_PAD, VRMA_OUTRO_SEC,
    VRMA_GAIN, VRMA_HIPS_Y, VRMA_SEG_TARGET_SEC, VRMA_MAX_SEGMENTS_TOTAL,
    VRMA_BODY_TILT, VRMA_YAW_LIMIT, VRMA_HEAD_YAW, VRMA_HEAD_COUNTER,
    plan_vrma_from_sentences, vrma_unity_args, env_flag,
    esc_drawtext, base_vf_parts, build_subtitle_filters, build_target_filters,
    build_top_subtitle_filters, TOP_SUB_MAX_CHARS, TOP_SUB_Y, plan_cuts, build_cut_filters,
    resolve_peak,
    run_ffmpeg_finalize, cleanup_old_temp_files,
)

# 冒頭のアップ（カメラを引かない区間）の長さ[秒]。
# 以前は Animatorの既定ステート Blow A Kiss（4.57秒）の尺に合わせていた名残で、
# いまは冒頭も生成モーションで動かす（-skipIntroClip で投げキッスを飛ばす）
HOOK_CLOSEUP_SEC = 4.6
# 台本が長すぎたときに警告を出す閾値[秒]。目標は30秒。
# 無人実行なので生成は止めず、ログに実測尺と文字数を残してプロンプト調整の材料にする
NIGHT_DURATION_WARN_SEC = 35.0
# 引き開始以降カメラを引く量[m]
VRMA_PULLBACK   = float(os.getenv("VRMA_PULLBACK", "0.7"))
# 表情プリセット(Fcl_ALL_*)は口が開くモーフを含むため、arousal が高い文のあとは
# 無音でも口が半開きのまま残る（2026-10-03 夜版で arousal が 1.0 まで上がり、
# だんだん口が開いていくように見えた）。朝版・ライブと同じく無音時に口成分を打ち消す
MOUTH_CLOSE = float(os.getenv("NIGHT_MOUTH_CLOSE", "1.0"))
# カット割りで寄るときの中心（顔の画面座標）。冒頭のアップと、カメラを引いたあとで違う。
# 2026-10-08 の録画のフレームから実測した値（目と口の中間）。冒頭は顔が画面いっぱい
NIGHT_FACE_CLOSE = (500, 1080)
NIGHT_FACE_WIDE  = (530, 990)
# Unity がカメラを引ききるまでの時間[秒]（VideoRecorder.PullbackDuration）
PULLBACK_MOVE_SEC = 0.4
# 最後の発話のあと、ループの継ぎ目までに残す余韻[秒]
LOOP_TAIL_SEC = 0.15

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
                                # 山場の演出（ちびキャラの差分名）。山場でない文は "none"
                                "reaction": {"type": "string", "enum": [
                                    "none", "surprising", "down", "thinking", "shame",
                                    "joyful", "smug", "sitting"]},
                                # 山場の文のうち、いちばん強調する語句（text からそのまま抜き出す）。
                                # 山場でない文は ""
                                "peak": {"type": "string"},
                            },
                            # reaction / peak も必須にする。任意にするとローカルLLMは
                            # reaction だけ書いて peak を省いた（2026-10-08 の試し撮りで2文とも）
                            "required": ["text", "valence", "arousal", "reaction", "peak"],
                        },
                    },
                },
                "required": ["section", "sentences"],
            },
        },
        "meta": {
            "type": "object",
            "properties": {
                "nagi_themes": {"type": "array", "items": {"type": "string"}},
                # ②で紹介した投稿の番号（prompts.usable_interactions の1始まり）
                "picked_post": {"type": "integer"},
                # Bluesky の動画ポストの添え文（日本語）
                "post_caption": {"type": "string"},
            },
            "required": ["nagi_themes", "picked_post", "post_caption"],
        },
    },
    "required": ["sections", "meta"],
}

# ──────────────────────────────────────────────
# Step 1: ラズパイDBからデータ取得
# ──────────────────────────────────────────────

from psycopg2.extras import RealDictCursor

from common import bgm
from common.db import connect_raw
import bluesky
import night_videos

# 1ネットワークあたりの候補数。プロンプトの投稿一覧が長くなりすぎないように絞る
POSTS_PER_NETWORK = 15


def fetch_from_bot_db() -> dict:
    """ラズパイDBから Bluesky / Nagi の高得点ポストを取得する。

    夜版は Bluesky に出すので、紹介する投稿も Bluesky と Nagi の両方から取る。
    各行に network（"bsky" / "nagi"）と表示名（display_name）を付ける。
    表示名は動画ポストの添え文に入れる（誰の投稿かが分かる単独のポストにするため）。

    以前は締めで使う botたん自身の出来事（biorhythm_history）も取っていたが、
    ループする台本にしたときに締めから外した（prompts.py の構成を参照）。
    """
    print("[DB] ラズパイDBに接続中...")

    conn = connect_raw()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            # 今日のNagiの高得点ポスト
            cur.execute("""
                SELECT
                    'nagi'                AS network,
                    p.uri,
                    p.did,
                    p.text                AS post_text,
                    s.score               AS score,
                    p.record_created_at   AS created_at,
                    COALESCE(NULLIF(pr.display_name, ''), a.handle, p.did) AS display_name
                FROM nagi.post_scores s
                JOIN nagi.posts p ON s.post_uri = p.uri
                LEFT JOIN nagi.profiles pr ON pr.did = p.did
                LEFT JOIN nagi.actors a ON a.did = p.did
                WHERE s.score >= 88
                  AND p.record_created_at >= NOW() - INTERVAL '1 days'
                  AND p.deleted_at IS NULL
                  AND p.kossori = false
                ORDER BY s.score DESC
                LIMIT %s
            """, (POSTS_PER_NETWORK,))
            nagi_rows = [dict(r) for r in cur.fetchall()]

            # 今日の Bluesky の高得点ポスト（botたんが全肯定したもの。1人1行）
            cur.execute("""
                SELECT
                    'bsky'      AS network,
                    uri,
                    did,
                    post        AS post_text,
                    score,
                    created_at
                FROM affirmative_bot.posts
                WHERE score >= 88
                  AND created_at >= NOW() - INTERVAL '1 days'
                  AND uri IS NOT NULL
                ORDER BY score DESC, created_at DESC
                LIMIT %s
            """, (POSTS_PER_NETWORK,))
            bsky_rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    names = bluesky.fetch_display_names([r["did"] for r in bsky_rows])
    for r in bsky_rows:
        r["display_name"] = names.get(r["did"]) or r["did"]

    # 両方を混ぜて並べる。どちらかに偏らないよう交互に置く（LLM は前のほうを選びやすい）
    interactions = []
    for i in range(max(len(nagi_rows), len(bsky_rows))):
        for rows in (bsky_rows, nagi_rows):
            if i < len(rows):
                interactions.append(rows[i])

    print(f"[DB] Blueskyポスト: {len(bsky_rows)}件, Nagiポスト: {len(nagi_rows)}件")
    return {"interactions": interactions}


def pick_source_post(data: dict, picked_post) -> dict | None:
    """台本の meta.picked_post（1始まり）から紹介元の投稿を引く。

    番号が範囲外・未指定なら None（添え文にリンクを付けないだけで、投稿は止めない）。
    """
    rows = usable_interactions(data)
    if not isinstance(picked_post, int) or not 1 <= picked_post <= len(rows):
        print(f"[紹介元] picked_post={picked_post!r} が投稿一覧（{len(rows)}件）にありません。リンクなしで続行")
        return None
    r = rows[picked_post - 1]
    return {
        "network": r.get("network") or "nagi",
        "uri": r.get("uri"),
        "did": r.get("did"),
        "display_name": r.get("display_name") or "",
        "url": bluesky.public_post_url(r.get("uri") or ""),
    }


# ──────────────────────────────────────────────
# Step 2: LLMで台本生成
# ──────────────────────────────────────────────

# NagiCorner に「Bluesky」「Nagi」が無いときに台本を書き直させる回数（ローカルLLMなので課金は無い）
NAGI_MENTION_RETRIES = 2


def mentions_nagi(raw_script: str) -> bool:
    """NagiCorner の文に、どこで見た投稿か（「Bluesky」か「Nagi」）が入っているか。
    JSON が壊れていたら判定しない（真を返す）。"""
    try:
        sd = parse_script_json(raw_script)
    except Exception:
        return True
    texts = [st.get("text", "") for sec in sd.get("sections", [])
             if sec.get("section") == "NagiCorner" for st in sec.get("sentences", [])]
    return not texts or any(w in t for t in texts for w in ("Nagi", "ナギ", "Bluesky", "ブルースカイ"))


def generate_script(data: dict, corner_context: dict = None) -> str:
    """DBデータから台本を生成する"""
    print(f"[LLM] 台本生成中...")

    user_prompt = build_user_prompt(data, corner_context=corner_context)
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
) -> list[dict]:
    """セクションタグで確定したタイミングでコーナーラベルを生成する"""
    # label/color は画面に出さなくなった（左上のコーナーテロップは
    # ターゲットテロップに置き換えた）が、corners はカメラ引き・サムネ撮影・
    # DoThankful の探索開始位置を決めるのに使い続けるので残してある。
    corner_meta = [('NagiCorner', "今日のSNS", "#00A88A"),
                   ('Closing', "全肯定メッセージ", "#7ec8e3")]
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
                      intro_duration: float, vrma_end: float,
                      intro_motion: str | None = None) -> list[dict]:
    """文ごとのモーションを、その文が読まれる時刻に置く連続モーションを組む。

    sentences: 本編の文（"motion" を持ちうる）。generate_voice に渡したものと同じ順。
    durations: generate_voice が返した各文の実測尺[秒]。
    intro_duration: 冒頭一言の尺[秒]。本編はここから始まる。
    vrma_end: 生成モーションを終わらせる時刻[秒]。録画は音声の後ろにも続くので、
              音声の全長 + VRMA_OUTRO_SEC を渡す。最後の文の動きがそこまで延びる。
    intro_motion: 冒頭一言（Thumbnail）の motion。

    冒頭の Blow A Kiss と締めの DoThankful・DoWave（Mixamo）は撤去し、
    0秒から最後まで1本の連続モーションで埋める。冒頭・締めの動きも
    ペルソナが文ごとに書いたものを使うので、毎回違う動きになる。

    NOTE: 以前はコーナー単位の motions を尺で按分していたが、どの文に対応するかを
          見ていなかったため、最後の文のために書いた動きがコーナーの中盤で再生されていた。
          「ただ動いている」だけで話している内容と合わない状態だったので、文に紐づけた。
    """
    w_start, w_end = 0.0, float(vrma_end)
    if w_end - w_start < VRMA_SEG_MIN_SEC:
        print("[モーション] 埋められる区間がありません")
        return []

    spans, t = [], float(intro_duration)
    if intro_duration > 0:
        spans.append({"start": 0.0, "end": float(intro_duration), "motion": intro_motion})
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

def sentence_spans(sentences: list[dict], durations: list[float],
                   intro_duration: float) -> list[dict]:
    """文ごとの区間と演出。山場（reaction のある文と決め台詞）は強く寄る。"""
    spans = [{"start": 0.0, "end": intro_duration, "emphasis": False}] if intro_duration > 0 else []
    t = intro_duration
    for sent, dur in zip(sentences, durations):
        reaction = chibi.reaction_of(sent)
        spans.append({"start": round(t, 3), "end": round(t + dur, 3), "reaction": reaction,
                      "emphasis": bool(reaction) or sent.get("_section") == "Closing"})
        t += dur
    return spans


# 寄らない文がこれより長いときは、途中の字幕の切れ目で画角を1回変える
CALM_SPLIT_SEC = 5.0


def cut_spans(spans: list[dict], subtitles: list[dict]) -> list[dict]:
    """文の区間から、カットの区間を作る。

    山場の文は丸ごと寄るのではなく、普段の画で入って、強調語句の手前で強く寄る。
    ちびキャラを出す文は強調語句の字幕がイラストになるので、その1枚前の字幕で寄る
    （寄り → イラスト の順に盛り上げる）。出さない文（決め台詞など）は強調語句の
    字幕（無ければ最後の字幕）で寄る。
    寄らない長い文は、真ん中に近い字幕の切れ目で1回だけ画角を変える。
    """
    out = []
    for sp in spans:
        out.append({"start": sp["start"], "emphasis": False})
        inside = chibi.subtitles_in(sp, subtitles)
        if sp.get("emphasis"):
            # 強調語句（peak）の字幕を基準にする。無ければ最後の字幕
            k = next((i for i, s in enumerate(inside) if s.get("peak")), len(inside) - 1)
            if sp.get("reaction"):
                k -= 1          # イラストになる1枚の手前で寄る
            if k >= 0 and inside and inside[k]["start"] - sp["start"] >= 0.3:
                out.append({"start": inside[k]["start"], "emphasis": True})
            else:
                out[-1]["emphasis"] = True
        elif sp["end"] - sp["start"] > CALM_SPLIT_SEC and len(inside) >= 2:
            mid = (sp["start"] + sp["end"]) / 2
            out.append({"start": min(inside[1:], key=lambda s: abs(s["start"] - mid))["start"],
                        "emphasis": False})
    return out


def finalize_video(input_webm: str, output_mp4: str,
                   subtitles: list[dict] = None, bgm_path=None,
                   pullback_at: float = None, end: float = None,
                   spans: list[dict] = None, work_dir=None,
                   en_cues: list[dict] = None) -> None:
    """FFmpegで縦型Shorts用MP4に変換・字幕合成する（夜版レイアウト）

    字幕は上部に大きく1枚ずつ出す（build_top_subtitle_filters）。以前は下の帯に
    小さい字幕、上部にはテーマの一言（Thumbnail）を動画全体に出していた。
    冒頭の字幕がテーマの一言そのものなので、入口でテーマが見えるのは変わらない。

    カットは文の切り替わりで切り、山場の文だけ強く寄る（core.plan_cuts）。
    reaction の付いた文では、ちびキャラに切り替えて効果音を鳴らす（chibi.py）。
    end は最後の発話の終わり。そこで切ってループさせる。

    en_cues は英訳の字幕（文単位）。画面下部に縁取り文字で焼き込む（rich_text.english_overlays）。
    Bluesky には YouTube の CC 字幕が無いので、英語圏の人にも動画の中で読めるようにする。
    """
    print(f"[FFmpeg] MP4変換中...")

    def face_at(t: float) -> tuple:
        if pullback_at is not None and t >= pullback_at:
            return NIGHT_FACE_WIDE
        return NIGHT_FACE_CLOSE

    vf_parts = base_vf_parts()
    overlays, sfx = [], []
    if subtitles:
        cut_end = end or subtitles[-1]["end"]
        spans = spans or [{"start": s["start"]} for s in subtitles]
        cspans = cut_spans(spans, subtitles)
        if pullback_at is not None:
            # 引いている 0.4秒は寄らない。引ききったところで次のカットへ
            cuts = plan_cuts(cspans, cut_end, forced=[pullback_at + PULLBACK_MOVE_SEC],
                             wide_at=[pullback_at])
        else:
            cuts = plan_cuts(cspans, cut_end)
        print(f"[カット] {len(cuts)}カット: "
              + " ".join(f"{c['start']:.1f}s×{c['zoom']}/{c['angle']:+g}°" for c in cuts))
        vf_parts += build_cut_filters(cuts, face_at)

        inserts = chibi.plan_inserts(spans, subtitles, video_end=cut_end)
        print("[ちびキャラ] " + (" ".join(f"{i['start']:.1f}-{i['end']:.1f}s {i['reaction']}"
                                         for i in inserts) or "なし"))
        overlays = chibi.build_overlays(inserts, work_dir or tempfile.gettempdir())
        # ちびキャラを出さない山場（決め台詞など）は、寄ったカットの頭で音を鳴らす
        emph = [c["start"] for c in cuts if c["zoom"] > 1.2
                and not any(sp.get("reaction") and sp["start"] <= c["start"] < sp["end"]
                            for sp in spans)]
        sfx = chibi.build_sfx(inserts, emph)
        # 字幕は画像で描いてイラストの上に重ねる（rich_text。文字の内側に紺が出ない）
        overlays += rich_text.caption_overlays(subtitles, TOP_SUB_Y, work_dir or tempfile.gettempdir())
        if en_cues:
            overlays += rich_text.english_overlays(en_cues, work_dir or tempfile.gettempdir(),
                                                   end=cut_end)

    run_ffmpeg_finalize(input_webm, output_mp4, vf_parts, bgm_path=bgm_path,
                        max_duration=end, overlays=overlays, sfx=sfx)



def main():
    from common.unity_license import ensure
    from core import UNITY_EXE
    ensure(UNITY_EXE)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = Path(tempfile.gettempdir())

    wav_path       = str(tmp_dir / f"bottan_{ts}.wav")
    intro_wav_path = wav_path.replace(".wav", "_intro.wav")
    webm_path      = str(tmp_dir / f"bottan_{ts}.webm")
    bgm_path       = None   # Step3.7 で生成できたときだけ入る
    mp4_path    = str(tmp_dir / f"bottan_{ts}.mp4")

    total_start = time.time()
    try:
        # Step 1: DBからデータ取得
        data = _timed("Step1 DB取得", fetch_from_bot_db)
        if not data["interactions"]:
            print("[ERROR] データが取得できませんでした")
            return

        # corner_context取得（直近に扱ったテーマの除外リスト）
        corner_context = {}
        try:
            corner_context = night_videos.fetch_recent_context()
        except Exception as e:
            print(f"[corner_context] 取得失敗（スキップ）: {e}")

        # 音声が完成してから ARDY を起動する。
        ardy_proc = None

        # Step 2: 台本生成
        script_cache = os.getenv("SCRIPT_CACHE", "")
        if script_cache and Path(script_cache).exists():
            raw_script = open(script_cache).read()
            print(f"[LLM] キャッシュから台本読み込み: {script_cache}")
        else:
            raw_script = _timed("Step2 台本生成", generate_script, data, corner_context)
            # どこで見た投稿かは外せない（Nagi の紹介も兼ねている）。抜けたら書き直させる
            for retry in range(NAGI_MENTION_RETRIES):
                if mentions_nagi(raw_script):
                    break
                print(f"[LLM] NagiCorner に「Bluesky」「Nagi」が無いので書き直します（{retry + 1}回目）")
                raw_script = _timed("Step2 台本生成", generate_script, data, corner_context)
            else:
                if not mentions_nagi(raw_script):
                    print("[警告] 書き直しても NagiCorner に「Bluesky」「Nagi」が入りませんでした。このまま続けます")

        # Step 2.5: JSONパース、クリーン台本作成
        script_data = parse_script_json(raw_script)
        sections = {s["section"]: s["sentences"] for s in script_data["sections"]}
        script_meta = script_data["meta"]
        print(f"[META] nagi_themes={script_meta.get('nagi_themes')}")
        thumbnail_sentences = sections.get("Thumbnail", [])
        thumbnail_text = thumbnail_sentences[0]["text"][:20] if thumbnail_sentences else "今日も全肯定だよ！"
        print(f"[サムネイル] 一言: {thumbnail_text}")
        # Thumbnail以外の全文をフラットなリストに
        main_sentences = [
            {**sent, "_section": s["section"]}
            for s in script_data["sections"]
            if s["section"] != "Thumbnail"
            for sent in s["sentences"]
        ]
        main_sentences = enforce_variance(main_sentences)
        clean_script = "".join(s["text"] for s in main_sentences)
        # コーナータイミング用: 各セクション先頭テキストの先頭8文字
        section_starts = {k: v[0]["text"][:8] if v else "" for k, v in sections.items()}
        print(f"[セクション] 検出: {list(sections.keys())}")

        # 紹介元の投稿（動画ポストの添え文にリンクを付ける）
        picked = pick_source_post(data, script_meta.get("picked_post"))
        caption_ja = bluesky.strip_links(script_meta.get("post_caption") or "") or thumbnail_text
        print(f"[紹介元] {picked and (picked['network'], picked['display_name'], picked['uri'])}")
        print(f"[添え文] {caption_ja}")

        # Step 2.5: 英訳（動画に焼き込む英語字幕と、動画ポストの添え文の英語版）。
        # ollama が載っているうちに済ませる。添え文は最後の行として一緒に訳す（呼び出しは1回）。
        # 失敗したら None で、日本語だけで公開する
        en = _timed("Step2.5 英訳", english.translate,
                    [thumbnail_text] + [s["text"] for s in main_sentences] + [caption_ja],
                    thumbnail_text)
        if en is None:
            from common import notify
            notify.warn("夜の動画: 英訳に失敗しました。日本語字幕だけで投稿します（logs/pipeline_*.log を確認）")

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
        # 字幕は上部に大きく出すので、1枚を短めに切る（core.TOP_SUB_MAX_CHARS）
        # 山場の強調語句（台本の "peak"）で字幕を区切り、ちびキャラをその1枚に合わせる
        # reaction の無い文の peak は使わない（"none" の文に書かれても強調しない）
        peaks = [p for s in main_sentences
                 if chibi.reaction_of(s) and (p := resolve_peak(s["text"], s.get("peak")))]
        print(f"[強調] {peaks or 'なし'}")
        subtitles = generate_subtitle_timing(clean_script, time_offset=intro_duration,
                                             actual_duration=actual_main_duration,
                                             max_chars=TOP_SUB_MAX_CHARS, peaks=peaks)
        if intro_duration > 0:
            # 冒頭一言も本編チャンクと同じ扱いにする（+0.05 の余韻を付け、
            # 本編1枚目との間隔を dedupe に通す）。以前は dedupe のあとに
            # 足していたので、冒頭字幕だけ言い終わる前に消えることがあった
            subtitles = [{"start": 0.0, "end": round(intro_duration + 0.05, 3),
                          "text": thumbnail_text}] + subtitles
            _dedupe_subtitle_overlaps(subtitles)
        corners   = generate_corner_timing(clean_script, subtitles, intro_duration, section_starts)

        # 感情タイムライン生成
        emotions, wave_time = build_emotion_timeline(main_sentences, subtitles, intro_duration)

        # Mixamo のトリガー発火時刻を字幕から算出。生成モーションに失敗したときだけ使う
        # DoThankful: 締めセクション内に限定（corners末尾がClosing）
        closing_start = corners[-1]["start"] if corners else 0.0
        thankful_time  = _find_subtitle_time(subtitles, "高評価",   start_from=closing_start) or 0.0

        # BGM 生成（ACE-Step）。失敗したら固定曲に戻る。
        # ARDY と同じく ollama・Irodori を空けてから GPU を使うので、ARDY の直前に置く
        bgm_path = _timed("Step3.7 BGM生成", bgm.generate, "night", tmp_dir)

        # AI生成モーション。失敗しても動画は作る
        # 冒頭（Blow A Kiss）と締め（DoThankful・DoWave）の Mixamo も生成モーションに
        # 置き換えたので、0秒から録画の最後まで敷く
        vrma_motions = []
        if VRMA_MOTION_DIR:
            try:
                intro_motion = thumbnail_sentences[0].get("motion") if thumbnail_sentences else None
                blocks = build_vrma_blocks(main_sentences, sentence_durations,
                                           intro_duration, total_sec + VRMA_OUTRO_SEC,
                                           intro_motion)
                if blocks:
                    ardy_proc = ardy_start(reuse=False)
                if ardy_proc is not None and ardy_wait_ready():
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
                            "Mixamo の固定モーションだけで公開します（logs/pipeline_*.log を確認）")

        # 感情JSONファイル保存
        emotion_path = str(tmp_dir / f"bottan_{ts}_emotions.json")
        # greetingTime1 は書き出さない。corners[0].end - 2.0 で発火するが、NagiCorner が
        # 毎回未検出のため常に「締めの2秒前」になり、Standing Greeting の手振り(5.10秒)が
        # コーナー境界を分断していた。省くと生成モーションが最後まで連続する
        # 生成モーションがあるときは waveTime / thankfulTime を書き出さない
        # （キーが無ければ VRM1LipSync は DoWave / DoThankful を撃たない）。
        # 失敗時だけ従来の Mixamo に戻し、棒立ちのまま締まるのを防ぐ
        payload = {"emotions": emotions}
        if vrma_motions:
            payload["vrmaMotions"] = vrma_motions
        else:
            payload["waveTime"] = wave_time
            payload["thankfulTime"] = thankful_time
            print(f"[トリガー] waveTime: {wave_time}s, thankfulTime: {thankful_time}s")
        with open(emotion_path, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"[感情] 保存: {emotion_path}")
        for m in vrma_motions:
            print(f"[モーション] 生成: {m['file']} @{m['time']}s")

        # Step 4: Unity録画（Mono GC 競合による確率的クラッシュへの対策でリトライあり）
        # 生成モーションがあるときだけ、フックの次のコーナーからカメラを引く。
        # 冒頭はサムネを撮るのでアップのままにする
        extra = ["-mouthCloseOnSilence", f"{MOUTH_CLOSE}"] if MOUTH_CLOSE > 0 else []
        if vrma_motions:
            # 冒頭も生成モーションで動かすので、既定ステートの Blow A Kiss を飛ばす
            extra += ["-skipIntroClip", "1"]
        pullback_at = None
        if vrma_motions and VRMA_PULLBACK > 0:
            pullback_at = max(corners[0]["start"], HOOK_CLOSEUP_SEC) if corners else HOOK_CLOSEUP_SEC
            extra += ["-cameraPullbackAt", f"{pullback_at:.2f}",
                      "-cameraPullbackZ", f"{VRMA_PULLBACK}"] + vrma_unity_args()
        _retry("Step4 Unity録画", record_with_unity, wav_path, webm_path, emotion_path,
               extra_args=extra or None,
               catch=(RuntimeError, TimeoutError), delay=15)

        # 英語字幕は文単位。冒頭一言 + 本編の各文を、音声の実測尺で並べる（build_vrma_blocks と同じ）
        en_cues = None
        if en:
            en_cues, t = [{"start": 0.0, "end": intro_duration, "text": en["lines"][0]}], intro_duration
            for line, dur in zip(en["lines"][1:1 + len(sentence_durations)], sentence_durations):
                en_cues.append({"start": t, "end": t + dur, "text": line})
                t += dur

        # Step 5: MP4変換
        # 最後の発話の直後で切る。録画の末尾にはモーションの余白（VRMA_RECORD_TAIL_SEC）
        # があり、残すとループの継ぎ目で間が空く
        _timed("Step5 MP4変換", finalize_video, webm_path, mp4_path, subtitles,
               bgm_path, pullback_at, total_sec + LOOP_TAIL_SEC,
               sentence_spans(main_sentences, sentence_durations, intro_duration), tmp_dir,
               en_cues)

        # Step 6: Bluesky へ投稿する。投稿に要るものはすべて JSON に書き出し、
        # 投稿は bluesky.post_from_job が JSON だけを見て行う。
        # SKIP_BLUESKY=true のときは JSON と mp4 を残して止まる（確認してから手で投稿できる）
        job = {
            "mp4": mp4_path,
            "date": night_videos.bot_day(),
            "hook_ja": thumbnail_text,
            "hook_en": en["title"] if en else "",
            "caption_ja": caption_ja,
            "caption_en": bluesky.strip_links(en["lines"][-1]) if en else "",
            "source": picked,
            "themes": script_meta.get("nagi_themes") or [],
            "width": W, "height": H,
        }
        job_path = mp4_path.replace(".mp4", "_bluesky.json")
        with open(job_path, "w") as f:
            json.dump(job, f, ensure_ascii=False, indent=2, default=str)
        print(f"[Bluesky] 投稿用データ: {job_path}")

        if env_flag("SKIP_BLUESKY") or env_flag("SKIP_YOUTUBE"):
            print(f"[Bluesky] スキップ (SKIP_BLUESKY=true)。投稿するには:\n"
                  f"  ./venv/bin/python shorts/bluesky.py --from {job_path}")
        else:
            _timed("Step6 Bluesky投稿", bluesky.post_from_job, job_path)

        elapsed = time.time() - total_start
        print(f"\n✅ パイプライン完了: {mp4_path}  (合計: {elapsed:.1f}秒)")

    except Exception as e:
        print(f"\n❌ エラー発生: {e}")
        raise

    finally:
        #pass
        # 一時ファイル削除
        # KEEP_TEMP=true なら残す（朝版と同じ。カット割りの顔の位置を録画から測り直すとき用）
        for path in ([] if env_flag("KEEP_TEMP") else [wav_path, intro_wav_path, webm_path, bgm_path]):
            if path is None:
                continue
            if Path(path).exists():
                Path(path).unlink()
                print(f"[Cleanup] 削除: {path}")


if __name__ == "__main__":
    main()
