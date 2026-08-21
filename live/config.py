"""AITuberライブ配信（live/）の設定。

`.env` はリポジトリのルートに1本だけ。Shorts パイプラインと同じファイルを読む
（DB・Gemini・YouTube の資格情報は共通なので、2箇所で管理しない）。

共通の設定値（VOICEVOX・LLM・DB・ARDY）は common/ が原典で、ここでは
後方互換のために再輸出するだけ。ライブ配信にしか無い設定はここが原典。
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# `python live/live.py` で起動されると sys.path[0] が live/ になるので、
# リポジトリのルートを足して common/ を読めるようにする
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from common.env import (  # noqa: E402
    ROOT, DATA_DIR, LOGS_DIR, AFFIRMATIVE_BOT_DIR,
    env_flag, env_float, env_int, env_float_opt,
)
from common import ardy as _ardy, llm as _llm, voice as _voice  # noqa: E402
from common.db import DB_CONFIG  # noqa: E402,F401


# ── 配信スケジュール ───────────────────────────────
LIVE_START_HHMM = os.getenv("LIVE_START_HHMM", "21:00")
LIVE_END_HHMM   = os.getenv("LIVE_END_HHMM", "22:00")
# クロージングを話し始める時刻。ここから終了までに締めの発話を終わらせる
LIVE_CLOSING_HHMM = os.getenv("LIVE_CLOSING_HHMM", "21:55")

# ── Unity 常駐（LiveController） ──────────────────
UNITY_EXE     = os.getenv("UNITY_EXE", "/home/suibari/Unity/Hub/Editor/6000.0.76f1/Editor/Unity")
UNITY_PROJECT = os.getenv("UNITY_LIVE_PROJECT", "/home/suibari/bottan-video-dev")
# 配信に使う X ディスプレイ。setup/install_xorg.sh が作る GPU 付き仮想ディスプレイ。
#
# 空にすると Xvfb を自前で起動するが、Xvfb は Mesa の llvmpipe（CPU）で描くので
# 1920x1080 の URP + VRM では実測 1.7fps しか出ず配信にならない。
# 本番では必ず :99 を指定すること。
LIVE_DISPLAY  = os.getenv("LIVE_DISPLAY", ":99")

# 横画面の構図。カメラは Y に 180度 回っているので、X を負にすると
# botたんは画面の左へ寄る（右側にコメント欄の余白ができる）
LIVE_CAMERA_X = env_float_opt("LIVE_CAMERA_X")
LIVE_CAMERA_Z = env_float_opt("LIVE_CAMERA_Z")
# 夜の部屋に合わせた平行光の明るさ。既定は Unity 側の 0.85
LIVE_LIGHT_INTENSITY = env_float_opt("LIVE_LIGHT_INTENSITY")
# 背景の光源に合わせたキーライトと環境光。月明かりの部屋なら寒色にする
LIVE_LIGHT_COLOR   = os.getenv("LIVE_LIGHT_COLOR", "").strip()
LIVE_AMBIENT_COLOR = os.getenv("LIVE_AMBIENT_COLOR", "").strip()
# 無音時に表情の口成分を打ち消す強さ 0〜1。0 で無効。
# これを渡さないと、ApplyEmotion が常に合計 _maxEmotionWeight を配分する都合で
# 「無表情」が存在せず、喋り終わったあとも口が開いたままになる（初回の配信で発生）。
# 朝版パイプライン（quiz_pipeline.py の MORNING_MOUTH_CLOSE）と同じ 1.0 を既定にする
LIVE_MOUTH_CLOSE = env_float("LIVE_MOUTH_CLOSE", 1.0)
# 体の向き。未設定ならカメラの方を向く（画面の外を向いて見えるのを防ぐ）。
# 度で指定。正の値でさらに画面の内側（コメント欄側）へ振れる
LIVE_CHARACTER_YAW = env_float_opt("LIVE_CHARACTER_YAW")
LIVE_PORT     = env_int("LIVE_PORT", 2338)
UNITY_URL     = f"http://127.0.0.1:{LIVE_PORT}"

# ── 音声の経路 ────────────────────────────────────
# Unity の音を OBS へ渡す専用の null シンク。詳細は audio.py の冒頭を参照
LIVE_AUDIO_SINK = os.getenv("LIVE_AUDIO_SINK", "bottan_live")

# ── 待機中の演出 ──────────────────────────────────
# 喋っていない間もモーションと表情を動かす。止まっていると人形に見える
IDLE_ENABLED       = env_flag("IDLE_ENABLED", True)
IDLE_MOTION_MIN_SEC = env_float("IDLE_MOTION_MIN_SEC", 9.0)
IDLE_MOTION_MAX_SEC = env_float("IDLE_MOTION_MAX_SEC", 20.0)
IDLE_EMOTION_MIN_SEC = env_float("IDLE_EMOTION_MIN_SEC", 6.0)
IDLE_EMOTION_MAX_SEC = env_float("IDLE_EMOTION_MAX_SEC", 14.0)

# ── VOICEVOX ─────────────────────────────────────
# 10101 は AivisSpeech Engine の既定ポートでもある。起動時に /speakers で実体を確認する
VOICEVOX_URL     = _voice.VOICEVOX_URL
VOICEVOX_SPEAKER = _voice.VOICEVOX_SPEAKER   # VOICEVOX: 春日部つむぎ ノーマル

# VOICEVOX の出力フォーマット。無音WAVを混ぜて concat -c copy するため一致させる
WAV_RATE     = _voice.WAV_RATE
WAV_CHANNELS = _voice.WAV_CHANNELS

# ── LLM ──────────────────────────────────────────
USE_LOCAL_LLM   = _llm.USE_LOCAL_LLM
GEMINI_API_KEY  = _llm.GEMINI_API_KEY
GEMINI_BASE_URL = _llm.GEMINI_BASE_URL
LOCAL_LLM_URL   = _llm.LOCAL_LLM_URL
LOCAL_LLM_MODEL = _llm.LOCAL_LLM_MODEL
# カンマ区切りで複数指定でき、左から順にフォールバックする
GEMINI_MODELS   = _llm.GEMINI_MODELS

# ── DB（botたんの記憶） ───────────────────────────
# ── biorhythm（energy） ──────────────────────────
BIORHYTHM_URL = os.getenv("BIORHYTHM_URL", "http://localhost:3002")
# フェーズ1では bsky-affirmative-bot を改修せず既存 type を流用する。
# 将来 apps/biorhythm_server に live_comment を足したらここを差し替えるだけでよい
ENERGY_TYPE_COMMENT = os.getenv("ENERGY_TYPE_COMMENT", "conversation")

# ── ARDY（モーション生成） ────────────────────────
ARDY_PORT          = _ardy.ARDY_PORT
ARDY_URL           = _ardy.ARDY_URL
ARDY_ENGINE_ROOT   = _ardy.ARDY_ENGINE_ROOT
ARDY_REPO          = _ardy.ARDY_REPO
ARDY_MERGED_BASE   = _ardy.ARDY_MERGED_BASE
ARDY_MIN_AVAIL_GB  = _ardy.ARDY_MIN_AVAIL_GB
ARDY_READY_TIMEOUT = _ardy.ARDY_READY_TIMEOUT
ARDY_GEN_TIMEOUT   = _ardy.ARDY_GEN_TIMEOUT
ARDY_CFG           = _ardy.ARDY_CFG
ARDY_ARM_SPREAD    = _ardy.ARDY_ARM_SPREAD
# 生成した .vrma を貯めておく場所。過去配信ぶんを再利用するので消さない
MOTION_POOL_DIR = Path(os.getenv("MOTION_POOL_DIR", DATA_DIR / "motions"))

# ── OBS ──────────────────────────────────────────
OBS_HOST     = os.getenv("OBS_HOST", "localhost")
OBS_PORT     = env_int("OBS_PORT", 4455)
OBS_PASSWORD = os.getenv("OBS_PASSWORD", "")
# OBS は LIVE_DISPLAY と同じ X ディスプレイで動かすこと。X11 のウィンドウ
# キャプチャは OBS が接続しているディスプレイの窓しか列挙できないので、
# デスクトップ(:0)の OBS からは :99 の Unity は永久に見えない
OBS_LAUNCH  = env_flag("OBS_LAUNCH", True)
OBS_COMMAND = os.getenv("OBS_COMMAND", "flatpak run com.obsproject.Studio")
# 配信用の設定はデスクトップで普段使いする OBS と分けておく。同じ設定を
# 共有していると、:0 側で触った状態がそのまま配信に出てしまう
# 1080p30 の目安。NVENC ではなく x264（CPU）で回すので上げすぎない
STREAM_VIDEO_KBPS = env_int("STREAM_VIDEO_KBPS", 6000)
STREAM_AUDIO_KBPS = env_int("STREAM_AUDIO_KBPS", 160)
OBS_COLLECTION = os.getenv("OBS_COLLECTION", "bottan-live")
OBS_PROFILE    = os.getenv("OBS_PROFILE", "bottan-live")

# ── 字幕（OBS のテキストソースが読むファイル） ──────
SUBTITLE_DIR = Path(os.getenv("SUBTITLE_DIR", DATA_DIR / "obs"))
SUBTITLE_JA  = SUBTITLE_DIR / "subtitle_ja.txt"
SUBTITLE_EN  = SUBTITLE_DIR / "subtitle_en.txt"
COMMENTS_TXT = SUBTITLE_DIR / "comments.txt"
CLOCK_TXT    = SUBTITLE_DIR / "clock.txt"

# ── 配信の振る舞い ────────────────────────────────
# コメントがこの秒数途切れたらフリートークを挟む
FILLER_IDLE_SEC = env_float("FILLER_IDLE_SEC", 90.0)
# 同一ユーザーの連投をこの秒数だけ間引く
COMMENT_USER_COOLDOWN_SEC = env_float("COMMENT_USER_COOLDOWN_SEC", 60.0)
# 視聴者コメントの文字数上限。これを超える分は切り捨てる
COMMENT_MAX_CHARS = env_int("COMMENT_MAX_CHARS", 200)

# ── 運用 ─────────────────────────────────────────
DRY_RUN         = env_flag("DRY_RUN")          # YouTube に触らずローカルだけで回す
# ARDY はサーバ起動に4〜5分・常駐15GB かかる。動作確認では飛ばしたい
SKIP_ARDY       = env_flag("SKIP_ARDY")
FAKE_COMMENTS   = os.getenv("FAKE_COMMENTS", "")  # DRY_RUN 時に流し込む偽コメントJSON
# 配信枠の公開設定。Shorts 投稿の YOUTUBE_PRIVACY とは**別のキー**にしてある。
# .env を1本にまとめた際、配信用の private が Shorts 側にも効いて毎晩の投稿が
# 限定公開になる事故を避けるため。空文字を渡されたら既定に倒す（API が弾くため）
YOUTUBE_PRIVACY = os.getenv("LIVE_YOUTUBE_PRIVACY", "").strip() or "private"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

WORK_DIR = Path(os.getenv("LIVE_WORK_DIR", "/tmp/bottan-live"))


def ensure_dirs() -> None:
    """出力先を作る。OBS のテキストソースは起動時にファイルが無いと読み込みに失敗する。"""
    for d in (WORK_DIR, SUBTITLE_DIR, MOTION_POOL_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for f in (SUBTITLE_JA, SUBTITLE_EN, COMMENTS_TXT, CLOCK_TXT):
        if not f.exists():
            f.write_text("", encoding="utf-8")
