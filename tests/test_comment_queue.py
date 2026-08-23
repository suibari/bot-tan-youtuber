"""CommentQueue の取り出し順とクールダウン。

クールダウンは「1人が喋り続けて他の視聴者が読まれなくなる」のを防ぐためのもので、
待っているのがその人だけなら無視する。ここが壊れると、1対1で話しかけられている
状況で60秒黙ってフリートークへ流れる（会話が続かない）。
"""

import sys
import time
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "live"))

# chat.py は import 時に config / YouTube / DB を掴むので、最小スタブへ差し替える
config_stub = types.ModuleType("config")
config_stub.COMMENT_USER_COOLDOWN_SEC = 60
config_stub.COMMENT_MAX_AGE_SEC = 180
config_stub.DRY_RUN = False
config_stub.FAKE_COMMENTS = ""
sys.modules.setdefault("config", config_stub)

safety_stub = types.ModuleType("safety")
safety_stub.sanitize_comment = lambda text: (bool(text), text, "")
sys.modules.setdefault("safety", safety_stub)

subtitle_stub = types.ModuleType("subtitle")
subtitle_stub.write_comments = lambda comments: None
sys.modules.setdefault("subtitle", subtitle_stub)

youtube_auth_stub = types.ModuleType("youtube_auth")
youtube_auth_stub.get_client = lambda: None
sys.modules.setdefault("youtube_auth", youtube_auth_stub)

import chat  # noqa: E402


def comment(author, text="こんばんは", **kwargs):
    return chat.Comment(author=author, channel_id=f"channel-{author}",
                        text=text, **kwargs)


class CooldownTest(unittest.TestCase):
    def test_same_user_is_held_back_while_someone_else_waits(self):
        queue = chat.CommentQueue(cooldown_sec=60)
        queue.push(comment("A"))
        self.assertEqual(queue.pop().author, "A")
        queue.push(comment("A", "もういっこ"))
        queue.push(comment("B"))
        # Bが待っているので、Aの連投は後回しになる
        self.assertEqual(queue.pop().author, "B")

    def test_same_user_is_answered_when_nobody_else_waits(self):
        queue = chat.CommentQueue(cooldown_sec=60)
        queue.push(comment("A"))
        self.assertEqual(queue.pop().author, "A")
        queue.push(comment("A", "もういっこ"))
        # 待っているのがAだけならクールダウンを無視して会話を続ける
        popped = queue.pop()
        self.assertIsNotNone(popped)
        self.assertEqual(popped.text, "もういっこ")

    def test_held_back_user_is_answered_after_the_others(self):
        queue = chat.CommentQueue(cooldown_sec=60)
        queue.push(comment("A"))
        queue.pop()
        queue.push(comment("A", "もういっこ"))
        queue.push(comment("B"))
        self.assertEqual(queue.pop().author, "B")
        self.assertEqual(queue.pop().author, "A")

    def test_super_chat_ignores_cooldown_even_with_others_waiting(self):
        queue = chat.CommentQueue(cooldown_sec=60)
        queue.push(comment("A"))
        queue.pop()
        queue.push(comment("B"))
        queue.push(comment("A", "スパチャ", is_super_chat=True))
        self.assertEqual(queue.pop().author, "A")

    def test_has_pending_agrees_with_pop(self):
        queue = chat.CommentQueue(cooldown_sec=60)
        self.assertFalse(queue.has_pending())
        queue.push(comment("A"))
        queue.pop()
        queue.push(comment("A", "もういっこ"))
        # クールダウン中でも他に誰も居なければ返せる＝フリートークは切り上げてよい
        self.assertTrue(queue.has_pending())
        self.assertIsNotNone(queue.pop())
        self.assertFalse(queue.has_pending())

    def test_priority_still_wins_over_arrival_order(self):
        queue = chat.CommentQueue(cooldown_sec=60)
        queue.push(comment("A"))
        queue.push(comment("B", "スパチャ", is_super_chat=True))
        self.assertEqual(queue.pop().author, "B")


class OverflowTest(unittest.TestCase):
    """溢れたときに何を捨てるか。

    以前は `(priority, received_at)` 昇順に並べて先頭 maxlen 件を残していた。
    残す側を先頭から取るので、捨てられるのは末尾＝**一般視聴者のいちばん新しい
    コメント**。「古い低優先度のものから捨てる」と書いてあるのに正反対で、
    しかも件数を数えていないので溢れに気づけなかった。
    """

    def test_the_newest_comment_survives_and_the_oldest_is_dropped(self):
        queue = chat.CommentQueue(cooldown_sec=0, maxlen=3, max_age_sec=0)
        old = comment("oldest", text="いちばん古い")
        old.received_at = 100.0
        queue.push(old)
        for i in range(3):
            c = comment(f"later{i}", text=f"あと{i}")
            c.received_at = 200.0 + i
            queue.push(c)

        texts = [c.text for c in queue._items]
        self.assertNotIn("いちばん古い", texts)
        self.assertIn("あと2", texts)
        self.assertEqual(len(queue._items), 3)

    def test_dropping_is_counted(self):
        queue = chat.CommentQueue(cooldown_sec=0, maxlen=2, max_age_sec=0)
        for i in range(5):
            queue.push(comment(f"viewer{i}"))
        self.assertEqual(queue.dropped, 3)
        self.assertEqual(queue.total_received, 5)

    def test_super_chat_survives_an_overflow_of_ordinary_comments(self):
        queue = chat.CommentQueue(cooldown_sec=0, maxlen=2, max_age_sec=0)
        queue.push(comment("payer", text="スパチャ", is_super_chat=True))
        for i in range(5):
            queue.push(comment(f"viewer{i}"))
        self.assertIn("スパチャ", [c.text for c in queue._items])


class StalenessTest(unittest.TestCase):
    """古すぎるコメントは捨てる。

    取り出しは同じ優先度なら古い順なので、滞留を放っておくと「5分前のコメントに
    いま返事する」状態になる。視聴者から見た遅れが配信の後半ほど伸びていく。
    """

    def test_an_ordinary_comment_that_waited_too_long_is_skipped(self):
        queue = chat.CommentQueue(cooldown_sec=0, maxlen=200, max_age_sec=60)
        stale = comment("stale", text="ずっと前のコメント")
        stale.received_at = time.monotonic() - 600
        queue.push(stale)
        fresh = comment("fresh", text="いまのコメント")
        queue.push(fresh)

        popped = queue.pop()
        self.assertEqual(popped.text, "いまのコメント")
        self.assertIsNone(queue.pop())
        self.assertEqual(queue.dropped, 1)

    def test_super_chat_is_never_dropped_for_being_old(self):
        queue = chat.CommentQueue(cooldown_sec=0, maxlen=200, max_age_sec=60)
        old_payer = comment("payer", text="スパチャ", is_super_chat=True)
        old_payer.received_at = time.monotonic() - 600
        queue.push(old_payer)
        self.assertEqual(queue.pop().text, "スパチャ")

    def test_members_and_owners_are_never_dropped_for_being_old(self):
        queue = chat.CommentQueue(cooldown_sec=0, maxlen=200, max_age_sec=60)
        for kwargs in ({"is_member": True}, {"is_owner": True}):
            with self.subTest(**kwargs):
                queue = chat.CommentQueue(cooldown_sec=0, maxlen=200, max_age_sec=60)
                c = comment("regular", text="常連", **kwargs)
                c.received_at = time.monotonic() - 600
                queue.push(c)
                self.assertIsNotNone(queue.pop())

    def test_cutoff_can_be_switched_off(self):
        queue = chat.CommentQueue(cooldown_sec=0, maxlen=200, max_age_sec=0)
        stale = comment("stale")
        stale.received_at = time.monotonic() - 100000
        queue.push(stale)
        self.assertIsNotNone(queue.pop())

    def test_stats_reports_the_backlog(self):
        queue = chat.CommentQueue(cooldown_sec=0, maxlen=200, max_age_sec=0)
        waiting = comment("waiting")
        waiting.received_at = time.monotonic() - 30
        queue.push(waiting)
        stats = queue.stats()
        self.assertEqual(stats["waiting"], 1)
        self.assertEqual(stats["received"], 1)
        self.assertGreaterEqual(stats["oldest"], 29)


if __name__ == "__main__":
    unittest.main()
