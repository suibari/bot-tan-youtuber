"""CommentQueue の取り出し順とクールダウン。

クールダウンは「1人が喋り続けて他の視聴者が読まれなくなる」のを防ぐためのもので、
待っているのがその人だけなら無視する。ここが壊れると、1対1で話しかけられている
状況で60秒黙ってフリートークへ流れる（会話が続かない）。
"""

import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "live"))

# chat.py は import 時に config / YouTube / DB を掴むので、最小スタブへ差し替える
config_stub = types.ModuleType("config")
config_stub.COMMENT_USER_COOLDOWN_SEC = 60
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


if __name__ == "__main__":
    unittest.main()
