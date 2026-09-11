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
                    LIVE_CHARACTER_YAW, vrma_unity_args)
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


def _display_refresh_hz(display: str) -> float:
    """xrandr が示す現在モードのリフレッシュレート。取得不能なら0。

    **これは「絵が出ている」ことの保証にはならない。** 返るのはモードの
    公称値で、実際のスキャンアウトが止まっていても 59.95 と答える。
    2026-09-10 の配信ではこの点検が 59.95Hz で素通りしたまま、同じ
    ディスプレイの glxgears が 1.0〜1.4fps しか出ていなかった
    （:99 が gdm より先に起動していて、描画から締め出されていた）。
    実際に描けているかは UnityLive.measure_fps で実測すること。
    """
    try:
        out = subprocess.run(
            ["xrandr", "--current"], env={**os.environ, "DISPLAY": display},
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0.0
    for line in out.splitlines():
        for token in line.split():
            if "*" not in token:
                continue
            try:
                return float(token.rstrip("*+"))
            except ValueError:
                continue
    return 0.0


# CPU ラスタライザの名前。glxinfo のレンダラ文字列にこれが出たら GPU ではない
SOFTWARE_RENDERERS = ("llvmpipe", "softpipe", "swrast", "zink")


def _display_renderer(display: str) -> str:
    """そのディスプレイの GL レンダラ名。取得不能なら空文字。"""
    try:
        out = subprocess.run(
            ["glxinfo", "-B"], env={**os.environ, "DISPLAY": display},
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        if "renderer string" in line.lower():
            return line.split(":", 1)[-1].strip()
    return ""


def _is_software_renderer(renderer: str) -> bool:
    low = renderer.lower()
    return any(name in low for name in SOFTWARE_RENDERERS)


# Unity Hub の実行ファイルの置き場。ここを前方一致で見るので、配信用の
# Unity Editor（UNITY_EXE = ~/Unity/Hub/Editor/<版>/Editor/Unity）には当たらない
UNITY_HUB_PREFIX = "/opt/unityhub/"


def close_unity_hub(grace_sec: float = 5.0) -> int:
    """起動しっぱなしの Unity Hub を閉じる。閉じた数を返す。

    Hub は配信にも収録にも要らない。run_live.sh が起こすのは Editor の実体
    （UNITY_EXE）で、Hub 経由ではない。にもかかわらず Electron + Chromium の
    プロセス群が常駐して RAM を掴み続ける。

    2026-09-09 の配信では開いたままだった。この日はホストの commit が
    RAM+swap の 110% に達し、swap への書き出しが 1320ページ/秒まで跳ねて
    OBS の送出が止まり、YouTube に配信を切られている。README のメモリの節が
    「配信中は Unity Hub と chrome を落としておくこと」と書いていたが、
    人の手に任せている限り忘れる。ここで機械的に落とす。

    **Editor を巻き添えにしないこと。** 収録側の shorts/core.py が
    `pkill -9 -f "Unity -projectPath"` で Unity を無差別に殺して事故に
    なった前例がある（run_live.sh の flock の説明を参照）。ここは
    /opt/unityhub/ で始まるコマンドラインだけを見る。
    """
    pids = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (proc / "cmdline").read_bytes().split(b"\0")[0].decode()
        except (OSError, UnicodeDecodeError):
            continue          # 見ている間に消えたプロセス
        if cmdline.startswith(UNITY_HUB_PREFIX):
            try:
                pids.append(int(proc.name))
            except ValueError:
                continue
    if not pids:
        return 0

    print(f"[Unity] Unity Hub が起動しています。閉じます（{len(pids)}プロセス）")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    # Electron は親を落とすと子も畳まれる。少し待ってから残りを見る
    deadline = time.time() + grace_sec
    while time.time() < deadline:
        alive = [pid for pid in pids if Path(f"/proc/{pid}").exists()]
        if not alive:
            return len(pids)
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    print("[Unity] SIGTERM で閉じなかったぶんを強制終了しました")
    return len(pids)


class UnityLive:
    """配信中ずっと生きている Unity プロセス。"""

    def __init__(self, log_path=None):
        self.xvfb = None
        self.proc = None
        self.display = None
        self._display_num = None
        self.log_path = Path(log_path) if log_path else Path("/tmp/bottan-live-unity.log")
        self._start_count = 0

    def _next_log_path(self) -> Path:
        """再起動で最初のクラッシュログを上書きしないログ名を返す。"""
        self._start_count += 1
        if self._start_count == 1:
            return self.log_path
        return self.log_path.with_name(
            f"{self.log_path.stem}_restart{self._start_count - 1}{self.log_path.suffix}")

    def start(self, ready_timeout: float = 300.0) -> dict:
        """Unity を起動し、LiveController が応答するまで待つ。"""
        current_log_path = self._next_log_path()
        env = os.environ.copy()
        env.pop("XAUTHORITY", None)          # 仮想ディスプレイには不要

        # **配信用ディスプレイには表示クロックが無い（NoScanout）。**
        # vblank を待たせると、GPU が描けていても待ちっぱなしで 1fps に見える。
        # 8/31 の a109cff はこの 1fps を「NoScanout では描けない」と読んで
        # ダミーEDIDの実スキャンアウトへ切り替えたが、それはデスクトップの
        # Xorg と DRM master を奪い合う構成で、9/10 と 9/11 の配信事故を生んだ。
        # スキャンアウトを持たないまま vsync を切るほうが、GPU も1枚で足りる。
        # 詳細は setup/xorg-bottan-live.conf の Screen セクション。
        env["__GL_SYNC_TO_VBLANK"] = "0"

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
            # 表示クロックはログに残すだけで、門にはしない。
            # NoScanout の :99 には現在モードが無いのが正常で、ここは 0.00Hz に
            # なる。そもそも xrandr が返すのはモードの公称値で、絵が出ている
            # 保証にならない（_display_refresh_hz の docstring 参照）。
            # 実際に描けているかは live 遷移前の measure_fps と LIVE_MIN_FPS、
            # および setup/reset_display.sh の glxgears 実測で見る。
            refresh = _display_refresh_hz(LIVE_DISPLAY)
            # **ソケットがあるだけでは GPU の Xorg とは限らない。**
            # :99 はこのサービスの予約番号だが、番号を予約する仕組みは無い。
            # 2026-09-10 22:39 には Orca IDE が
            # `Xvfb :99 -screen 0 1280x1024x24` を起こして先に取っており、
            # glxgears は 5694fps（llvmpipe = CPU 描画）を返していた。
            # このまま配信すると URP + VRM は 1.7fps しか出ない。
            renderer = _display_renderer(LIVE_DISPLAY)
            if _is_software_renderer(renderer):
                raise RuntimeError(
                    f"ディスプレイ {LIVE_DISPLAY} が CPU 描画です（{renderer}）。"
                    f"GPU の Xorg ではなく Xvfb などが先に {LIVE_DISPLAY} を"
                    f"取っています。`fuser -v /tmp/.X11-unix/X"
                    f"{LIVE_DISPLAY.lstrip(':')}` で掴んでいるプロセスを確かめ、"
                    f"それを止めてから "
                    f"`sudo systemctl restart bottan-live-xorg` してください"
                    f"（配信用の Xorg はデスクトップのセッションが GPU を"
                    f"握っていると起動できません。その場合は再起動が要ります）")
            self.display = LIVE_DISPLAY
            self._cleanup_stale(remove_x_locks=False)
            print(f"[Unity] 既存ディスプレイを使います: "
                  f"DISPLAY={self.display} refresh={refresh:.2f}Hz "
                  f"renderer={renderer or '不明'}")
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
            "-logFile", str(current_log_path),
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
        # 生成モーションの見た目。録画パイプラインと同じ値を渡す。
        # 渡さないと VrmaMotionPlayer は既定値（平滑化なし・手のポーズを上書き・
        # 正面固定）で動き、素の ARDY 出力がそのまま出てカクついて見える
        cmd += vrma_unity_args()
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
                    f"ログ: {current_log_path}"
                )
            try:
                st = unity_client.status()
                print(f"[Unity] 準備完了: {st}")
                return st
            except unity_client.UnityError:
                time.sleep(2.0)

        self.stop()
        raise RuntimeError(f"{ready_timeout:.0f}秒待っても LiveController が応答しません。"
                           f"ログ: {current_log_path}")

    def measure_fps(self, probe_sec: float = 12.0) -> float:
        """しばらく /status を叩いて、観測できた最大の fps を返す。0 なら測れていない。

        **最大を採ること。** LiveController の fps は1秒窓の実測値で、
        モーションの .vrma をメインスレッドでパースした直後などは一時的に
        落ちる。平均や最小を見ると健全なホストでも閾値を割ってしまう。
        ここで見分けたいのは「たまに落ちる」ではなく「そもそも出ていない」で、
        絵が出ていないホストでは最大でも 2fps に届かない（2026-09-10 の実測）。

        窓が閉じるまで fps は 0 のままなので、起動直後の1サンプルでは判定
        できない。probe_sec のあいだ何度も見る。
        """
        best = 0.0
        deadline = time.time() + probe_sec
        while time.time() < deadline:
            try:
                best = max(best, float(unity_client.status().get("fps", 0.0)))
            except (unity_client.UnityError, TypeError, ValueError):
                pass
            time.sleep(1.0)
        return best

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
