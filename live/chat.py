"""YouTube Live のチャットを読む。

liveChatMessages.list をポーリングする。レスポンスの pollingIntervalMillis を
必ず守ること。無視して短い間隔で叩くとクォータを使い切り、配信の途中から
コメントが一切読めなくなる。

読んだコメントは優先度つきで並べる。1時間で来る量に対して返事できる数は
限られるので、拾う順番が配信の印象を決める。
"""

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from config import COMMENT_USER_COOLDOWN_SEC, DRY_RUN, FAKE_COMMENTS
import safety
import subtitle
from youtube_auth import get_client


@dataclass
class Comment:
    author: str
    channel_id: str
    text: str
    is_super_chat: bool = False
    is_member: bool = False
    is_owner: bool = False
    received_at: float = field(default_factory=time.monotonic)

    @property
    def priority(self) -> int:
        """小さいほど先に返事する。"""
        if self.is_super_chat:
            return 0
        if self.is_member or self.is_owner:
            return 1
        return 2


class CommentQueue:
    """返事待ちのコメント。優先度順に取り出す。

    同一ユーザーの連投は間引く。1人が喋り続けると他の視聴者の
    コメントが永久に読まれなくなる。
    """

    def __init__(self, cooldown_sec: float = None, maxlen: int = 200):
        self._items: list = []
        self._lock = threading.Lock()
        self._last_replied = {}
        self._cooldown = COMMENT_USER_COOLDOWN_SEC if cooldown_sec is None else cooldown_sec
        self._maxlen = maxlen
        self.seen_authors = set()
        self.total_received = 0

    def push(self, comment: Comment) -> bool:
        with self._lock:
            self._items.append(comment)
            self.total_received += 1
            # 溢れたら古い低優先度のものから捨てる。配信は待ってくれない
            if len(self._items) > self._maxlen:
                self._items.sort(key=lambda c: (c.priority, c.received_at))
                self._items = self._items[:self._maxlen]
            return True

    def _next_index(self, now: float):
        """返事してよいコメントの位置。無ければ None。呼ぶ側でロックすること。"""
        self._items.sort(key=lambda c: (c.priority, c.received_at))
        for i, c in enumerate(self._items):
            last = self._last_replied.get(c.channel_id)
            # スパチャはクールダウンを無視する（対価を払っている）
            if last is not None and not c.is_super_chat \
                    and now - last < self._cooldown:
                continue
            return i
        return None

    def pop(self) -> Comment:
        """次に返事すべきコメントを1件取り出す。無ければ None。"""
        now = time.monotonic()
        with self._lock:
            i = self._next_index(now)
            if i is None:
                return None
            c = self._items.pop(i)
            self._last_replied[c.channel_id] = now
            return c

    def has_pending(self) -> bool:
        """いま返事できるコメントが待っているか。

        フリートークを途中で切り上げるかの判断に使う。単に空でないかを見ると、
        クールダウン中のコメントしか無いときに毎回切り上げてしまう。
        """
        with self._lock:
            return self._next_index(time.monotonic()) is not None

    def is_first_time(self, channel_id: str) -> bool:
        return channel_id not in self.seen_authors

    def mark_seen(self, channel_id: str) -> None:
        self.seen_authors.add(channel_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class ChatPoller:
    """ライブチャットを別スレッドで読み続ける。"""

    def __init__(self, live_chat_id: str, queue: CommentQueue):
        self.live_chat_id = live_chat_id
        self.queue = queue
        self.recent = deque(maxlen=20)      # コメント欄の表示用
        self.dropped = 0
        self._thread = None
        self._stop = threading.Event()
        self._page_token = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="chat")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        interval = 5.0
        while not self._stop.is_set():
            try:
                interval = self._poll_once()
            except Exception as e:
                # チャットが読めなくても配信は続ける。フリートークで場は持つ
                print(f"[chat] 取得に失敗（配信は継続します）: {e}")
                interval = 10.0
            self._stop.wait(interval)

    def _poll_once(self) -> float:
        """1回ぶん読んで、次までの待ち時間[秒]を返す。"""
        yt = get_client()
        res = yt.liveChatMessages().list(
            liveChatId=self.live_chat_id,
            part="snippet,authorDetails",
            pageToken=self._page_token,
            maxResults=200,
        ).execute()

        self._page_token = res.get("nextPageToken")

        for item in res.get("items", []):
            self._accept(item)

        # サーバが指定した間隔を守る。短く叩くとクォータを焼き切る
        return max(1.0, res.get("pollingIntervalMillis", 5000) / 1000.0)

    def _accept(self, item: dict) -> None:
        snippet = item.get("snippet", {})
        author = item.get("authorDetails", {})

        # テキストメッセージとスパチャだけ扱う。参加通知などは無視
        kind = snippet.get("type")
        if kind not in ("textMessageEvent", "superChatEvent", "superStickerEvent"):
            return

        raw = (snippet.get("displayMessage")
               or snippet.get("superChatDetails", {}).get("userComment")
               or "")
        ok, text, why = safety.sanitize_comment(raw)
        name = author.get("displayName", "")
        if not ok:
            self.dropped += 1
            print(f"[chat] 除外({why}): {name}: {raw[:40]}")
            return

        comment = Comment(
            author=name,
            channel_id=author.get("channelId", ""),
            text=text,
            is_super_chat=kind in ("superChatEvent", "superStickerEvent"),
            is_member=bool(author.get("isChatSponsor")),
            is_owner=bool(author.get("isChatOwner")),
        )
        self.recent.append({"author": comment.author, "text": comment.text})
        self.queue.push(comment)
        # コメント欄はここで書く。配信ループの雑務（数秒おき）に任せると、
        # 視聴者から見て「自分のコメントが画面に出るまで」がそのぶん遅れる
        try:
            subtitle.write_comments(list(self.recent))
        except Exception:
            pass


class FakeChatPoller:
    """DRY_RUN 用。JSON から偽コメントを一定間隔で流し込む。

    形式: [{"author": "suibari", "text": "こんばんは", "delay": 3}, ...]
    delay は前のコメントからの秒数。省略すると5秒。
    """

    def __init__(self, queue: CommentQueue, path: str = None):
        self.queue = queue
        self.recent = deque(maxlen=20)
        self.dropped = 0
        self.path = path or FAKE_COMMENTS
        self._thread = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="fakechat")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        if not self.path or not Path(self.path).exists():
            print(f"[chat] 偽コメントのファイルがありません: {self.path}")
            return
        items = json.loads(Path(self.path).read_text(encoding="utf-8"))
        for item in items:
            if self._stop.wait(float(item.get("delay", 5))):
                return
            ok, text, why = safety.sanitize_comment(item.get("text", ""))
            if not ok:
                self.dropped += 1
                print(f"[chat] 除外({why}): {item.get('text', '')[:40]}")
                continue
            c = Comment(
                author=item.get("author", "テスト視聴者"),
                channel_id=item.get("channel_id", item.get("author", "test")),
                text=text,
                is_super_chat=bool(item.get("super_chat")),
            )
            self.recent.append({"author": c.author, "text": c.text})
            self.queue.push(c)
            print(f"[chat] 偽コメント投入: {c.author}: {c.text}")


def make_poller(live_chat_id: str, queue: CommentQueue):
    """DRY_RUN なら偽コメント、そうでなければ本物のチャットを読む。"""
    if DRY_RUN or not live_chat_id:
        return FakeChatPoller(queue)
    return ChatPoller(live_chat_id, queue)
