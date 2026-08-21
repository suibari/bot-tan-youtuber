"""Discord への通知。

無人で動くので、起きたことは全部ここへ流す。
通知に失敗しても処理は止めない（通知のために配信や投稿を落としては本末転倒）。
"""

import os

import requests


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
    send(f"⚠️ {message}")


# ── Shorts 投稿 ───────────────────────────────────────

def youtube_uploaded(yt_url: str, title: str) -> None:
    """Shorts の投稿完了。文面は統合前の youtube.notify_discord と同一。"""
    if not _webhook():
        return          # 統合前はここで黙って返っていた。挙動を変えない
    if send(f"✅ YouTube投稿完了！\n**{title}**\n{yt_url}"):
        print("[Discord] 通知送信完了")
