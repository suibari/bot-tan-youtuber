"""夜版Shortsのプロンプト（shorts/prompts.py）。

2026-08-25 18:00 の締めが

    「botたんも今日、資格取得のために警察署に行って、緊張したけど、
      全肯定で乗り切ったよ！高評価も嬉しいな。また明日ね！」

になった。この出来事は Nagi の他人の投稿（「警察署に行ってきました🫡 …」）そのもの
だった。当時は締めで botたん自身の出来事を語らせていて、それと他人の投稿が同じ
プロンプトに並んでいた。

2026-10 にループする台本（③決め台詞が①掴みへつながる）へ変えたときに、
締めの「今日の出来事」と挨拶を外した。投稿が他人のものだという印は残す。
"""

import unittest

import prompts


POLICE_POST = ("警察署に行ってきました🫡 資格取得の為に用事がありましてね "
               "決して悪い事したわけじゃないのにめっちゃ緊張した💧")
DATA = {
    "interactions": [
        {"post_text": "Don't ever give up.", "score": 90},
        {"post_text": POLICE_POST, "score": 88},
        {"post_text": "   ", "score": 88},          # 本文が空のものは載せない
    ],
}


def build(**kwargs):
    return prompts.build_user_prompt(DATA, **kwargs)


class OtherPeoplesPostsTest(unittest.TestCase):
    def test_the_post_list_is_marked_as_written_by_other_people(self):
        prompt = build()
        self.assertIn(POLICE_POST, prompt)       # 紹介はする（②で使う）
        self.assertIn("他の人が書いた投稿", prompt)
        self.assertIn("botたん自身の体験ではありません", prompt)

    def test_empty_posts_are_dropped(self):
        self.assertNotIn("3. (score:88)", build())

    def test_the_output_rules_state_where_things_come_from(self):
        self.assertIn("【出どころの区別（最重要）】", prompts.SYSTEM_PROMPT)
        self.assertIn("botたん自身の体験として語ってはいけません", prompts.SYSTEM_PROMPT)


class LoopStructureTest(unittest.TestCase):
    def test_the_closing_hands_over_to_the_hook(self):
        prompt = build()
        self.assertIn("③決め台詞を言い終えた直後に①掴みがもう一度流れる", prompt)
        self.assertIn("意外な事実", prompt)
        self.assertIn("本音", prompt)

    def test_the_closing_has_no_sign_off(self):
        # 「また明日ね」「高評価」を言わせるとループが切れる
        prompt = build()
        self.assertIn("「高評価」・「チャンネル登録」・「また明日ね」などの挨拶は入れない", prompt)
        self.assertNotIn("「高評価」という語を必ず含めること", prompt)
        self.assertNotIn("また明日ね」で終わる", prompt)

    def test_botttan_own_episode_is_not_requested(self):
        prompt = build()
        self.assertNotIn("botたんの今日の出来事", prompt)
        self.assertNotIn("first_greeting_status", prompts.SYSTEM_PROMPT)

    def test_comment_days_keep_the_same_closing(self):
        prompt = build(comments=[{"author": "a", "text": "かわいい"}])
        self.assertIn('section名を"CommentCorner"', prompt)
        self.assertNotIn('section名を"NagiCorner"', prompt)
        self.assertIn("③ 決め台詞", prompt)


class NagiMentionTest(unittest.TestCase):
    """この動画は Nagi の紹介も兼ねているので、②に「Nagiで」が無ければ書き直させる。"""

    @classmethod
    def setUpClass(cls):
        import json
        import pipeline
        cls.check = staticmethod(pipeline.mentions_nagi)
        cls.script = staticmethod(lambda text: json.dumps({"sections": [
            {"section": "Thumbnail", "sentences": [{"text": "えらいよ"}]},
            {"section": "NagiCorner", "sentences": [{"text": text}, {"text": "だからね"}]},
        ], "meta": {"nagi_themes": []}}, ensure_ascii=False))

    def test_a_script_that_names_nagi_passes(self):
        self.assertTrue(self.check(self.script("実はね、Nagiで免許更新の投稿を見たんだ。")))

    def test_a_script_without_nagi_is_rejected(self):
        # 2026-10-08 の試し撮りで実際に出た文
        self.assertFalse(self.check(self.script("実はね、視力検査で動揺しつつも合格できたっていう投稿を見たんだ。")))

    def test_the_prompt_requires_it(self):
        self.assertIn("「Nagiで」の一言は必ず入れる", build())


class ConstraintSectionTest(unittest.TestCase):
    def test_nagi_theme_exclusion_is_passed(self):
        prompt = build(corner_context={"excluded_nagi_themes": ["眠れない夜"]})
        self.assertIn("眠れない夜", prompt)


if __name__ == "__main__":
    unittest.main()
