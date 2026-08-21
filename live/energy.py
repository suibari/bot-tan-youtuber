"""biorhythm（energy）との連携。

フェーズ1では bsky-affirmative-bot を一切改修せず、既存の type を流用する。
apps/biorhythm_server/src/index.ts:22-53 の分岐にある type だけが有効で、
クライアントが送る amount はサーバ側で必ず無視される（実効値はサーバ定数）。

将来 live_comment type を足したら ENERGY_TYPE_COMMENT を差し替えるだけでよい。
"""

import requests

from config import BIORHYTHM_URL, ENERGY_TYPE_COMMENT
import memory

_TIMEOUT = (3, 8)

# biorhythm_server が落ちている環境では毎回同じ例外が出る。配信1時間ぶんの
# ログが埋まって他が読めなくなるので、警告は1回だけにする
_warned = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    print(message)


def add_comment_energy() -> bool:
    """コメントを1件さばいたぶんの energy を加算する。

    biorhythm_server が落ちていても配信は続ける。energy が増えないだけで
    喋れなくなるわけではないので、ここで例外を投げると損のほうが大きい。
    """
    try:
        res = requests.post(
            f"{BIORHYTHM_URL}/energy",
            json={"type": ENERGY_TYPE_COMMENT},
            timeout=_TIMEOUT,
        )
        res.raise_for_status()
        return True
    except Exception as e:
        _warn_once("add", f"[energy] 加算できません（配信は継続します。以降この警告は出しません）: {e}")
        return False


def get_energy() -> float:
    """いまの energy を 0〜100 で返す。

    biorhythm_server の /status を先に見て、落ちていたら DB の bot_state を読む。
    サーバはメモリ上の状態を持っているので本来そちらが正だが、
    配信中にサーバが落ちても energy をプロンプトに載せ続けたい。
    """
    try:
        res = requests.get(f"{BIORHYTHM_URL}/status", timeout=_TIMEOUT)
        res.raise_for_status()
        data = res.json()
        for key in ("energy", "getEnergy"):
            if key in data:
                return max(0.0, min(100.0, float(data[key])))
    except Exception as e:
        _warn_once("get", f"[energy] biorhythm_server を読めないのでDBから引きます"
                          f"（以降この警告は出しません）: {e}")

    return memory.get_biorhythm()["energy"]
