from datetime import date, datetime
from types import SimpleNamespace

from app.services import report_service


def _draft(*, today_work=None, problems=None, tomorrow_plan=None):
    return SimpleNamespace(
        today_work=list(today_work or []),
        problems=list(problems or []),
        tomorrow_plan=list(tomorrow_plan or []),
        section_status={},
    )


def test_weekday_before_nine_defaults_to_current_calendar_date():
    assert report_service._default_report_date_for_received_at(
        datetime(2026, 7, 15, 8, 0)
    ) == date(2026, 7, 15)


def test_saturday_before_nine_retains_friday_backfill_default():
    assert report_service._default_report_date_for_received_at(
        datetime(2026, 7, 18, 8, 0)
    ) == date(2026, 7, 17)


def test_missing_tomorrow_slot_does_not_capture_chat_acknowledgement():
    report = _draft(today_work=["完成合同审核"], problems=["暂无明显问题"])

    assert report_service._direct_plan_slot_answer_plan("谢谢", report) is None


def test_current_and_future_fact_is_not_collapsed_into_one_tomorrow_item():
    report = _draft()
    text = "昨天日报作为参考，今天完成合同审核，明天继续跟进。"

    assert report_service._looks_like_current_report_content(text) is True
    assert report_service._direct_tomorrow_plan_phrase_plan(text, report) is None


def test_current_fact_that_mentions_yesterday_is_not_cutoff_blocked():
    text = "今天复盘了昨天的合同问题，没有新风险，明天继续跟进印章"

    assert report_service._is_previous_report_blocked_by_cutoff(
        text,
        date(2026, 6, 18),
        datetime(2026, 6, 18, 20, 30),
    ) is False
