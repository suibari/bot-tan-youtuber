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

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────

BLUESKY_HANDLE   = os.getenv("BLUESKY_HANDLE", "bot-tan.bsky.social")
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
    LLM_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
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
                  AND details->>'score' IS NOT NULL
                  AND created_at >= date_trunc('week', CURRENT_DATE)
                ORDER BY RANDOM()
                LIMIT 3
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
                WHERE created_at >= date_trunc('week', CURRENT_DATE)
                ORDER BY energy DESC
                LIMIT 1
            """)
            moods = cur.fetchall()

    finally:
        conn.close()

    print(f"[DB] インタラクション: {len(interactions)}件, Mood履歴: {len(moods)}件")
    return {
        "interactions": [dict(r) for r in interactions],
        "moods":        [dict(r) for r in moods],
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
        max_tokens=4000,
        temperature=0.5,
        extra_body=extra if extra else None,
    )
    print(f"[DEBUG] response: {response}")

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
            if Path(output_webm).exists():
                unity_proc.terminate()
                try:
                    unity_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    unity_proc.kill()
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

def finalize_video(input_webm: str, output_mp4: str) -> None:
    """FFmpegで縦型Shorts用MP4に変換する"""
    print(f"[FFmpeg] MP4変換中...")

    cmd = [
        "ffmpeg", "-y",
        "-i", input_webm,
    ]

    # BGMがあれば合成
    if BGM_PATH and Path(BGM_PATH).exists():
        cmd += ["-i", BGM_PATH, "-filter_complex",
                "[1:a]volume=0.2[bgm];[0:a][bgm]amix=inputs=2:duration=first[aout]",
                "-map", "0:v", "-map", "[aout]"]
    
    cmd += [
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
               "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
        "-c:v", "h264_nvenc",
        "-c:a", "aac",
        "-shortest",
        output_mp4
    ]

    subprocess.run(cmd, check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        _timed("Step5 MP4変換", finalize_video, webm_path, mp4_path)

        # Step 6: YouTubeアップロード
        _timed("Step6 YT投稿", upload_to_youtube, mp4_path, title, description)

        elapsed = time.time() - total_start
        print(f"\n✅ パイプライン完了: {mp4_path}  (合計: {elapsed:.1f}秒)")

    except Exception as e:
        print(f"\n❌ エラー発生: {e}")
        raise

    finally:
        # 一時ファイル削除
        for path in [wav_path, webm_path]:
            if Path(path).exists():
                Path(path).unlink()
                print(f"[Cleanup] 削除: {path}")


if __name__ == "__main__":
    main()
