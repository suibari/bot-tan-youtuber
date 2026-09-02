"""OBS の制御（obs-websocket v5）。

やることは RTMP の設定と配信の開始・停止だけ。画面レイアウト（Unity の
ウィンドウキャプチャ・字幕・コメント欄・energy メーター・BGM）は
OBS のシーンとして事前に手で組んでおく。毎回プログラムから組み立てる必要はない。
"""

import configparser
import os
import re
import shlex
import signal
import threading
from pathlib import Path
import socket
import subprocess
import time

import logging
import obsws_python as obs
from obsws_python.error import OBSSDKRequestError

# obsws_python は失敗のたびに logger.exception でトレースバックを吐く。
# ハンドラを1つ足しておかないと logging の lastResort が stderr へ流し、
# connect() のリトライ中に同じトレースがログを埋めて肝心の原因が見えなくなる。
# 失敗の中身はこちらの ObsError のメッセージに載せている。
logging.getLogger("obsws_python").addHandler(logging.NullHandler())

from config import (OBS_HOST, OBS_PORT, OBS_PASSWORD, OBS_LAUNCH, OBS_COMMAND,
                    OBS_COLLECTION, OBS_PROFILE, LIVE_DISPLAY,
                    STREAM_VIDEO_KBPS, STREAM_AUDIO_KBPS,
                    SUBTITLE_JA, SUBTITLE_EN)

# 字幕の送信を何回続けて失敗したらファイル経路へ戻すか。
# 一時的な取りこぼしで戻すと、以後ずっと1秒遅れの字幕になってしまう
_TEXT_FAIL_LIMIT = 3


# xwininfo -root -tree の1行:
#   0x600267 "Unity - bottan-video - SampleScene ...": ("Unity" "Unity")  2560x1440+0+0  +0+0
_WINDOW_RE = re.compile(
    r'^\s*(0x[0-9a-fA-F]+)\s+"(.+)":\s+\("[^"]*"\s+"([^"]*)"\)\s+(\d+)x(\d+)')


def find_window(title_part: str, display: str = LIVE_DISPLAY,
                min_width: int = 640) -> tuple[int, str, str] | None:
    """display 上でタイトルに title_part を含む一番大きい窓を返す。

    Unity は 10x10 のダミー窓も作るので、小さすぎるものは除く。
    """
    try:
        out = subprocess.run(["xwininfo", "-root", "-tree"],
                             env={**os.environ, "DISPLAY": display},
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[OBS] {display} の窓を数えられません: {e}")
        return None

    best = None
    for line in out.splitlines():
        m = _WINDOW_RE.match(line)
        if not m:
            continue
        wid, title, cls, w, h = m.groups()
        if title_part not in title or int(w) < min_width:
            continue
        area = int(w) * int(h)
        if best is None or area > best[0]:
            best = (area, int(wid, 16), title, cls)
    return best[1:] if best else None


class ObsError(RuntimeError):
    pass


# 配信プロファイルに入っていてほしい値。OBS は「シンプル」出力の
# エンコーダをプロファイル読み込み時に一度だけ決めるので、起動前に
# ini へ書いておかないと効かない（websocket で書いても次回起動から）
def _wanted_profile_settings() -> dict:
    return {
        "Output": {"Mode": "Simple"},
        "SimpleOutput": {
            "StreamEncoder": "x264",
            "VBitrate": str(STREAM_VIDEO_KBPS),
            "ABitrate": str(STREAM_AUDIO_KBPS),
            "Preset": "veryfast",
            "UseAdvanced": "false",
        },
    }


def _profile_dir() -> Path | None:
    """OBS_PROFILE という名前のプロファイルのディレクトリを探す。

    ディレクトリ名は OBS が名前から記号を落として作るので（bottan-live →
    bottanlive）、名前で決め打ちできない。中の basic.ini を読んで判別する。
    """
    for base in (Path.home() / ".var/app/com.obsproject.Studio/config/obs-studio",
                 Path.home() / ".config/obs-studio"):
        root = base / "basic" / "profiles"
        if not root.is_dir():
            continue
        for d in root.iterdir():
            ini = d / "basic.ini"
            if not ini.is_file():
                continue
            cfg = configparser.ConfigParser()
            cfg.optionxform = str
            try:
                cfg.read(ini, encoding="utf-8")
            except Exception:
                continue
            if cfg.get("General", "Name", fallback=None) == OBS_PROFILE:
                return d
    return None


def ensure_profile_settings() -> bool:
    """プロファイルの ini を望む値に揃える。書き換えたら True。

    既定の NVENC は GPU の VRAM に CUDA コンテキストを作る。この機械の GPU は
    8GB しかなく ARDY・Unity・VOICEVOX で埋まるため、実際に

      cuda_ctx_init: ... CUDA_ERROR_OUT_OF_MEMORY (2): out of memory
      Already in non_texture encoder, can't fall back further!
      Stream output type 'rtmp_output' failed to start!

    で配信開始に失敗し、YouTube 側が「配信を待っています」のまま止まった。
    OBS はここからソフトウェアエンコーダへ落ちてくれないので x264 を明示する。
    """
    d = _profile_dir()
    if d is None:
        print(f"[OBS] プロファイル「{OBS_PROFILE}」が見つかりません"
              f"（初回はシーン構築時に作られます）")
        return False

    ini = d / "basic.ini"
    cfg = configparser.ConfigParser()
    cfg.optionxform = str          # OBS のキーは大文字小文字を保つ
    cfg.read(ini, encoding="utf-8")

    changed = []
    for section, values in _wanted_profile_settings().items():
        if not cfg.has_section(section):
            cfg.add_section(section)
        for key, value in values.items():
            if cfg.get(section, key, fallback=None) != value:
                cfg.set(section, key, value)
                changed.append(f"{section}/{key}={value}")
    if not changed:
        return False

    with ini.open("w", encoding="utf-8") as f:
        cfg.write(f, space_around_delimiters=False)
    print(f"[OBS] プロファイルを更新しました: {', '.join(changed)}")
    return True


def _last_output_error() -> str:
    """OBS のログから出力開始まわりのエラーを拾う。原因を即座に出すため。"""
    logs = sorted(
        (Path.home() / ".var/app/com.obsproject.Studio/config/obs-studio/logs").glob("*.txt"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        logs = sorted((Path.home() / ".config/obs-studio/logs").glob("*.txt"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return "  （OBS のログが見つかりません）"
    lines = logs[0].read_text(errors="replace").splitlines()[-60:]
    keys = ("failed to start", "out of memory", "fall back", "cuda_ctx_init",
            "Connection to", "is offline", "Failed to connect")
    hits = [l for l in lines if any(k in l for k in keys)]
    return "\n".join("  " + h for h in hits) if hits else f"  （{logs[0]} を確認してください）"


def _clear_crash_sentinel() -> None:
    """前回の異常終了の痕跡を消す。

    OBS は起動時に設定ディレクトリの `.sentinel/run_<uuid>` を作り、正常終了で
    消す。残っていると「OBS Studioのクラッシュが検出されました。セーフモードで
    起動しますか？」のモーダルが出て起動が止まる。:99 には押す人が居ないので
    無人配信がそこで詰む（`--safe-mode` はあるが WebSocket まで無効になるので
    使えない）。OBS が動いていないときだけ消す。
    """
    if _port_open(OBS_HOST, OBS_PORT):
        return
    for base in (Path.home() / ".var/app/com.obsproject.Studio/config/obs-studio",
                 Path.home() / ".config/obs-studio"):
        sentinel = base / ".sentinel"
        if not sentinel.is_dir():
            continue
        stale = list(sentinel.glob("run_*"))
        for f in stale:
            try:
                f.unlink()
            except OSError:
                pass
        if stale:
            print(f"[OBS] 前回の異常終了の痕跡を {len(stale)} 件消しました（{sentinel}）")


def _signal_running(sig: int) -> None:
    r = subprocess.run(["pgrep", "-x", "obs"], capture_output=True, text=True)
    for pid in (int(p) for p in r.stdout.split()):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _wait_port_closed(timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_open(OBS_HOST, OBS_PORT):
            return True
        time.sleep(0.5)
    return False


def _stop_running(timeout: float = 30.0) -> bool:
    """動いている OBS を行儀よく止める（シーンを保存させるため）。

    止めきれたら True。時間内に消えなければ False。ポートが開いたままだと
    launch() が「もう起動している」と誤認して、これから死ぬ OBS に繋ぎにいく。
    """
    _signal_running(signal.SIGTERM)
    if _wait_port_closed(timeout):
        return True
    print("[OBS] 終了を待てませんでした")
    return False


def _kill_running(timeout: float = 10.0) -> None:
    """SIGTERM で消えなかった OBS を落とす。シーンの保存は諦める。"""
    print("[OBS] 応答しないので強制終了します")
    _signal_running(signal.SIGKILL)
    if not _wait_port_closed(timeout):
        raise ObsError(
            f"動いている OBS を止められませんでした（{OBS_HOST}:{OBS_PORT} が開いたまま）。"
            f"手で終了させてください"
        )


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def launch(timeout: float = 90.0) -> bool:
    """OBS を LIVE_DISPLAY 上で起動する。すでに動いていれば何もしない。

    X11 のウィンドウキャプチャは「OBS 自身が接続している X ディスプレイ」の
    窓しか列挙できない。デスクトップ(:0)で起動した OBS からは :99 の Unity は
    ソース一覧に絶対に出てこないので、OBS も :99 側で起動する必要がある。

    さらに :99 には EWMH 準拠のウィンドウマネージャ（openbox、
    bottan-live-wm.service）が居ること。居ないと OBS が起動時に
    「XComposite capture disabled.」と言ってプラグインごと読み込みを諦め、
    「ウィンドウキャプチャ」という選択肢自体が消える。
    """
    if _port_open(OBS_HOST, OBS_PORT):
        if ensure_profile_settings():
            # エンコーダの選択はプロファイル読み込み時に一度だけ決まるので、
            # 書き換えたなら起動し直さないと反映されない
            print("[OBS] 設定を反映するため起動し直します")
            if not _stop_running():
                _kill_running()
        else:
            print(f"[OBS] すでに起動しています（{OBS_HOST}:{OBS_PORT}）")
            return False
    else:
        ensure_profile_settings()
    if not OBS_LAUNCH:
        raise ObsError(
            f"OBS が起動していません（{OBS_HOST}:{OBS_PORT}）。"
            f"OBS_LAUNCH=true にするか、DISPLAY={LIVE_DISPLAY} で手動起動してください"
        )

    env = dict(os.environ)
    env["DISPLAY"] = LIVE_DISPLAY
    # Wayland が見えていると OBS がそちらのプラットフォームを選び、
    # X11 用のキャプチャプラグインが読まれない
    env.pop("WAYLAND_DISPLAY", None)
    cmd = shlex.split(OBS_COMMAND)
    if cmd[0] == "flatpak":
        # flatpak のサンドボックスには外の DISPLAY が引き継がれないので明示する
        cmd.insert(2, f"--env=DISPLAY={LIVE_DISPLAY}")
    # 起動時に出うるダイアログを抑止する。:99 には誰も居ないので、
    # ダイアログが出た時点で無人配信は詰む
    cmd.append("--disable-missing-files-check")
    # 配信用のシーンコレクションとプロファイルを名指しで開く。デスクトップ側で
    # OBS を触って別のコレクションを開いたままにしていても、配信は必ず
    # こちらで始まる
    cmd += ["--collection", OBS_COLLECTION, "--profile", OBS_PROFILE]
    _clear_crash_sentinel()

    print(f"[OBS] {LIVE_DISPLAY} で起動します: {' '.join(cmd)}")
    subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(OBS_HOST, OBS_PORT):
            print(f"[OBS] 起動しました（{time.time() - (deadline - timeout):.0f}秒）")
            return True
        time.sleep(1.0)
    raise ObsError(
        f"OBS が {timeout:.0f} 秒で起動しませんでした。"
        f"[ツール]→[WebSocket サーバー設定]が有効か確認してください"
    )


class Obs:
    def __init__(self):
        self.client = None
        # 字幕のテキストソース名。bind_text_sources() が埋める
        self._text_sources = {}
        # ReqClient はスレッドセーフではない。字幕は subtitle スレッドから
        # 呼ばれるので、配信ループ側の呼び出しと直列化する
        self._lock = threading.Lock()
        self._text_fails = 0

    def connect(self, timeout: float = 10.0, ready_timeout: float = 60.0) -> "Obs":
        """OBS がリクエストを受けられるようになるまで待ってから繋ぐ。

        obs-websocket のポートは OBS 本体の初期化が終わる前から開いていて、
        Hello/Identify のハンドシェイクまでは通ってしまう。その状態でリクエストを
        投げると 207（NotReady, "OBS is not ready to perform the request"）が返る。
        launch() はポートが開いた時点で戻ってくるので、ready はここで待つ。
        シーンコレクションの読み込みが重い日は数十秒かかる。
        """
        deadline = time.time() + ready_timeout
        waited = False
        last = None
        while True:
            try:
                self.client = obs.ReqClient(
                    host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=timeout
                )
                version = self.client.get_version()
                break
            except OBSSDKRequestError as e:
                if e.code != 207:
                    # 待っても直らない類い（パスワード違い・未知のリクエスト等）
                    raise ObsError(f"OBS がリクエストを拒否しました: {e}") from e
                last = e
            except Exception as e:
                last = e
            # 張りかけの接続を捨てる。捨てないとリトライのたびにソケットが残る
            self.disconnect()
            if time.time() >= deadline:
                if isinstance(last, OBSSDKRequestError):
                    raise ObsError(
                        f"OBS の初期化が {ready_timeout:.0f} 秒で終わりませんでした: {last}。"
                        f"シーンコレクション {OBS_COLLECTION} の読み込みが重い可能性があります"
                    ) from last
                raise ObsError(
                    f"OBS に接続できません（{OBS_HOST}:{OBS_PORT}）: {last}。"
                    f"OBS の[ツール]→[WebSocket サーバー設定]で有効化してください"
                ) from last
            if not waited:
                # 207 とは限らない（起動直後は接続拒否のこともある）ので理由を添える
                print(f"[OBS] まだ受け付けません。待ちます: {last}")
                waited = True
            time.sleep(1.0)
        print(f"[OBS] 接続しました: OBS {version.obs_version} / "
              f"websocket {version.obs_web_socket_version}")
        self.check_encoder()
        return self

    def check_encoder(self) -> None:
        """いま走っている OBS のエンコーダ設定を確かめる。

        値そのものは `ensure_profile_settings()` が起動前に ini へ書く。
        ここで websocket 越しに書いても**次回起動まで効かない**（OBS は
        「シンプル」出力のエンコーダをプロファイル読み込み時に一度だけ
        決めるため）ので、ずれていたら知らせるだけにする。
        """
        try:
            enc = self.client.get_profile_parameter(
                "SimpleOutput", "StreamEncoder").parameter_value
            vb = self.client.get_profile_parameter(
                "SimpleOutput", "VBitrate").parameter_value
        except Exception as e:
            print(f"[OBS] エンコーダ設定を読めませんでした: {e}")
            return

        if enc != "x264":
            print(f"[OBS] 警告: エンコーダが {enc} です。GPU の VRAM が足りないと"
                  f"配信開始に失敗します（OBS を起動し直すと x264 になります）")
        else:
            print(f"[OBS] エンコーダ: x264 / 映像 {vb}kbps")

    # ── 字幕 ────────────────────────────────────────

    def _scene_items(self) -> list:
        """いま映っているシーンのソース一覧。読めなければ空。"""
        try:
            scene = self.client.get_current_program_scene()
            scene_name = (getattr(scene, "current_program_scene_name", None)
                          or scene.scene_name)
            return self.client.get_scene_item_list(scene_name).scene_items
        except Exception as e:
            print(f"[OBS] シーンを読めません（続行します）: {e}")
            return []

    def bind_text_sources(self) -> bool:
        """字幕のテキストソースを見つけ、ファイル読み込みをやめさせる。

        OBS の `text_ft2_source_v2` は `from_file` のとき**約1秒に1回しか**
        ファイルを見に行かない（間隔を指定するプロパティは存在しない）。
        発話より字幕が遅れる最大の原因がこれなので、websocket で直接
        流し込む経路に切り替える。シーンを手で直す必要はない。

        ソース名は決め打ちにせず、`text_file` が SUBTITLE_JA / SUBTITLE_EN を
        指しているものを探す（`tools/build_scene.py` が付ける「字幕(日)」等の
        名前に依存すると、手で組んだシーンで動かなくなる）。
        """
        want = {str(Path(SUBTITLE_JA).resolve()): "ja",
                str(Path(SUBTITLE_EN).resolve()): "en"}
        found = {}
        for item in self._scene_items():
            source = item["sourceName"]
            try:
                settings = self.client.get_input_settings(source)
                if not str(settings.input_kind).startswith("text_"):
                    continue
                path = (settings.input_settings or {}).get("text_file", "")
                if not path:
                    continue
                key = want.get(str(Path(path).resolve()))
                if key is None:
                    continue
                new = dict(settings.input_settings)
                new["from_file"] = False
                new["text"] = ""
                self.client.set_input_settings(source, new, overlay=True)
                found[key] = source
            except Exception as e:
                print(f"[OBS] 「{source}」を字幕として使えません（続行します）: {e}")

        self._text_sources = found
        self._text_fails = 0
        if found:
            print(f"[OBS] 字幕を websocket 経由に切り替えました: "
                  f"{', '.join(f'{k}={v}' for k, v in found.items())}")
        else:
            print("[OBS] 字幕のテキストソースが見つかりません。"
                  "ファイル経由のまま進みます（最大1秒遅れます）")
        return bool(found)

    def set_text(self, ja: str = "", en: str = "") -> None:
        """字幕を即時に差し替える。subtitle.set_sink() から呼ばれる。

        失敗が続いたらファイル読み込みへ戻す。配信中に websocket が切れても
        字幕が出続けるようにするため（subtitle.show はファイルも必ず書く）。
        """
        if not self._text_sources or self.client is None:
            return
        pairs = [(self._text_sources.get("ja"), ja),
                 (self._text_sources.get("en"), en)]
        try:
            with self._lock:
                for source, text in pairs:
                    if source:
                        self.client.set_input_settings(
                            source, {"text": text or ""}, overlay=True)
            self._text_fails = 0
        except Exception as e:
            self._text_fails += 1
            print(f"[OBS] 字幕を送れません（{self._text_fails}回目）: {e}")
            if self._text_fails >= _TEXT_FAIL_LIMIT:
                self.restore_text_files()

    def restore_text_files(self) -> None:
        """字幕をファイル読み込みへ戻す。websocket を諦めるときに呼ぶ。"""
        sources = list(self._text_sources.items())
        self._text_sources = {}
        paths = {"ja": str(SUBTITLE_JA), "en": str(SUBTITLE_EN)}
        for key, source in sources:
            try:
                with self._lock:
                    self.client.set_input_settings(
                        source, {"from_file": True, "text_file": paths[key]},
                        overlay=True)
            except Exception as e:
                print(f"[OBS] 「{source}」をファイル読み込みに戻せません: {e}")
        if sources:
            print("[OBS] 字幕をファイル読み込みに戻しました（最大1秒遅れます）")

    def bind_window_capture(self, unity_project: str) -> bool:
        """ウィンドウキャプチャの参照先を、いま動いている Unity の窓に合わせる。

        OBS はシーンに保存された「ウィンドウID + タイトル + クラス」で窓を探す。
        ID は Unity を起動し直すたびに変わり、タイトルにはプロジェクト名が入る。
        dev で組んだシーンをそのまま本番で使うと `bottan-video-dev` を探し続けて
        見つからず、配信が真っ暗のまま流れる（2026-08-21 の配信がこれ）。
        毎回ここで今の窓に張り替えるので、シーンを手で直す必要はない。
        """
        hint = f"Unity - {Path(unity_project).name} - "
        found = find_window(hint)
        if found is None:
            print(f"[OBS] {LIVE_DISPLAY} に「{hint}」の窓が見つかりません。"
                  f"シーンの設定のまま進みます")
            return False
        wid, title, cls = found
        want = f"{wid}\r\n{title}\r\n{cls}"

        items = self._scene_items()
        bound = 0
        for item in items:
            source = item["sourceName"]
            try:
                settings = self.client.get_input_settings(source)
                if settings.input_kind != "xcomposite_input":
                    continue
                current = settings.input_settings.get("capture_window", "")
                if current == want:
                    print(f"[OBS] ウィンドウキャプチャ「{source}」はすでに今の Unity を見ています")
                    bound += 1
                    continue
                new = dict(settings.input_settings)
                new["capture_window"] = want
                self.client.set_input_settings(source, new, overlay=True)
                print(f"[OBS] ウィンドウキャプチャ「{source}」を今の Unity に張り替えました: "
                      f"{title[:60]}")
                bound += 1
            except Exception as e:
                print(f"[OBS] 「{source}」を張り替えられません（続行します）: {e}")
        if not bound:
            print("[OBS] シーンにウィンドウキャプチャがありません")
        return bound > 0

    def set_stream_target(self, ingestion_address: str, stream_key: str) -> None:
        """YouTube から受け取った RTMP の宛先を設定する。"""
        self.client.set_stream_service_settings(
            ss_type="rtmp_custom",
            ss_settings={
                "server": ingestion_address,
                "key": stream_key,
                "use_auth": False,
            },
        )
        print(f"[OBS] 配信先を設定しました: {ingestion_address}")

    def start_stream(self, timeout: float = 15.0) -> None:
        status = self.client.get_stream_status()
        if status.output_active:
            print("[OBS] すでに配信中です")
            return
        self.client.start_stream()

        # StartStream はリクエストが通っただけで成功を返す。エンコーダの
        # 初期化に失敗しても例外にならないので、実際に走り出したか確かめる。
        # ここを見ていなかったせいで、YouTube 側の 180秒 タイムアウトまで
        # 原因が分からなかった
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.client.get_stream_status().output_active:
                print("[OBS] 配信を開始しました")
                return
            time.sleep(0.5)

        raise ObsError(
            f"OBS が配信を開始できませんでした（{timeout:.0f}秒）。"
            f"多くはエンコーダの初期化失敗です:\n"
            + _last_output_error()
        )

    def stop_stream(self) -> None:
        try:
            status = self.client.get_stream_status()
            if not status.output_active:
                return
            self.client.stop_stream()
            print("[OBS] 配信を停止しました")
        except Exception as e:
            print(f"[OBS] 停止に失敗: {e}")

    def stream_stats(self) -> dict:
        """配信の健全性。フレームドロップが増えていたら回線か描画が苦しい。"""
        try:
            s = self.client.get_stream_status()
            return {
                "active": s.output_active,
                "duration_sec": s.output_duration / 1000.0,
                "skipped_frames": s.output_skipped_frames,
                "total_frames": s.output_total_frames,
                "congestion": s.output_congestion,
            }
        except Exception as e:
            return {"error": str(e)}

    def disconnect(self) -> None:
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
