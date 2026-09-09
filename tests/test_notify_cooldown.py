"""同じ警告を続けて Discord へ流さない。

2026-09-09 の配信では、ホストが swap で詰まっているあいだ
`Unity へ発話を送れません: /speak に接続できません: ...` が23分で6回、
VOICEVOX の合成失敗が2回流れた。詰まっている間は同じ文面が出続けるので、
1本目だけ出せば足りる。抑制するのは warn だけで、error（配信そのものの失敗）
と Shorts の投稿通知は見落としたくないので素通しにする。
"""

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import notify   # noqa: E402


class WarnCooldownTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.real_send = notify.send
        notify.send = self.sent.append
        notify._warned_at.clear()
        self.addCleanup(self.restore)

    def restore(self):
        notify.send = self.real_send
        notify._warned_at.clear()

    def test_the_first_warning_goes_out(self):
        notify.warn("Unity へ発話を送れません")
        self.assertEqual(len(self.sent), 1)

    def test_the_same_warning_is_held_back(self):
        for _ in range(6):
            notify.warn("Unity へ発話を送れません")
        self.assertEqual(len(self.sent), 1)

    def test_a_different_warning_still_gets_through(self):
        notify.warn("Unity へ発話を送れません")
        notify.warn("音声合成に失敗しました")
        self.assertEqual(len(self.sent), 2)

    def test_it_comes_back_after_the_cooldown(self):
        notify.warn("Unity へ発話を送れません")
        # 時計を戻す代わりに、記録した時刻を古くする
        for key in notify._warned_at:
            notify._warned_at[key] -= notify.NOTIFY_COOLDOWN_SEC + 1
        notify.warn("Unity へ発話を送れません")
        self.assertEqual(len(self.sent), 2)

    def test_errors_are_never_held_back(self):
        """配信が落ちたことは毎回知りたい。"""
        for _ in range(3):
            notify.error("配信", "配信が途切れたため終了します")
        self.assertEqual(len(self.sent), 3)

    def test_the_table_does_not_grow_without_bound(self):
        for i in range(500):
            notify.warn(f"警告 {i}")
        self.assertLessEqual(len(notify._warned_at), 200)


if __name__ == "__main__":
    unittest.main()
