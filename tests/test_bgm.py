"""BGM 生成（common/bgm.py）が、使えないときに固定曲へ戻ること。"""

import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import bgm  # noqa: E402


class BgmTest(unittest.TestCase):
    def test_missing_acestep_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(bgm, "BGM_GENERATE", True), \
                mock.patch.object(bgm, "ACESTEP_DIR", Path(tmp) / "nothing"), \
                mock.patch.object(bgm, "_release_gpu") as release:
            self.assertIsNone(bgm.generate("quiz", Path(tmp)))
            # 使えないなら GPU も空けない（ollama を無駄に追い出さない）
            release.assert_not_called()

    def test_disabled_returns_none(self):
        with mock.patch.object(bgm, "BGM_GENERATE", False):
            self.assertIsNone(bgm.generate("night", Path("/tmp")))

    def test_caption_varies_and_forbids_vocals(self):
        rng = random.Random(0)
        captions = {bgm.build_caption("quiz", rng) for _ in range(20)}
        self.assertGreater(len(captions), 5)
        for c in captions:
            self.assertIn("no vocals", c)


if __name__ == "__main__":
    unittest.main()
