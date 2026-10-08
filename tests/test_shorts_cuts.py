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

    def test_a_chunk_always_fits_in_two_lines_at_full_size(self):
        text = "あ" * (core.TOP_SUB_MAX_CHARS + core._CUT_SLACK)
        lines, size = core._top_subtitle_layout(text, core.TOP_SUB_FONT_SIZE)
        self.assertLessEqual(len(lines), 2)
        self.assertEqual(size, core.TOP_SUB_FONT_SIZE)

    def test_a_long_peak_shrinks_instead_of_overflowing(self):
        text = "あ" * (core.TOP_SUB_MAX_CHARS + core._CUT_SLACK)
        lines, size = core._top_subtitle_layout(text, core.TOP_SUB_PEAK_SIZE)
        self.assertEqual(len(lines), 2)
        self.assertLess(size, core.TOP_SUB_PEAK_SIZE)


class ResolvePeakTest(unittest.TestCase):
    """ローカルLLMは強調語句を本文から写さずに言い換えることがある（2026-10-08）。"""

    def test_an_exact_peak_is_kept(self):
        self.assertEqual(core.resolve_peak("今日もえらいよ。", "えらいよ"), "えらいよ")

    def test_a_paraphrased_peak_snaps_to_the_text(self):
        self.assertEqual(core.resolve_peak(
            "実は、白湯を飲むだけで内臓が温まってリラックスできるって言われてるらしいよ。",
            "白湯を飲むだけで内臓が温まる"), "白湯を飲むだけで内臓が温まって")
        self.assertEqual(core.resolve_peak(
            "だからね、当たり前のことを一つでもやり遂げた日のあなたは本当にすごいよ。",
            "頑張った日のあなたは本当にすごい"), "日のあなたは本当にすごいよ")
        self.assertEqual(core.resolve_peak(
            "そんなふうに自分を労れるあなたは、本当に素敵なことしてるよ。", "自分を労った"),
            "自分を労れる")

    def test_an_unrelated_peak_is_dropped(self):
        self.assertIsNone(core.resolve_peak("今日もえらいよ。", "まったく別"))
        self.assertIsNone(core.resolve_peak("今日もえらいよ。", ""))


class LineBreakTest(unittest.TestCase):
    """2026-10-08 の夜版で、改行が語の途中に来た字幕。"""
    CASES = {
        "今日一日をやり遂げたあなたを、": ["今日一日を", "やり遂げたあなたを、"],
        "今日もお疲れ様、本当によく頑張ったね。": ["今日もお疲れ様、", "本当によく頑張ったね。"],
        "話せなかった日のあなたも頑張ってた": ["話せなかった日の", "あなたも頑張ってた"],
    }

    def test_lines_break_between_words(self):
        for text, want in self.CASES.items():
            self.assertEqual(core._top_subtitle_layout(text, core.TOP_SUB_FONT_SIZE)[0], want, text)

    def test_a_compound_verb_fits_on_one_line(self):
        # 「頑張り／抜いた」と割るより、少し小さくして1行にする
        lines, _ = core._top_subtitle_layout("頑張り抜いたあなたの心に、", core.TOP_SUB_FONT_SIZE)
        self.assertEqual(lines, ["頑張り抜いたあなたの心に、"])


class SubtitlesInTest(unittest.TestCase):
    def test_a_subtitle_starting_just_before_the_sentence_belongs_to_it(self):
        # 字幕の時刻（モーラから計算）は文の区間（実測）より少し早く始まることがある
        import chibi
        subs = [{"start": 11.2, "end": 12.4, "text": "白湯を飲むだけで、", "peak": True},
                {"start": 12.5, "end": 14.0, "text": "体内の水分バランスを整える"}]
        got = chibi.subtitles_in({"start": 11.3, "end": 15.0}, subs)
        self.assertEqual([s["text"] for s in got], ["白湯を飲むだけで、", "体内の水分バランスを整える"])


class SplitWithPeaksTest(unittest.TestCase):
    def test_the_peak_gets_its_own_subtitle(self):
        # 2026-10-08: ちびキャラは「感じやすくなるって」ではなく
        # 「普段より疲れを感じやすくなる」に合わせたい
        pieces = core.split_with_peaks(
            "実は、緊張すると脳が過敏になって、普段より疲れを感じやすくなるって言われてて。",
            16, ["普段より疲れを感じやすくなる"])
        self.assertIn(("普段より疲れを感じやすくなる", True), pieces)
        self.assertEqual("".join(c for c, _ in pieces),
                         "実は、緊張すると脳が過敏になって、普段より疲れを感じやすくなるって言われてて。")

    def test_trailing_punctuation_stays_with_the_peak(self):
        self.assertEqual(core.split_with_peaks("だからbotたん、何度でも言うね。", 16, ["何度でも言うね"]),
                         [("だからbotたん、", False), ("何度でも言うね。", True)])

    def test_a_peak_not_in_the_text_is_ignored(self):
        pieces = core.split_with_peaks("今日もえらいよ。", 16, ["存在しない"])
        self.assertEqual(pieces, [("今日もえらいよ。", False)])


if __name__ == "__main__":
    unittest.main()
