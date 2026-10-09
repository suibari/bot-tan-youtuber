"""朝版が隔日で番になること。"""

import datetime
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shorts import turn  # noqa: E402


class TurnTest(unittest.TestCase):
    def test_quiz_every_other_day(self):
        start = datetime.date(2026, 10, 1)
        with mock.patch.dict(os.environ, {"SHORTS_ALTERNATE_DAYS": "true"}):
            turns = [turn.is_my_turn("quiz", start + datetime.timedelta(days=i)) for i in range(14)]
        self.assertEqual(turns.count(True), 7)
        self.assertTrue(all(a != b for a, b in zip(turns, turns[1:])))

    def test_alternates_across_month_boundary(self):
        with mock.patch.dict(os.environ, {"SHORTS_ALTERNATE_DAYS": "true"}):
            last = datetime.date(2026, 10, 31)
            first = datetime.date(2026, 11, 1)
            self.assertNotEqual(turn.is_my_turn("quiz", last), turn.is_my_turn("quiz", first))

    def test_disabled_means_every_day(self):
        day = datetime.date(2026, 10, 7)
        with mock.patch.dict(os.environ, {"SHORTS_ALTERNATE_DAYS": "false"}):
            self.assertTrue(turn.is_my_turn("quiz", day))


if __name__ == "__main__":
    unittest.main()
