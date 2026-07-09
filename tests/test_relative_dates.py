from datetime import datetime
from zoneinfo import ZoneInfo

from app.workflows.relative_dates import date_hint_from_text, resolve_relative_weekday


SUNDAY_NIGHT = datetime(2026, 7, 5, 21, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
THURSDAY = datetime(2026, 7, 9, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_unqualified_monday_on_sunday_means_tomorrow():
    result = resolve_relative_weekday("\u5468\u4e00\u9884\u8ba1\u51fa\u5dee\u53bb\u5170\u5dde", received_at=SUNDAY_NIGHT)

    assert result.hint == "tomorrow"
    assert result.days_delta == 1
    assert date_hint_from_text("\u5468\u4e00\u9884\u8ba1\u51fa\u5dee\u53bb\u5170\u5dde", received_at=SUNDAY_NIGHT) == "tomorrow"


def test_unqualified_tuesday_on_sunday_is_future_weekday_not_tomorrow():
    result = resolve_relative_weekday("\u5468\u4e8c\u5e94\u8be5\u51fa\u5dee\u53bb\u897f\u5b81", received_at=SUNDAY_NIGHT)

    assert result.hint == "future_weekday"
    assert result.days_delta == 2


def test_unqualified_monday_later_in_week_defaults_to_past_without_future_marker():
    result = resolve_relative_weekday("\u5468\u4e00\u53bb\u5357\u4eac\u5f00\u5ead", received_at=THURSDAY)

    assert result.hint == "past_weekday"
    assert result.days_delta == -3


def test_unqualified_monday_later_in_week_can_be_future_with_future_marker():
    result = resolve_relative_weekday("\u5468\u4e00\u8ba1\u5212\u53bb\u5357\u4eac\u5f00\u5ead", received_at=THURSDAY)

    assert result.hint == "future_weekday"
    assert result.days_delta == 4
