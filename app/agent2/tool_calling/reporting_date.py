from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


MORNING_DAILY_CUTOFF = time(9)


def default_daily_write_date(*, now: datetime, timezone: str) -> date:
    """Return the server-owned reporting date for an undated daily write."""

    local_now = now.astimezone(ZoneInfo(timezone))
    if local_now.timetz().replace(tzinfo=None) < MORNING_DAILY_CUTOFF:
        return local_now.date() - timedelta(days=1)
    return local_now.date()
