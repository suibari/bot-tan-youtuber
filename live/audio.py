"""配信の音声経路。

Unity（VOICEVOX で作った WAV を鳴らす側）の音は、OS のサウンドサーバを
経由しないと OBS に届かない。OBS の中で完結しているのは BGM
（ffmpeg_source）だけなので、BGM は聞こえるのに声だけ出ない、という
状態になる。実際に初回の配信でそうなった。

そこで配信専用の null シンクを1本作り、

    Unity ──(PULSE_SINK=bottan_live)──▶ bottan_live ──▶ bottan_live.monitor ──▶ OBS

という経路を通す。既定の出力をそのまま使わない理由は2つ:

  - PipeWire は「実デバイスが1つも無いとき」だけ auto_null を自動生成する。
    実デバイスが現れると消えるので、名前で決め打ちできない
  - 既定の出力を拾うと、ブラウザの音や通知音まで配信に乗る

このシンクは pactl のモジュールなので、ログインセッションが生きている限り
残る。二重に作らないよう、名前で存在確認してから作る。
"""

import os
import subprocess

from config import LIVE_AUDIO_SINK


class AudioError(RuntimeError):
    pass


def _pactl(*args: str) -> str:
    env = os.environ.copy()
    # systemd から起動されると XDG_RUNTIME_DIR が無いことがある。
    # 無いと pactl はユーザーのサウンドサーバを見つけられない
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    res = subprocess.run(["pactl", *args], capture_output=True, text=True, env=env)
    if res.returncode != 0:
        raise AudioError(f"pactl {' '.join(args)} が失敗しました: {res.stderr.strip()}")
    return res.stdout


def sink_exists(name: str) -> bool:
    for line in _pactl("list", "short", "sinks").splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and cols[1] == name:
            return True
    return False


def ensure_sink(name: str = None) -> str:
    """配信用の null シンクを用意し、OBS が拾うモニタ名を返す。

    すでにあれば何もしない（作り直すと Unity の接続先が切れる）。
    """
    name = name or LIVE_AUDIO_SINK
    if not sink_exists(name):
        _pactl("load-module", "module-null-sink",
               f"sink_name={name}",
               f"sink_properties=device.description={name}")
        if not sink_exists(name):
            raise AudioError(f"シンク {name} を作れませんでした")
        print(f"[音声] 配信用シンクを作りました: {name}")
    else:
        print(f"[音声] 配信用シンクはすでにあります: {name}")
    return f"{name}.monitor"


def monitor_name(name: str = None) -> str:
    return f"{name or LIVE_AUDIO_SINK}.monitor"


def unity_env(env: dict, name: str = None) -> dict:
    """Unity に渡す環境変数へ出力先を差し込む。"""
    env["PULSE_SINK"] = name or LIVE_AUDIO_SINK
    return env


def describe() -> str:
    """いま何が鳴っているか。声が出ないときの切り分け用。"""
    out = []
    for line in _pactl("list", "short", "sink-inputs").splitlines():
        if line.strip():
            out.append(line)
    return "\n".join(out) if out else "（このシンクに繋いでいるプロセスはありません）"
