"""フリートークの話題が配信中に重複しないこと。

同じ話を二度するのが一番つまらない。以前は rag / nagi / previous_live にしか
重複防止が無く、mood は常に activities[0]、ask は固定文1つだったので、
配信中に何度も同じ話が出ていた。

Bluesky の枠があることも見る。botたんのホームは Nagi だが Bluesky は毎日通う
出張先で、片方しか話題に出ないと「そちらだけが居場所」であるかのように喋る。
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
config_stub.BOT_MEMORY_QUERY_MAX_CHARS = 500


class DisabledClient:
    """RAG は使わない。SQL 由来の話題だけを見たいテスト用。"""
    enabled = False

    def search(self, *_args, **_kwargs):
        return []

    def record_usage(self, *_args, **_kwargs):
        return True


def load_filler():
    memory_stub = types.ModuleType("memory")
    bot_client_stub = types.ModuleType("bot_memory_client")
    bot_client_stub.BotMemoryClient = DisabledClient
    return load_with_stubs(
        "filler_topics_test_module",
        LIVE / "filler.py",
        {"config": config_stub, "memory": memory_stub,
         "bot_memory_client": bot_client_stub},
    )


filler = load_filler()


MEMORY = {
    "activities": [{"mood": "散歩していた"}, {"mood": "本を読んでいた"}],
    "nagi_posts": [{"post_text": "Nagiの投稿A"}, {"post_text": "Nagiの投稿B"}],
    "bsky_posts": [{"post_text": "Blueskyの投稿A"}, {"post_text": "Blueskyの投稿B"}],
    "latest_short": {"title": "ショートのタイトル"},
    "previous_live": [{"comment": "前回のコメントA"}],
}


def planner_with_memory(mem=None):
    planner = filler.FillerPlanner(memory_client=DisabledClient())
    planner._cache = dict(MEMORY if mem is None else mem)
    return planner


class TopicRotatorTest(unittest.TestCase):
    def test_the_same_kind_never_comes_out_twice_in_a_row(self):
        """袋の切れ目で同じものが続くと、連続で同じ種類の話になる。"""
        rotator = filler.TopicRotator(["a", "b", "c"])
        previous = None
        for _ in range(300):
            current = rotator.next()
            self.assertNotEqual(current, previous)
            previous = current

    def test_a_single_kind_still_works(self):
        rotator = filler.TopicRotator(["only"])
        self.assertEqual(rotator.next(), "only")
        self.assertEqual(rotator.next(), "only")


class TopicUniquenessTest(unittest.TestCase):
    def test_a_topic_is_never_offered_twice_in_one_stream(self):
        """在庫を使い切るまでは、同じ話題が二度出ない。

        在庫 = hobby 11 + ask 6 + mood 2 + nagi 2 + bsky 2 + short 1 + prev 1。
        使い切る前にリセットが入ると（＝抽選で拾えなかっただけなのに畳むと）
        ここで重複が出る。
        """
        expected = (len(filler._HOBBY_TOPICS) + len(filler._ASK_TOPICS)
                    + len(MEMORY["activities"]) + len(MEMORY["nagi_posts"])
                    + len(MEMORY["bsky_posts"]) + 1 + len(MEMORY["previous_live"]))
        planner = planner_with_memory()
        seen = []
        for _ in range(expected):
            seen.append(planner.next_topic()["key"])
        self.assertEqual(len(set(seen)), expected, "同じ話題が二度出ている")

    def test_mood_walks_past_the_row_it_already_used(self):
        """以前は activities[0] 固定で、同じ行動の話を何度もしていた。"""
        planner = planner_with_memory()
        first = planner._build("mood", planner.cache)
        second = planner._build("mood", planner.cache)
        third = planner._build("mood", planner.cache)
        self.assertIn("散歩していた", first["hint"])
        self.assertIn("本を読んでいた", second["hint"])
        self.assertIsNone(third)

    def test_short_and_ask_are_offered_only_once(self):
        planner = planner_with_memory()
        self.assertIsNotNone(planner._build("short", planner.cache))
        self.assertIsNone(planner._build("short", planner.cache))

        asked = set()
        for _ in range(len(filler._ASK_TOPICS)):
            topic = planner._build("ask", planner.cache)
            self.assertIsNotNone(topic)
            self.assertNotIn(topic["key"], asked)
            asked.add(topic["key"])
        self.assertIsNone(planner._build("ask", planner.cache))

    def test_whitespace_only_differences_still_count_as_the_same_topic(self):
        """本文の完全一致で見ていると、空白差だけで同じ話がすり抜ける。"""
        planner = planner_with_memory({
            "nagi_posts": [{"post_text": "おなじ 投稿"},
                           {"post_text": "おなじ　投稿"}],
        })
        self.assertIsNotNone(planner._build("nagi", planner.cache))
        self.assertIsNone(planner._build("nagi", planner.cache))

    def test_running_out_resets_and_keeps_talking(self):
        """ネタ切れで黙るくらいなら、二周目に入るほうがよい。"""
        planner = planner_with_memory({})       # SQL 由来の話題は無し
        for _ in range(len(filler._HOBBY_TOPICS) + len(filler._ASK_TOPICS) + 5):
            topic = planner.next_topic()
            self.assertTrue(topic["hint"])


class BlueskyTopicTest(unittest.TestCase):
    def test_bluesky_has_its_own_slot(self):
        self.assertIn("bsky", filler._KINDS)
        self.assertIn("nagi", filler._KINDS)

    def test_bluesky_posts_are_offered_like_nagi_posts(self):
        planner = planner_with_memory()
        topic = planner._build("bsky", planner.cache)
        self.assertIn("Bluesky", topic["hint"])
        self.assertIn("Blueskyの投稿A", topic["hint"])
        # 投稿者の名前は渡さない（nagi 側と同じ約束）
        self.assertIn("投稿した人の名前は出さないこと", topic["hint"])

    def test_bluesky_posts_are_not_reused(self):
        planner = planner_with_memory()
        self.assertIsNotNone(planner._build("bsky", planner.cache))
        self.assertIsNotNone(planner._build("bsky", planner.cache))
        self.assertIsNone(planner._build("bsky", planner.cache))

    def test_nagi_and_bluesky_do_not_share_a_key(self):
        """同じ本文が両方のSNSに流れていても、別の話題として数える。"""
        planner = planner_with_memory({
            "nagi_posts": [{"post_text": "同じ本文"}],
            "bsky_posts": [{"post_text": "同じ本文"}],
        })
        self.assertIsNotNone(planner._build("nagi", planner.cache))
        self.assertIsNotNone(planner._build("bsky", planner.cache))


class SameStoryInOtherWordingTest(unittest.TestCase):
    """言い回しが違うだけの同じネタを、二度出さないこと。

    2026-08-24 の配信で「FLASHBULB」の話が繰り返された。biorhythm_history には
    同じ曲を聴いている行が文面違いで2行入っており、頭60文字のキーでは
    別物として通っていた。以下は実データそのまま。
    """

    MOOD_A = ("全肯定botたんは、お風呂で「FLASHBULB」を聴きながら、"
              "今日の出来事をモルフォと一緒に思い出しているところです。")
    MOOD_B = ("全肯定botたんは、お風呂で「FLASHBULB」を聴きながら、"
              "今日の出来事を思い出しているよ。モルフォは家で静かに待っているみたい。")

    def test_the_same_song_is_not_offered_twice(self):
        planner = planner_with_memory({
            "activities": [{"mood": self.MOOD_A}, {"mood": self.MOOD_B}],
        })
        self.assertIsNotNone(planner._build("mood", planner.cache))
        self.assertIsNone(planner._build("mood", planner.cache),
                          "同じ曲の話が言い回し違いで二度出ている")

    def test_unrelated_activities_are_still_offered(self):
        """過剰に弾いていないこと。無関係な行動は two とも出る。"""
        planner = planner_with_memory({
            "activities": [{"mood": "公園を散歩していた"}, {"mood": "本を読んでいた"}],
        })
        self.assertIsNotNone(planner._build("mood", planner.cache))
        self.assertIsNotNone(planner._build("mood", planner.cache))
        self.assertIsNone(planner._build("mood", planner.cache))

    def test_a_topic_from_one_kind_blocks_the_same_story_in_another(self):
        """種別をまたいでも同じネタなら弾く。RAG と mood に同じ話が入っている。"""
        planner = planner_with_memory({
            "activities": [{"mood": self.MOOD_A}],
            "previous_live": [{"comment": "FLASHBULBって曲、いいよね"}],
        })
        self.assertIsNotNone(planner._build("mood", planner.cache))
        self.assertIsNone(planner._build("previous_live", planner.cache))

    def test_resetting_forgets_the_words_too(self):
        """畳んだら語も忘れる。忘れないと2周目に何も出せなくなる。"""
        planner = planner_with_memory({
            "activities": [{"mood": self.MOOD_A}, {"mood": self.MOOD_B}],
        })
        self.assertIsNotNone(planner._build("mood", planner.cache))
        planner.reset_used()
        self.assertIsNotNone(planner._build("mood", planner.cache))

    def test_the_last_resort_topic_still_has_a_key(self):
        """在庫が空でも黙らない。話題の種別をログに出すので key も要る。

        _pick_topic が二度とも空を返す＝DBが全滅した状態を作る。
        この救済パスだけ key を持っていなかった。
        """
        planner = planner_with_memory({})
        planner._pick_topic = lambda: None
        topic = planner.next_topic()
        self.assertIn("key", topic)
        self.assertEqual(topic["key"][0], "hobby")
        self.assertTrue(topic["hint"])


if __name__ == "__main__":
    unittest.main()
