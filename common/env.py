"""リポジトリのパス解決と、環境変数の読み取り。

`.env` はリポジトリのルートに1本だけ置く。以前は Shorts 側とライブ側で別々に
持っていて、ライブ側が Shorts 側の `.env` へフォールバックしていたため、
「本番のライブが dev の設定を読む」導線ができていた。

パスも同じ理由でここに集約する。各モジュールが `Path(__file__).parent / "data"`
と書いていると、ファイルをサブディレクトリへ移した瞬間に指す先が変わる。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"

# 隣接リポジトリ。ペルソナの語彙（好きな言葉・苦手な話題）の原典
AFFIRMATIVE_BOT_DIR = Path(os.getenv("BOTTAN_BOT_DIR", ROOT.parent / "bsky-affirmative-bot"))

load_dotenv(ROOT / ".env")


def env_flag(name: str, default: bool = False) -> bool:
    """真偽値の環境変数を読む。

    `SKIP_YOUTUBE=TRUE` のような大文字や `1` / `yes` も受け付ける。
    以前は `os.getenv("SKIP_YOUTUBE") == "true"` と完全一致で見ていたため、
    **TRUE を渡したのにスキップされず動画が公開された**（2026-08-15）。
    真偽値の環境変数は必ずこれを通すこと。
    """
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("true", "1", "yes", "on")


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except ValueError:
        print(f"[config] {name} を数値として解釈できません: {v!r} → {default} を使います")
        return default


def env_int(name: str, default: int) -> int:
    return int(env_float(name, default))


def env_float_opt(name: str):
    """未設定なら None を返す。「指定があるときだけ Unity に渡す」引数に使う。"""
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return None
    try:
        return float(v)
    except ValueError:
        print(f"[config] {name} を数値として解釈できません: {v!r} → 無視します")
        return None


def ensure_dirs() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
