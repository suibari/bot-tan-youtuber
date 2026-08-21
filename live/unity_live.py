"""配信用 Unity 常駐プロセスの起動と停止。

Xvfb 上で Unity Editor を GUI 起動し、-liveMode で自動 Play させる。
Player ビルドは作らない（bottan-video-dev の VideoRecorder.cs が UnityEditor を
無条件 import しているのでビルドが通らない）。録画パイプラインが同じやり方で
1年近く動いている実績があるので、配信でもそれに倣う。

Editor が1時間もつかは実配信リハーサルで確認する項目。rss_mb() を定期的に
ログへ残しておくこと。
"""

import os
import signal
import subprocess
import time
from pathlib import Path

from config import (UNITY_EXE, UNITY_PROJECT, LIVE_PORT, LIVE_DISPLAY,
                    LIVE_CAMERA_X, LIVE_CAMERA_Z, LIVE_LIGHT_INTENSITY,
                    LIVE_LIGHT_COLOR, LIVE_AMBIENT_COLOR, LIVE_MOUTH_CLOSE,
                    LIVE_CHARACTER_YAW)
from common import xvfb
import audio
import unity_client


# Xvfb を立てるときの番号の探索範囲。
#
# Shorts の収録は 99 から探すが、配信用の GPU 仮想ディスプレイ（Xorg）が :99 を
# 予約しているので、そこから始めると Xvfb が先に :99 を奪い、Xorg が起動できなく
# なる（実際に踏んだ。systemd が Restart=always で無限にリトライする状態になる）。
_XVFB_RANGE = range(120, 200)


def _start_xvfb() -> tuple:
    """空きディスプレイ番号で Xvfb を起動し (proc, display) を返す。"""
    return xvfb.start_xvfb(_XVFB_RANGE.start, _XVFB_RANGE.stop,
                           reserved=(LIVE_DISPLAY,))


class UnityLive:
    """配信中ずっと生きている Unity プロセス。"""

    def __init__(self, log_path=None):
        self.xvfb = None
        self.proc = None
        self.display = None
        self._display_num = None
        self.log_path = Path(log_path) if log_path else Path("/tmp/bottan-live-unity.log")

    def start(self, ready_timeout: float = 300.0) -> dict:
        """Unity を起動し、LiveController が応答するまで待つ。"""
        env = os.environ.copy()
        env.pop("XAUTHORITY", None)          # 仮想ディスプレイには不要

        if LIVE_DISPLAY:
            # setup/install_xorg.sh が用意した GPU 付き仮想ディスプレイに相乗りする。
            # 自分で起こしたものではないので、停止時に後始末してはいけない
            sock = Path(f"/tmp/.X11-unix/X{LIVE_DISPLAY.lstrip(':')}")
            if not sock.exists():
                raise RuntimeError(
                    f"ディスプレイ {LIVE_DISPLAY} がありません。"
                    f"`sudo systemctl start bottan-live-xorg` で起動するか、"
                    f"LIVE_DISPLAY='' にして Xvfb にフォールバックしてください"
                    f"（Xvfb は GPU を使えないため 1.7fps しか出ません）"
                )
            self.display = LIVE_DISPLAY
            self._cleanup_stale(remove_x_locks=False)
            print(f"[Unity] 既存ディスプレイを使います: DISPLAY={self.display}")
        else:
            print("[Unity] 警告: Xvfb で起動します。GPU が使われないため配信品質は出ません")
            self._cleanup_stale(remove_x_locks=True)
            self.xvfb, self.display = _start_xvfb()
            self._display_num = self.display.lstrip(":")

        env["DISPLAY"] = self.display
        # 音は OS のサウンドサーバを通らないと OBS に届かない。
        # 配信専用のシンクへ流し、その monitor を OBS が拾う（audio.py 参照）
        try:
            audio.ensure_sink()
            audio.unity_env(env)
            print(f"[Unity] 音声の出力先: {env['PULSE_SINK']}")
        except Exception as e:
            # 音が出なくても映像は出る。配信を止める理由にはしない
            print(f"[Unity] 音声シンクを用意できません（声が出ない可能性があります）: {e}")

        cmd = [
            UNITY_EXE,
            "-projectPath", UNITY_PROJECT,
            "-liveMode",
            "-livePort", str(LIVE_PORT),
            "-logFile", str(self.log_path),
        ]
        # 横画面の構図。既定は縦画面と同じ真ん中寄せなので、指定がなければ渡さない
        if LIVE_CAMERA_X is not None:
            cmd += ["-liveCameraX", str(LIVE_CAMERA_X)]
        if LIVE_CAMERA_Z is not None:
            cmd += ["-liveCameraZ", str(LIVE_CAMERA_Z)]
        if LIVE_LIGHT_INTENSITY is not None:
            cmd += ["-liveLightIntensity", str(LIVE_LIGHT_INTENSITY)]
        if LIVE_LIGHT_COLOR:
            cmd += ["-liveLightColor", LIVE_LIGHT_COLOR]
        if LIVE_AMBIENT_COLOR:
            cmd += ["-liveAmbientColor", LIVE_AMBIENT_COLOR]
        # 喋り終わったあとに口が開いたままになるのを防ぐ（config.py の解説を参照）
        if LIVE_MOUTH_CLOSE > 0:
            cmd += ["-mouthCloseOnSilence", str(LIVE_MOUTH_CLOSE)]
        if LIVE_CHARACTER_YAW is not None:
            cmd += ["-liveCharacterYaw", str(LIVE_CHARACTER_YAW)]
        print(f"[Unity] 起動: {' '.join(cmd)}")
        # プロセスグループを分けておく。停止時にまとめて畳める
        self.proc = subprocess.Popen(
            cmd, env=env, stderr=subprocess.DEVNULL, preexec_fn=os.setsid
        )

        print(f"[Unity] LiveController の応答を待っています（最大{ready_timeout:.0f}秒）...")
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"Unity が起動直後に終了しました (code={self.proc.returncode})。"
                    f"ログ: {self.log_path}"
                )
            try:
                st = unity_client.status()
                print(f"[Unity] 準備完了: {st}")
                return st
            except unity_client.UnityError:
                time.sleep(2.0)

        self.stop()
        raise RuntimeError(f"{ready_timeout:.0f}秒待っても LiveController が応答しません。"
                           f"ログ: {self.log_path}")

    def _cleanup_stale(self, remove_x_locks: bool = True) -> None:
        """前回の残骸を片付ける。

        録画パイプラインの record_with_unity と同じ後始末だが、こちらは
        flock を取ってから呼ばれる前提なので、他人の Unity を巻き込むことはない。

        remove_x_locks=False のときは /tmp/.X*-lock を消さない。常駐している
        Xorg :99 のロックまで巻き込んで消すと、生きているディスプレイが
        「空き番号」に見えて次の起動と衝突する。
        """
        subprocess.run(["pkill", "-9", "-f", "Unity -projectPath"], capture_output=True)
        if remove_x_locks:
            subprocess.run(["pkill", "-9", "-f", "Xvfb :"], capture_output=True)
        time.sleep(3)
        if remove_x_locks:
            for lock in Path("/tmp").glob(".X*-lock"):
                lock.unlink(missing_ok=True)
        for unity_lock in (
            Path(UNITY_PROJECT) / "Temp" / "UnityLockFile",
            Path(UNITY_PROJECT) / "Library" / "ArtifactDB-lock",
        ):
            unity_lock.unlink(missing_ok=True)

    def rss_mb(self) -> float:
        """Unity プロセスの常駐メモリ[MB]。0 なら測れていない。

        Editor 常駐のメモリリークは今回はじめて踏む可能性のある領域なので、
        配信中は定期的にログへ残すこと。
        """
        if self.proc is None or self.proc.poll() is not None:
            return 0.0
        try:
            out = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(self.proc.pid)],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            return int(out) / 1024.0
        except Exception:
            return 0.0

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 20.0) -> None:
        """Unity と Xvfb を終了する。

        Unity は終了時に mono が SIGSEGV することがあるが（core.py:711-724 の既知事象）、
        配信では成果物が無いので終了コードは見ない。落ちればよい。
        """
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                pass
            deadline = time.time() + timeout
            while time.time() < deadline and self.proc.poll() is None:
                time.sleep(0.5)
            if self.proc.poll() is None:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            print("[Unity] 終了しました")
        self.proc = None

        if self.xvfb is not None:
            self.xvfb.kill()
            self.xvfb.wait(timeout=5)
            self.xvfb = None
            print("[Xvfb] 終了しました")

        # ソケットとロックを消す。残すと _start_xvfb が次の番号へずれていき、
        # 起動を繰り返すうちに 99〜199 を使い切って起動できなくなる
        if self._display_num:
            for path in (Path(f"/tmp/.X{self._display_num}-lock"),
                         Path(f"/tmp/.X11-unix/X{self._display_num}")):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._display_num = None
            self.display = None
