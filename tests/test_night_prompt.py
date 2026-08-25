"""夜版Shortsのプロンプト（shorts/prompts.py・shorts/pipeline.py）。

2026-08-25 18:00 の締めが

    「botたんも今日、資格取得のために警察署に行って、緊張したけど、
      全肯定で乗り切ったよ！高評価も嬉しいな。また明日ね！」

になった。この出来事は biorhythm_history に存在せず、同じプロンプトに並んでいた
Nagi の他人の投稿（「警察署に行ってきました🫡 資格取得の為に用事がありましてね
決して悪い事したわけじゃないのにめっちゃ緊張した💧」）そのものだった。

原因は2つ:

1. 【今日のbotたんの状態一覧】と【今日Nagiで心に残った投稿一覧】が、どちらも
   「一人称の日本語で書かれた具体的な出来事」の箇条書きとして並んでいた。
   投稿側に「他人が書いたもの」という印が無かった。
2. ガードが「②で紹介した投稿の内容を流用しない」しか禁じていなかった。
   ②で紹介したのは別の投稿だったので、警察署の投稿は文面上セーフだった。

対処は「LLM に選ばせない」こと。締めで使うエピソードは pick_closing_mood が
Python 側で1件に確定し、プロンプトにはそれだけを載せる。
"""

import unittest
from datetime import datetime, timezone

import prompts


MOOD_RELAX = {
    "status": "Relax", "energy": 45,
    "mood": "お風呂上がりでリラックスして、ハンバーグを思い返して嬉しくなった",
    "mood_en": "Bot-tan is relaxing after a bath.",
    "created_at": datetime(2026, 8, 25, 12, 22, tzinfo=timezone.utc),
}
MOOD_STUDY = {"status": "Study", "energy": 64, "mood": "数学の課題で達成感",
              "mood_en": "solving math", "created_at": None}
POLICE_POST = ("警察署に行ってきました🫡 資格取得の為に用事がありましてね "
               "決して悪い事したわけじゃないのにめっちゃ緊張した💧")
DATA = {
    "moods": [MOOD_RELAX, MOOD_STUDY],
    "interactions": [
        {"post_text": "Don't ever give up.", "score": 90},
        {"post_text": POLICE_POST, "score": 88},
        {"post_text": "   ", "score": 88},          # 本文が空のものは載せない
    ],
}


def build(**kwargs):
    return prompts.build_user_prompt(DATA, **kwargs)


class ClosingMoodBlockTest(unittest.TestCase):
    def test_only_the_chosen_episode_is_offered(self):
        prompt = build(closing_mood=MOOD_RELAX)
        self.assertIn("ハンバーグ", prompt)
        # 選ばなかった Mood は載らない＝LLM に「選ぶ」余地を残さない
        self.assertNotIn("数学の課題", prompt)
        self.assertNotIn("自分で選んでください", prompt)
        self.assertNotIn("【今日のbotたんの状態一覧】", prompt)

    def test_the_english_mood_is_not_sent(self):
        # 英文が増えるほど混同の材料になり、字幕の文字数↔モーラ対応も狂う
        self.assertNotIn("Bot-tan is relaxing", build(closing_mood=MOOD_RELAX))

    def test_energy_is_labelled_on_the_0_to_100_scale(self):
        # 以前は 0.7 / 0.3 で判定していたので、実データでは常に「高め」だった
        self.assertIn("エネルギー:普通", build(closing_mood=MOOD_RELAX))
        self.assertIn("エネルギー:高め", build(closing_mood={**MOOD_RELAX, "energy": 80}))
        self.assertIn("エネルギー:低め", build(closing_mood={**MOOD_RELAX, "energy": 10}))

    def test_it_falls_back_to_the_first_mood(self):
        # キャッシュ再生など closing_mood を渡さない経路でも壊れない
        self.assertIn("ハンバーグ", build())


class NagiPostListTest(unittest.TestCase):
    def test_the_post_list_is_marked_as_written_by_other_people(self):
        prompt = build(closing_mood=MOOD_RELAX)
        self.assertIn(POLICE_POST, prompt)       # 紹介はする（②で使う）
        self.assertIn("他の人が書いた投稿", prompt)
        self.assertIn("botたん自身の体験ではありません", prompt)

    def test_the_guard_covers_every_post_not_just_the_one_used(self):
        prompt = build(closing_mood=MOOD_RELAX)
        self.assertIn("②で紹介したかどうかに関わらず", prompt)
        self.assertIn("どの投稿の内容も〇〇に流用してはいけない", prompt)

    def test_empty_posts_are_dropped(self):
        # 画像だけの投稿は紹介できないので一覧に出さない
        self.assertNotIn("3. (score:88)", build(closing_mood=MOOD_RELAX))


class SystemPromptTest(unittest.TestCase):
    def test_the_output_rules_state_where_things_come_from(self):
        self.assertIn("【出どころの区別（最重要）】", prompts.SYSTEM_PROMPT)
        self.assertIn("botたん自身の体験として語ってはいけません", prompts.SYSTEM_PROMPT)


class ConstraintSectionTest(unittest.TestCase):
    def test_the_excluded_status_is_not_repeated_in_the_prompt(self):
        # 除外は pick_closing_mood が Python 側で適用済み。重ねて書いても効かない
        prompt = build(closing_mood=MOOD_RELAX,
                       corner_context={"excluded_first_greeting_statuses": ["Study"],
                                       "excluded_nagi_themes": ["眠れない夜"]})
        self.assertNotIn("状態のエピソードを選ばないこと", prompt)
        self.assertIn("眠れない夜", prompt)      # Nagi のテーマ除外はそのまま効く


class PickClosingMoodTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pipeline
        cls.pick = staticmethod(pipeline.pick_closing_mood)

    def test_recently_used_statuses_are_avoided(self):
        ctx = {"excluded_first_greeting_statuses": ["Study"]}
        for _ in range(20):
            self.assertEqual(self.pick([MOOD_RELAX, MOOD_STUDY], ctx)["status"], "Relax")

    def test_it_still_returns_something_when_everything_is_excluded(self):
        # 無人実行なので、候補ゼロで落とさない
        ctx = {"excluded_first_greeting_statuses": ["Study", "Relax"]}
        self.assertIsNotNone(self.pick([MOOD_RELAX, MOOD_STUDY], ctx))

    def test_no_moods_gives_none(self):
        self.assertIsNone(self.pick([], {}))
        self.assertIsNone(self.pick(None, None))

    def test_it_works_without_a_corner_context(self):
        self.assertIn(self.pick([MOOD_RELAX, MOOD_STUDY], None)["status"],
                      ("Relax", "Study"))


if __name__ == "__main__":
    unittest.main()
