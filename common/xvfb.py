"""Xvfb（仮想ディスプレイ）の起動。

Shorts の録画とライブ配信の Unity で共有する。ライブ配信は GPU 仮想ディスプレイ
（Xorg :99）を別に立てているので、そこを Xvfb に奪われないよう `reserved` で除外する。
奪うと Xorg が起動できず、systemd が Restart=always で無限にリトライする状態になる。
"""

import shutil
import subprocess
import time
from pathlib import Path

# 既定の探索範囲。ライブ配信の Xorg は :99 に居るので `reserved` で除外して使う
DEFAULT_START = 99
DEFAULT_STOP = 200


def start_xvfb(start: int = DEFAULT_START, stop: int = DEFAULT_STOP,
               reserved=(), size: str = "1920x1080x24") -> tuple:
    """空きディスプレイ番号で Xvfb を起動し (proc, display) を返す。

    reserved: 使ってはいけない番号。":99" でも "99" でも 99 でも受け付ける。
    """
    if not shutil.which("Xvfb"):
        raise RuntimeError("Xvfb が見つかりません。`sudo apt install -y xvfb` でインストールしてください")

    blocked = {str(r).lstrip(":") for r in reserved if str(r).strip()}

    for n in range(start, stop):
        if str(n) in blocked:
            continue
        lock = Path(f"/tmp/.X{n}-lock")
        sock = Path(f"/tmp/.X11-unix/X{n}")
        if lock.exists() or sock.exists():
            continue
        display = f":{n}"
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", size],
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
    raise RuntimeError(f"Xvfb: 空きディスプレイ番号が見つかりません ({start}-{stop - 1})")
