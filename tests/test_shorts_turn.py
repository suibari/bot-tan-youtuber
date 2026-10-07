"""朝版と夜版が日替わりで、どの日もちょうど片方だけが番になること。"""

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
    def test_exactly_one_kind_each_day(self):
        start = datetime.date(2026, 10, 1)
        with mock.patch.dict(os.environ, {"SHORTS_ALTERNATE_DAYS": "true"}):
            for i in range(14):
                day = start + datetime.timedelta(days=i)
                turns = [k for k in turn.KINDS if turn.is_my_turn(k, day)]
                self.assertEqual(len(turns), 1, day)

    def test_alternates_across_month_boundary(self):
        with mock.patch.dict(os.environ, {"SHORTS_ALTERNATE_DAYS": "true"}):
            last = datetime.date(2026, 10, 31)
            first = datetime.date(2026, 11, 1)
            self.assertNotEqual(turn.is_my_turn("quiz", last), turn.is_my_turn("quiz", first))

    def test_disabled_means_every_day(self):
        day = datetime.date(2026, 10, 7)
        with mock.patch.dict(os.environ, {"SHORTS_ALTERNATE_DAYS": "false"}):
            self.assertTrue(turn.is_my_turn("quiz", day))
            self.assertTrue(turn.is_my_turn("night", day))


if __name__ == "__main__":
    unittest.main()
