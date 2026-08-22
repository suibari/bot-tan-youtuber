"""当日21時のYouTube配信枠だけを先行作成する軽量ジョブ。"""

import sys
import traceback
from datetime import datetime

import broadcast
import memory
from schedule import broadcast_text, today_schedule


def prepare_today(now: datetime | None = None) -> broadcast.Broadcast:
    memory.ensure_schema()
    scheduled_start, scheduled_end = today_schedule(now)
    title, description = broadcast_text(scheduled_start)

    prepared = memory.get_prepared_broadcast(scheduled_start)
    event = None
    if prepared:
        event = broadcast.Broadcast.load(prepared["broadcast_id"])
        if event:
            print(f"[prepare] DBの配信枠を再利用: {event.url}")

    # YouTubeへの作成だけ成功しDB保存に失敗した場合も、開始時刻から見つけ直す。
    if event is None:
        event = broadcast.Broadcast.find_scheduled(scheduled_start)
        if event:
            print(f"[prepare] YouTube上の配信枠を再利用: {event.url}")

    if event is None:
        event = broadcast.Broadcast().create_event(
            title, description, scheduled_start,
        )

    broadcast.cleanup_stale(
        preserve_ids=[event.broadcast_id],
        scheduled_before=scheduled_start,
    )
    memory.save_prepared_broadcast(
        event.broadcast_id,
        event.url,
        event.title or title,
        scheduled_start,
        scheduled_end,
    )
    print(f"[prepare] 当日配信URLを保存: {event.url}")
    return event


def main() -> int:
    try:
        prepare_today()
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
