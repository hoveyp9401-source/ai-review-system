from __future__ import annotations

from datetime import date, datetime
import os
from zoneinfo import ZoneInfo


_TIME_OVERRIDE: datetime | None = None


def set_time_override(value: datetime | None) -> None:
    global _TIME_OVERRIDE
    _TIME_OVERRIDE = value
    if value is None:
        os.environ.pop("CLOCK_OVERRIDE_NOW", None)
    else:
        os.environ["CLOCK_OVERRIDE_NOW"] = value.isoformat()


def get_time_override(timezone_name: str) -> datetime | None:
    if not _clock_override_enabled():
        return None
    override = _TIME_OVERRIDE or _parse_override(os.getenv("CLOCK_OVERRIDE_NOW", ""), timezone_name)
    if override is None:
        return None
    zone = ZoneInfo(timezone_name or "Asia/Shanghai")
    if override.tzinfo is None:
        return override.replace(tzinfo=zone)
    return override.astimezone(zone)


def clear_time_override() -> None:
    set_time_override(None)


def now_in_timezone(timezone_name: str) -> datetime:
    override = get_time_override(timezone_name)
    if override is not None:
        return override
    return datetime.now(ZoneInfo(timezone_name or "Asia/Shanghai"))


def today_in_timezone(timezone_name: str) -> date:
    return now_in_timezone(timezone_name).date()


def _clock_override_enabled() -> bool:
    value = os.getenv("CLOCK_OVERRIDE_ENABLED", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_override(value: str, timezone_name: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    zone = ZoneInfo(timezone_name or "Asia/Shanghai")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)
