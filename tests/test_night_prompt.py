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

    def test_no_comment_corner_anymore(self):
        # 夜版は Bluesky に出すので、YouTube の前日動画のコメントは読まない
        prompt = build()
        self.assertNotIn("CommentCorner", prompt)
        self.assertNotIn("CommentCorner", prompts.SYSTEM_PROMPT)
        self.assertIn('section名を"NagiCorner"', prompt)


class NagiMentionTest(unittest.TestCase):
    """②に、どこで見た投稿か（「Blueskyで」「Nagiで」）が無ければ書き直させる。"""

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

    def test_a_script_that_names_bluesky_passes(self):
        self.assertTrue(self.check(self.script("実はね、Blueskyで猫の投稿を見たんだ。")))

    def test_a_script_without_nagi_is_rejected(self):
        # 2026-10-08 の試し撮りで実際に出た文
        self.assertFalse(self.check(self.script("実はね、視力検査で動揺しつつも合格できたっていう投稿を見たんだ。")))

    def test_the_prompt_requires_it(self):
        self.assertIn("「Blueskyで」か「Nagiで」の一言は必ず入れる", build())


class PostListTest(unittest.TestCase):
    def test_each_post_shows_network_and_name(self):
        data = {"interactions": [
            {"network": "bsky", "display_name": "ねこ", "post_text": "猫がかわいい", "score": 88},
            {"network": "nagi", "display_name": "すいばり", "post_text": "働きすぎた", "score": 90},
        ]}
        prompt = prompts.build_user_prompt(data)
        self.assertIn("1. [Bluesky] 名前:ねこ", prompt)
        self.assertIn("2. [Nagi] 名前:すいばり", prompt)
        self.assertIn("post_caption", prompts.SYSTEM_PROMPT)
        self.assertIn("picked_post", prompts.SYSTEM_PROMPT)

    def test_picked_post_numbers_match_the_prompt(self):
        import pipeline
        # 本文が空の行は一覧に載らないので、番号もそれを飛ばして数える
        data = {"interactions": [
            {"network": "nagi", "uri": "at://did:plc:a/com.suibari.nagi.post/1", "post_text": " "},
            {"network": "bsky", "uri": "at://did:plc:b/app.bsky.feed.post/2", "post_text": "猫",
             "display_name": "ねこ"},
        ]}
        picked = pipeline.pick_source_post(data, 1)
        self.assertEqual(picked["url"], "https://bsky.app/profile/did:plc:b/post/2")
        self.assertIsNone(pipeline.pick_source_post(data, 2))
        self.assertIsNone(pipeline.pick_source_post(data, None))


class TypeRotationTest(unittest.TestCase):
    """②の2ステップ目は日替わりの型で書かせる。

    2026-10-10〜17 は「意外な事実」の8本中7本が脳・ホルモンの話だった。
    唯一の例が「人と話すだけで、幸せに関わるホルモンが出る」だったのも一因。
    """

    def test_the_hormone_example_is_gone(self):
        self.assertNotIn("ホルモン", build())

    def test_each_type_gets_its_own_step(self):
        for kind, (label, _what, _ex) in prompts.TYPE_STEPS.items():
            prompt = build(plan={"type": kind, "angle": None, "idea": "", "picked_post": None})
            self.assertIn(f"2. {label}:", prompt)
            for other, (other_label, _w, _e) in prompts.TYPE_STEPS.items():
                if other != kind:
                    self.assertNotIn(f"2. {other_label}:", prompt)

    def test_the_planned_angle_and_idea_are_passed(self):
        prompt = build(plan={"type": "fact", "angle": "歴史・由来",
                             "idea": "警察署の建物は明治に洋風になった", "picked_post": 2})
        self.assertIn("切り口は「歴史・由来」", prompt)
        self.assertIn("警察署の建物は明治に洋風になった", prompt)

    def test_a_planned_post_is_the_only_one_listed(self):
        prompt = build(plan={"type": "question", "angle": None, "idea": "", "picked_post": 2})
        self.assertIn("2. [Nagi]", prompt)
        self.assertNotIn("Don't ever give up.", prompt)
        self.assertIn("meta.picked_post には 2 を書く", prompt)

    def test_an_unknown_planned_post_falls_back_to_the_whole_list(self):
        prompt = build(plan={"type": "question", "angle": None, "idea": "", "picked_post": 9})
        self.assertIn("Don't ever give up.", prompt)
        self.assertIn(POLICE_POST, prompt)

    def test_each_closing_form_gets_its_own_example(self):
        for form, (_what, example) in prompts.CLOSING_STEPS.items():
            prompt = build(plan={"type": "fact", "closing_form": form})
            self.assertIn(f"「{example}」→（ループして）", prompt)

    def test_the_plan_prompt_asks_for_concrete_things(self):
        prompt = prompts.build_plan_prompt(DATA)
        self.assertIn("concrete_things", prompt)
        self.assertIn("about には concrete_things", prompt)

    def test_recent_hooks_and_closings_are_listed(self):
        prompt = build(history=[{"date": "2026-10-17", "type": "fact", "hook": "こだわりは正解だよ",
                                 "closing": "今から伝えるね。"}])
        self.assertIn("「こだわりは正解だよ」", prompt)
        self.assertIn("「今から伝えるね。」", prompt)

    def test_the_plan_prompt_asks_for_every_type_and_shows_history(self):
        prompt = prompts.build_plan_prompt(
            DATA, history=[{"date": "2026-10-17", "type": "fact", "angle": "心と体",
                            "idea": "脳の報酬系が反応する"}])
        for kind in prompts.TYPE_STEPS:
            self.assertIn(f"  - {kind}（", prompt)
        self.assertIn("意外な事実（心と体） 脳の報酬系が反応する", prompt)
        self.assertIn(POLICE_POST, prompt)


class ResearchedFactTest(unittest.TestCase):
    """豆知識は Gemini の検索で調べた事実だけを使う（shorts/night_research.py）。"""

    RESEARCH = "- ブックオフの1号店は1990年に相模原で開店した\n- 「本を売るならブックオフ」"

    def test_researched_facts_replace_the_planned_idea(self):
        prompt = build(plan={"type": "fact", "angle": "歴史・由来", "about": "ブックオフ",
                             "idea": "ブックオフは1980年に誕生した", "research": self.RESEARCH})
        self.assertIn("ブックオフの1号店は1990年に相模原で開店した", prompt)
        self.assertIn("ここに書かれていないことは足さない", prompt)
        self.assertNotIn("1980年", prompt)

    def test_plan_corner_uses_research_or_switches_type(self):
        from unittest import mock
        import pipeline
        raw = {"chosen_post": 1, "concrete_things": ["ブックオフ"], "ideas": {
            "fact": [{"about": "ブックオフ", "angle": "歴史・由来", "idea": "1980年に誕生"}],
            "question": "q", "reframe": "r", "metaphor": "m"}}
        with mock.patch.object(pipeline, "llm_json", return_value=raw), \
             mock.patch.object(pipeline.night_research, "research", return_value=self.RESEARCH) as r:
            plan = pipeline.plan_corner(DATA, history=[])
        r.assert_called_once()
        self.assertEqual((plan["type"], plan["research"]), ("fact", self.RESEARCH))

        # 検索が走らなかった日は推測の豆知識を出さず、Gemini をそれ以上呼ばずに次の型へ
        with mock.patch.object(pipeline, "llm_json", return_value=raw), \
             mock.patch.object(pipeline.night_research, "research", return_value=None) as r:
            plan = pipeline.plan_corner(DATA, history=[])
        r.assert_called_once()
        self.assertEqual(plan["type"], "question")
        self.assertNotIn("research", plan)

    def test_a_failed_plan_never_writes_an_unresearched_fact(self):
        from unittest import mock
        import pipeline
        with mock.patch.object(pipeline, "llm_json", side_effect=RuntimeError("down")), \
             mock.patch.object(pipeline.night_research, "research") as r:
            plan = pipeline.plan_corner(DATA, history=[])
        r.assert_not_called()
        self.assertNotEqual(plan["type"], "fact")


class ConstraintSectionTest(unittest.TestCase):
    def test_nagi_theme_exclusion_is_passed(self):
        prompt = build(corner_context={"excluded_nagi_themes": ["眠れない夜"]})
        self.assertIn("眠れない夜", prompt)


if __name__ == "__main__":
    unittest.main()
