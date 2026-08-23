"""配信の始めに画面のテキストをリセットする。

comments.txt はファイルに残り続けるので、消さないと配信開始から最初のコメントが
来るまで前回配信のコメントが画面に並んだままになる（実際にそうなっていた）。
"""

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"


def load_subtitle(directory: Path):
    """一時ディレクトリを出力先にした subtitle モジュールを1つ読む。

    live/config.py は .env と DB を読むので、必ずスタブへ差し替えること。
    """
    config_stub = types.ModuleType("config")
    config_stub.SUBTITLE_JA = directory / "subtitle_ja.txt"
    config_stub.SUBTITLE_EN = directory / "subtitle_en.txt"
    config_stub.COMMENTS_TXT = directory / "comments.txt"
    config_stub.CLOCK_TXT = directory / "clock.txt"

    previous = sys.modules.get("config")
    try:
        sys.modules["config"] = config_stub
        spec = importlib.util.spec_from_file_location(
            "subtitle_reset_test_module", LIVE / "subtitle.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, config_stub
    finally:
        if previous is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = previous


class SubtitleResetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.subtitle, self.config = load_subtitle(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_last_streams_comments_are_wiped(self):
        self.subtitle.write_comments([{"author": "前回の人", "text": "前回のコメント"}])
        self.assertIn("前回のコメント",
                      self.config.COMMENTS_TXT.read_text(encoding="utf-8"))

        self.subtitle.clear_comments()
        self.assertEqual(self.config.COMMENTS_TXT.read_text(encoding="utf-8"), "")

    def test_clearing_subtitles_does_not_touch_the_comment_column(self):
        """字幕を消すたびにコメント欄まで消えてはいけない（発話ごとに呼ばれる）。"""
        self.subtitle.write_comments([{"author": "視聴者", "text": "いまのコメント"}])
        self.subtitle.clear()
        self.assertIn("いまのコメント",
                      self.config.COMMENTS_TXT.read_text(encoding="utf-8"))
        self.assertEqual(self.config.SUBTITLE_JA.read_text(encoding="utf-8"), "")

    def test_clear_comments_works_before_any_comment_arrived(self):
        self.subtitle.clear_comments()
        self.assertEqual(self.config.COMMENTS_TXT.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
