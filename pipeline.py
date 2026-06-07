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
from description import build_description

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
    print(f"[LLM] Ollama ({LLM_MODEL}) を使用します")
else:
    llm_client = OpenAI(
        api_key=os.getenv("GEMINI_API_KEY", ""),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    LLM_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    print(f"[LLM] Gemini ({LLM_MODEL}) を使用します")

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

def fetch_weekly_data() -> dict:
    """ラズパイDBから今週のインタラクションとMood履歴を取得する"""
    print("[DB] ラズパイDBに接続中...")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            # 今週のAIリプライ（スコアあり・ランダム3件）
            cur.execute("""
                SELECT
                    did,
                    details->>'text'         AS post_text,
                    (details->>'score')::int AS score,
                    created_at
                FROM affirmative_bot.interaction
                WHERE type = 'NormalReply'
                  AND (details->>'score')::int >= 88
                  AND created_at >= NOW() - INTERVAL '7 days'
                ORDER BY score DESC
            """)
            interactions = cur.fetchall()

            # 今週エネルギーが最も高かった時のmood（1件）
            cur.execute("""
                SELECT
                    status,
                    mood,
                    mood_en,
                    energy,
                    created_at
                FROM affirmative_bot.biorhythm_history
                WHERE created_at >= NOW() - INTERVAL '7 days'
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

def generate_script(data: dict) -> str:
    """DBデータから台本を生成する"""
    print(f"[LLM] 台本生成中...")

    if USE_LOCAL_LLM:
        # ローカルLLMはコンテキストが小さいため、thinkingを無効化
        user_prompt = build_user_prompt(data)
        extra = {"options": {"num_ctx": int(os.getenv("LOCAL_LLM_CTX", "8192"))},
                 "think": False}
    else:
        user_prompt = build_user_prompt(data)
        extra = {}

    response = llm_client.chat.completions.create(
        model=LLM_MODEL,
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

# ──────────────────────────────────────────────
# Step 3: VOICEVOXで音声生成
# ──────────────────────────────────────────────

def generate_voice(script: str, output_path: str) -> None:
    """VOICEVOXで音声ファイルを生成する"""
    print(f"[VOICEVOX] 音声生成中... (speaker: {VOICEVOX_SPEAKER})")

    # audio_query
    query_res = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": script, "speaker": VOICEVOX_SPEAKER}
    )
    query_res.raise_for_status()
    query = query_res.json()

    # synthesis
    synth_res = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": VOICEVOX_SPEAKER},
        headers={"Content-Type": "application/json"},
        data=json.dumps(query)
    )
    synth_res.raise_for_status()

    with open(output_path, "wb") as f:
        f.write(synth_res.content)

    print(f"[VOICEVOX] 音声生成完了: {output_path}")

# ──────────────────────────────────────────────
# Step 3.5: 字幕タイミング生成
# ──────────────────────────────────────────────

def generate_subtitle_timing(script: str) -> list[dict]:
    """VOICEVOXのaudio_queryからタイミングを取得し、元のテキストで字幕データを生成する"""
    print("[字幕] タイミング情報取得中...")

    query_res = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": script, "speaker": VOICEVOX_SPEAKER}
    )
    query_res.raise_for_status()
    query = query_res.json()

    # モーラの総時間を積算
    current_time = float(query.get("prePhonemeLength", 0.1))
    mora_times = []
    for phrase in query["accent_phrases"]:
        for mora in phrase["moras"]:
            duration = (mora.get("consonant_length") or 0) + (mora.get("vowel_length") or 0)
            mora_times.append({"start": current_time, "duration": duration})
            current_time += duration
        if phrase.get("pause_mora"):
            pause = phrase["pause_mora"]
            duration = (pause.get("consonant_length") or 0) + (pause.get("vowel_length") or 0)
            current_time += duration

    total_duration = current_time + float(query.get("postPhonemeLength", 0.1))

    # 台本を句読点・改行で文に分割
    import re
    sentences = re.split(r"(?<=[。！？\n])", script)
    sentences = [s.strip() for s in sentences if s.strip()]

    # 各文にタイミングを割り当て（文字数比率で按分）
    total_chars = sum(len(s) for s in sentences)
    subtitles = []
    char_offset = 0
    for sentence in sentences:
        # 読点・中点でさらに分割
        chunks = re.split(r"(?<=[、,，・])", sentence)
        chunks = [c for c in chunks if c.strip()]
        # 分割後も15文字超えるものはさらに分割
        final_chunks = []
        for chunk in chunks:
            if len(chunk) <= 18:
                final_chunks.append(chunk)
            else:
                for i in range(0, len(chunk), 18):
                    final_chunks.append(chunk[i:i+18])

        for chunk in final_chunks:
            chunk_ratio_start = char_offset / total_chars
            chunk_ratio_end   = (char_offset + len(chunk)) / total_chars
            subtitles.append({
                "start": round(total_duration * chunk_ratio_start, 3),
                "end":   round(total_duration * chunk_ratio_end + 0.05, 3),
                "text":  chunk
            })
            char_offset += len(chunk)

    print(f"[字幕] {len(subtitles)}ブロック生成完了")
    return subtitles


def generate_corner_timing(script: str, subtitles: list[dict]) -> list[dict]:
    """台本からコーナー名と表示タイミングを推定する"""
    corners = []
    total_duration = subtitles[-1]["end"] if subtitles else 90

    # 台本を行に分割してセクションを推定
    lines = [l.strip() for l in script.split("\n") if l.strip()]
    section_keywords = {
        "挨拶": ("やっほー！botたんだよ", "#0085ff"),
        "全肯定コーナー": ("こんなとこにも全肯定コーナー", "#ff6b9d"),
        "Bluesky": ("今週のBluesky", "#0085ff"),
        "締め": ("全肯定メッセージ", "#7ec8e3"),
    }

    # 簡易的にdurationを4等分して各コーナーを割り当て
    quarter = total_duration / 4
    corner_list = list(section_keywords.values())
    for idx, (label, color) in enumerate(corner_list):
        corners.append({
            "start": round(quarter * idx, 3),
            "end":   round(quarter * (idx + 1), 3),
            "label": label,
            "color": color,
        })

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


def record_with_unity(wav_path: str, output_webm: str) -> None:
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

    finally:
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
                   corners: list[dict] = None) -> None:
    """FFmpegで縦型Shorts用MP4に変換・字幕合成する"""
    print(f"[FFmpeg] MP4変換中...")

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
                f"drawbox=x=0:y={H-180}:w={W}:h=160:color=0x0085ff@0.92:t=fill"
                f":enable='between(t,{start},{end})'"
            )
            vf_parts.append(
                f"drawtext=fontfile={FONT_PATH}:text='{text}'"
                f":fontcolor=white:fontsize=52:x=(w-text_w)/2:y={H-130}"
                f":enable='between(t,{start},{end})'"
            )

    # コーナーテロップフィルター追加
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
                f"[0:v]{vf}[v];[1:a]volume=0.2[bgm];[0:a][bgm]amix=inputs=2:duration=first[a]",
                "-map", "[v]", "-map", "[a]"]
    else:
        cmd += ["-vf", vf]

    cmd += [
        "-c:v", "h264_nvenc",
        "-c:a", "aac",
        "-shortest",
        output_mp4
    ]

    subprocess.run(cmd, check=True, timeout=120)
    print(f"[FFmpeg] 変換完了: {output_mp4}")

# ──────────────────────────────────────────────
# Step 6: YouTubeにアップロード
# ──────────────────────────────────────────────

def upload_to_youtube(mp4_path: str, title: str, description: str) -> None:
    """YouTube Data API v3で動画をアップロードする"""
    print(f"[YouTube] アップロード中: {title}")
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        import pickle

        SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
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
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRETS), SCOPES
            )
            flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
            auth_url, _ = flow.authorization_url(prompt="consent")
            print(f"\n以下のURLをブラウザで開いてください:\n{auth_url}\n")
            code = input("認証後に表示されたコードを入力してください: ")
            flow.fetch_token(code=code)
            creds = flow.credentials
            with open(TOKEN_PATH, "wb") as f:
                pickle.dump(creds, f)

        youtube = build("youtube", "v3", credentials=creds)

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": ["botたん", "全肯定", "Bluesky", "VTuber"],
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "public",
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

        print(f"[YouTube] アップロード完了: https://youtube.com/watch?v={response['id']}")

    except ImportError:
        print("[YouTube] google-api-python-client未インストール。スキップします。")
        print("pip install google-api-python-client google-auth-oauthlib")

def _timed(label, fn, *args):
    import time
    print(f"[{label}] 開始...")
    start = time.time()
    result = fn(*args)
    print(f"[{label}] 完了 ({time.time()-start:.1f}s)")
    return result

def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = Path(tempfile.gettempdir())

    wav_path    = str(tmp_dir / f"bottan_{ts}.wav")
    webm_path   = str(tmp_dir / f"bottan_{ts}.webm")
    mp4_path    = str(tmp_dir / f"bottan_{ts}.mp4")

    date_label  = datetime.now().strftime("%Y/%m/%d")
    title       = f"botたんの今週のひとこと {date_label}"
    description = "全肯定botたんが今週Blueskyで感じたことをお話しします。\n#botたん #全肯定 #Bluesky"

    total_start = time.time()
    try:
        # Step 1: DBからデータ取得
        data = _timed("Step1 DB取得", fetch_weekly_data)
        if not data["interactions"] and not data["moods"]:
            print("[ERROR] データが取得できませんでした")
            return

        # Step 2: 台本生成
        script = _timed("Step2 台本生成", generate_script, data)

        # Step 3: 音声生成
        _timed("Step3 音声生成", generate_voice, script, wav_path)

        # Step 3.5: 字幕タイミング生成
        subtitles = generate_subtitle_timing(script)
        corners   = generate_corner_timing(script, subtitles)

        # Step 4: Unity録画（Mono GC 競合による確率的クラッシュへの対策でリトライあり）
        for attempt in range(1, 4):
            try:
                _timed(f"Step4 Unity録画 (試行{attempt})", record_with_unity, wav_path, webm_path)
                break
            except (RuntimeError, TimeoutError) as e:
                if attempt == 3:
                    raise
                print(f"[Unity] 試行{attempt}失敗、リトライします... ({e})")

        # Step 5: MP4変換
        _timed("Step5 MP4変換", finalize_video, webm_path, mp4_path, subtitles, corners)

        # Step 6: YouTubeアップロード
        description = build_description()
        _timed("Step6 YT投稿", upload_to_youtube, mp4_path, title, description)

        elapsed = time.time() - total_start
        print(f"\n✅ パイプライン完了: {mp4_path}  (合計: {elapsed:.1f}秒)")

    except Exception as e:
        print(f"\n❌ エラー発生: {e}")
        raise

    finally:
        #pass
        # 一時ファイル削除
        for path in [wav_path, webm_path]:
            if Path(path).exists():
                Path(path).unlink()
                print(f"[Cleanup] 削除: {path}")


if __name__ == "__main__":
    main()
