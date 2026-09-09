"""配信の準備で Unity Hub だけを閉じ、Unity Editor は巻き添えにしないこと。

Unity Hub は配信にも収録にも要らない。run_live.sh が起こすのは Editor の実体
（UNITY_EXE）で、Hub 経由ではない。にもかかわらず Electron + Chromium の
プロセス群が常駐して RAM を掴む。2026-09-09 の配信では開いたままで、この日は
ホストの commit が RAM+swap の 110% に達し、OBS の送出が止まって YouTube に
配信を切られた。

**巻き添えのほうが怖い。** 収録側の shorts/core.py が
`pkill -9 -f "Unity -projectPath"` で Unity を無差別に殺して事故になった前例が
あり、run_live.sh はそのために flock を取っている。ここで配信中の Editor を
掴んでしまうと同じ壊れ方をするので、当たり判定を名指しで確かめる。
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

    ほかのテスト（tests/test_live_loop.py など）が sys.modules["config"] へ
    中身の無いスタブを置いたまま終わるので、そのままだと UNITY_EXE の import に
    失敗する。tests/test_obs_start.py の load_obs と同じ手当て。
    ここは本物の設定値そのものを確かめたいので、スタブでは代えられない。
    """
    previous = sys.modules.pop("config", None)
    try:
        sys.modules["config"] = importlib.import_module("config")
        spec = importlib.util.spec_from_file_location(
            "unity_hub_test_module", LIVE / "unity_live.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, sys.modules["config"].UNITY_EXE
    finally:
        if previous is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = previous


unity_live, UNITY_EXE = load_unity_live()


# 実際にこのホストで観測されるコマンドライン
HUB = "/opt/unityhub/unityhub"
HUB_BIN = "/opt/unityhub/unityhub-bin"
HUB_RENDERER = "/opt/unityhub/unityhub-bin"
EDITOR = "/home/suibari/Unity/Hub/Editor/6000.0.76f1/Editor/Unity"


def matches(cmdline: str) -> bool:
    """close_unity_hub が使うのと同じ判定。"""
    return cmdline.startswith(unity_live.UNITY_HUB_PREFIX)


class HubTargetingTest(unittest.TestCase):
    def test_it_matches_the_hub(self):
        for cmdline in (HUB, HUB_BIN, HUB_RENDERER):
            self.assertTrue(matches(cmdline), cmdline)

    def test_it_never_matches_the_editor(self):
        """配信の Unity は Hub のインストール先の下にあるが、/opt ではない。"""
        self.assertFalse(matches(EDITOR))

    def test_the_configured_editor_is_out_of_range(self):
        """UNITY_EXE の実際の値でも当たらないこと。"""
        self.assertFalse(matches(UNITY_EXE))

    def test_it_does_not_match_unrelated_processes(self):
        for cmdline in ("/usr/bin/obs", "/opt/unityhub-something-else/x",
                        "/usr/lib/xorg/Xorg", ""):
            self.assertFalse(matches(cmdline), cmdline)

    def test_nothing_to_close_is_not_an_error(self):
        """Hub が起動していない日がふつう。0 を返して静かに進む。"""
        original = unity_live.UNITY_HUB_PREFIX
        try:
            # 実在しない接頭辞にして「1件も当たらない」状態を作る
            unity_live.UNITY_HUB_PREFIX = "/nonexistent-prefix-for-test/"
            self.assertEqual(unity_live.close_unity_hub(), 0)
        finally:
            unity_live.UNITY_HUB_PREFIX = original


class PrepareOrderTest(unittest.TestCase):
    def test_the_hub_is_closed_before_ardy_loads(self):
        """あとから閉じても ARDY が swap に落ちたあとでは遅い。"""
        source = (LIVE / "live.py").read_text(encoding="utf-8")
        close = source.index("unity_live.close_unity_hub()")
        ardy = source.index('"[準備] ARDY を起動します')
        unity = source.index('"[準備] Unity を起動します"')
        self.assertLess(close, ardy)
        self.assertLess(close, unity)


if __name__ == "__main__":
    unittest.main()
