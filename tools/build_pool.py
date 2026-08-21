"""ARDY を起動してモーションプールを作る。配信前に1回走らせておく。

プールが空だと配信中モーションが一切出ない（Animator の Idle だけになる）。
ARDY はサーバ起動に4〜5分、生成は1本あたり数秒〜数十秒かかるので、
配信直前ではなく事前に走らせること。すでに持っているぶんは飛ばす。
"""

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))          # common/
sys.path.insert(0, str(_ROOT / "live"))  # config, motion, unity_client ...

import motion as motion_mod
from config import ensure_dirs
from llm import MOTION_CATEGORIES


def main() -> int:
    ensure_dirs()
    per_category = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    pool = motion_mod.MotionPool()
    print(f"[pool] 現在: {pool.summary()} (合計 {pool.total()})")

    worker = motion_mod.ArdyWorker(pool, max_queue=64)
    started = time.time()
    if not worker.start(wait=True):
        print("[pool] ARDY を起動できませんでした")
        return 1
    print(f"[pool] ARDY 起動に {time.time() - started:.0f} 秒")

    want = sum(max(0, per_category - pool.count(c)) for c in MOTION_CATEGORIES)
    if want == 0:
        print("[pool] すでに十分あります")
        worker.stop()
        return 0

    print(f"[pool] {want} 本を生成します（カテゴリごとに {per_category} 本まで）")
    worker.prewarm(per_category=per_category)

    made = 0
    gen_started = time.time()
    # 1本あたり最大3分、全体でも余裕を見て打ち切る
    deadline = gen_started + want * 180
    while made < want and time.time() < deadline:
        ready = worker.poll_ready()
        if ready is None:
            time.sleep(2)
            continue
        made += 1
        print(f"[pool] {made}/{want} 完了 ({time.time() - gen_started:.0f}秒経過): "
              f"{ready[1]} / {Path(ready[0]).name}")

    worker.stop()
    print(f"[pool] 生成 {made}/{want} 本 / 所要 {time.time() - gen_started:.0f}秒")
    print(f"[pool] 最終: {pool.summary()} (合計 {pool.total()})")
    return 0 if made > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
