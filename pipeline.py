#!/usr/bin/env python3
"""
botたん YouTube Shorts 自動投稿パイプライン

環境変数:
  BLUESKY_HANDLE      : Blueskyハンドル (例: bot-tan.bsky.social)
  BLUESKY_PASSWORD    : Blueskyアプリパスワード
  GEMINI_API_KEY      : Gemini APIキー (USE_LOCAL_LLM=false時)
  YOUTUBE_CLIENT_ID   : YouTube OAuth2 クライアントID
  YOUTUBE_CLIENT_SECRET: YouTube OAuth2 クライアントシークレット
  USE_LOCAL_LLM       : true でOllama使用、false でGemini使用 (デフォルト: false)
  LOCAL_LLM_MODEL     : Ollamaで使うモデル名 (デフォルト: hf.co/unsloth/gemma-4-12b-it-GGUF:Q4_K_M)
  LOCAL_LLM_CTX       : OllamaのコンテキストサイズOverride (デフォルト: 8192)
  GEMINI_MODEL        : Geminiのモデル名 (デフォルト: gemini-2.0-flash)
  VOICEVOX_URL        : VOICEVOXのURL (デフォルト: http://localhost:10101)
  VOICEVOX_SPEAKER    : VOICEVOXのスピーカーID (デフォルト: 8)
  UNITY_EXE           : UnityエディタのパスまたはビルドされたPlayerのパス
  UNITY_PROJECT       : Unityプロジェクトのパス
  BGM_PATH            : BGM音声ファイルのパス (省略可)
  YOUTUBE_PRIVACY     : YouTube動画の公開設定 (public/private/unlisted, デフォルト: public)
"""

import os
import signal
import subprocess
import tempfile
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from atproto import Client
from openai import OpenAI, BadRequestError as _OpenAIBadRequestError

from dotenv import load_dotenv
load_dotenv()

from prompts import SYSTEM_PROMPT, build_user_prompt
from description import build_description, build_title

from thumbnail import capture_thumbnail_frame, generate_thumbnail

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────

BLUESKY_HANDLE   = os.getenv("BLUESKY_HANDLE", "bot-tan.suibari.com")
BLUESKY_PASSWORD = os.getenv("BLUESKY_PASSWORD", "")
VOICEVOX_URL     = os.getenv("VOICEVOX_URL", "http://localhost:10101")
VOICEVOX_SPEAKER = int(os.getenv("VOICEVOX_SPEAKER", "8"))
UNITY_EXE        = os.getenv("UNITY_EXE", "/home/suibari/Unity/Hub/Editor/6000.0.76f1/Editor/Unity")
UNITY_PROJECT    = os.getenv("UNITY_PROJECT", "/home/suibari/bottan-video")
BGM_PATH         = os.getenv("BGM_PATH", "")
USE_LOCAL_LLM    = os.getenv("USE_LOCAL_LLM", "false").lower() == "true"

# LLMクライアント初期化
if USE_LOCAL_LLM:
    llm_client = OpenAI(
        api_key="ollama",
        base_url="http://localhost:11434/v1"
    )
    LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "hf.co/unsloth/gemma-4-12b-it-GGUF:Q4_K_M")
    LLM_MODELS = [LLM_MODEL]
    print(f"[LLM] Ollama ({LLM_MODEL}) を使用します")
else:
    llm_client = OpenAI(
        api_key=os.getenv("GEMINI_API_KEY", ""),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    LLM_MODELS = [m.strip() for m in os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite").split(",") if m.strip()]
    LLM_MODEL = LLM_MODELS[0]
    print(f"[LLM] Gemini ({', '.join(LLM_MODELS)}) を使用します")

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
                "bluesky_themes":        {"type": "array", "items": {"type": "string"}},
            },
            "required": ["first_greeting_status", "bluesky_themes"],
        },
    },
    "required": ["sections", "meta"],
}

# ──────────────────────────────────────────────
# Step 1: ラズパイDBからデータ取得
# ──────────────────────────────────────────────

import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "192.168.1.200"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME", ""),
    "user":     os.getenv("DB_USER", ""),
    "password": os.getenv("DB_PASSWORD", ""),
}

def fetch_from_bot_db() -> dict:
    """ラズパイDBからインタラクションとMood履歴を取得する"""
    print("[DB] ラズパイDBに接続中...")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            # 今週のAIリプライ
            cur.execute("""
                SELECT
                    did,
                    details->>'text'         AS post_text,
                    (details->>'score')::int AS score,
                    created_at
                FROM affirmative_bot.interaction
                WHERE type = 'NormalReply'
                  AND (details->>'score')::int >= 88
                  AND created_at >= NOW() - INTERVAL '1 days'
                ORDER BY score DESC
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

    print(f"[DB] インタラクション: {len(interactions)}件, Mood履歴: {len(moods)}件")
    all_moods = [dict(r) for r in moods]
    # statusごとに1件ずつランダムサンプリング
    import random
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

# ──────────────────────────────────────────────
# Step 2: LLMで台本生成
# ──────────────────────────────────────────────

def generate_script(data: dict, comments: list[dict] = None, corner_context: dict = None) -> str:
    """DBデータから台本を生成する"""
    print(f"[LLM] 台本生成中...")

    user_prompt = build_user_prompt(data, comments=comments, corner_context=corner_context)
    extra_kwargs: dict = {}

    if USE_LOCAL_LLM:
        # Gemma(Ollama)はresponse_schema非対応のため構造化出力不可
        extra_kwargs["extra_body"] = {
            "options": {"num_ctx": int(os.getenv("LOCAL_LLM_CTX", "8192"))},
            "think": False,
        }
    else:
        # Gemini OpenAI互換エンドポイントの構造化出力はresponse_formatで渡す
        # （extra_bodyにresponse_mime_type/response_schemaを渡すと400エラーになる）
        extra_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name":   "script",
                "schema": SCRIPT_SCHEMA,
            },
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

import re

def parse_script_json(raw_script: str) -> dict:
    """Gemini構造化出力のJSONをパースする。
    戻り値: {"sections": [...], "meta": {...}}
    USE_LOCAL_LLM=True（Gemma）の場合はJSONパースを試みるが、
    失敗しても呼び出し元でエラーとして扱う（Gemmaでの構造化出力は非サポート）。
    """
    cleaned = re.sub(r"```json|```", "", raw_script).strip()
    return json.loads(cleaned)


def _rescale_va(values: list[float], target_min: float, target_max: float) -> list[float]:
    v_min, v_max = min(values), max(values)
    if v_max == v_min:
        mid = (target_min + target_max) / 2
        return [mid] * len(values)
    return [
        target_min + (v - v_min) / (v_max - v_min) * (target_max - target_min)
        for v in values
    ]


def enforce_variance(sentences: list[dict]) -> list[dict]:
    """valence/arousalを線形スケーリングして分散を強制する。LLMの第1象限偏りを補正。"""
    valences = [s.get("valence", 0.0) for s in sentences]
    arousals = [s.get("arousal", 0.0) for s in sentences]
    new_v = _rescale_va(valences, target_min=-0.3, target_max=1.0)
    new_a = _rescale_va(arousals, target_min=-0.8, target_max=1.0)
    return [
        {**s, "valence": round(v, 2), "arousal": round(a, 2)}
        for s, v, a in zip(sentences, new_v, new_a)
    ]


def build_emotion_timeline(
    sentences: list[dict],
    subtitles: list[dict],
    intro_duration: float = 0.0
) -> tuple[list[dict], float]:
    """文ごとのvalence/arousalから感情タイムラインを生成する。
    戻り値: (emotions, wave_time)
    """
    total_duration = subtitles[-1]["end"] if subtitles else 90
    total_chars = sum(len(s["text"]) for s in sentences)

    emotions = []
    char_offset = 0
    for sentence in sentences:
        ratio = char_offset / total_chars if total_chars > 0 else 0
        emotions.append({
            "time":    round(total_duration * ratio, 2),
            "valence": sentence.get("valence", 0.0),
            "arousal": sentence.get("arousal", 0.0),
        })
        char_offset += len(sentence["text"])

    wave_time = subtitles[-1]["start"] if subtitles else 0

    print(f"[感情] {len(emotions)}件のタイムライン生成完了, waveTime: {wave_time}s")
    return emotions, wave_time

# ──────────────────────────────────────────────
# Step 3: VOICEVOXで音声生成
# ──────────────────────────────────────────────

def get_wav_duration(wav_path: str) -> float:
    """WAVファイルの長さを秒で返す"""
    import wave
    with wave.open(wav_path, 'r') as f:
        return f.getnframes() / float(f.getframerate())


def _synthesize(text: str, output_path: str, extra_params: dict = None) -> None:
    """VOICEVOXでテキストを音声合成してファイルに保存する"""
    query_res = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": VOICEVOX_SPEAKER}
    )
    query_res.raise_for_status()
    query = query_res.json()
    if extra_params:
        query.update(extra_params)
    synth_res = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": VOICEVOX_SPEAKER},
        headers={"Content-Type": "application/json"},
        data=json.dumps(query)
    )
    synth_res.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(synth_res.content)


def valence_arousal_to_voicevox_params(valence: float, arousal: float) -> dict:
    """valence/arousalをVOICEVOXの音声パラメータに変換する。
    speedScaleは一定に保つ（YouTube視聴体験のため速さの変動を避ける）。
    """
    return {
        "pitchScale":      round(arousal  * 0.08, 3),   # 興奮→高め
        "intonationScale": round(1.0 + valence * 0.3, 3),  # ポジティブ→抑揚強め
        "volumeScale":     round(1.0 + arousal * 0.1, 3),  # 興奮→大きめ
    }


def generate_voice(sentences: list[dict], output_path: str, intro_text: str = "") -> None:
    """VOICEVOXで文ごとに音声合成し結合する。intro_textがある場合は冒頭一言を先頭に付ける。
    sentences: [{"text": str, "valence": float, "arousal": float}, ...]
    """
    print(f"[VOICEVOX] 文ごと音声生成中... (speaker: {VOICEVOX_SPEAKER})")
    tmp_dir = Path(tempfile.gettempdir())
    part_paths = []
    list_file = output_path + ".concat_list.txt"
    intro_wav = output_path.replace(".wav", "_intro.wav")

    try:
        if intro_text:
            _synthesize(intro_text, intro_wav, {
                "speedScale":      0.85,
                "intonationScale": 1.4,
                "volumeScale":     1.3,
                "pitchScale":      0.05,
            })
            part_paths.append(intro_wav)

        for i, sentence in enumerate(sentences):
            part_path = str(tmp_dir / f"{Path(output_path).stem}_part{i:03d}.wav")
            _synthesize(sentence["text"], part_path)
            part_paths.append(part_path)

        with open(list_file, "w") as f:
            for p in part_paths:
                f.write(f"file '{p}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_file, "-c", "copy", output_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print(f"[VOICEVOX] 音声生成完了: {output_path} ({len(sentences)}文)")

    finally:
        for p in part_paths:
            if p != intro_wav:  # intro_wavはmain()でcleanup
                if Path(p).exists():
                    Path(p).unlink()
        if Path(list_file).exists():
            Path(list_file).unlink()

# ──────────────────────────────────────────────
# Step 3.5: 字幕タイミング生成
# ──────────────────────────────────────────────

def _query_mora_times(text: str) -> tuple[list[dict], float]:
    """audio_queryからモーラタイミングリストと総尺(秒)を返す"""
    res = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": VOICEVOX_SPEAKER}
    )
    res.raise_for_status()
    query = res.json()

    t = float(query.get("prePhonemeLength", 0.1))
    mora_times = []
    for phrase in query["accent_phrases"]:
        for mora in phrase["moras"]:
            dur = (mora.get("consonant_length") or 0) + (mora.get("vowel_length") or 0)
            mora_times.append({"start": t, "duration": dur})
            t += dur
        if phrase.get("pause_mora"):
            p = phrase["pause_mora"]
            t += (p.get("consonant_length") or 0) + (p.get("vowel_length") or 0)
    total = t + float(query.get("postPhonemeLength", 0.1))
    return mora_times, total


def split_sentences(script: str) -> list[str]:
    """台本を句読点・改行で文に分割する（共通ユーティリティ）"""
    sentences = re.split(r"(?<=[。！？\n])", script)
    return [s.strip() for s in sentences if s.strip()]


def generate_subtitle_timing(script: str, time_offset: float = 0.0, actual_duration: float = None) -> list[dict]:
    """文ごとにaudio_queryを発行してタイミングを取得し、字幕データを生成する。

    文単位で独立したモーラ計測を行うことで、漢字とかなの混在による
    文字数比率ずれを排除する。
    actual_duration: 実際の音声WAVの本編部分の長さ（秒）。渡された場合はそれを
    スケーリング基準にする。各文個別合成+concatの場合はこれを使うと正確になる。
    """
    print("[字幕] タイミング情報取得中...")

    # フルスクリプトのクエリで合計尺を取得（actual_duration未指定時のフォールバック用）
    _, total_duration = _query_mora_times(script)

    # 台本を句読点・改行で文に分割
    sentences = split_sentences(script)

    # 各文のモーラタイミングと尺を取得
    sentence_data = []
    for sentence in sentences:
        mora_times, sent_dur = _query_mora_times(sentence)
        sentence_data.append((sentence, mora_times, sent_dur))

    total_sent = sum(d for _, _, d in sentence_data)
    # actual_durationが渡された場合は実際のWAV長さを基準にスケーリング
    # （各文個別合成+concatでは total_sent が実際の音声長さに近い）
    # フルスクリプトクエリは文境界の pause_mora を含むため total_duration > total_sent になり
    # スケールが1.0を超えて字幕が遅れる問題が起きるため、actual_durationで補正する
    if actual_duration is not None:
        scale = actual_duration / total_sent if total_sent > 0 else 1.0
    else:
        scale = total_duration / total_sent if total_sent > 0 else 1.0

    subtitles = []
    current_time = 0.0

    for sentence, sent_moras, sent_dur in sentence_data:
        scaled_dur = sent_dur * scale
        n_sent_moras = len(sent_moras)
        mora_scale = scaled_dur / sent_dur if sent_dur > 0 else 1.0

        # 読点・中点でさらに分割 → 15文字上限チャンク
        chunks = re.split(r"(?<=[、,，・])", sentence)
        chunks = [c for c in chunks if c.strip()]
        final_chunks = []
        for chunk in chunks:
            if len(chunk) <= 15:
                final_chunks.append(chunk)
            else:
                for i in range(0, len(chunk), 15):
                    final_chunks.append(chunk[i:i+15])

        sentence_chars = sum(len(c) for c in final_chunks)
        char_offset = 0

        for chunk in final_chunks:
            if n_sent_moras > 0 and sentence_chars > 0:
                s_idx = min(int(char_offset * n_sent_moras / sentence_chars), n_sent_moras - 1)
                e_idx = min(int((char_offset + len(chunk)) * n_sent_moras / sentence_chars), n_sent_moras - 1)
                start_t = current_time + sent_moras[s_idx]["start"] * mora_scale
                end_t   = current_time + (sent_moras[e_idx]["start"] + sent_moras[e_idx]["duration"]) * mora_scale + 0.05
            else:
                ratio_s = char_offset / max(sentence_chars, 1)
                ratio_e = (char_offset + len(chunk)) / max(sentence_chars, 1)
                start_t = current_time + ratio_s * scaled_dur
                end_t   = current_time + ratio_e * scaled_dur + 0.05
            subtitles.append({
                "start": round(start_t + time_offset, 3),
                "end":   round(end_t   + time_offset, 3),
                "text":  chunk,
            })
            char_offset += len(chunk)

        current_time += scaled_dur

    print(f"[字幕] {len(subtitles)}ブロック生成完了")
    return subtitles



def _find_subtitle_time(subtitles: list[dict], keyword: str, start_from: float = 0.0) -> float | None:
    """keyword を含む最初の字幕の start を返す。見つからなければ None。"""
    for sub in subtitles:
        if sub["start"] >= start_from and keyword in sub["text"]:
            return sub["start"]
    return None


def generate_corner_timing(
    script: str,
    subtitles: list[dict],
    intro_duration: float = 0.0,
    section_starts: dict[str, str] = None,
    has_comments: bool = False,
) -> list[dict]:
    """セクションタグで確定したタイミングでコーナーラベルを生成する"""
    corner_meta = [('OpeningAffirmation', "全肯定タイム", "#ff6b9d")]
    corner_meta.append(('BlueskyCorner', "今日のBluesky", "#0085ff"))
    if has_comments:
        corner_meta.append(('CommentCorner', "コメントコーナー", "#ff9f43"))
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

    # フォールバック: 見つからないセクションは4等分で補完
    body_duration = total_duration - intro_duration
    quarter = body_duration / 4
    corners = []
    for i, (_tag, label, color) in enumerate(corner_meta):
        start = resolved_starts[i] if resolved_starts[i] is not None else round(quarter * i + intro_duration, 3)
        next_start = next((resolved_starts[j] for j in range(i + 1, len(resolved_starts)) if resolved_starts[j] is not None), None)
        end = next_start if next_start is not None else total_duration
        corners.append({"start": round(start, 3), "end": round(end, 3), "label": label, "color": color})

    return corners

# ──────────────────────────────────────────────
# Step 4: Unityで録画
# ──────────────────────────────────────────────

def _start_xvfb() -> tuple:
    """空きディスプレイ番号でXvfbを起動し (proc, display) を返す"""
    import shutil
    if not shutil.which("Xvfb"):
        raise RuntimeError("Xvfb が見つかりません。`sudo apt install -y xvfb` でインストールしてください")
    for n in range(99, 200):
        lock = Path(f"/tmp/.X{n}-lock")
        sock = Path(f"/tmp/.X11-unix/X{n}")
        if lock.exists() or sock.exists():
            continue
        display = f":{n}"
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if sock.exists():
                print(f"[Xvfb] 起動完了: DISPLAY={display}")
                return proc, display
            time.sleep(0.2)
        proc.kill()
    raise RuntimeError("Xvfb: 空きディスプレイ番号が見つかりません (99-199)")


def record_with_unity(wav_path: str, output_webm: str, emotion_path: str) -> None:
    """Unityを起動してVRM口パク録画を行う"""
    print(f"[Unity] 録画開始...")
    # 既存のUnityプロセスを終了
    import subprocess as sp
    sp.run(["pkill", "-f", "Unity -projectPath"], capture_output=True)
    time.sleep(2)

    output_base = output_webm.replace(".webm", "")

    env = os.environ.copy()
    env.pop("XAUTHORITY", None)  # Xvfb は XAUTHORITY 不要

    xvfb_proc = None
    unity_proc = None
    try:
        xvfb_proc, display = _start_xvfb()
        env["DISPLAY"] = display

        cmd = [
            UNITY_EXE,
            "-projectPath", UNITY_PROJECT,
            "-wavFile", wav_path,
            "-outputFile", output_base,
            "-emotionFile", emotion_path,
        ]
        print(f"[Unity] コマンド: {' '.join(cmd)}")

        unity_proc = subprocess.Popen(cmd, env=env, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
        deadline = time.time() + 300
        while time.time() < deadline:
            if Path(output_webm).exists() and os.path.getsize(output_webm) > 0:
                print(f"[Unity] ファイル検出: {output_webm}")
                # ファイルサイズが安定するまで待つ
                prev_size = 0
                while True:
                    time.sleep(2)
                    current_size = os.path.getsize(output_webm)
                    print(f"[Unity] ファイルサイズ: {current_size} bytes")
                    if current_size == prev_size and current_size > 0:
                        print(f"[Unity] 書き込み完了を確認")
                        break
                    prev_size = current_size
                try:
                    os.killpg(os.getpgid(unity_proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    unity_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(unity_proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        unity_proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                break
            if unity_proc.poll() is not None:
                break
            time.sleep(2)
        else:
            try:
                os.killpg(os.getpgid(unity_proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise TimeoutError("Unity録画タイムアウト (300秒)")

        # SIGTERM(-15), SIGKILL(-9) は正常終了扱い（出力ファイル検知でkillした場合）
        if unity_proc.returncode not in (0, None, -15, -9):
            raise RuntimeError(
                f"Unity録画失敗 (returncode: {unity_proc.returncode})"
            )
        if not Path(output_webm).exists():
            raise FileNotFoundError(f"録画ファイルが見つかりません: {output_webm}")

        print(f"[Unity] 録画完了: {output_webm}")
        time.sleep(5)  # GPU メモリ解放待ち

    finally:
        if unity_proc and unity_proc.poll() is None:
            try:
                os.killpg(os.getpgid(unity_proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                unity_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(unity_proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if xvfb_proc and xvfb_proc.poll() is None:
            xvfb_proc.terminate()
            try:
                xvfb_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb_proc.kill()

# ──────────────────────────────────────────────
# Step 5: FFmpegでMP4に変換・仕上げ
# ──────────────────────────────────────────────

def finalize_video(input_webm: str, output_mp4: str,
                   subtitles: list[dict] = None,
                   corners: list[dict] = None,
                   intro_duration: float = 0.0) -> None:
    """FFmpegで縦型Shorts用MP4に変換・字幕合成する"""
    print(f"[FFmpeg] MP4変換中...")

    if intro_duration > 0 and corners is not None:
        corners = [{"start": 0, "end": intro_duration, "label": "今日の全肯定", "color": "#0085ff"}] + corners

    FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
    W, H = 1080, 1920

    # ベース変換フィルター
    vf_parts = [
        f"scale={W}:{H}:force_original_aspect_ratio=decrease",
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black"
    ]

    # 字幕フィルター追加
    if subtitles:
        for sub in subtitles:
            start = sub["start"]
            end   = sub["end"]
            text  = sub["text"].replace("'", "\\'").replace(":", "\\:")
            # 青背景ボックス + 白文字
            vf_parts.append(
                f"drawbox=x=0:y={H-420}:w={W}:h=160:color=0x0085ff@0.92:t=fill"
                f":enable='between(t,{start},{end})'"
            )
            vf_parts.append(
                f"drawtext=fontfile={FONT_PATH}:text='{text}'"
                f":fontcolor=white:fontsize=52:x=(w-text_w)/2:y={H-370}"
                f":enable='between(t,{start},{end})'"
            )

    # コーナーテロップフィルター追加
    if corners:
        for corner in corners:
            start = corner["start"]
            end   = corner["end"]
            label = corner["label"].replace("'", "\\'").replace(":", "\\:")
            color = corner["color"].replace("#", "0x")
            box_w = min(len(corner["label"]) * 38 + 40, W - 40)
            vf_parts.append(
                f"drawbox=x=20:y=40:w={box_w}:h=70:color=white@0.9:t=fill"
                f":enable='between(t,{start},{end})'"
            )
            vf_parts.append(
                f"drawbox=x=20:y=108:w={box_w}:h=6:color={color}@1.0:t=fill"
                f":enable='between(t,{start},{end})'"
            )
            vf_parts.append(
                f"drawtext=fontfile={FONT_PATH}:text='{label}'"
                f":fontcolor={color}:fontsize=36:x=30:y=52"
                f":enable='between(t,{start},{end})'"
            )
    vf = ",".join(vf_parts)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_webm,
    ]

    if BGM_PATH and Path(BGM_PATH).exists():
        cmd += ["-i", BGM_PATH,
                "-filter_complex",
                f"[0:v]{vf}[v];[1:a]volume=0.05[bgm];[0:a][bgm]amix=inputs=2:duration=first[a]",
                "-map", "[v]", "-map", "[a]"]
    else:
        cmd += ["-vf", vf]

    cmd += [
        # "-c:v", "h264_nvenc", #NOTE: UnityスクショとGPUエンコードは両立不可
        "-c:v", "libx264",
        "-preset", "fast",
        "-c:a", "aac",
        "-shortest",
        output_mp4
    ]

    subprocess.run(cmd, check=True, timeout=120)
    print(f"[FFmpeg] 変換完了: {output_mp4}")

# ──────────────────────────────────────────────
# Step 6: YouTubeにアップロード
# ──────────────────────────────────────────────

from youtube import (
    fetch_youtube_comments,
    upload_to_youtube,
    fetch_recent_corners,
    get_recent_video_stats,
    should_enable_comment_corner,
    fetch_bluesky_corner_context,
    save_youtube_upload_to_db,
    notify_discord,
)

def _timed(label, fn, *args):
    import time
    print(f"[{label}] 開始...")
    start = time.time()
    result = fn(*args)
    print(f"[{label}] 完了 ({time.time()-start:.1f}s)")
    return result

def _retry(label: str, fn, *args, attempts: int = 3, catch=(Exception,)):
    """最大 attempts 回リトライする共通ヘルパー"""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args)
        except catch as e:
            if attempt == attempts:
                raise
            print(f"[{label}] 試行{attempt}失敗、リトライします... ({e})")

def _llm_create(attempts_per_model: int = 3, **kwargs):
    """LLM_MODELS を左から順に attempts_per_model 回ずつ試す。
    400/422 などリクエスト不正エラーは即時再送しても意味がないので1回で失敗させる。
    """
    last_exc = None
    for model in LLM_MODELS:
        for attempt in range(1, attempts_per_model + 1):
            try:
                return llm_client.chat.completions.create(model=model, **kwargs)
            except _OpenAIBadRequestError as e:
                # 400系はリトライ不要（プロンプトやスキーマの問題）
                raise
            except Exception as e:
                last_exc = e
                if attempt < attempts_per_model:
                    print(f"[LLM] {model} 試行{attempt}失敗、リトライします... ({e})")
                else:
                    print(f"[LLM] {model} {attempts_per_model}回失敗、次のモデルへ移行します ({e})")
    raise last_exc

def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = Path(tempfile.gettempdir())

    wav_path       = str(tmp_dir / f"bottan_{ts}.wav")
    intro_wav_path = wav_path.replace(".wav", "_intro.wav")
    webm_path      = str(tmp_dir / f"bottan_{ts}.webm")
    mp4_path    = str(tmp_dir / f"bottan_{ts}.mp4")
    screenshot_path = str(tmp_dir / f"bottan_{ts}_thumbnail.png")

    date_label  = datetime.now().strftime("%Y/%m/%d")
    title       = f"botたんの今週のひとこと {date_label}"
    description = "全肯定botたんが今週Blueskyで感じたことをお話しします。\n#botたん #全肯定 #Bluesky"

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

        # corner_context取得（直近status除外・Bluesky参考/除外リスト）
        corner_context = {}
        try:
            recent_corners = fetch_recent_corners(limit=2)
            bsky_context = fetch_bluesky_corner_context()
            corner_context = {**recent_corners, **bsky_context}
        except Exception as e:
            print(f"[corner_context] 取得失敗（スキップ）: {e}")

        # Step 2: 台本生成
        script_cache = os.getenv("SCRIPT_CACHE", "")
        if script_cache and Path(script_cache).exists():
            raw_script = open(script_cache).read()
            print(f"[LLM] キャッシュから台本読み込み: {script_cache}")
        else:
            raw_script = _timed("Step2 台本生成", generate_script, data, comments, corner_context)

        # Step 2.5: JSONパース、クリーン台本作成
        script_data = parse_script_json(raw_script)
        sections = {s["section"]: s["sentences"] for s in script_data["sections"]}
        script_meta = script_data["meta"]
        print(f"[META] first_greeting_status={script_meta.get('first_greeting_status')}, "
              f"bluesky_themes={script_meta.get('bluesky_themes')}")
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

        # Step 3: 音声生成
        _timed("Step3 音声生成", generate_voice, main_sentences, wav_path, thumbnail_text)

        # 冒頭一言の音声時間を取得
        intro_duration = get_wav_duration(intro_wav_path) if thumbnail_text and Path(intro_wav_path).exists() else 0.0

        # 本編音声の実際の長さを取得（字幕タイミングのスケーリング基準に使用）
        # 各文個別合成+concatの場合はtotal_sentが実際の音声長さに近い（フルスクリプト
        # 1回クエリより正確）ため、実際のWAV長さを渡すことで字幕ずれを防ぐ
        actual_main_duration = get_wav_duration(wav_path) - intro_duration if Path(wav_path).exists() else None
        print(f"[字幕] 本編音声長さ: {actual_main_duration:.3f}s" if actual_main_duration else "[字幕] 本編音声長さ取得失敗、フォールバック使用")

        # Step 3.5: 字幕・コーナータイミング生成
        subtitles = generate_subtitle_timing(clean_script, time_offset=intro_duration, actual_duration=actual_main_duration)
        if intro_duration > 0:
            subtitles = [{"start": 0.0, "end": round(intro_duration, 3), "text": thumbnail_text}] + subtitles
        corners   = generate_corner_timing(clean_script, subtitles, intro_duration, section_starts, has_comments)

        # 感情タイムライン生成
        emotions, wave_time = build_emotion_timeline(main_sentences, subtitles, intro_duration)

        # トリガー発火時刻を字幕から算出
        # DoGreeting①: OpeningAffirmationセクション終了2秒前（フレーズに依存しないよう cornersベースで計算）
        opening_end = corners[0]["end"] if corners else 0.0
        greeting_time1 = max(0.0, opening_end - 2.0)
        # DoThankful: 締めセクション内に限定（corners末尾がClosing）
        closing_start = corners[-1]["start"] if corners else 0.0
        thankful_time  = _find_subtitle_time(subtitles, "高評価",   start_from=closing_start) or 0.0
        print(f"[トリガー] greetingTime1: {greeting_time1}s, waveTime: {wave_time}s, thankfulTime: {thankful_time}s")

        # 感情JSONファイル保存
        emotion_path = str(tmp_dir / f"bottan_{ts}_emotions.json")
        with open(emotion_path, "w") as f:
            json.dump({
                "emotions":      emotions,
                "waveTime":      wave_time,
                "thankfulTime":  thankful_time,
                "greetingTime1": greeting_time1,
            }, f, ensure_ascii=False)
        print(f"[感情] 保存: {emotion_path}")

        # Step 4: Unity録画（Mono GC 競合による確率的クラッシュへの対策でリトライあり）
        _retry("Step4 Unity録画", record_with_unity, wav_path, webm_path, emotion_path,
               catch=(RuntimeError, TimeoutError))

        # サムネ作成
        thumbnail_path = str(tmp_dir / f"bottan_{ts}_thumbnail.png")
        capture_thumbnail_frame(webm_path, screenshot_path, emotions)
        generate_thumbnail(screenshot_path, thumbnail_path, thumbnail_text)

        # Step 5: MP4変換
        _timed("Step5 MP4変換", finalize_video, webm_path, mp4_path, subtitles, corners, intro_duration)

        # Step 6: YouTubeアップロード
        title = build_title(thumbnail_text)
        description = build_description()
        if os.getenv("SKIP_YOUTUBE") != "true":
            yt_url = _timed("Step6 YT投稿", upload_to_youtube, mp4_path, title, description, thumbnail_path)
            if yt_url and os.getenv("YOUTUBE_PRIVACY", "public") == "public":
                corners_metadata = [
                    {"corner_name": "Thumbnail", "theme": thumbnail_text},
                    {"corner_name": "Closing", "status": script_meta.get("first_greeting_status", "")},
                ]
                bluesky_themes = script_meta.get("bluesky_themes", [])
                if isinstance(bluesky_themes, list) and bluesky_themes:
                    corners_metadata.append({"corner_name": "BlueskyCorner", "theme": bluesky_themes})
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
