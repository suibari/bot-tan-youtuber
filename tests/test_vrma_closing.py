"""動画の締めのモーション計画。

録画は音声の後ろにも続くので、最後の文の動きを録画の終わりまで延ばす。
延ばした分は同じ指示文が並ぶと毎回そっくりな手振りになるので、2本目以降を差し替える。
"""
import random
import unittest

from shorts import core

WAVE = "A person waves cheerfully in a feminine way."
NOD = "A person nods happily in a feminine way."


class PlanClosingTest(unittest.TestCase):
    def test_last_sentence_is_extended_to_the_window_end(self):
        spans = [{"start": 0.0, "end": 5.0, "motion": NOD},
                 {"start": 5.0, "end": 10.0, "motion": WAVE}]
        segs = core.plan_vrma_from_sentences(spans, 0.0, 14.0)
        self.assertAlmostEqual(sum(s["duration"] for s in segs), 14.0, places=1)

    def test_closing_repeats_are_replaced_with_different_motions(self):
        spans = [{"start": 0.0, "end": 5.0, "motion": NOD},
                 {"start": 5.0, "end": 14.0, "motion": WAVE}]
        segs = core.plan_vrma_from_sentences(spans, 0.0, 14.0)
        tail = [s["text"] for s in segs if s["text"] not in (NOD,)]
        self.assertEqual(tail[0], WAVE)                 # 1本目は台本の動きのまま
        self.assertEqual(len(set(tail)), len(tail))     # 同じ文が続かない
        for text in tail[1:]:
            self.assertIn(text, core.VRMA_CLOSING_MOTIONS)

    def test_middle_repeats_are_left_alone(self):
        segs = [{"text": NOD, "duration": 2.5}, {"text": NOD, "duration": 2.5},
                {"text": WAVE, "duration": 3.0}]
        self.assertEqual(core.vary_closing_segments(segs, random.Random(0)), segs)

    def test_closing_motions_are_safe(self):
        from common import motion_safety as ms
        for text in ms.CLOSING_MOTIONS:
            self.assertEqual(ms.sanitize_motion(text), text)


if __name__ == "__main__":
    unittest.main()
