"""テキストから固有名詞を拾い、同じネタかどうかを見られること。

2026-08-24 の配信で「FLASHBULB」を聴いている mood が文面違いで2行あり、
先頭60文字のキーでは別物として通って両方お題になった。ここはその判定役。

**取りこぼしても壊れない**のが前提（呼ぶ側が従来のキーに落ちる）ので、
拾えないケースを潰すために判定を増やさないこと。逆に、拾ってはいけないものは
必ず落とすこと。抑制が効きすぎるほうが配信では目立つ。
"""

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_topics():
    """topics.py は依存ゼロ。スタブ無しで読めること自体が設計の検算になる。"""
    spec = importlib.util.spec_from_file_location(
        "topics_test_module", ROOT / "live" / "topics.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


topics = load_topics()

# 実際に配信で繰り返された mood（affirmative_bot.biorhythm_history の実データ）
MOOD_A = ("全肯定botたんは、お風呂で「FLASHBULB」を聴きながら、"
          "今日の出来事をモルフォと一緒に思い出しているところです。")
MOOD_B = ("全肯定botたんは、お風呂で「FLASHBULB」を聴きながら、"
          "今日の出来事を思い出しているよ。モルフォは家で静かに待っているみたい。")


class TermsTest(unittest.TestCase):
    def test_a_song_title_in_brackets(self):
        self.assertIn("FLASHBULB", topics.terms(MOOD_A))

    def test_a_latin_word_without_brackets(self):
        self.assertIn("FLASHBULB", topics.terms("今日はFLASHBULBを聴いてたんだ"))

    def test_a_work_title_in_brackets(self):
        self.assertIn("ふつつかな悪女でございますが",
                      topics.terms("「ふつつかな悪女でございますが」を読んでた"))

    def test_quoted_lines_are_not_names(self):
        """カギ括弧はセリフの引用にも使う。決め台詞を同じネタ扱いにしない。

        実ログ（2026-08-24）には以下がそのまま入っていた。
        """
        for quoted in ("「大丈夫。全部、いいんだよ。」",
                       "「止まるんじゃねぇぞ~~~!!」",
                       "「この人は私、あなたでもあるんだ」",
                       "「おかしいな」", "「いいね」", "「すごい!」"):
            self.assertEqual(topics.terms(quoted), set(), quoted)

    def test_home_sns_names_are_stopped(self):
        """Nagi / Bluesky は固有名詞だが「話題」ではなく居場所の名前。

        数えると「Nagiの投稿A」と「Nagiの投稿B」が同じネタ扱いになり、
        SNSの話題枠が1件しか出せなくなる。
        """
        self.assertEqual(topics.terms("NagiとBlueskyのみんな、ありがとう"), set())

    def test_plain_japanese_yields_nothing(self):
        """拾えないのは想定内。呼ぶ側が従来のキーに落ちる。"""
        self.assertEqual(topics.terms("公園を散歩して、本を読んでいた"), set())

    def test_broken_input_does_not_raise(self):
        for value in (None, 123, [], {"a": 1}):
            self.assertEqual(topics.terms(value), set())


class KeyTest(unittest.TestCase):
    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(topics.term_key("ＦＬＡＳＨＢＵＬＢ"), topics.term_key("flashbulb"))
        self.assertEqual(topics.term_key("ファイアーエムブレム 万紫千紅"),
                         topics.term_key("ファイアーエムブレム万紫千紅"))


class OverlapTest(unittest.TestCase):
    def test_the_same_song_in_different_wording(self):
        self.assertTrue(topics.overlaps(MOOD_B, topics.keys(MOOD_A)))

    def test_a_name_written_without_brackets_still_matches(self):
        """喋るときにカギ括弧を付けるとは限らない。本文を直に見る。"""
        wanted = topics.keys("「ファイアーエムブレム 万紫千紅」の戦略を考えている")
        self.assertTrue(
            topics.overlaps("今日はファイアーエムブレム万紫千紅をやってたんだ", wanted))

    def test_unrelated_text_does_not_overlap(self):
        self.assertFalse(topics.overlaps("公園を散歩してきた", topics.keys(MOOD_A)))

    def test_empty_never_overlaps(self):
        self.assertFalse(topics.overlaps(MOOD_A, set()))


if __name__ == "__main__":
    unittest.main()
