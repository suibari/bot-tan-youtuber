"""発話の出どころ（自分の体験か、他人の話か）を落とさないこと。

2026-08-25 の夜版Shortsで、botたんが Nagi の他人の投稿（「資格取得の為に
警察署に行ってきました」）を自分の体験として喋った。配信側にも同じ構造の穴があり、

  - `_memory_block` が「## あなたが覚えていること」ひとつの下に、
    自分の行動（今日やってたこと）と他人の投稿（Nagiで見かけた投稿）を
    同じ箇条書きで並べていた。投稿本文は一人称で書かれているので、
    行頭ラベルより本文の一人称が強く効く
  - `_filler_topic_block` の未知 source のフォールバックが「みんなとの思い出」で、
    他人の投稿が botたん自身の思い出として読める文言だった
  - `_history_said` が solo turn を「（フリートーク）」にしてしまい、お題と出どころが
    履歴から消えていた。発話自体は role=assistant で6ターン残るので、
    以後「自分が言ったこと」としてだけ扱われる
  - `build_followup_prompt` には記憶ブロックが付かないので、テーマの出どころが
    無いと「## さっき話していたこと」が丸ごと自分の体験として読まれる

ここはその回帰テスト。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"


def load_with_stubs(name, path, stubs):
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


config_stub = types.ModuleType("config")
config_stub.AFFIRMATIVE_BOT_DIR = str(ROOT / "does-not-exist")

persona = load_with_stubs(
    "persona_origin_test_module", LIVE / "persona.py", {"config": config_stub})
conversation = load_with_stubs(
    "conversation_origin_test_module", LIVE / "conversation.py", {"config": config_stub})

POLICE_POST = ("警察署に行ってきました🫡 資格取得の為に用事がありましてね "
               "決して悪い事したわけじゃないのにめっちゃ緊張した💧")
MEMORY = {
    "activities": [{"mood": "モルフォと散歩した"}],
    "latest_short": {"title": "今日の全肯定"},
    "nagi_posts": [{"post_text": POLICE_POST}],
    "bsky_posts": [{"post_text": "夜勤明けの朝マックなう🍔"}],
    "previous_live": [{"comment": "眠れない夜が続いてて"}],
}
BOT = {"now": "21:30", "status": "Relax", "energy": 50.0}


class MemoryBlockSeparationTest(unittest.TestCase):
    def test_my_experience_and_other_peoples_posts_are_in_separate_blocks(self):
        block = persona._memory_block(MEMORY)
        self.assertIn(persona._MEMORY_MINE_HEADING, block)
        self.assertIn(persona._MEMORY_THEIRS_HEADING, block)
        # 他人の投稿は必ず「他人の話」の見出しより後ろに来る
        self.assertLess(block.index("モルフォと散歩した"),
                        block.index(persona._MEMORY_THEIRS_HEADING))
        self.assertLess(block.index(persona._MEMORY_THEIRS_HEADING),
                        block.index(POLICE_POST))

    def test_the_theirs_heading_says_it_is_not_my_experience(self):
        self.assertIn("あなたの体験ではない", persona._MEMORY_THEIRS_HEADING)

    def test_only_the_block_that_has_rows_is_shown(self):
        mine_only = persona._memory_block({"activities": [{"mood": "散歩した"}]})
        self.assertIn(persona._MEMORY_MINE_HEADING, mine_only)
        self.assertNotIn(persona._MEMORY_THEIRS_HEADING, mine_only)

        theirs_only = persona._memory_block({"nagi_posts": [{"post_text": POLICE_POST}]})
        self.assertNotIn(persona._MEMORY_MINE_HEADING, theirs_only)
        self.assertIn(persona._MEMORY_THEIRS_HEADING, theirs_only)

        self.assertEqual(persona._memory_block({}), "")
        self.assertEqual(persona._memory_block(None), "")


class RagSourceLabelTest(unittest.TestCase):
    def test_an_unknown_source_is_not_called_a_memory_of_mine(self):
        block = persona._filler_topic_block({"rag_source": "???", "rag_content": "なにか"})
        self.assertNotIn("思い出", block)
        self.assertIn("あなた自身の体験としては話さないこと", block)

    def test_other_peoples_sources_say_so(self):
        # biorhythm 以外はすべて他人の言葉。ラベルだけが両者を分ける手がかりになる
        for source, label in persona._RAG_SOURCE_LABELS.items():
            if source == "biorhythm":
                self.assertIn("あなた自身", label)
            elif source == "youtube_live_comment":
                self.assertIn("視聴者", label)
            else:
                self.assertIn("他の人", label)


class TopicOriginTest(unittest.TestCase):
    def test_posts_from_other_people_get_a_label(self):
        self.assertEqual(persona.topic_origin({"key": ("nagi", "x")}),
                         "SNSのNagiで見かけた他の人の投稿")
        self.assertEqual(persona.topic_origin({"key": ("bsky", "x")}),
                         "Blueskyで見かけた他の人の投稿")
        self.assertIn("視聴者", persona.topic_origin({"key": ("previous_live", "x")}))

    def test_my_own_topics_get_no_label(self):
        for kind in ("mood", "hobby", "ask", "short"):
            self.assertEqual(persona.topic_origin({"key": (kind, "x")}), "")

    def test_rag_is_decided_by_its_source_not_its_kind(self):
        def rag(source):
            return {"key": ("rag", 1), "hint": {"rag_source": source, "rag_content": "x"}}
        self.assertEqual(persona.topic_origin(rag("biorhythm")), "")
        self.assertEqual(persona.topic_origin(rag("nagi_affirmed_post")),
                         "SNSのNagiで見かけた他の人の投稿")
        # 「〜に反応して話したこと」の形に入るので、説明つきの長いラベルではなく
        # 短い語を返す
        self.assertEqual(persona.topic_origin(rag("???")), persona._ORIGIN_UNKNOWN)
        self.assertNotIn("こと）", persona.topic_origin(rag("???")))

    def test_junk_input_is_harmless(self):
        self.assertEqual(persona.topic_origin(None), "")
        self.assertEqual(persona.topic_origin("ただの文字列"), "")
        self.assertEqual(persona.topic_origin({}), "")


class HistoryOriginTest(unittest.TestCase):
    def test_a_solo_turn_keeps_where_the_topic_came_from(self):
        log = conversation.ConversationLog()
        log.add_solo_turn("フリートーク", "そういう投稿を見かけたよ",
                          origin="SNSのNagiで見かけた他の人の投稿")
        history = persona.build_history(log.recent_turns())
        self.assertIn("SNSのNagiで見かけた他の人の投稿", history[0]["user"])

    def test_my_own_talk_stays_plain(self):
        log = conversation.ConversationLog()
        log.add_solo_turn("フリートーク", "散歩してきたんだ")
        self.assertEqual(persona.build_history(log.recent_turns())[0]["user"],
                         "（フリートーク）")

    def test_comment_turns_are_unchanged(self):
        log = conversation.ConversationLog()
        log.add_comment_turn("ch1", "suibari", "やっほー", "やっほー！")
        self.assertIn("送り主：suibari", persona.build_history(log.recent_turns())[0]["user"])


class FollowupOriginTest(unittest.TestCase):
    def test_the_followup_prompt_says_the_theme_is_someone_elses(self):
        prompt = persona.build_followup_prompt(
            "警察署に行った人の話", None, BOT,
            origin="SNSのNagiで見かけた他の人の投稿")
        self.assertIn("SNSのNagiで見かけた他の人の投稿", prompt)
        self.assertIn("あなた自身の体験ではありません", prompt)

    def test_without_an_origin_nothing_extra_is_added(self):
        prompt = persona.build_followup_prompt("散歩の話", None, BOT)
        self.assertIn("さっき話していたこと", prompt)
        self.assertNotIn("あなた自身の体験ではありません", prompt)


class PersonaRuleTest(unittest.TestCase):
    def test_the_character_prompt_forbids_claiming_other_peoples_experiences(self):
        rules = persona.build_character_prompt()
        self.assertIn("すべて「他の人の話」です", rules)
        self.assertIn("自分がやったことへ言い換えるのは禁止", rules)


if __name__ == "__main__":
    unittest.main()
