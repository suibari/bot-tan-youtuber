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
import subprocess
import tempfile
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from atproto import Client
from openai import OpenAI

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

def generate_script(data: dict, comments: list[dict] = None) -> str:
    """DBデータから台本を生成する"""
    print(f"[LLM] 台本生成中...")

    if USE_LOCAL_LLM:
        # ローカルLLMはコンテキストが小さいため、thinkingを無効化
        user_prompt = build_user_prompt(data, comments=comments)
        extra = {"options": {"num_ctx": int(os.getenv("LOCAL_LLM_CTX", "8192"))},
                 "think": False}
    else:
        user_prompt = build_user_prompt(data, comments=comments)
        extra = {}

    response = _llm_create(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        #max_tokens=4000,
        #temperature=0.5,
        extra_body=extra if extra else None,
    )
    print(f"[DEBUG] finish_reason: {response.choices[0].finish_reason}")

    script = response.choices[0].message.content.strip()
    print(f"[LLM] 台本生成完了:\n{script}\n")
    return script

import re

def extract_emotions_from_script(script: str) -> tuple[str, list[tuple[str, str]]]:
    """台本からタグを抽出し、クリーン台本と(emotion, text)リストを返す"""
    pattern = re.compile(r'\[(Happy|Sad|Angry|Surprised|Relaxed)\]([^\[]+)', re.DOTALL)
    matches = pattern.findall(script)

    clean_script = re.sub(r'\[(Happy|Sad|Angry|Surprised|Relaxed)\]', '', script)
    clean_script = re.sub(r'\[[^\]]*\]', '', clean_script).strip()  # [Study][SelfAffirmationCorner]等の残留タグを除去
    return clean_script, matches  # タイミングはまだ計算しない


def build_emotion_timeline(
    matches: list[tuple[str, str]],
    subtitles: list[dict],
    intro_duration: float = 0.0
) -> tuple[list[dict], float]:
    """字幕タイミング確定後に感情タイムラインを生成する
    戻り値: (emotions, wave_time)
    """
    total_duration = subtitles[-1]["end"] if subtitles else 90
    total_chars = sum(len(text.strip()) for _, text in matches)

    emotions = []
    char_offset = 0
    for emotion, text in matches:
        ratio = char_offset / total_chars if total_chars > 0 else 0
        emotions.append({
            "time": round(total_duration * ratio, 2),
            "emotion": emotion
        })
        char_offset += len(text.strip())

    wave_time = subtitles[-1]["start"] if subtitles else 0

    print(f"[感情] {len(emotions)}件のタイムライン生成完了, waveTime: {wave_time}s")
    return emotions, wave_time

def extract_thumbnail_text(raw_script: str) -> tuple[str, str, bool]:
    """台本からサムネイル一言を抽出する。戻り値: (script, thumbnail_text, in_script)"""
    if "---THUMBNAIL---" in raw_script:
        parts = raw_script.split("---THUMBNAIL---", 1)
        script = parts[0].strip()
        thumbnail_text = parts[1].strip()
        print(f"[サムネイル] 一言: {thumbnail_text}")
        return script, thumbnail_text, True
    else:
        print("[サムネイル] 一言なし、デフォルト使用")
        return raw_script, "今日も全肯定だよ！", False

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


def generate_voice(script: str, output_path: str, intro_text: str = "") -> None:
    """VOICEVOXで音声ファイルを生成する。intro_text がある場合は冒頭一言を結合する"""
    print(f"[VOICEVOX] 音声生成中... (speaker: {VOICEVOX_SPEAKER})")

    if intro_text:
        intro_wav = output_path.replace(".wav", "_intro.wav")
        main_tmp  = output_path + ".main_tmp.wav"
        list_file = output_path + ".concat_list.txt"
        try:
            _synthesize(intro_text, intro_wav, {
                "speedScale":      0.85,
                "intonationScale": 1.4,
                "volumeScale":     1.3,
                "pitchScale":      0.05,
            })
            _synthesize(script, main_tmp)
            with open(list_file, "w") as f:
                f.write(f"file '{intro_wav}'\nfile '{main_tmp}'\n")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", list_file, "-c", "copy", output_path],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print(f"[VOICEVOX] 冒頭+本編音声生成完了: {output_path}")
            return
        except Exception as e:
            print(f"[VOICEVOX] 冒頭一言生成失敗、本編のみで続行: {e}")
        finally:
            for p in [main_tmp, list_file]:
                if Path(p).exists():
                    Path(p).unlink()

    # intro_text なし or 冒頭生成失敗時は本編のみ
    _synthesize(script, output_path)
    print(f"[VOICEVOX] 音声生成完了: {output_path}")

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


def generate_subtitle_timing(script: str, time_offset: float = 0.0) -> list[dict]:
    """文ごとにaudio_queryを発行してタイミングを取得し、字幕データを生成する。

    文単位で独立したモーラ計測を行うことで、漢字とかなの混在による
    文字数比率ずれを排除する。
    """
    import re
    print("[字幕] タイミング情報取得中...")

    # フルスクリプトのクエリで合計尺を取得（スケーリング基準）
    _, total_duration = _query_mora_times(script)

    # 台本を句読点・改行で文に分割
    sentences = re.split(r"(?<=[。！？\n])", script)
    sentences = [s.strip() for s in sentences if s.strip()]

    # 各文のモーラタイミングと尺を取得
    sentence_data = []
    for sentence in sentences:
        mora_times, sent_dur = _query_mora_times(sentence)
        sentence_data.append((sentence, mora_times, sent_dur))

    # 文ごとの尺の合計をフルスクリプトの合計尺にスケーリング
    # （文ごとクエリはprePhoneme/postPhonemeが各文に付くため合計が実際より長くなる）
    total_sent = sum(d for _, _, d in sentence_data)
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


SECTION_TAGS = ['Thumbnail', 'FirstGreeting', 'CommentCorner', 'SelfAffirmationCorner', 'BlueskyCorner', 'Closing']


def extract_section_starts(raw_script: str) -> dict[str, str]:
    """セクションタグ直後の発話テキスト先頭8文字を返す（字幕検索キーワード用）"""
    result = {}
    tags_pattern = '|'.join(SECTION_TAGS)
    section_re = re.compile(
        r'\[(' + tags_pattern + r')\](.*?)(?=\[(?:' + tags_pattern + r')\]|---THUMBNAIL---|$)',
        re.DOTALL
    )
    for m in section_re.finditer(raw_script):
        tag = m.group(1)
        text = re.sub(r'\[[^\]]*\]', '', m.group(2)).strip()
        if text:
            result[tag] = text[:8]
    return result


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
    corner_meta = [('FirstGreeting', "やっほー！botたんだよ", "#0085ff")]
    if has_comments:
        corner_meta.append(('CommentCorner', "コメントコーナー", "#ff9f43"))
    corner_meta.append(('SelfAffirmationCorner', "こんなとこにも全肯定コーナー", "#ff6b9d"))
    if not has_comments:
        corner_meta.append(('BlueskyCorner', "今日のBluesky", "#0085ff"))
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

        unity_proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE)
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
                unity_proc.terminate()
                break
            if unity_proc.poll() is not None:
                break
            time.sleep(2)
        else:
            unity_proc.kill()
            raise TimeoutError("Unity録画タイムアウト (300秒)")

        # SIGTERM(-15) は正常終了扱い（出力ファイル検知でterminateした場合）
        if unity_proc.returncode not in (0, None, -15):
            stderr_out = unity_proc.stderr.read().decode("utf-8", errors="replace") if unity_proc.stderr else ""
            raise RuntimeError(
                f"Unity録画失敗 (returncode: {unity_proc.returncode})\n[Unity stderr]\n{stderr_out}"
            )
        if not Path(output_webm).exists():
            raise FileNotFoundError(f"録画ファイルが見つかりません: {output_webm}")

        print(f"[Unity] 録画完了: {output_webm}")
        time.sleep(5)  # GPU メモリ解放待ち

    finally:
        if unity_proc and unity_proc.poll() is None:
            unity_proc.terminate()
            try:
                unity_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                unity_proc.kill()
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

def _get_youtube_client():
    """YouTube API クライアントを返す（OAuth2認証）。失敗時は None。"""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        import pickle

        SCOPES = [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube",
            "https://www.googleapis.com/auth/youtube.force-ssl"
        ]
        TOKEN_PATH = Path.home() / ".bottan_youtube_token.pickle"
        CLIENT_SECRETS = Path(os.getenv("YOUTUBE_CLIENT_SECRETS", str(Path.home() / ".bottan_youtube_client_secrets.json")))

        creds = None
        if TOKEN_PATH.exists():
            with open(TOKEN_PATH, "rb") as f:
                creds = pickle.load(f)

        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            with open(TOKEN_PATH, "wb") as f:
                pickle.dump(creds, f)
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
            flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
            auth_url, _ = flow.authorization_url(prompt="consent")
            print(f"\n以下のURLをブラウザで開いてください:\n{auth_url}\n")
            code = input("認証後に表示されたコードを入力してください: ")
            flow.fetch_token(code=code)
            creds = flow.credentials
            with open(TOKEN_PATH, "wb") as f:
                pickle.dump(creds, f)

        return build("youtube", "v3", credentials=creds)

    except ImportError:
        print("[YouTube] google-api-python-client未インストール。")
        return None
    except Exception as e:
        print(f"[YouTube] 認証失敗: {e}")
        return None


def fetch_youtube_comments() -> list[dict]:
    """前日のYouTube動画へのコメントをランダム3件取得する。取得失敗時は []。"""
    try:
        import random
        from urllib.parse import urlparse, parse_qs

        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT url FROM affirmative_bot.youtube_shorts ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        conn.close()

        if not row:
            print("[コメント] 前日動画なし → CommentCornerスキップ")
            return []

        video_id = parse_qs(urlparse(row["url"]).query).get("v", [None])[0]
        if not video_id:
            print("[コメント] video_id抽出失敗")
            return []

        youtube = _get_youtube_client()
        if youtube is None:
            return []

        result = youtube.commentThreads().list(
            part="snippet",
            videoId=video_id,
            maxResults=50,
            order="relevance",
            textFormat="plainText",
        ).execute()

        comments = [
            {
                "text":   item["snippet"]["topLevelComment"]["snippet"]["textDisplay"].strip(),
                "author": item["snippet"]["topLevelComment"]["snippet"]["authorDisplayName"],
            }
            for item in result.get("items", [])
            if item["snippet"]["topLevelComment"]["snippet"]["textDisplay"].strip()
        ]

        if not comments:
            print("[コメント] コメントなし → CommentCornerスキップ")
            return []

        selected = random.sample(comments, min(3, len(comments)))
        print(f"[コメント] {len(selected)}件取得 (候補{len(comments)}件中)")
        return selected

    except Exception as e:
        print(f"[コメント] 取得失敗: {e} → CommentCornerスキップ")
        return []


def upload_to_youtube(mp4_path: str, title: str, description: str, thumbnail_path: str = "") -> None:
    """YouTube Data API v3で動画をアップロードする"""
    print(f"[YouTube] アップロード中: {title}")
    youtube = _get_youtube_client()
    if youtube is None:
        print("[YouTube] クライアント取得失敗。スキップします。")
        return None

    try:
        from googleapiclient.http import MediaFileUpload

        privacy = os.getenv("YOUTUBE_PRIVACY", "public")
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": ["botたん", "全肯定", "Bluesky", "VTuber"],
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            }
        }

        media = MediaFileUpload(mp4_path, mimetype="video/mp4", resumable=True)
        request = youtube.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=media
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"[YouTube] アップロード進捗: {int(status.progress() * 100)}%")

        video_id = response['id']

        if thumbnail_path and Path(thumbnail_path).exists():
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype="image/png")
            ).execute()
            print(f"[YouTube] サムネイル設定完了")

        url = f"https://youtube.com/watch?v={video_id}"
        print(f"[YouTube] アップロード完了 ({privacy}): {url}")
        return url

    except ImportError:
        print("[YouTube] google-api-python-client未インストール。スキップします。")
        print("pip install google-api-python-client google-auth-oauthlib")

def save_youtube_upload_to_db(url: str, title: str) -> None:
    """YouTube投稿情報をDBのyoutube_shortsテーブルに記録する"""
    print(f"[DB] YouTube投稿情報を記録中: {url}")
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO affirmative_bot.youtube_shorts (url, title, status)
                VALUES (%s, %s, 'new')
                ON CONFLICT (url) DO NOTHING
                """,
                (url, title),
            )
        conn.commit()
        print("[DB] youtube_shorts に記録完了")
    finally:
        conn.close()

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
    """LLM_MODELS を左から順に attempts_per_model 回ずつ試す"""
    last_exc = None
    for model in LLM_MODELS:
        for attempt in range(1, attempts_per_model + 1):
            try:
                return llm_client.chat.completions.create(model=model, **kwargs)
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

        # Step 1.5: 前日動画のコメント取得（CommentCornerの有無を決定）
        comments = fetch_youtube_comments()
        has_comments = bool(comments)
        print(f"[コメント] CommentCorner: {'あり (' + str(len(comments)) + '件)' if has_comments else 'なし → スキップ'}")

        # Step 2: 台本生成
        script_cache = os.getenv("SCRIPT_CACHE", "")
        if script_cache and Path(script_cache).exists():
            raw_script = open(script_cache).read()
            print(f"[LLM] キャッシュから台本読み込み: {script_cache}")
        else:
            raw_script = _timed("Step2 台本生成", generate_script, data, comments)

        # Step 2.5: タグ抽出、クリーン台本作成
        raw_script, thumbnail_text, thumbnail_in_script = extract_thumbnail_text(raw_script)
        section_starts = extract_section_starts(raw_script)  # セクションタグ除去前に抽出
        print(f"[セクション] 検出: {section_starts}")
        clean_script, emotion_matches = extract_emotions_from_script(raw_script)

        # 冒頭一言（thumbnail_text）をclean_scriptの先頭から除去してmain_scriptを作る
        # ---THUMBNAIL--- がなく fallback テキストの場合はスキップ（誤削除防止）
        main_script = clean_script
        if thumbnail_in_script and thumbnail_text:
            stripped = clean_script.strip()
            if stripped.startswith(thumbnail_text):
                main_script = stripped[len(thumbnail_text):].strip()
            else:
                m = re.match(r'^[^。！？\n]+[。！？\n]?', stripped)
                if m:
                    main_script = stripped[m.end():].strip()

        # Step 3: 音声生成
        _timed("Step3 音声生成", generate_voice, main_script, wav_path, thumbnail_text)

        # 冒頭一言の音声時間を取得
        intro_duration = get_wav_duration(intro_wav_path) if thumbnail_text and Path(intro_wav_path).exists() else 0.0

        # Step 3.5: 字幕・コーナータイミング生成
        subtitles = generate_subtitle_timing(main_script, time_offset=intro_duration)
        if intro_duration > 0:
            subtitles = [{"start": 0.0, "end": round(intro_duration, 3), "text": thumbnail_text}] + subtitles
        corners   = generate_corner_timing(main_script, subtitles, intro_duration, section_starts, has_comments)

        # 感情タイムライン生成
        emotions, wave_time = build_emotion_timeline(emotion_matches, subtitles, intro_duration)

        # トリガー発火時刻を字幕から算出
        # DoGreeting①: ②挨拶末尾の「botたんだよ」発話タイミング（セクション先頭ではなく末尾）
        greeting_time1 = max(0.0, (_find_subtitle_time(subtitles, "botたんだよ") or 0.0) - 2.0)
        # DoGreeting②・DoThankful: ⑤/⑥締めセクション内に限定（corners末尾がClosing）
        closing_start = corners[-1]["start"] if corners else 0.0
        greeting_time2 = _find_subtitle_time(subtitles, "フォロー", start_from=closing_start) or 0.0
        thankful_time  = _find_subtitle_time(subtitles, "高評価",   start_from=closing_start) or 0.0
        print(f"[トリガー] greetingTime1: {greeting_time1}s, greetingTime2: {greeting_time2}s, thankfulTime: {thankful_time}s")

        # 感情JSONファイル保存
        emotion_path = str(tmp_dir / f"bottan_{ts}_emotions.json")
        with open(emotion_path, "w") as f:
            json.dump({
                "emotions":      emotions,
                "waveTime":      wave_time,
                "thankfulTime":  thankful_time,
                "greetingTime1": greeting_time1,
                "greetingTime2": greeting_time2,
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
        title = build_title()
        description = build_description()
        if os.getenv("SKIP_YOUTUBE") != "true":
            yt_url = _timed("Step6 YT投稿", upload_to_youtube, mp4_path, title, description, thumbnail_path)
            if yt_url and os.getenv("YOUTUBE_PRIVACY", "public") == "public":
                save_youtube_upload_to_db(yt_url, title)
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
