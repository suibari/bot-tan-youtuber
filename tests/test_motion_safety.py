"""モーション指示文の正規化と禁止動作の遮断。

LLM はプロンプトの形を普通に破るので、ARDY に渡る文の形はコード側で保証する。
"""
import unittest

from common import motion_safety as ms


class NormalizeMotionTextTest(unittest.TestCase):
    def test_keeps_the_new_form_as_is(self):
        text = "A person waves cheerfully in a feminine way."
        self.assertEqual(ms.normalize_motion_text(text), text)

    def test_rewrites_the_old_form(self):
        self.assertEqual(
            ms.normalize_motion_text("A woman stands in place and brings one hand up to her chin."),
            "A person brings one hand up to their chin in a feminine way.")

    def test_adds_subject_and_style_to_a_fragment(self):
        self.assertEqual(ms.normalize_motion_text("raises one hand up to her chin"),
                         "A person raises one hand up to their chin in a feminine way.")

    def test_drops_a_pronoun_subject(self):
        self.assertEqual(ms.normalize_motion_text("She nods happily."),
                         "A person nods happily in a feminine way.")

    def test_subject_only_is_discarded(self):
        self.assertEqual(ms.normalize_motion_text("A woman stands in place."), "")
        self.assertEqual(ms.normalize_motion_text(""), "")


class SanitizeMotionTest(unittest.TestCase):
    def test_lower_body_and_clapping_are_blocked(self):
        for text in ("A person jumps happily.", "She claps her hands.",
                     "A person bends their knees in a feminine way."):
            with self.subTest(text=text):
                self.assertEqual(ms.sanitize_motion(text), "")

    def test_idle_motions_survive_unchanged(self):
        for text in ms.IDLE_MOTIONS:
            with self.subTest(text=text):
                self.assertEqual(ms.sanitize_motion(text), text)


if __name__ == "__main__":
    unittest.main()
