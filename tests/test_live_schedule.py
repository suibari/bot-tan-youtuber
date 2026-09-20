import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9))
SCHEDULE = Path(__file__).resolve().parents[1] / "live/schedule.py"


class LiveScheduleTest(unittest.TestCase):
    def setUp(self):
        config = types.ModuleType("config")
        config.LIVE_START_HHMM = "21:00"
        config.LIVE_END_HHMM = "22:00"
        previous = sys.modules.get("config")
        try:
            sys.modules["config"] = config
            spec = importlib.util.spec_from_file_location("live_schedule_test", SCHEDULE)
            self.schedule = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.schedule)
        finally:
            if previous is None:
                sys.modules.pop("config", None)
            else:
                sys.modules["config"] = previous
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.schedule.LIVE_TIMER = Path(self.temp.name) / "bottan-live.timer"

    def test_weekly_timer_publishes_today_and_following_week(self):
        self.schedule.LIVE_TIMER.write_text("OnCalendar=Sat *-*-* 20:40:00 Asia/Tokyo\n")
        monday_starts = [start for start, _ in self.schedule.upcoming_schedules(
            datetime(2026, 9, 21, 12, tzinfo=JST))]
        self.assertEqual(monday_starts[0], datetime(2026, 9, 26, 21, tzinfo=JST))
        starts = [start for start, _ in self.schedule.upcoming_schedules(
            datetime(2026, 9, 26, 4, tzinfo=JST))]
        self.assertEqual(starts, [datetime(2026, 9, 26, 21, tzinfo=JST),
                                  datetime(2026, 10, 3, 21, tzinfo=JST)])

    def test_daily_timer_switches_next_opportunity_to_tomorrow(self):
        self.schedule.LIVE_TIMER.write_text("OnCalendar=*-*-* 20:40:00 Asia/Tokyo\n")
        starts = [start for start, _ in self.schedule.upcoming_schedules(
            datetime(2026, 9, 26, 22, tzinfo=JST))]
        self.assertEqual(starts, [datetime(2026, 9, 27, 21, tzinfo=JST),
                                  datetime(2026, 9, 28, 21, tzinfo=JST)])
        self.assertIn("2026年9月27日（日）21:00", self.schedule.broadcast_text(starts[0])[1])


if __name__ == "__main__":
    unittest.main()
