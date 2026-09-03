"""字幕のチャンク分割（shorts/core.py）。

2026-08-25 18:00 の夜版で「冒頭直後の字幕が一瞬で消える」「次の字幕が一文字」が
出た。実台本で再現した結果:

    3.101-3.280 dur=0.179 |実はね、|          ← ほぼ見えない
    3.340-6.582           |Nagiで「Don't eve| ← 単語の途中で切断
   22.754-23.065 dur=0.311 |、|               ← 1文字だけの字幕

原因は2つで、どちらも朝版も踏んでいた:

1. max_chars 超えを固定長スライスしていた。16文字 / max_chars=15 は必ず
   「15文字 + 1文字」になる。救済するはずの merge_short は分割の**後**に
   `len(a)+len(b) <= max_chars` で判定するので 15+1=16 > 15 となり効かない。
2. チャンク→モーラの対応を文字数比で取っていた。ラテン文字が混ざると
   1文字あたりのモーラ数が文中で一定でなくなり、割り当てが大きく狂う。

ここでは 1（純関数）だけを固定する。2 は VOICEVOX が要るので手動プローブで見る。
"""

import unittest

import core


class SplitSubtitleChunksTest(unittest.TestCase):
    def test_a_chunk_one_over_the_limit_is_split_evenly(self):
        # 固定長スライスなら [15, 1]。均等割りなら [8, 8]
        chunks = core.split_subtitle_chunks("資格取得のために警察署に行って、", 15)
        self.assertEqual([len(c) for c in chunks], [8, 8])
        self.assertEqual("".join(chunks), "資格取得のために警察署に行って、")

    def test_no_chunk_is_a_single_character(self):
        for max_chars in (15, 20):
            for n in range(1, 60):
                text = "あ" * n + "。"
                for chunk in core.split_subtitle_chunks(text, max_chars):
                    self.assertGreater(
                        len(chunk), 1,
                        f"1文字の字幕が出た: n={n} max_chars={max_chars}")

    def test_latin_words_are_not_cut_in_the_middle(self):
        text = "Nagiで「Don't ever give up.」っていう投稿を見たんだ。"
        chunks = core.split_subtitle_chunks(text, 20)
        self.assertEqual("".join(chunks), text)
        # 各語がどれか1つのチャンクに丸ごと入っていること
        for word in ("Don't", "ever", "give", "up."):
            self.assertTrue(any(word in c for c in chunks),
                            f"{word!r} が割れている: {chunks}")

    def test_short_comma_fragments_are_merged_before_splitting(self):
        """「萩、」「桔梗、」のような1語だけの字幕が並ぶのを防ぐ。

        **結合は分割より先**でないと効かない。以前は分割の後にやっていたので、
        16文字 / max_chars=15 が「15文字 + 1文字」に割れたあと
        `15+1=16 > 15` で判定に落ち、1文字の字幕が必ず残っていた。
        """
        self.assertEqual(
            core.split_subtitle_chunks("秋の七草は萩、桔梗、葛など七つで、", 20),
            ["秋の七草は萩、桔梗、葛など七つで、"])
        self.assertEqual(core.split_subtitle_chunks("実はね、うれしかったよ。", 20),
                         ["実はね、うれしかったよ。"])

    def test_merging_stops_at_max_chars(self):
        chunks = core.split_subtitle_chunks("あああああ、いいいいい、ううううう、えええええ、", 20)
        self.assertEqual(chunks, ["あああああ、いいいいい、ううううう、", "えええええ、"])
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 20)

    def test_punctuation_splits_come_first_when_merging_would_overflow(self):
        chunks = core.split_subtitle_chunks("実はね、" + "あ" * 18 + "。", 20)
        self.assertEqual(chunks[0], "実はね、")

    def test_short_and_empty_input(self):
        self.assertEqual(core.split_subtitle_chunks("あ", 20), ["あ"])
        self.assertEqual(core.split_subtitle_chunks("", 20), [])
        self.assertEqual(core.split_subtitle_chunks("   ", 20), [])

    def test_one_long_word_falls_back_to_an_even_split(self):
        # 1語が max_chars より長いときは、語の境界へ寄せられないので均等割りのまま
        text = "A" * 36
        chunks = core.split_subtitle_chunks(text, 20)
        self.assertEqual([len(c) for c in chunks], [18, 18])

    def test_chunks_stay_within_the_slack(self):
        # 語を割らないための余裕（_CUT_SLACK）を超えて長くならないこと。
        # 帯は2行に折り返すので多少はみ出してよいが、青天井にはしない
        text = "実はね、Nagiで「Don't ever give up.」っていう投稿を見たんだ。"
        for chunk in core.split_subtitle_chunks(text, 20):
            self.assertLessEqual(len(chunk), 20 + core._CUT_SLACK)


class MergeShortSubtitlesTest(unittest.TestCase):
    def test_a_flash_of_a_block_is_merged_into_its_neighbour(self):
        subs = [
            {"start": 0.0, "end": 0.15, "text": "ねぇ、"},     # 0.15秒＝見えない
            {"start": 0.2, "end": 2.0, "text": "きょうもおつかれさま。"},
        ]
        core._merge_short_subtitles(subs, 20)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["text"], "ねぇ、きょうもおつかれさま。")
        self.assertEqual((subs[0]["start"], subs[0]["end"]), (0.0, 2.0))

    def test_blocks_long_enough_are_left_alone(self):
        subs = [
            {"start": 0.0, "end": 1.0, "text": "ねぇ、"},
            {"start": 1.1, "end": 2.0, "text": "おつかれさま。"},
        ]
        before = [dict(s) for s in subs]
        core._merge_short_subtitles(subs, 20)
        self.assertEqual(subs, before)

    def test_merging_never_exceeds_two_lines_worth_of_text(self):
        subs = [
            {"start": 0.0, "end": 0.1, "text": "あ" * 20},
            {"start": 0.2, "end": 2.0, "text": "い" * 20},
        ]
        core._merge_short_subtitles(subs, 20)
        # 20+20=40 > 20*2 ではないので結合される。上限を1文字超えると結合しない
        self.assertEqual(len(subs), 1)
        subs = [
            {"start": 0.0, "end": 0.1, "text": "あ" * 21},
            {"start": 0.2, "end": 2.0, "text": "い" * 20},
        ]
        core._merge_short_subtitles(subs, 20)
        self.assertEqual(len(subs), 2)


class CloseSubtitleGapsTest(unittest.TestCase):
    def test_a_pause_between_blocks_is_filled_by_holding_the_previous_one(self):
        # モーラ列に読点のポーズが入らないので、素だと 0.6秒 の空白ができる
        subs = [
            {"start": 0.0, "end": 1.0, "text": "ねぇ、"},
            {"start": 1.62, "end": 3.0, "text": "きょうもおつかれさま。"},
        ]
        core._close_subtitle_gaps(subs)
        self.assertAlmostEqual(subs[0]["end"], 1.56, places=3)

    def test_a_long_silence_does_not_leave_a_stale_caption(self):
        subs = [
            {"start": 0.0, "end": 1.0, "text": "ねぇ、"},
            {"start": 9.0, "end": 10.0, "text": "おかえり。"},
        ]
        core._close_subtitle_gaps(subs, max_hold=1.2)
        self.assertAlmostEqual(subs[0]["end"], 2.2, places=3)

    def test_the_last_block_is_untouched(self):
        subs = [{"start": 0.0, "end": 1.0, "text": "またね。"}]
        core._close_subtitle_gaps(subs)
        self.assertEqual(subs[0]["end"], 1.0)


class FindSubtitleTimeTest(unittest.TestCase):
    def test_a_keyword_split_across_two_blocks_is_still_found(self):
        # 「高評価」が割れると thankful_time が 0 に落ちてモーションが変わる
        subs = [
            {"start": 0.0, "end": 1.0, "text": "また明日ね！高"},
            {"start": 1.1, "end": 2.0, "text": "評価も嬉しいな。"},
        ]
        self.assertEqual(core._find_subtitle_time(subs, "高評価"), 0.0)

    def test_a_keyword_inside_one_block_is_found(self):
        subs = [{"start": 3.0, "end": 4.0, "text": "高評価も嬉しいな。"}]
        self.assertEqual(core._find_subtitle_time(subs, "高評価"), 3.0)

    def test_start_from_is_respected(self):
        subs = [
            {"start": 0.0, "end": 1.0, "text": "高評価してね"},
            {"start": 5.0, "end": 6.0, "text": "高評価も嬉しいな。"},
        ]
        self.assertEqual(core._find_subtitle_time(subs, "高評価", start_from=2.0), 5.0)

    def test_a_missing_keyword_returns_none(self):
        subs = [{"start": 0.0, "end": 1.0, "text": "またね。"}]
        self.assertIsNone(core._find_subtitle_time(subs, "高評価"))


class EscDrawtextTest(unittest.TestCase):
    """drawtext のエスケープ。**壊れても ffmpeg は 0 で終了する。**

    `'` を `'\\''`（バックスラッシュ1個）で囲んでいたため、アポストロフィを含む
    字幕は「その drawtext だけ何も描かれない」状態になっていた。後続フィルタは
    そのまま動き、終了コードもログも正常なので気づけない。
    実測では「Nagiで「Don't ever」の1枚が丸ごと透明だった。

    アンエスケープが2段（フィルタグラフ → オプション値）かかるので、
    いったんクォートを閉じてバックスラッシュ3個を付ける必要がある。
    確認は scratchpad の実描画スクリプト（白ピクセル数を数える）で行った。
    """

    def test_an_apostrophe_closes_the_quote_and_uses_three_backslashes(self):
        self.assertEqual(core.esc_drawtext("Don't"), "Don'\\\\\\''t")

    def test_a_colon_is_escaped_so_it_does_not_end_the_option(self):
        self.assertEqual(core.esc_drawtext("12:34"), "12\\:34")

    def test_plain_japanese_is_untouched(self):
        self.assertEqual(core.esc_drawtext("ふつうの字幕だよ、"), "ふつうの字幕だよ、")


class WrapCjkTest(unittest.TestCase):
    def test_wrap_is_shared_with_the_morning_layout(self):
        import quiz_layout
        self.assertIs(quiz_layout.wrap_cjk, core.wrap_cjk)

    def test_two_lines_are_balanced_instead_of_leaving_a_widow(self):
        """素の wrap_cjk は先頭行を目一杯詰めるので下の行が2文字になる。"""
        lines = core.wrap_subtitle_lines("ねぇ、今日ちょっと立ち止まっちゃっても、", 36)
        self.assertEqual(len(lines), 2)
        self.assertEqual("".join(lines), "ねぇ、今日ちょっと立ち止まっちゃっても、")
        self.assertLessEqual(abs(len(lines[0]) - len(lines[1])), 2)

    def test_text_that_fits_stays_on_one_line(self):
        self.assertEqual(core.wrap_subtitle_lines("資格取得のために警察署に行って、", 36),
                         ["資格取得のために警察署に行って、"])

    def test_it_never_returns_more_than_max_lines(self):
        for n in range(1, 60):
            self.assertLessEqual(len(core.wrap_subtitle_lines("あ" * n, 36)), 2)

    def test_the_morning_layout_uses_the_same_wrapper(self):
        import quiz_layout
        self.assertIs(quiz_layout.wrap_subtitle_lines, core.wrap_subtitle_lines)

    def test_a_line_never_starts_with_a_small_kana_or_punctuation(self):
        lines = core.wrap_cjk("きょうはとってもたのしかったっけ、そうだね", 20)
        for line in lines[1:]:
            self.assertNotIn(line[0], core._NO_LINE_START)


if __name__ == "__main__":
    unittest.main()
