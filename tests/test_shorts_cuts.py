"""カット割り（shorts/core.py の plan_cuts / build_cut_filters）。

文が変わるところでカットを切り、山場の文だけ強く寄る。2026-10-08 に字幕1枚ごとに
寄り・傾きを変えたら「カットが多すぎる。寄りが内容と同期していない」となった。
"""

import random
import unittest

import core


def subs(*starts, end=10.0):
    out = []
    for i, s in enumerate(starts):
        out.append({"start": s, "end": starts[i + 1] if i + 1 < len(starts) else end,
                    "text": "あ"})
    return out


class PlanCutsTest(unittest.TestCase):
    def test_every_subtitle_change_is_a_cut(self):
        cuts = core.plan_cuts(subs(0.0, 1.5, 3.0, 4.5), 6.0, random.Random(0))
        self.assertEqual([c["start"] for c in cuts], [0.0, 1.5, 3.0, 4.5])
        self.assertEqual(cuts[-1]["end"], 6.0)

    def test_the_first_cut_is_not_zoomed(self):
        # サムネと同じ画角で始める
        for seed in range(10):
            c = core.plan_cuts(subs(0.0, 1.5, 3.0), 5.0, random.Random(seed))[0]
            self.assertEqual((c["zoom"], c["angle"]), (1.0, 0.0))

    def test_the_same_framing_never_repeats(self):
        cuts = core.plan_cuts(subs(*[i * 1.0 for i in range(30)], end=31.0), 31.0,
                              random.Random(3))
        for a, b in zip(cuts, cuts[1:]):
            self.assertNotEqual((a["zoom"], a["angle"]), (b["zoom"], b["angle"]))

    def test_short_cuts_are_absorbed(self):
        cuts = core.plan_cuts(subs(0.0, 1.5, 1.8, 3.0), 5.0, random.Random(0))
        self.assertEqual([c["start"] for c in cuts], [0.0, 1.5, 3.0])

    def test_a_short_last_cut_is_absorbed(self):
        cuts = core.plan_cuts(subs(0.0, 1.5, 4.7), 5.0, random.Random(0))
        self.assertEqual([c["start"] for c in cuts], [0.0, 1.5])

    def test_the_camera_pullback_is_always_cut_and_wide(self):
        # 引いている最中は寄らない。引ききったところで必ずカットを切る
        cuts = core.plan_cuts(subs(0.0, 3.0, 4.8, 7.0), 9.0, random.Random(0),
                              forced=[5.0], wide_at=[4.6])
        starts = [c["start"] for c in cuts]
        self.assertIn(4.6, starts)
        self.assertIn(5.0, starts)
        wide = cuts[starts.index(4.6)]
        self.assertEqual((wide["zoom"], wide["angle"]), (1.0, 0.0))

    def test_tilt_only_when_zoomed(self):
        # 寄らずに傾けると角に黒が出る
        for zoom, angle in core.CUT_PRESETS:
            if angle:
                self.assertGreaterEqual(zoom, 1.08)


class EmphasisTest(unittest.TestCase):
    SPANS = [{"start": 0.0}, {"start": 3.0}, {"start": 6.0, "emphasis": True},
             {"start": 9.0}, {"start": 12.0, "emphasis": True}]

    def test_only_the_peaks_are_zoomed_in(self):
        cuts = core.plan_cuts(self.SPANS, 15.0, random.Random(0))
        strong = [c["start"] for c in cuts if c["zoom"] > 1.2]
        self.assertEqual(strong, [6.0, 12.0])

    def test_calm_sentences_are_not_tilted(self):
        cuts = core.plan_cuts(self.SPANS, 15.0, random.Random(0))
        for c in cuts:
            if c["start"] not in (6.0, 12.0):
                self.assertEqual(c["angle"], 0.0)

    def test_one_cut_per_sentence(self):
        self.assertEqual(len(core.plan_cuts(self.SPANS, 15.0, random.Random(0))), 5)


class BuildCutFiltersTest(unittest.TestCase):
    def test_no_filter_when_nothing_moves(self):
        cuts = [{"start": 0.0, "end": 5.0, "zoom": 1.0, "angle": 0.0}]
        self.assertEqual(core.build_cut_filters(cuts, lambda t: (540, 950)), [])

    def test_the_face_stays_put(self):
        # 寄っても顔 (fx, fy) の画面座標が変わらない切り出し位置になっている
        cuts = [{"start": 0.0, "end": 1.0, "zoom": 1.0, "angle": 0.0},
                {"start": 1.0, "end": 2.0, "zoom": 1.25, "angle": 0.0}]
        zp = core.build_cut_filters(cuts, lambda t: (500, 1000))[-1]
        self.assertIn("if(lt(it,1.0),0.00000,100.00000)", zp)   # 500 - 500/1.25
        self.assertIn("if(lt(it,1.0),0.00000,200.00000)", zp)   # 1000 - 1000/1.25

    def test_rotation_comes_before_the_zoom(self):
        # 寄ったあとで回すと必ず角に黒が出る
        cuts = [{"start": 0.0, "end": 1.0, "zoom": 1.0, "angle": 0.0},
                {"start": 1.0, "end": 2.0, "zoom": 1.12, "angle": 2.0}]
        names = [f.split("=")[0] for f in core.build_cut_filters(cuts, lambda t: (540, 950))]
        self.assertEqual(names, ["fps", "rotate", "zoompan"])


class TopSubtitleTest(unittest.TestCase):
    def test_two_lines_split_at_a_comma(self):
        self.assertEqual(core._wrap_top_subtitle("だからbotたん、何度でも言うね。", 24),
                         ["だからbotたん、", "何度でも言うね。"])

    def test_no_line_breaks_right_after_a_small_tsu(self):
        # 2026-10-08 夜版: 「感じやすくなるっ」「て言われてて。」
        self.assertEqual(core._wrap_top_subtitle("感じやすくなるって言われてて。", 24),
                         ["感じやすくなるって", "言われてて。"])

    def test_a_line_may_end_with_n(self):
        # 「ん」は行末に来てよい。行頭禁則と混同して「褒／めてあげてね。」になっていた
        self.assertEqual(core._wrap_top_subtitle("自分をたくさん褒めてあげてね。", 24),
                         ["自分をたくさん", "褒めてあげてね。"])

    def test_a_chunk_always_fits_in_two_lines(self):
        max_units = (core.W - 40 - 2 * core.TOP_SUB_PAD_X) * 2 // core.TOP_SUB_FONT_SIZE
        text = "あ" * (core.TOP_SUB_MAX_CHARS + core._CUT_SLACK)
        self.assertLessEqual(len(core.wrap_cjk(text, max_units)), 2)


if __name__ == "__main__":
    unittest.main()
