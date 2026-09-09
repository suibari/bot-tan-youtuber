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


def host_pressure() -> str:
    """そのときのホストの詰まり具合を1行で返す。読めなければ空文字。

    VOICEVOX 自体は速い（99字で約2秒、CPU を6コア占有されても3.2秒）。それが
    15秒返らないのは ollama がモデル（11GB, mmap）を読み直して I/O を飽和させ、
    同居プロセスが道連れになるとき（2026-09-01 の配信で iowait 39%、
    load_tensors 128回、ARDY も 300秒タイムアウト）。PSI の io/some avg10 が
    その状態を直接示す——平常時は 1 前後、あの日なら数十——ので、遅かったとき
    と落ちたときのログに必ず添える。エンジンが遅いのかホストが詰まったのかは
    これが無いと切り分けられない。

    2026-09-09 の配信でも同じ数字が出た（io avg10 が 72〜76、load 15.6）。
    このときは ollama のモデル再読込ではなく swap への書き出しが原因で、
    OBS の送出まで止まって YouTube に配信を切られた。**合成の失敗だけでなく
    配信前の点検と配信中のメモリ記録からも同じものを見たい**ので、
    common/voice.py の私物からここへ移した。
    """
    parts = []
    try:
        with open("/proc/pressure/io") as f:
            # "some avg10=39.21 avg60=... avg300=... total=..."
            parts.append("io " + f.readline().split()[1])
    except (OSError, IndexError):
        pass          # PSI の無い環境。診断が欠けるだけで呼び出し元は止めない
    try:
        parts.append(f"load {os.getloadavg()[0]:.1f}")
    except OSError:
        pass
    return " ".join(parts)


def meminfo_kb() -> dict:
    """/proc/meminfo を {キー: KB} で返す。読めなければ空の辞書。

    空き RAM だけを見ても足りない。`MemAvailable` は**回収できるページ
    キャッシュを含む**ので、匿名ページで RAM が埋まっていても大きい値が出る。
    2026-09-09 の配信は MemAvailable が 15.3GB あるまま swap を 2.7GB 吐き、
    OBS が固まった。`SwapFree` と `Committed_AS` を併せて見るために足した。
    """
    values = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                try:
                    values[key] = int(rest.split()[0])
                except (IndexError, ValueError):
                    continue
    except OSError:
        return {}
    return values


def ensure_dirs() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
