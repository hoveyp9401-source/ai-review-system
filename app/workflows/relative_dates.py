from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class RelativeDateResolution:
    hint: str = "unknown"
    days_delta: int | None = None
    weekday: int | None = None
    explicit_prefix: str = ""


_WEEKDAY_VALUES = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}

_WEEKDAY_PATTERN = re.compile(r"(?:(上|下|本|这)?(?:周|星期|礼拜)([一二三四五六日天]))")


def date_hint_from_text(text: str, *, received_at: datetime | None = None) -> str:
    raw = str(text or "")
    if _contains_any(raw, ("\u660e\u65e9", "\u660e\u5929\u65e9\u4e0a", "\u660e\u65e5\u65e9\u4e0a", "\u660e\u5929\u4e0a\u5348", "\u660e\u65e5\u4e0a\u5348")):
        return "tomorrow"
    if _contains_any(raw, ("明天", "明日", "明儿", "明个")):
        return "tomorrow"
    if _contains_any(raw, ("今天", "今日", "本日", "今儿")):
        return "today"
    if _contains_any(raw, ("后天", "大后天")):
        return "future_weekday"
    if _contains_any(raw, ("下周", "下星期", "下礼拜")) and not _contains_any(raw, ("上周", "上星期", "上礼拜")):
        weekday = resolve_relative_weekday(raw, received_at=received_at)
        if weekday.hint in {"today", "tomorrow"}:
            return weekday.hint
        return "next_week"
    weekday = resolve_relative_weekday(raw, received_at=received_at)
    return weekday.hint


def has_relative_day_anchor(text: str, *, received_at: datetime | None = None) -> bool:
    if _contains_any(str(text or ""), ("\u660e\u65e9", "\u660e\u5929\u65e9\u4e0a", "\u660e\u65e5\u65e9\u4e0a", "\u660e\u5929\u4e0a\u5348", "\u660e\u65e5\u4e0a\u5348")):
        return True
    if _contains_any(str(text or ""), ("今天", "今日", "今儿", "明天", "明日", "后天", "大后天", "昨天", "昨日")):
        return True
    return resolve_relative_weekday(text, received_at=received_at).hint != "unknown"


def has_past_day_anchor(text: str) -> bool:
    return _contains_any(str(text or ""), ("昨天", "昨日", "昨儿", "前天", "前日", "上一工作日", "上一个工作日"))


def has_current_day_anchor(text: str) -> bool:
    return _contains_any(str(text or ""), ("今天", "今日", "今儿", "本日"))


def has_repeat_relation(text: str) -> bool:
    raw = str(text or "")
    compact = re.sub(r"[\s\u3000，,。.!！?？:：；;]+", "", raw)
    return _contains_any(
        compact,
        (
            "一样",
            "差不多",
            "还是那些",
            "还是那几",
            "照旧",
            "照着",
            "同昨天",
            "同昨日",
            "同昨儿",
            "继续那些",
            "接着干",
        ),
    ) or compact.endswith(("呢", "一样呢", "差不多呢"))


def has_previous_to_current_repeat_reference(text: str) -> bool:
    return has_past_day_anchor(text) and has_current_day_anchor(text) and has_repeat_relation(text)


def resolve_relative_weekday(
    text: str,
    *,
    received_at: datetime | None = None,
) -> RelativeDateResolution:
    raw = str(text or "")
    match = _WEEKDAY_PATTERN.search(raw)
    if not match:
        return RelativeDateResolution()
    prefix = match.group(1) or ""
    weekday_text = match.group(2)
    target_weekday = _WEEKDAY_VALUES.get(weekday_text)
    if target_weekday is None:
        return RelativeDateResolution()

    today = _reference_date(received_at)
    current_weekday = today.weekday()
    days_delta = _days_delta(prefix, current_weekday=current_weekday, target_weekday=target_weekday)
    hint = _hint_for_delta(days_delta)
    if not prefix and days_delta is not None and days_delta < 0 and _looks_future_or_tentative(raw):
        days_delta = (target_weekday - current_weekday) % 7
        if days_delta == 0:
            days_delta = 7
        hint = _hint_for_delta(days_delta)
    return RelativeDateResolution(
        hint=hint,
        days_delta=days_delta,
        weekday=target_weekday,
        explicit_prefix=prefix,
    )


def _days_delta(prefix: str, *, current_weekday: int, target_weekday: int) -> int:
    if prefix == "上":
        return target_weekday - current_weekday - 7
    if prefix == "下":
        return target_weekday - current_weekday + 7
    if prefix in {"本", "这"}:
        return target_weekday - current_weekday
    delta = target_weekday - current_weekday
    if delta < 0 and current_weekday >= 5:
        return (target_weekday - current_weekday) % 7
    return delta


def _hint_for_delta(days_delta: int | None) -> str:
    if days_delta is None:
        return "unknown"
    if days_delta == 0:
        return "today"
    if days_delta == 1:
        return "tomorrow"
    if days_delta > 1:
        return "future_weekday"
    return "past_weekday"


def _reference_date(received_at: datetime | None) -> date:
    if received_at is None:
        return datetime.now(LOCAL_TZ).date()
    if received_at.tzinfo is None:
        return received_at.date()
    return received_at.astimezone(LOCAL_TZ).date()


def _looks_future_or_tentative(text: str) -> bool:
    return _contains_any(text, ("预计", "计划", "准备", "打算", "拟", "可能", "应该", "要去", "将"))


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)
