"""返答ごとの身振りをその場で作り、発話中に差し込む仕組み（LIVE_MOTION_ON_DEMAND）。

ARDY と Unity には触らない。生成は generate_vrma、送出は unity_client.motion を差し替える。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "live"))

# 先に走ったテストが sys.modules に残したスタブ（ファイルを持たない）を外して本物を読む
for _name in ("config", "unity_client", "safety", "motion", "idle", "llm"):
    _mod = sys.modules.get(_name)
    if _mod is not None and getattr(_mod, "__file__", None) is None:
        del sys.modules[_name]

import config  # noqa: E402,F401  common/ を import パスに載せる
import idle  # noqa: E402
import motion  # noqa: E402

TEXT = "A person draws a circle in the air in a feminine way."


class WorkerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = motion.MotionPool(self.tmp.name)
        self.worker = motion.ArdyWorker(self.pool)
        self.worker.enabled = True

    def tearDown(self):
        self.tmp.cleanup()


class RequestTest(WorkerTestBase):
    def test_disabled_worker_returns_none(self):
        self.worker.enabled = False
        self.assertIsNone(self.worker.request(TEXT, "neutral", 4.0))

    def test_newer_request_cancels_the_older_one(self):
        old = self.worker.request(TEXT, "neutral", 4.0)
        new = self.worker.request("A person waves cheerfully.", "happy", 4.0)
        self.assertTrue(old.cancelled)
        self.assertFalse(old.ready())
        self.assertIs(self.worker._take_urgent(), new)

    def test_existing_clip_is_returned_without_generating(self):
        text = motion.safety.sanitize_motion(TEXT)
        path = self.pool.path_for("neutral", text)
        path.write_bytes(b"vrma")
        self.pool.write_duration(path, 4.0)
        job = self.worker.request(TEXT, "neutral", 4.0)
        self.assertTrue(job.ready())
        self.assertEqual(job.seconds, 4.0)
        self.assertIsNone(self.worker._take_urgent())


class RunUrgentTest(WorkerTestBase):
    def test_generates_a_single_segment_and_finishes(self):
        job = self.worker.request(TEXT, "neutral", 4.0)
        with mock.patch.object(motion, "generate_vrma", return_value=3.9) as gen:
            self.worker._run_urgent(self.worker._take_urgent())
        self.assertEqual(gen.call_args.kwargs["count"], 1)
        self.assertEqual(gen.call_args.kwargs["duration"], 4.0)
        self.assertTrue(job.ready())
        self.assertAlmostEqual(job.seconds, 3.9)
        self.assertEqual(self.pool.duration_of(job.path), 3.9)

    def test_failure_finishes_without_a_path(self):
        job = self.worker.request(TEXT, "neutral", 4.0)
        with mock.patch.object(motion, "generate_vrma", return_value=None):
            self.worker._run_urgent(self.worker._take_urgent())
        self.assertTrue(job.done.is_set())
        self.assertFalse(job.ready())


class PlayJobTest(unittest.TestCase):
    def setUp(self):
        self.animator = idle.IdleAnimator(pool=mock.Mock(), enabled=False)
        self.job = motion.MotionJob(TEXT, "neutral", 4.0)
        self.job.finish("/tmp/x.vrma", 4.0)

    def test_plays_the_ready_job_once(self):
        self.animator.speak_begin("neutral", job=self.job)
        with mock.patch.object(idle.unity_client, "motion") as send:
            now = time.monotonic() + 10
            self.assertTrue(self.animator._play_job(now))
            self.assertFalse(self.animator._play_job(now + 10))
        send.assert_called_once_with("/tmp/x.vrma")
        self.assertEqual(self.job.played_at, now)

    def test_waits_for_the_previous_clip_to_fade_in(self):
        self.animator.speak_begin("neutral", job=self.job)
        now = time.monotonic()
        self.animator._last_play_at = now
        with mock.patch.object(idle.unity_client, "motion") as send:
            self.assertFalse(self.animator._play_job(now + 0.1))
        send.assert_not_called()

    def test_not_ready_job_is_not_played(self):
        pending = motion.MotionJob(TEXT, "neutral", 4.0)
        self.animator.speak_begin("neutral", job=pending)
        with mock.patch.object(idle.unity_client, "motion") as send:
            self.assertFalse(self.animator._play_job(time.monotonic() + 10))
        send.assert_not_called()

    def test_speak_end_drops_the_job(self):
        self.animator.speak_begin("neutral", job=self.job)
        self.animator.speak_end()
        with mock.patch.object(idle.unity_client, "motion") as send:
            self.assertFalse(self.animator._play_job(time.monotonic() + 10))
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
