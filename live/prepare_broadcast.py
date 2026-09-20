"""次の配信機会のYouTube枠を先行作成する軽量ジョブ。"""

import sys
import traceback
from datetime import datetime

import broadcast
import memory
from schedule import broadcast_text, today_schedule, upcoming_schedules


def prepare_at(scheduled_start: datetime, scheduled_end: datetime) -> broadcast.Broadcast:
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

    memory.save_prepared_broadcast(
        event.broadcast_id,
        event.url,
        event.title or title,
        scheduled_start,
        scheduled_end,
    )
    print(f"[prepare] {scheduled_start.isoformat()} の配信URLを保存: {event.url}")
    return event


def prepare_today(now: datetime | None = None) -> broadcast.Broadcast:
    memory.ensure_schema()
    scheduled_start, scheduled_end = today_schedule(now)
    event = prepare_at(scheduled_start, scheduled_end)
    broadcast.cleanup_stale(
        preserve_ids=[event.broadcast_id],
        scheduled_before=scheduled_start,
    )
    return event


def prepare_upcoming(now: datetime | None = None) -> list[broadcast.Broadcast]:
    """今日を含む次の2枠を作る。配信中の後にも次回URLを残す。"""
    memory.ensure_schema()
    events = [prepare_at(start, end) for start, end in upcoming_schedules(now)]
    ids = [event.broadcast_id for event in events]
    memory.retire_other_prepared_broadcasts(ids)
    broadcast.cleanup_stale(preserve_ids=ids)
    return events


def main() -> int:
    try:
        prepare_upcoming()
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
