#!/usr/bin/env python3
"""朝版と夜版を日替わりにする。今日がその版の番かを判定する。

2026-09-14 から Shorts フィードに一本も乗らなくなった（流入元にショートフィードが
出てこない。見た人の7割は視聴を継続していて、中身が飛ばされているわけではない）。
同じ型の自動生成を毎日2本出し続けたことで、量産コンテンツとして絞られていると見て、
朝夜を1日おきの交互にして合計1日1本へ減らす。

タイマー（systemd）はそのままにして、起動スクリプトの頭でこれを呼ぶ。
自分の番でない日は何もせず終わる。SHORTS_ALTERNATE_DAYS=false で毎日に戻る。

    python shorts/turn.py quiz   # 番なら終了コード 0、そうでなければ 1
"""

import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import env_flag  # noqa: E402  (.env もここで読まれる)

JST = datetime.timezone(datetime.timedelta(hours=9))

# JST の日付の通し番号が偶数の日は朝版、奇数の日は夜版
KINDS = {"quiz": 0, "night": 1}


def today_jst() -> datetime.date:
    """テストや手動確認のため SHORTS_TURN_DATE=YYYY-MM-DD で差し替えられる。"""
    override = os.getenv("SHORTS_TURN_DATE", "").strip()
    if override:
        return datetime.date.fromisoformat(override)
    return datetime.datetime.now(JST).date()


def is_my_turn(kind: str, today: datetime.date | None = None) -> bool:
    if not env_flag("SHORTS_ALTERNATE_DAYS", True):
        return True
    today = today or today_jst()
    return today.toordinal() % 2 == KINDS[kind]


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in KINDS:
        print(f"usage: {sys.argv[0]} {{{'|'.join(KINDS)}}}", file=sys.stderr)
        return 2
    kind = sys.argv[1]
    today = today_jst()
    if is_my_turn(kind, today):
        return 0
    other = next(k for k in KINDS if k != kind)
    print(f"[turn] {today} は {other} の番なので {kind} は投稿しません"
          f"（毎日に戻すには SHORTS_ALTERNATE_DAYS=false）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
