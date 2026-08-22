"""YouTube Liveの日次スケジュールと公開文言の共通定義。"""

from datetime import datetime, timedelta, timezone

from config import LIVE_END_HHMM, LIVE_START_HHMM

JST = timezone(timedelta(hours=9))


def _at(base: datetime, hhmm: str) -> datetime:
    hour, minute = (int(value) for value in hhmm.split(":"))
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def today_schedule(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now.astimezone(JST) if now else datetime.now(JST)
    start = _at(current, LIVE_START_HHMM)
    end = _at(current, LIVE_END_HHMM)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def broadcast_text(scheduled_start: datetime) -> tuple[str, str]:
    local_start = scheduled_start.astimezone(JST)
    title = f"【全肯定botたん】夜のおしゃべり配信 {local_start:%Y年%-m月%-d日}"
    description = (
        "全肯定botたんが、コメントに全部お返事する1時間の配信だよ。\n"
        "毎日21時から22時までやってるよ。気軽に話しかけてね。\n\n"
        "ボイス: VOICEVOX:春日部つむぎ\n"
    )
    return title, description
