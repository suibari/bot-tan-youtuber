"""YouTube Live のチャットを読む。

liveChatMessages.streamList の持続接続で新着を受け取る。
切断時は最後のnextPageTokenから再開し、重複した返答を防ぐ。

読んだコメントは優先度つきで並べる。1時間で来る量に対して返事できる数は
限られるので、拾う順番が配信の印象を決める。
"""

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from config import (COMMENT_MAX_AGE_SEC, COMMENT_USER_COOLDOWN_SEC,
                    DRY_RUN, FAKE_COMMENTS)
import safety
import subtitle


@dataclass
class Comment:
    author: str
    channel_id: str
    text: str
    message_id: str = ""
    is_super_chat: bool = False
    is_member: bool = False
    is_owner: bool = False
    received_at: float = field(default_factory=time.monotonic)
    delivery_delay_sec: float | None = None  # YouTube投稿時刻→受信。ローカル時計との差も含む

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

    def __init__(self, cooldown_sec: float = None, maxlen: int = 200,
                 max_age_sec: float = None):
        self._items: list = []
        self._lock = threading.Lock()
        self._last_replied = {}
        self._cooldown = COMMENT_USER_COOLDOWN_SEC if cooldown_sec is None else cooldown_sec
        self._maxlen = maxlen
        self._max_age = COMMENT_MAX_AGE_SEC if max_age_sec is None else max_age_sec
        self.seen_authors = set()
        self.total_received = 0
        # 返事せずに捨てた件数。溢れも古すぎたぶんもここに積む。
        # 数えていないと「コメント欄には出たのに返事が来ない」に気づけない
        self.dropped = 0

    def push(self, comment: Comment) -> bool:
        with self._lock:
            self._items.append(comment)
            self.total_received += 1
            # 溢れたら「優先度が低く、かつ古いもの」から捨てる。配信は待ってくれない。
            #
            # 残すのはソート後の先頭 maxlen 件なので、**受信時刻は降順**にすること。
            # 昇順にすると末尾＝「一般視聴者のいちばん新しいコメント」が捨てられ、
            # 意図と正反対になる（実際そうなっていた）。取り出しは古い順なので、
            # ここで新しいものを残すのと矛盾しない
            if len(self._items) > self._maxlen:
                self._items.sort(key=lambda c: (c.priority, -c.received_at))
                lost = len(self._items) - self._maxlen
                self._items = self._items[:self._maxlen]
                self.dropped += lost
                print(f"[chat] 返事待ちが{self._maxlen}件を超えたので"
                      f"{lost}件捨てました（累計{self.dropped}件）")
            return True

    def _evict_stale(self, now: float) -> None:
        """古すぎるコメントを捨てる。呼ぶ側でロックすること。

        取り出しは同じ優先度なら古い順なので、滞留したまま放っておくと
        「5分前のコメントにいま返事する」状態になる。視聴者から見た遅れは
        待ち行列の長さそのものなので、頭を打たせる。

        **スパチャ・メンバー・オーナーは古くても捨てない。**
        """
        if self._max_age <= 0:
            return
        fresh = [c for c in self._items
                 if c.priority < 2 or now - c.received_at <= self._max_age]
        lost = len(self._items) - len(fresh)
        if lost:
            self._items = fresh
            self.dropped += lost
            print(f"[chat] {self._max_age:.0f}秒以上待たせたコメントを{lost}件"
                  f"捨てました（累計{self.dropped}件）")

    def stats(self) -> dict:
        """待ち行列の様子。配信中の観測用（_housekeeping から呼ぶ）。"""
        now = time.monotonic()
        with self._lock:
            return {
                "waiting": len(self._items),
                "oldest": max((now - c.received_at for c in self._items),
                              default=0.0),
                "received": self.total_received,
                "dropped": self.dropped,
            }

    def _next_index(self, now: float):
        """返事してよいコメントの位置。無ければ None。呼ぶ側でロックすること。

        クールダウンは「1人が喋り続けると他の視聴者のコメントが読まれなくなる」のを
        防ぐためのもの。**他に返事できる人が居ないなら、その心配は無いので無視する。**
        待っているのがその人だけなのに60秒黙ってフリートークへ流れると、
        1対1で話しかけられている状況で会話が続かない。
        """
        self._evict_stale(now)
        self._items.sort(key=lambda c: (c.priority, c.received_at))
        held = None
        for i, c in enumerate(self._items):
            last = self._last_replied.get(c.channel_id)
            # スパチャはクールダウンを無視する（対価を払っている）
            if last is not None and not c.is_super_chat \
                    and now - last < self._cooldown:
                if held is None:
                    held = i
                continue
            return i
        return held

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

    def __init__(self, live_chat_id: str, queue: CommentQueue,
                 on_comment: Optional[Callable[[Comment], None]] = None,
                 on_delete: Optional[Callable[[str], None]] = None):
        self.live_chat_id = live_chat_id
        self.queue = queue
        self.recent = deque(maxlen=20)      # コメント欄の表示用
        self.dropped = 0
        self._thread = None
        self._stop = threading.Event()
        self._page_token = None
        self._on_comment = on_comment
        self._on_delete = on_delete
        self._transport = None
        self._seen_ids = set()
        self._seen_order = deque()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="chat")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._transport is not None:
            self._transport.close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    FAIL_INTERVAL_MIN = 1.0
    FAIL_INTERVAL_MAX = 120.0
    # これだけ続いた接続の切断は一時的なものとして待ち時間を戻す。コメントが
    # 来ない配信でも接続自体は続くので、受信件数ではなく接続時間で判断する。
    # 短くしすぎると、1件だけ通してすぐ制限される状態でリセットを繰り返す。
    STABLE_CONNECTION_SEC = 60.0
    SEEN_LIMIT = 10000

    def _run(self) -> None:
        try:
            from common.youtube_stream import LiveChatStream
            transport = LiveChatStream()
            self._transport = transport
            self._receive(transport)
        except Exception as e:
            print(f"[chat] streamListを開始できません（配信は継続します）: {type(e).__name__}")
        finally:
            if self._transport is not None:
                self._transport.close()

    def _receive(self, transport) -> None:
        import grpc

        fail_interval = self.FAIL_INTERVAL_MIN
        while not self._stop.is_set():
            started = time.monotonic()
            interval = fail_interval
            try:
                print(f"[chat] streamList接続（{'再開' if self._page_token else '初回'}）")
                for res in transport.responses(self.live_chat_id, self._page_token):
                    if self._stop.is_set():
                        return
                    if not self._handle_response(res):
                        print("[chat] ライブチャット終了")
                        return
                if self._connection_was_stable(started):
                    fail_interval = self.FAIL_INTERVAL_MIN
                interval = max(1.0, fail_interval)
            except Exception as e:
                if self._stop.is_set():
                    return
                code = e.code() if isinstance(e, grpc.RpcError) else None
                if code in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.FAILED_PRECONDITION,
                            grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.INVALID_ARGUMENT):
                    print(f"[chat] streamList停止: {code.name}（配信は継続します）")
                    return
                if code == grpc.StatusCode.UNAUTHENTICATED:
                    transport.force_refresh = True
                # 無言のまま接続上限で切れた場合も、待たせずに繋ぎ直す。
                if self._connection_was_stable(started):
                    fail_interval = self.FAIL_INTERVAL_MIN
                interval = fail_interval
                if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    # 日次枯渇とは断定しない。配信中にも回復した実績がある。
                    interval = max(30.0, interval)
                print(f"[chat] streamList切断: {code.name if code else type(e).__name__} "
                      f"（配信は継続します。次は{interval:.0f}秒後）")
            fail_interval = min(self.FAIL_INTERVAL_MAX, interval * 2)
            self._stop.wait(interval)

    def _connection_was_stable(self, started: float) -> bool:
        return time.monotonic() - started >= self.STABLE_CONNECTION_SEC

    def _handle_response(self, res: dict) -> bool:
        for item in res.get("items", []):
            if self._stop.is_set():
                return False
            if item.get("snippet", {}).get("type") == "chatEndedEvent":
                return False
            self._accept(item)
        # 全件処理できてから進める。処理途中の例外では再接続時に取り直す。
        self._page_token = res.get("nextPageToken") or self._page_token
        return not bool(res.get("offlineAt"))

    def _accept(self, item: dict) -> None:
        snippet = item.get("snippet", {})
        author = item.get("authorDetails", {})

        kind = snippet.get("type")
        if kind in ("messageDeletedEvent", "messageRetractedEvent", "tombstone"):
            deleted_id = (snippet.get("messageDeletedDetails", {}).get("deletedMessageId")
                          or snippet.get("messageRetractedDetails", {}).get("retractedMessageId")
                          or (item.get("id", "") if kind == "tombstone" else ""))
            if deleted_id and self._on_delete:
                try:
                    self._on_delete(deleted_id)
                except Exception as e:
                    print(f"[chat] 削除同期を依頼できません（配信は継続します）: {e}")
            return

        # テキストメッセージとスパチャだけ扱う。参加通知などは無視
        if kind not in ("textMessageEvent", "superChatEvent", "superStickerEvent"):
            return

        message_id = item.get("id", "")
        if message_id and message_id in self._seen_ids:
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

        delay = None
        try:
            published = datetime.fromisoformat(snippet.get("publishedAt", "").replace("Z", "+00:00"))
            if published.tzinfo is not None:
                delay = max(0.0, (datetime.now(timezone.utc) - published).total_seconds())
        except (TypeError, ValueError, AttributeError):
            pass

        comment = Comment(
            author=name,
            channel_id=author.get("channelId", ""),
            text=text,
            message_id=message_id,
            is_super_chat=kind in ("superChatEvent", "superStickerEvent"),
            is_member=bool(author.get("isChatSponsor")),
            is_owner=bool(author.get("isChatOwner")),
            delivery_delay_sec=delay,
        )
        self.recent.append({"author": comment.author, "text": comment.text})
        self.queue.push(comment)
        if message_id:
            self._seen_ids.add(message_id)
            self._seen_order.append(message_id)
            if len(self._seen_order) > self.SEEN_LIMIT:
                self._seen_ids.discard(self._seen_order.popleft())
        if delay is not None:
            print(f"[chat] 投稿→受信 {delay:.2f}秒")
        if self._on_comment:
            try:
                self._on_comment(comment)
            except Exception as e:
                print(f"[chat] コメントを記憶キューへ渡せません（配信は継続します）: {e}")
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

    def __init__(self, queue: CommentQueue, path: str = None,
                 on_comment: Optional[Callable[[Comment], None]] = None):
        self.queue = queue
        self.recent = deque(maxlen=20)
        self.dropped = 0
        self.path = path or FAKE_COMMENTS
        self._thread = None
        self._stop = threading.Event()
        self._on_comment = on_comment

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
        for index, item in enumerate(items):
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
                message_id=item.get("message_id", f"fake:{index}"),
                is_super_chat=bool(item.get("super_chat")),
            )
            self.recent.append({"author": c.author, "text": c.text})
            self.queue.push(c)
            if self._on_comment:
                try:
                    self._on_comment(c)
                except Exception as e:
                    print(f"[chat] 偽コメントを記憶キューへ渡せません: {e}")
            print(f"[chat] 偽コメント投入: {c.author}: {c.text}")


def make_poller(live_chat_id: str, queue: CommentQueue,
                on_comment: Optional[Callable[[Comment], None]] = None,
                on_delete: Optional[Callable[[str], None]] = None):
    """DRY_RUN なら偽コメント、そうでなければ本物のチャットを読む。"""
    if DRY_RUN or not live_chat_id:
        return FakeChatPoller(queue, on_comment=on_comment)
    return ChatPoller(live_chat_id, queue, on_comment=on_comment, on_delete=on_delete)
