"""Unity 常駐（LiveController）への HTTP クライアント。

対応するサーバ実装は bottan-video-dev/Assets/Scripts/LiveController.cs。
Unity は -liveMode 付きで起動しておくこと。
"""

import time

import requests

from config import UNITY_URL

_TIMEOUT = (3, 10)


class UnityError(RuntimeError):
    pass


def _post(path: str, payload: dict) -> dict:
    try:
        res = requests.post(f"{UNITY_URL}{path}", json=payload, timeout=_TIMEOUT)
    except Exception as e:
        raise UnityError(f"{path} に接続できません: {e}") from e
    if res.status_code >= 400:
        raise UnityError(f"{path} が {res.status_code}: {res.text}")
    return res.json() if res.content else {}


def speak(wav_path: str, valence: float = 0.0, arousal: float = 0.0) -> None:
    """WAV を発話キューに積む。すぐには再生されないことがある（順番待ち）。"""
    _post("/speak", {"wav_path": str(wav_path), "valence": valence, "arousal": arousal})


def motion(vrma_path: str) -> None:
    """モーションをいまから再生する。再生中のものは 0.5 秒でクロスフェードして終わる。"""
    _post("/motion", {"vrma_path": str(vrma_path)})


def emotion(valence: float, arousal: float) -> None:
    """表情だけ変える。発話とは独立に効く。"""
    _post("/emotion", {"valence": valence, "arousal": arousal})


def status() -> dict:
    try:
        res = requests.get(f"{UNITY_URL}/status", timeout=_TIMEOUT)
    except Exception as e:
        raise UnityError(f"/status に接続できません: {e}") from e
    res.raise_for_status()
    return res.json()


def wait_ready(timeout: float = 300.0, interval: float = 2.0) -> dict:
    """LiveController が待ち受けを始めるまで待つ。

    Unity は Editor の起動 → シーンロード → Play 開始 を経てからサーバを立てるので、
    プロセスを起こした直後は必ず接続に失敗する。
    """
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            return status()
        except UnityError as e:
            last_error = e
            time.sleep(interval)
    raise UnityError(f"{timeout:.0f}秒待っても応答がありません: {last_error}")


def wait_until_idle(timeout: float = 120.0, interval: float = 0.2) -> bool:
    """発話キューが空になり、いま喋っている分も終わるまで待つ。

    タイムアウトしても例外にはしない。配信中に待ち続けて進行が止まるより、
    先へ進んで次の発話を積むほうが被害が小さい。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            st = status()
        except UnityError:
            return False
        if not st.get("speaking") and st.get("speak_queue", 0) == 0:
            return True
        time.sleep(interval)
    return False
