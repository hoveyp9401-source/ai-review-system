from datetime import date, datetime
from zoneinfo import ZoneInfo


def now_in_timezone(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))


def today_in_timezone(timezone: str) -> date:
    return now_in_timezone(timezone).date()


def to_utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
