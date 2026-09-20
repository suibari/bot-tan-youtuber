"""YouTube Liveの当日スケジュールと公開文言の共通定義。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import LIVE_END_HHMM, LIVE_START_HHMM

JST = timezone(timedelta(hours=9))
# インストール済みtimerが実際の配信頻度。未インストール環境だけリポジトリの定義を使う。
INSTALLED_LIVE_TIMER = Path("/etc/systemd/system/bottan-live.timer")
LIVE_TIMER = (INSTALLED_LIVE_TIMER if INSTALLED_LIVE_TIMER.exists()
              else Path(__file__).resolve().parents[1] / "setup/bottan-live.timer")
WEEKDAYS = {name: index for index, name in enumerate(
    ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
)}


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


def live_weekdays() -> set[int]:
    """配信timerの曜日を読む。日次へ戻した場合も枠作成が同じ曜日に追従する。"""
    calendars = [line.split("=", 1)[1].strip() for line in LIVE_TIMER.read_text().splitlines()
                 if line.startswith("OnCalendar=")]
    if len(calendars) != 1:
        raise ValueError(f"配信timerのOnCalendarは1件にしてください: {LIVE_TIMER}")
    parts = calendars[0].split()
    if parts[0] == "*-*-*":
        return set(range(7))
    if len(parts) >= 2 and parts[1] == "*-*-*":
        try:
            return {WEEKDAYS[name] for name in parts[0].split(",")}
        except KeyError as error:
            raise ValueError(f"未対応の配信曜日: {parts[0]}") from error
    raise ValueError(f"未対応の配信timer: {calendars[0]}")


def upcoming_schedules(now: datetime | None = None, count: int = 2
                       ) -> list[tuple[datetime, datetime]]:
    """配信timerの曜日に沿った、これから始まる枠を近い順に返す。"""
    current = now.astimezone(JST) if now else datetime.now(JST)
    weekdays = live_weekdays()
    result = []
    for offset in range(7 * (count + 1)):
        day = current + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        start, end = today_schedule(day)
        if start <= current:
            continue
        result.append((start, end))
        if len(result) == count:
            return result
    raise ValueError("次回の配信日を決められません")


def broadcast_text(scheduled_start: datetime) -> tuple[str, str]:
    local_start = scheduled_start.astimezone(JST)
    title = f"【全肯定botたん】夜のおしゃべり配信 {local_start:%Y年%-m月%-d日}"
    weekday = "月火水木金土日"[local_start.weekday()]
    description = (
        "全肯定botたんが、コメントに全部お返事する1時間の配信だよ。\n"
        f"{local_start:%Y年%-m月%-d日}（{weekday}）{local_start:%H:%M}からやるよ。"
        "気軽に話しかけてね。\n\n"
        "ボイス: VOICEVOX:春日部つむぎ\n"
    )
    return title, description
