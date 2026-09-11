"""絵が動いていない配信を live へ入れないこと。

2026-09-10 の配信は最初から最後まで 0.1〜1.9fps で、視聴者からは止まった
人形が喋っているだけに見えていた。Unity 側の問題ではなく、:99 の Xorg の
表示経路が停止していたのが原因で、同時刻の glxgears が 1.0〜1.4fps しか
出ていない（GPU の計算そのものは正常だった）。

当日の点検はどれも素通りしている:

  - unity.is_alive()      … プロセスは生きている
  - _display_refresh_hz() … xrandr はモードの公称値 59.95Hz を返す
  - OBS の出力フレーム数  … 止まった窓を 30fps でキャプチャし続ける

fps は FPS_LOG_SEC ごとにログへ出ていたが、それを見て何かする経路が
どこにも無かった。ここでは実測して止めるところまでを確かめる。
"""

import importlib
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"
for path in (str(ROOT), str(LIVE)):
    if path not in sys.path:
        sys.path.insert(0, path)


def load_unity_live():
    """unity_live.py を、本物の live/config.py 越しに読み込む。

    ほかのテストが sys.modules["config"] へ中身の無いスタブを置いたまま
    終わるので、そのままだと UNITY_EXE の import に失敗する
    （tests/test_unity_hub.py の load_unity_live と同じ手当て）。
    """
    previous = sys.modules.pop("config", None)
    try:
        sys.modules["config"] = importlib.import_module("config")
        spec = importlib.util.spec_from_file_location(
            "render_gate_test_module", LIVE / "unity_live.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = previous


unity_live = load_unity_live()


class FakeClient:
    """/status を順番に返す差し替え。尽きたら最後の値を返し続ける。"""

    class UnityError(RuntimeError):
        pass

    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = 0

    def status(self):
        self.calls += 1
        value = self.samples[min(self.calls - 1, len(self.samples) - 1)]
        if value is None:
            raise self.UnityError("接続できません")
        return {"fps": value}


class FakeTime:
    """sleep で進む時計。probe_sec ぶん実時間を待たずに測り切るため。"""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += sec


class SoftwareRendererTest(unittest.TestCase):
    """:99 を GPU 以外のものが取っていたら配信を始めないこと。

    :99 はこのサービスの予約番号だが、番号を予約する仕組みは無い。
    2026-09-10 22:39 には Orca IDE が `Xvfb :99` を起こして先に取っており、
    glxgears は 5694fps を返していた。速い値が出るので一見よさそうに
    見えるが llvmpipe（CPU 描画）で、URP + VRM では 1.7fps しか出ない。
    """

    def test_it_catches_the_cpu_rasterisers(self):
        for renderer in ("llvmpipe (LLVM 20.1.2, 256 bits)",
                         "softpipe", "swrast", "zink Vulkan 1.3"):
            self.assertTrue(unity_live._is_software_renderer(renderer), renderer)

    def test_the_gpu_passes(self):
        self.assertFalse(unity_live._is_software_renderer(
            "NVIDIA GeForce RTX 5070 Ti/PCIe/SSE2"))

    def test_an_unknown_renderer_is_not_treated_as_software(self):
        """glxinfo が読めないだけで配信を止めない。fps の実測が後段にある。"""
        self.assertFalse(unity_live._is_software_renderer(""))


class MeasureFpsTest(unittest.TestCase):
    def setUp(self):
        self.live = unity_live.UnityLive()
        self.real_client = unity_live.unity_client
        self.real_time = unity_live.time
        # 実測は probe_sec のあいだ1秒おきに見る。実時間で待つと
        # テスト1件ごとに probe_sec かかるので、時計ごと差し替える
        unity_live.time = FakeTime()

    def tearDown(self):
        unity_live.unity_client = self.real_client
        unity_live.time = self.real_time

    def _measure(self, samples, probe_sec=5.0):
        unity_live.unity_client = FakeClient(samples)
        return self.live.measure_fps(probe_sec)

    def test_healthy_host_reports_sixty(self):
        self.assertGreaterEqual(self._measure([60.0, 59.8, 60.1]), 59.8)

    def test_stalled_host_stays_near_one(self):
        # 2026-09-10 に実際に観測された並び
        self.assertLess(self._measure([0.2, 1.0, 0.1, 1.9, 1.0]), 2.0)

    def test_it_takes_the_best_sample(self):
        """モーションのパースで一時的に落ちても閾値を割らないこと。

        最小や平均を見ると、健全なホストでも .vrma をメインスレッドで
        読んだ直後のサンプルだけで配信を止めてしまう。
        """
        self.assertGreaterEqual(self._measure([60.0, 3.0, 58.0]), 58.0)

    def test_startup_zero_is_not_a_verdict(self):
        """窓が閉じるまで fps は 0 のまま。1サンプルでは判定できない。"""
        self.assertGreaterEqual(self._measure([0.0, 0.0, 60.0]), 60.0)

    def test_unreachable_unity_measures_zero(self):
        self.assertEqual(self._measure([None]), 0.0)


if __name__ == "__main__":
    unittest.main()
