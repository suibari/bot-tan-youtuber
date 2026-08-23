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
from common import vrma_style as _vrma_style  # noqa: E402
from common.db import DB_CONFIG  # noqa: E402,F401


# ── 配信スケジュール ───────────────────────────────
LIVE_START_HHMM = os.getenv("LIVE_START_HHMM", "21:00")
LIVE_END_HHMM   = os.getenv("LIVE_END_HHMM", "22:00")
# クロージングを話し始める時刻。ここから終了までに締めの発話を終わらせる
LIVE_CLOSING_HHMM = os.getenv("LIVE_CLOSING_HHMM", "21:55")
# 開始の何秒前に testing へ入るか。YouTube は testing の要求を受けてから
# lifeCycleStatus が testing になるまで数十秒かかることがあり、その途中で
# live を投げると 403 invalidTransition で弾かれる。先に済ませておく
LIVE_TESTING_LEAD_SEC = env_int("LIVE_TESTING_LEAD_SEC", 120)
# live への遷移を何秒粘るか。弾かれても 5 秒おきに投げ直す
LIVE_GO_LIVE_RETRY_SEC = env_int("LIVE_GO_LIVE_RETRY_SEC", 300)

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
# energy は共有DBの bot_state を読むだけ（live/energy.py 参照）。加算は
# biorhythm_server が bottan_live.comments を見て自分でやるので、
# 配信側から biorhythm_server の HTTP を叩くことはない
BIORHYTHM_MEMORY_API_URL = os.getenv("BIORHYTHM_MEMORY_API_URL", "").rstrip("/")
BIORHYTHM_INTERNAL_SECRET = os.getenv("BIORHYTHM_INTERNAL_SECRET", "")
# 検索APIはクエリが長いほど遅くなる。サーバがクエリ全文を埋め込んだうえ、
# pg_trgm の similarity と ilike をクエリ全文で全行に当てるため。
# 実測（LAN内）: 100文字 1.9秒 / 300文字 3.2秒 / 600文字 5.2秒 / 1000文字 9.1秒
BIORHYTHM_MEMORY_API_TIMEOUT_SEC = env_float("BIORHYTHM_MEMORY_API_TIMEOUT_SEC", 15.0)
# 検索クエリの上限。長くしても当たりが良くなるわけではなく、遅くなるだけ
BOT_MEMORY_QUERY_MAX_CHARS = env_int("BOT_MEMORY_QUERY_MAX_CHARS", 500)
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
# 生成モーションの見た目を決める Unity 引数。Shorts の録画と同じ値を使う。
# これを渡さないと Unity 側は「改修前の見た目」の既定値で動き、-vrmaSmooth（実測で
# カクつき -64%）も効かない。統合前の配信はこれを1つも渡していなかった。
# 配信だけ変えたいときは LIVE_VRMA_SMOOTH のように LIVE_ を頭に付けた環境変数を置く。
def vrma_unity_args() -> list:
    return _vrma_style.vrma_unity_args(prefix="LIVE_")


# 次のモーションを投げる間隔を決めるときの重なり[秒]。
# VrmaMotionPlayer.FadeDuration と一致させること
VRMA_CHUNK_OVERLAP = _vrma_style.VRMA_CHUNK_OVERLAP

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
# 字幕を出すタイミングの微調整[秒]。
#   0  : Unity が鳴らし始めたのを /status で確認してから出す（既定）
#   正 : そのぶん字幕を遅らせる
#   負 : 鳴り始めを待たずに先に出す（先行量は Unity の WAV ロード時間ぶんで、
#        実測 0.2秒 前後。|値| そのものは効かない）
#
# OBS は映像とテキストを即時に描くが、音声は PulseAudio の null sink の
# monitor 経由で取り込むためバッファ遅延がある。どちらへずれるかは環境で
# 変わるので、実配信を見てここで詰めること
SUBTITLE_LEAD_SEC = env_float("SUBTITLE_LEAD_SEC", 0.0)
# energy ゲージ。OBS の「energy」ブラウザソースに file:// で読ませる
ENERGY_HTML  = SUBTITLE_DIR / "energy.html"
# ゲージを描き直す間隔[秒]。energy は分単位でしか動かないので短くしても意味がない
ENERGY_REFRESH_SEC = env_int("ENERGY_REFRESH_SEC", 30)

# Unity の fps をログへ残す間隔[秒]。0 以下で無効。
# Editor 常駐のメモリ増加と、モーション投入によるフレーム落ちの監視を兼ねる
FPS_LOG_SEC = env_float("FPS_LOG_SEC", 60.0)

# botたんの状態（気分・行動・energy）を DB から引き直す間隔[秒]。
# コメント返信のたびに引くと、DB が詰まったときにメインループごと止まる。
# energy は分単位でしか動かないので、数秒のキャッシュで実害はない
BOT_CONTEXT_TTL_SEC = env_float("BOT_CONTEXT_TTL_SEC", 20.0)

# ── 配信の振る舞い ────────────────────────────────
# コメントがこの秒数途切れたらフリートークを挟む
FILLER_IDLE_SEC = env_float("FILLER_IDLE_SEC", 90.0)
# 同一ユーザーの連投をこの秒数だけ間引く。
# **他に返事できるコメントが無いときは無視される**（chat.CommentQueue._next_index）。
# 1人しか居ないのに黙ってフリートークへ流れると、会話が続かないため
COMMENT_USER_COOLDOWN_SEC = env_float("COMMENT_USER_COOLDOWN_SEC", 60.0)
# LLM へ会話履歴として渡す「場の流れ」のターン数（コメント返答もフリートークも1ターン）。
# 増やすほど話はつながるが、そのぶん入力トークンが増えて返事が遅くなる。
# 1ターンは日本語で 100〜150字ほど。system prompt が約7000字あるので、6ターンで1割弱
LIVE_HISTORY_TURNS = env_int("LIVE_HISTORY_TURNS", 6)
# 上の流れから押し出されても、いま返す相手とのやりとりだけは覚えておく件数
LIVE_HISTORY_USER_TURNS = env_int("LIVE_HISTORY_USER_TURNS", 3)
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
    # ブラウザソースは URL が 404 でも黙って空を出すだけだが、
    # 最初の描画までゲージが出ないので、起動時に一度書いておく
    if not ENERGY_HTML.exists():
        import gauge
        gauge.write(0.0)
