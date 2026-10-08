"""Shorts の英語字幕トラックと英語タイトル（shorts/english.py）。

英語のために投稿を止めないことと、英文がどの台詞の区間に出るかを固定する。
英訳の質そのものは ollama が要るので手動で見る。
"""

import unittest
from unittest import mock

import english


class TranslateTest(unittest.TestCase):
    def test_returns_lines_and_title(self):
        with mock.patch.object(english._llm, "generate_json",
                               return_value={"lines": ["Hi!", "Bye!"], "title": "<Hello>"}):
            en = english.translate(["やっほー！", "またね！"], "こんにちは")
        self.assertEqual(en, {"lines": ["Hi!", "Bye!"], "title": "Hello"})

    def test_line_count_mismatch_gives_up(self):
        # 行がずれると別の台詞に別の英文が付くので、出さないほうがよい
        with mock.patch.object(english._llm, "generate_json",
                               return_value={"lines": ["Hi! Bye!"], "title": "x"}):
            self.assertIsNone(english.translate(["やっほー！", "またね！"], "x"))

    def test_llm_error_gives_up(self):
        with mock.patch.object(english._llm, "generate_json", side_effect=TimeoutError()):
            self.assertIsNone(english.translate(["やっほー！"], "x"))

    def test_disabled_by_env(self):
        with mock.patch.dict("os.environ", {"SHORTS_ENGLISH": "false"}), \
             mock.patch.object(english._llm, "generate_json") as gen:
            self.assertIsNone(english.translate(["やっほー！"], "x"))
        gen.assert_not_called()


class FormatTest(unittest.TestCase):
    def test_srt(self):
        srt = english.build_srt([
            {"start": 0.0, "end": 1.234, "text": "Hi!"},
            {"start": 1.234, "end": 1.234, "text": "skipped"},   # 長さ0
            {"start": 2.0, "end": 3661.5, "text": " "},           # 空
            {"start": 3.0, "end": 4.0, "text": "A: x\nB: y"},
        ])
        self.assertEqual(srt, "1\n00:00:00,000 --> 00:00:01,234\nHi!\n\n"
                              "2\n00:00:03,000 --> 00:00:04,000\nA: x\nB: y\n")

    def test_title_fits_youtube_limit(self):
        title = english.build_title("x" * 200)
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith(" / Bot-tan"))
        self.assertEqual(english.build_title(""), "Bot-tan's daily affirmation")


class QuizCuesTest(unittest.TestCase):
    def setUp(self):
        import quiz_pipeline
        self.qp = quiz_pipeline

    def test_translate_script_and_cues(self):
        quiz = {"問題": "問", "選択肢A": "あ", "選択肢B": "い", "正解": "A"}
        script = {"question_intro": [{"text": "q1"}], "answer_reveal": [{"text": "a1"}],
                  "explanation": [{"text": "e1"}, {"text": "e2"}],
                  "affirmation": [{"text": "f1"}], "ending": [{"text": "n1"}],
                  "title_hook": "フック"}
        lines = ["Q1", "A1", "E1", "E2", "F1", "N1", "Question", "Apple", "Ink"]
        with mock.patch.object(english._llm, "generate_json",
                               return_value={"lines": lines, "title": "Hook"}):
            en = self.qp.translate_script(quiz, script)
        self.assertEqual(en, {"question": "Question", "choice_a": "Apple",
                              "choice_b": "Ink", "title": "Hook"})
        self.assertEqual(script["explanation"][1]["en"], "E2")

        def seg(pid, sents, start, end):
            spans, t = [], start
            step = (end - start) / max(len(sents), 1)
            for _ in sents:
                spans.append({"start": t, "end": t + step})
                t += step
            return {"id": pid, "sentences": sents, "spans": spans, "start": start, "end": end}

        segments = [seg("Q", script["question_intro"], 0, 2),
                    seg("THINK", [], 2, 5),
                    seg("EXPL", script["explanation"], 5, 9)]
        cues = self.qp.build_en_cues(segments, en)
        self.assertEqual([(c["start"], c["end"], c["text"]) for c in cues], [
            (0, 2, "Q1"), (2, 5, "A: Apple\nB: Ink"), (5, 7, "E1"), (7, 9, "E2")])

    def test_translate_failure_leaves_script_untouched(self):
        quiz = {"問題": "問", "選択肢A": "あ", "選択肢B": "い", "正解": "A"}
        script = {"question_intro": [{"text": "q1"}]}
        with mock.patch.object(english._llm, "generate_json",
                               return_value={"lines": ["only one"], "title": "x"}):
            self.assertIsNone(self.qp.translate_script(quiz, script))
        self.assertNotIn("en", script["question_intro"][0])


class UploadEnglishTest(unittest.TestCase):
    """英語版は投稿と同時に付ける。英語まわりの失敗で投稿を止めない。"""

    EN = {"title": "T", "description": "D", "srt": "1\n..."}

    def upload(self, insert_side_effect=None, english=EN):
        import youtube
        self.yt = mock.MagicMock()
        insert = mock.Mock(side_effect=insert_side_effect, return_value={"id": "vid"})
        with mock.patch.object(youtube, "_get_youtube_client", return_value=self.yt), \
             mock.patch.object(youtube, "_insert", insert), \
             mock.patch.object(youtube, "THUMBNAIL_RETRY_DELAYS", []):
            url = youtube.upload_to_youtube("x.mp4", "t", "d", "", english)
        return url, [c.args[1] for c in insert.call_args_list]

    def test_localizations_go_into_insert_not_update(self):
        # 投稿直後に videos.update するとタグが消える（2026-10-08 の非公開テストで実測）
        url, bodies = self.upload()
        self.assertEqual(url, "https://youtube.com/watch?v=vid")
        body = bodies[0]
        self.assertEqual(body["localizations"]["en"], {"title": "T", "description": "D"})
        self.assertEqual(body["snippet"]["defaultLanguage"], "ja")
        self.assertIn("botたん", body["snippet"]["tags"])
        self.assertEqual(self.yt.videos().update.call_count, 0)
        cap = self.yt.captions().insert.call_args.kwargs["body"]["snippet"]
        self.assertEqual((cap["videoId"], cap["language"]), ("vid", "en"))

    def test_rejected_english_title_retries_without_it(self):
        from googleapiclient.errors import HttpError
        err = HttpError(mock.Mock(status=400), b"invalid title")
        url, bodies = self.upload(insert_side_effect=[err, {"id": "vid"}])
        self.assertEqual(url, "https://youtube.com/watch?v=vid")
        self.assertIn("localizations", bodies[0])
        self.assertNotIn("localizations", bodies[1])

    def test_caption_failure_does_not_raise(self):
        import youtube
        yt = mock.MagicMock()
        yt.captions().insert().execute.side_effect = RuntimeError("404")
        with mock.patch.object(youtube, "THUMBNAIL_RETRY_DELAYS", []):
            youtube._add_english_caption(yt, "vid", "1\n...")

    def test_no_english_uploads_as_before(self):
        url, bodies = self.upload(english=None)
        self.assertNotIn("localizations", bodies[0])
        self.assertEqual(self.yt.captions().insert.call_count, 0)


if __name__ == "__main__":
    unittest.main()
