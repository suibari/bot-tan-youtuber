"""配信開始の待ちと、失敗したときの後始末・原因表示。

2026-09-07 の失敗が出発点。OBS は 28.6 秒かけて配信を立ち上げていたのに、
15 秒で見切っていたため ObsError になり、しかもその後 OBS が遅れて走り出して
誰も見ていない配信が 29 秒ぶん YouTube へ流れた。エラー本文には別セッションの
RTMP 接続**成功**行が「原因」として並んでいた。
"""

import importlib
import importlib.util
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"

if str(LIVE) not in sys.path:
    sys.path.insert(0, str(LIVE))


def load_obs():
    """obs.py を、本物の live/config.py 越しに読み込む。

    ほかのテスト（tests/test_comment_queue.py）が sys.modules["config"] へ
    中身の無いスタブを置いたまま終わることがあり、そのままだと import に
    失敗する。ここはスタブで代えず本物を読ませる: 待ち時間の既定値
    （OBS_START_TIMEOUT）そのものが確かめたいものだから。
    """
    previous = sys.modules.pop("config", None)
    try:
        sys.modules["config"] = importlib.import_module("config")
        spec = importlib.util.spec_from_file_location(
            "obs_start_test_module", LIVE / "obs.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = previous


obs_mod = load_obs()


class FakeClock:
    """time.sleep で進む仮想時計。実時間を使うと1ケース十数秒かかる。"""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += sec

    def strftime(self, fmt, *args):
        return "20:42:14"


class Status:
    def __init__(self, active, reconnecting=False):
        self.output_active = active
        self.output_reconnecting = reconnecting


class FakeClient:
    """`active_after` 秒後に配信が立ち上がる OBS。

    `reconnecting` を立てると、前回の配信が再接続を繰り返したまま
    居座っている状態（2026-09-07 の翌朝の OBS）から始まる。
    """

    def __init__(self, clock, active_after, reconnecting=False):
        self.clock = clock
        self.active_after = active_after
        self.started_at = 0.0 if reconnecting else None
        self.reconnecting = reconnecting
        self.stopped = False
        self.stop_calls = 0
        self.start_calls = 0

    @property
    def running(self):
        if self.started_at is None or self.stopped:
            return False
        if self.reconnecting:
            return True   # 繋がらないまま active であり続ける
        return self.clock.time() - self.started_at >= self.active_after

    def get_stream_status(self):
        return Status(self.running, self.reconnecting and self.running)

    def start_stream(self):
        self.start_calls += 1
        self.started_at = self.clock.time()
        self.stopped = False

    def stop_stream(self):
        self.stop_calls += 1
        if not self.running:
            # OBS もまだ走っていない出力を止めろと言われれば失敗を返す
            raise RuntimeError("output not active")
        self.stopped = True
        self.reconnecting = False


class StartStreamTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        for name in ("time", "sleep", "strftime"):
            patcher = unittest.mock.patch.object(
                obs_mod.time, name, getattr(self.clock, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        # ログの引用はここでは見ない（別のテストで見る）
        patcher = unittest.mock.patch.object(
            obs_mod, "_last_output_error", lambda since=None: "  （ログ）")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = unittest.mock.patch.object(
            obs_mod, "memory_note", lambda: "  （メモリ）")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, active_after, reconnecting=False):
        session = obs_mod.Obs.__new__(obs_mod.Obs)
        session.client = FakeClient(self.clock, active_after, reconnecting)
        return session

    def test_a_slow_start_within_the_budget_is_a_success(self):
        """2026-09-07 の 28.6 秒。既定（OBS_START_TIMEOUT）で間に合うこと。"""
        session = self.make(28.6)
        session.start_stream()
        self.assertTrue(session.client.running)
        self.assertEqual(session.client.stop_calls, 0)

    def test_the_old_fifteen_second_budget_would_have_missed_it(self):
        """既定を戻したら同じ配信を取りこぼすことを、テストとして残しておく。"""
        session = self.make(28.6)
        with self.assertRaises(obs_mod.ObsError):
            session.start_stream(timeout=15.0)

    def test_a_start_just_past_the_deadline_is_caught_by_the_grace(self):
        """0.5秒ごとのポーリングがデッドラインをまたいでも取りこぼさない。"""
        session = self.make(61.0)
        session.start_stream(timeout=60.0)
        self.assertTrue(session.client.running)

    def test_a_stream_that_starts_after_we_gave_up_is_stopped(self):
        """ゾンビ配信を残さない。teardown の stop_stream はここを通れない。"""
        session = self.make(70.0)
        with self.assertRaises(obs_mod.ObsError):
            session.start_stream(timeout=60.0)
        self.assertTrue(session.client.stopped)

    def test_a_stream_that_never_starts_raises_with_the_cause(self):
        session = self.make(float("inf"))
        with self.assertRaises(obs_mod.ObsError) as caught:
            session.start_stream(timeout=60.0)
        self.assertIn("（メモリ）", str(caught.exception))
        self.assertIn("（ログ）", str(caught.exception))

    def test_an_already_running_stream_is_left_alone(self):
        session = self.make(0.0)
        session.client.start_stream()
        session.start_stream(timeout=60.0)
        self.assertEqual(session.client.stop_calls, 0)
        self.assertEqual(session.client.start_calls, 1)

    def test_a_stale_reconnecting_output_is_restarted(self):
        """昨日の配信が再接続を繰り返したまま残っていたら、止めて始め直す。

        OBS は出力を開始する時点の配信先を使う。「すでに配信中です」と返すと、
        新しく設定した宛先ではなく昨日の宛先へ繋ぎに行く出力をそのまま使う
        ことになる。2026-09-07 のゾンビは翌朝までこの状態で残っていた。
        """
        session = self.make(2.0, reconnecting=True)
        session.start_stream(timeout=60.0)
        self.assertGreaterEqual(session.client.stop_calls, 1)
        self.assertEqual(session.client.start_calls, 1)
        self.assertFalse(session.client.get_stream_status().output_reconnecting)
        self.assertTrue(session.client.running)


class LastOutputErrorTest(unittest.TestCase):
    """OBS のログは1つのファイルに複数セッションぶんが時刻順に並ばず追記される。"""

    LOG = "\n".join([
        # 前のセッション。時刻としては後ろだが、ファイル上は手前にある
        "20:51:31.838: libfdk_aac encoder created",
        "20:51:33.480: [rtmp stream: 'simple_stream'] Connection to "
        "rtmp://a.rtmp.youtube.com/live2 (2404:6800:400b:c006::86) successful",
        "20:51:34.416: ==== Streaming Start ====",
        "21:55:51.537: [rtmp stream: 'simple_stream'] User stopped the stream",
        # ここから今回ぶん
        "20:42:28.629: [x264 encoder: 'simple_video_stream'] preset: veryfast",
        "20:42:48.639: libfdk_aac encoder created",
        "20:42:55.706: [rtmp stream: 'simple_stream'] Connection to "
        "rtmp://a.rtmp.youtube.com/live2 (2404:6800:400b:c019::86) successful",
        "20:42:57.224: ==== Streaming Start ====",
        "20:43:26.193: WriteN, RTMP send error 32 (66 bytes)",
        "20:43:26.193: [rtmp stream: 'simple_stream'] Disconnected from "
        "rtmp://a.rtmp.youtube.com/live2",
    ])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        logs = home / ".var/app/com.obsproject.Studio/config/obs-studio/logs"
        logs.mkdir(parents=True)
        (logs / "2026-09-02 20-58-55.txt").write_text(self.LOG, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)
        patcher = unittest.mock.patch.object(Path, "home", staticmethod(lambda: home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, since, now):
        with unittest.mock.patch.object(obs_mod.time, "strftime", lambda *a: now):
            return obs_mod._last_output_error(since)

    def test_a_successful_rtmp_connection_is_not_reported_as_the_cause(self):
        report = self.read("20:42:14", "20:43:40")
        self.assertNotIn("successful", report)
        self.assertIn("RTMP send error 32", report)

    def test_an_earlier_sessions_lines_are_not_quoted(self):
        """時刻順に並んでいない前のセッションを巻き込まないこと。"""
        report = self.read("20:42:14", "20:43:40")
        self.assertNotIn("20:51:", report)
        self.assertNotIn("21:55:", report)

    def test_silence_from_obs_is_reported_as_silence(self):
        """OBS が1行も書いていないなら、過去のログを持ち出すよりそう言う。"""
        report = self.read("20:44:00", "20:45:00")
        self.assertIn("ログを1行も書いていません", report)


if __name__ == "__main__":
    unittest.main()
