"""Discord への通知。

無人で動くので、起きたことは全部ここへ流す。
通知に失敗しても処理は止めない（通知のために配信や投稿を落としては本末転倒）。
"""

import os
import time

import requests

from common.env import env_float

# 同じ文面の警告を続けて流さない間隔[秒]。0 以下で無効。
# 2026-09-09 の配信では Unity の `/speak に接続できません` が23分で6回、
# VOICEVOX の合成失敗が2回流れた。ホストが詰まっている間は同じ警告が延々と
# 続くので、1本目だけ出せば足りる。`error()` と Shorts 側の通知は対象外
# （見落としたくないため、抑制するのは warn だけにする）
NOTIFY_COOLDOWN_SEC = env_float("NOTIFY_COOLDOWN_SEC", 300.0)

# 文面 -> 最後に送った時刻（time.monotonic）
_warned_at = {}


def _webhook() -> str:
    # import 時ではなく呼び出し時に読む。テストで差し替えられるようにするため
    return os.getenv("DISCORD_WEBHOOK_URL", "")


def send(message: str) -> bool:
    url = _webhook()
    if not url:
        print(f"[通知] {message}")
        return False
    try:
        res = requests.post(url, json={"content": message}, timeout=10)
        res.raise_for_status()
        return True
    except Exception as e:
        print(f"[通知] 送信に失敗（無視します）: {e}")
        return False


# ── ライブ配信 ────────────────────────────────────────

def live_started(url: str, title: str) -> None:
    send(f"🔴 **botたんライブ配信を開始しました**\n{title}\n{url}")


def live_ended(url: str, comments: int, duration_min: float) -> None:
    send(f"⚫ **配信を終了しました**（{duration_min:.0f}分 / コメント {comments}件）\n{url}")


def error(where: str, detail: str) -> None:
    send(f"⚠️ **{where}** でエラーが起きました\n```\n{detail[:1500]}\n```")


def warn(message: str) -> None:
    """警告。同じ文面が短時間に続くときは1本目だけ流す。"""
    if NOTIFY_COOLDOWN_SEC > 0:
        now = time.monotonic()
        last = _warned_at.get(message)
        if last is not None and now - last < NOTIFY_COOLDOWN_SEC:
            print(f"[通知] 抑制（{now - last:.0f}秒前に同じ警告）: {message}")
            return
        _warned_at[message] = now
        # 溜め込まない。配信1回ぶんで足りるので、増えすぎたら古い順に捨てる
        if len(_warned_at) > 200:
            for key in sorted(_warned_at, key=_warned_at.get)[:100]:
                del _warned_at[key]
    send(f"⚠️ {message}")


# ── Shorts 投稿 ───────────────────────────────────────

def youtube_uploaded(yt_url: str, title: str) -> None:
    """Shorts の投稿完了。文面は統合前の youtube.notify_discord と同一。"""
    if not _webhook():
        return          # 統合前はここで黙って返っていた。挙動を変えない
    if send(f"✅ YouTube投稿完了！\n**{title}**\n{yt_url}"):
        print("[Discord] 通知送信完了")
