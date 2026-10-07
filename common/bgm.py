"""Shorts の BGM を ACE-Step でその場で作る。

2026-09 から Shorts フィードに乗らなくなり、毎日同じ曲（シャイニングスター）が
流れる量産コンテンツに見えていると判断した（shorts/turn.py の冒頭を参照）。
回ごとに曲調を抽選して、40〜50秒のインスト曲を作る。

ACE-Step 1.5（MIT）はリポジトリの外（BGM_ACESTEP_DIR）に専用の venv で入れてあり、
tools/acestep_bgm.py をその venv の python で別プロセスとして呼ぶ。プロセスが
終われば VRAM も返るので、後に起動する ARDY と取り合わない。

実測（2026-10-07、RTX 5070 Ti）: 読み込み 8.7秒・生成 5.5〜9.5秒・VRAM ピーク 5.8GB。
ollama（7.8GB）と Irodori（3.8GB）が載ったままだと OOM になるので、ARDY の起動前と
同じ手順で先に解放する。どこかで失敗したら None を返し、呼び出し側は今の曲を使う。

GPU は 5070 Ti に固定する（5060 Ti は別プロジェクト用に空けておく）。
"""

import os
import random
import subprocess
import time
from pathlib import Path

from common.env import LOGS_DIR, ROOT, env_flag, env_int

BGM_GENERATE = env_flag("BGM_GENERATE", True)
ACESTEP_DIR = Path(os.getenv("BGM_ACESTEP_DIR", str(Path.home() / "work/acestep-poc")))
# 番号ではなく UUID で指定する（CUDA と nvidia-smi で番号の並びが違うことがあるため）。
# 既定は ARDY と同じ GPU
CUDA_DEVICES = os.getenv("BGM_CUDA_DEVICES") or os.getenv("ARDY_CUDA_DEVICES", "")
TIMEOUT_SEC = env_int("BGM_TIMEOUT_SEC", 300)

# 生成した曲は37〜38秒で鳴り終わって残りが無音になる（実測）。動画は最大40秒＋余韻
# なので長めに作り、最後をフェードアウトさせる
DURATION_SEC = 50
FADE_SEC = 3.0
# 今の曲（シャイニングスター）が -8.8 LUFS。生成曲は -14 前後で出てくるので揃える。
# 揃えておけば run_ffmpeg_finalize の bgm_volume をそのまま使える
TARGET_LUFS = -9

CREDIT = "BGM: AI生成（ACE-Step 1.5）"

# 曲調はコードで抽選する。LLM に選ばせると朝クイズが2本続けてマリンバポップになった
GENRES = [
    ("lo-fi hip hop", ["soft Rhodes piano", "mellow guitar", "vinyl crackle"]),
    ("city pop", ["electric piano", "slap bass", "bright synth pads"]),
    ("acoustic pop", ["acoustic guitar", "ukulele", "hand claps"]),
    ("bossa nova", ["nylon guitar", "brushed drums", "soft flute"]),
    ("marimba pop", ["marimba", "pizzicato strings", "glockenspiel"]),
    ("kawaii future bass", ["sparkly synth chords", "soft plucks", "light beat"]),
    ("chiptune", ["8-bit square leads", "soft arpeggios", "light drums"]),
    ("jazzy cafe", ["upright bass", "piano trio", "brushed snare"]),
    ("chill electronic", ["warm pads", "music box", "gentle beat"]),
]
MOODS = {
    "quiz":  "cheerful, bright and curious morning mood",
    "night": "warm, gentle and comforting evening mood",
}


def build_caption(kind: str, rng: random.Random = random) -> str:
    genre, instruments = rng.choice(GENRES)
    picked = rng.sample(instruments, 2)
    return (f"A light {genre} instrumental with {picked[0]} and {picked[1]}, "
            f"{MOODS[kind]}. Quiet background music under a voice, "
            f"no vocals, no loud leads, not distracting.")


def _release_gpu() -> None:
    """ARDY の起動前と同じ手順で ollama・画像生成・Irodori を空ける。"""
    from common.ardy import _free_imagegen, _free_ollama
    from common.voice import unload_irodori
    _free_ollama()
    _free_imagegen()
    unload_irodori()


def generate(kind: str, out_dir: Path) -> Path | None:
    """BGM を1曲作って、音量とフェードを整えた mp3 のパスを返す。失敗したら None。"""
    if not BGM_GENERATE:
        return None
    python = ACESTEP_DIR / ".venv/bin/python"
    if not python.exists():
        print(f"[BGM] ACE-Step が見つかりません（{ACESTEP_DIR}）。今の曲を使います")
        return None

    out_dir = Path(out_dir)
    raw = out_dir / f"bgm_raw_{int(time.time())}.wav"
    final = raw.with_name(raw.stem.replace("_raw", "") + ".mp3")
    caption = build_caption(kind)
    print(f"[BGM] 曲調: {caption}")

    _release_gpu()
    env = {**os.environ}
    if CUDA_DEVICES:
        env["CUDA_VISIBLE_DEVICES"] = CUDA_DEVICES
    # ACE-Step のログは1曲で数百行あるので、ARDY と同じく別ファイルに出す
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"acestep_{time.strftime('%Y%m%d_%H%M%S')}.log"
    print(f"[BGM] ACE-Step のログ: {log_path}")
    t0 = time.time()
    try:
        with open(log_path, "w") as log:
            subprocess.run([str(python), str(ROOT / "tools/acestep_bgm.py"),
                            "--caption", caption, "--duration", str(DURATION_SEC),
                            "--out", str(raw)],
                           cwd=ACESTEP_DIR, env=env, check=True, timeout=TIMEOUT_SEC,
                           stdout=log, stderr=subprocess.STDOUT)
        fade_start = DURATION_SEC - FADE_SEC
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                        "-af", f"loudnorm=I={TARGET_LUFS}:TP=-1,"
                               f"afade=t=out:st={fade_start}:d={FADE_SEC}",
                        "-b:a", "160k", str(final)],
                       check=True, timeout=120)
    except Exception as e:
        print(f"[BGM] 生成に失敗しました。今の曲を使います: {e}")
        return None
    finally:
        raw.unlink(missing_ok=True)
    print(f"[BGM] 生成完了 ({time.time() - t0:.1f}秒): {final}")
    return final
