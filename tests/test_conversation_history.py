"""配信中の会話をつなぐ経路。

- conversation.ConversationLog: 場の流れと相手ごとのやりとり
- persona.build_history: それを LLM へ渡す形にする
- common.llm.generate_json: history を messages のマルチターンにする

統合前は1コメント＝1回の独立した呼び出しで、視聴者の発言はどこにも残らなかった。
ここが壊れると「一つ前の会話を忘れている」に戻る。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"
sys.path.insert(0, str(ROOT))


def load_with_stubs(name, path, stubs):
    """スタブを差したままモジュールを1つ読み、あとで元へ戻す。

    tests/test_bot_memory_rag.py と同じやり方。live/*.py は import 時に
    本物の live/config.py（.env と DB を読む）を掴むので、discover で
    他のテストと同じプロセスに載るとスタブが食い合う。
    """
    previous = {key: sys.modules.get(key) for key in stubs}
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


# persona.py は config から原典リポジトリのパスだけを読む
config_stub = types.ModuleType("config")
config_stub.AFFIRMATIVE_BOT_DIR = ROOT.parent / "bsky-affirmative-bot"

conversation = load_with_stubs(
    "conversation_test_module", LIVE / "conversation.py", {})
persona = load_with_stubs(
    "persona_history_test_module", LIVE / "persona.py", {"config": config_stub})

from common import llm as common_llm  # noqa: E402


class ConversationLogTest(unittest.TestCase):
    def test_turns_come_back_in_order_with_solo_talk_mixed_in(self):
        log = conversation.ConversationLog(max_turns=6, per_user_turns=3)
        log.add_comment_turn("ch-1", "A", "こんばんは", "こんばんは、来てくれてうれしい")
        log.add_solo_turn("フリートーク", "今日は空がきれいだったよ")
        log.add_comment_turn("ch-2", "B", "元気？", "元気だよ")

        kinds = [turn["kind"] for turn in log.recent_turns()]
        self.assertEqual(kinds, ["comment", "フリートーク", "comment"])
        self.assertEqual(log.recent_replies(2),
                         ["今日は空がきれいだったよ", "元気だよ"])

    def test_oldest_turns_fall_off_but_the_user_index_keeps_theirs(self):
        log = conversation.ConversationLog(max_turns=2, per_user_turns=3)
        log.add_comment_turn("ch-1", "A", "仕事で失敗しちゃった", "つらかったね")
        log.add_solo_turn("フリートーク", "ひとりごと1")
        log.add_solo_turn("フリートーク", "ひとりごと2")

        # 場の流れからは押し出されている
        self.assertEqual(len(log.recent_turns()), 2)
        self.assertNotIn("つらかったね", log.recent_replies())
        # その人とのやりとりとしては残っている
        self.assertEqual(log.user_turns("ch-1")[0]["comment"], "仕事で失敗しちゃった")

    def test_user_index_is_limited_and_scoped_to_that_person(self):
        log = conversation.ConversationLog(max_turns=20, per_user_turns=2)
        for i in range(3):
            log.add_comment_turn("ch-1", "A", f"コメント{i}", f"返事{i}")
        log.add_comment_turn("ch-2", "B", "別の人", "別の返事")

        mine = log.user_turns("ch-1")
        self.assertEqual([t["comment"] for t in mine], ["コメント1", "コメント2"])
        self.assertEqual(len(log.user_turns("ch-2")), 1)
        self.assertEqual(log.user_turns(""), [])
        self.assertEqual(log.user_turns("知らない人"), [])

    def test_empty_speech_is_not_recorded(self):
        log = conversation.ConversationLog()
        log.add_comment_turn("ch-1", "A", "こんばんは", "")
        log.add_comment_turn("ch-1", "A", "", "返事だけ")
        log.add_solo_turn("フリートーク", "   ")
        self.assertEqual(log.recent_turns(), [])


class BuildHistoryTest(unittest.TestCase):
    def test_comment_turns_keep_the_author_and_solo_turns_are_labelled(self):
        log = conversation.ConversationLog()
        log.add_comment_turn("ch-1", "suibari", "こんばんは", "こんばんは！")
        log.add_solo_turn("オープニング", "今日も来てくれてありがとう")

        history = persona.build_history(log.recent_turns())
        self.assertEqual(history[0]["user"], "送り主：suibari\n内容：こんばんは")
        self.assertEqual(history[0]["assistant"], "こんばんは！")
        self.assertEqual(history[1]["user"], "（オープニング）")

    def test_empty_history_is_fine(self):
        self.assertEqual(persona.build_history([]), [])
        self.assertEqual(persona.build_history(None), [])


class CommentPromptTest(unittest.TestCase):
    def test_previous_exchange_with_the_same_person_is_shown(self):
        log = conversation.ConversationLog()
        log.add_comment_turn("ch-1", "A", "仕事で失敗しちゃった", "つらかったね")
        prompt = persona.build_comment_prompt(
            "A", "うん、まだ引きずってる", {"energy": 50},
            user_history=log.user_turns("ch-1"))
        self.assertIn("この人とさっきまで話していたこと", prompt)
        self.assertIn("仕事で失敗しちゃった", prompt)
        self.assertIn("つらかったね", prompt)

    def test_the_person_being_answered_is_named_explicitly(self):
        """会話履歴を渡すと、直前に呼んでいた別の人の名前を引きずる（実測）。

        「視聴者Aさん」への返事で「suibariさんはどう？」と聞いてしまっていた。
        """
        prompt = persona.build_comment_prompt("視聴者A", "今日の空きれいだったね",
                                              {"energy": 50})
        self.assertIn("いま返事をする相手は「視聴者A」さんです", prompt)
        self.assertIn("別の人の名前で呼びかけないこと", prompt)

    def test_without_history_the_block_is_absent(self):
        prompt = persona.build_comment_prompt("A", "こんばんは", {"energy": 50})
        self.assertNotIn("この人とさっきまで話していたこと", prompt)
        # 反転した「繰り返すな」の制約は、コメント返信からは外してある
        self.assertNotIn("同じ言い回しを繰り返さないこと", prompt)


class GenerateJsonHistoryTest(unittest.TestCase):
    """messages の並びだけを見る。LLM へは実際に投げない。"""

    class FakeResponse:
        class Choice:
            class Message:
                content = '{"ok": true}'
            message = Message()
            finish_reason = "stop"
        choices = [Choice()]

    def _messages_for(self, history):
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return self.FakeResponse()

        with patch.object(common_llm, "create", fake_create):
            data = common_llm.generate_json(
                "system", "いまのコメント", {"type": "object"},
                debug=False, history=history)
        self.assertEqual(data, {"ok": True})
        return captured["messages"]

    def test_no_history_keeps_the_two_message_request(self):
        messages = self._messages_for(None)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual(messages[1]["content"], "いまのコメント")

    def test_history_is_expanded_into_alternating_turns(self):
        messages = self._messages_for([
            {"user": "送り主：A\n内容：こんばんは", "assistant": "こんばんは！"},
            {"user": "（フリートーク）", "assistant": "ひとりごと"},
        ])
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user", "assistant", "user", "assistant", "user"])
        self.assertEqual(messages[2]["content"], "こんばんは！")
        self.assertEqual(messages[-1]["content"], "いまのコメント")

    def test_broken_turns_are_skipped_instead_of_raising(self):
        messages = self._messages_for([
            {"user": "残るほう", "assistant": "返事"},
            {"user": "片方だけ"},
            {"assistant": "片方だけ"},
            {"user": "  ", "assistant": "空白"},
            None,
            "文字列",
        ])
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user", "assistant", "user"])


if __name__ == "__main__":
    unittest.main()
