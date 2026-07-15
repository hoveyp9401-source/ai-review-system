from datetime import date
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.scheduler import jobs
from app.scheduler.jobs import (
    auto_submit_due_pending_reports,
    build_report_reminder_text,
    mark_report_auto_submitted,
    remind_missing_reports,
    send_user_message,
)
from app.scheduler.runner import _send_daily_briefings
from app.scheduler.runner import _catchup_reminder_report_date
from app.scheduler.runner import _daily_briefing_report_date
from app.scheduler.runner import _reporting_required_on
from app.scheduler.runner import _scheduler_paused
from app.scheduler.runner import _scheduler_pause_dates
from app.services.state_machine import CONFIRMATION_AUTO_SUBMITTED_TIMEOUT, STATUS_COMPLETED


def _user(name="Pang Hao"):
    return SimpleNamespace(id=uuid4(), name=name, dingtalk_user_id="user-1")


def _report(**overrides):
    values = {
        "status": "collecting",
        "today_work": [],
        "problems": [],
        "tomorrow_plan": [],
        "section_status": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_scheduler_pause_dates_parse_configured_holiday_range():
    settings = SimpleNamespace(scheduler_pause_dates="2026-06-19, 2026-06-20\n2026-06-21")

    assert _scheduler_pause_dates(settings) == {
        date(2026, 6, 19),
        date(2026, 6, 20),
        date(2026, 6, 21),
    }
    assert _scheduler_paused(settings, date(2026, 6, 19)) is True
    assert _scheduler_paused(settings, date(2026, 6, 22)) is False


def test_reporting_calendar_skips_weekend_employee_reminders():
    assert _reporting_required_on(date(2026, 6, 26)) is True
    assert _reporting_required_on(date(2026, 6, 27)) is False
    assert _reporting_required_on(date(2026, 6, 28)) is False
    assert _reporting_required_on(date(2026, 6, 29)) is True


def test_daily_briefing_calendar_sends_friday_on_saturday_and_skips_sunday_monday():
    assert _daily_briefing_report_date(date(2026, 6, 27)) == date(2026, 6, 26)
    assert _daily_briefing_report_date(date(2026, 6, 28)) is None
    assert _daily_briefing_report_date(date(2026, 6, 29)) is None
    assert _daily_briefing_report_date(date(2026, 6, 30)) == date(2026, 6, 29)


def test_catchup_reminder_calendar_skips_weekends_and_weekend_report_dates():
    assert _catchup_reminder_report_date(date(2026, 6, 27)) is None
    assert _catchup_reminder_report_date(date(2026, 6, 28)) is None
    assert _catchup_reminder_report_date(date(2026, 6, 29)) is None
    assert _catchup_reminder_report_date(date(2026, 6, 30)) == date(2026, 6, 29)


def test_reminder_text_for_user_without_report():
    text = build_report_reminder_text(date(2026, 6, 15), _user(), None)

    assert "\u5230\u4e86\u4eca\u5929\u7684\u590d\u76d8\u65f6\u95f4" in text
    assert "\u4eca\u5929\u4e3b\u8981\u505a\u4e86\u4ec0\u4e48" in text
    assert "\u4e0d\u7528\u5199\u5f97\u5f88\u6b63\u5f0f" in text


def test_reminder_text_for_collecting_report_lists_missing_fields():
    text = build_report_reminder_text(
        date(2026, 6, 15),
        _user(),
        _report(today_work=["reviewed contracts"], section_status={"today_work": True}),
    )

    assert "\u5df2\u7ecf\u8bb0\u5f55\u4e86\u4e00\u90e8\u5206" in text
    assert "\u95ee\u9898/\u98ce\u9669" in text
    assert "\u660e\u65e5\u8ba1\u5212" in text
    assert "\u4eca\u65e5\u5de5\u4f5c" not in text


def test_reminder_text_for_pending_confirmation_asks_to_confirm():
    text = build_report_reminder_text(
        date(2026, 6, 15),
        _user(),
        _report(
            status="pending_confirmation",
            today_work=["reviewed contracts"],
            problems=["no issue"],
            tomorrow_plan=["follow up"],
        ),
    )

    assert "\u8fd8\u5dee\u6700\u540e\u786e\u8ba4" in text
    assert "\u56de\u590d\u201c\u786e\u8ba4\u201d" in text


def test_catchup_reminder_text_for_yesterday_missing_report():
    text = build_report_reminder_text(date(2026, 6, 14), _user(), None, reminder_kind="catchup")

    assert "2026-06-14" in text
    assert "\u65e9\u4e0a\u597d" in text
    assert "\u590d\u76d8\u8fd8\u6ca1\u6709\u5f00\u59cb" in text


def test_catchup_reminder_text_for_collecting_report_uses_yesterday_wording():
    text = build_report_reminder_text(
        date(2026, 6, 14),
        _user(),
        _report(today_work=["reviewed contracts"], section_status={"today_work": True}),
        reminder_kind="catchup",
    )

    assert "\u4f60\u6628\u5929\u7684\u590d\u76d8" in text
    assert "\u4f60\u4eca\u5929\u7684\u590d\u76d8" not in text


def test_catchup_reminder_text_for_pending_confirmation_uses_yesterday_wording():
    text = build_report_reminder_text(
        date(2026, 6, 14),
        _user(),
        _report(
            status="pending_confirmation",
            today_work=["reviewed contracts"],
            problems=["no issue"],
            tomorrow_plan=["follow up"],
        ),
        reminder_kind="catchup",
    )

    assert "\u6574\u7406\u597d\u6628\u5929\u7684\u590d\u76d8" in text
    assert "\u6574\u7406\u597d\u4eca\u5929\u7684\u590d\u76d8" not in text


def test_mark_report_auto_submitted_updates_confirmation_fields():
    now = datetime(2026, 6, 15, 23, 0, tzinfo=timezone.utc)
    report = _report(status="pending_confirmation", confirmed_by_user=True, auto_submit_at=now)

    mark_report_auto_submitted(report, now)

    assert report.status == STATUS_COMPLETED
    assert report.confirmation_type == CONFIRMATION_AUTO_SUBMITTED_TIMEOUT
    assert report.confirmed_by_user is False
    assert report.submitted_at == now
    assert report.auto_submit_at is None


@pytest.mark.asyncio
async def test_reminder_dry_run_does_not_send(monkeypatch):
    class Team:
        name = "Team A"
        dingtalk_webhook_url = "https://example.invalid"
        dingtalk_webhook_secret = None

        def __hash__(self):
            return id(self)

    team = Team()
    user = _user()
    user.team = team

    async def fake_list_missing_users(session, report_date):
        return [user]

    async def fake_load_reports_by_user(session, report_date, user_ids):
        return {}

    class Robot:
        sent = False

        def has_enterprise_app(self):
            return True

        async def send_text(self, **kwargs):
            self.sent = True

        async def send_robot_direct_text(self, **kwargs):
            self.sent = True

    robot = Robot()
    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports_by_user)

    result = await remind_missing_reports(
        SimpleNamespace(),
        SimpleNamespace(
            timezone="Asia/Shanghai",
            dingtalk_default_robot_webhook="",
            dingtalk_default_robot_secret="",
            reminder_send_enabled=False,
            reminder_dry_run=True,
            reminder_test_user_ids="user-1",
        ),
        robot,
        date(2026, 6, 15),
        dry_run=True,
    )

    assert robot.sent is False
    assert result["dry_run"] is True
    assert result["sent"] == 0
    assert result["real_sent"] == 0
    assert result["target_users"] == 1
    assert result["would_send"] == 1
    assert result["skipped_real_users"] == 0
    assert result["dry_run_messages"][0]["target_count"] == 1


async def _run_first_reminder_dry_run(monkeypatch, report, *, reminder_kind="daily"):
    class Team:
        name = "Team A"
        dingtalk_webhook_url = ""
        dingtalk_webhook_secret = None

        def __hash__(self):
            return id(self)

    user = _user()
    user.team = Team()

    async def fake_list_missing_users(session, report_date):
        return [user]

    async def fake_load_reports_by_user(session, report_date, user_ids):
        assert report_date == date(2026, 6, 15)
        assert user_ids == [user.id]
        return {user.id: report} if report is not None else {}

    class Robot:
        sent = False

        def has_enterprise_app(self):
            return True

        async def send_text(self, **kwargs):
            self.sent = True

        async def send_robot_direct_text(self, **kwargs):
            self.sent = True

    robot = Robot()
    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports_by_user)

    result = await remind_missing_reports(
        SimpleNamespace(),
        SimpleNamespace(
            timezone="Asia/Shanghai",
            dingtalk_default_robot_webhook="",
            dingtalk_default_robot_secret="",
            reminder_send_enabled=False,
            reminder_dry_run=True,
            reminder_test_user_ids="user-1",
        ),
        robot,
        date(2026, 6, 15),
        reminder_kind=reminder_kind,
        dry_run=True,
    )
    assert robot.sent is False
    return result


@pytest.mark.asyncio
async def test_first_reminder_skips_completed_report(monkeypatch):
    result = await _run_first_reminder_dry_run(
        monkeypatch,
        _report(
            status=STATUS_COMPLETED,
            today_work=["审核合同"],
            problems=["暂无明显问题"],
            tomorrow_plan=["明天继续跟进"],
        ),
    )

    assert result["missing_count"] == 1
    assert result["target_users"] == 0
    assert result["would_send"] == 0
    assert result["real_sent"] == 0
    assert result["dry_run_messages"] == []


@pytest.mark.asyncio
async def test_first_reminder_pending_confirmation_does_not_send_start_prompt(monkeypatch):
    result = await _run_first_reminder_dry_run(
        monkeypatch,
        _report(
            status="pending_confirmation",
            today_work=["审核合同"],
            problems=["暂无明显问题"],
            tomorrow_plan=["明天继续跟进"],
        ),
    )

    assert result["target_users"] == 1
    assert result["would_send"] == 1
    text = result["dry_run_messages"][0]["text"]
    assert "\u8fd8\u5dee\u6700\u540e\u786e\u8ba4" in text
    assert "\u8fd8\u6ca1\u6709\u6536\u5230" not in text


@pytest.mark.asyncio
async def test_confirmation_reminder_sent_once_per_day_skips_second_reminder(monkeypatch):
    result = await _run_first_reminder_dry_run(
        monkeypatch,
        _report(
            status="pending_confirmation",
            today_work=["reviewed contracts"],
            problems=["no issue"],
            tomorrow_plan=["follow up"],
            section_status={
                jobs.CONFIRMATION_REMINDED_ON_KEY: "2026-06-15",
                jobs.CONFIRMATION_REMINDED_AT_KEY: "2026-06-15T20:00:00+08:00",
            },
        ),
        reminder_kind="second",
    )

    assert result["target_users"] == 0
    assert result["would_send"] == 0
    assert result["real_sent"] == 0
    assert result["dry_run_messages"] == []


@pytest.mark.asyncio
async def test_second_reminder_sends_confirmation_if_not_sent_today(monkeypatch):
    result = await _run_first_reminder_dry_run(
        monkeypatch,
        _report(
            status="pending_confirmation",
            today_work=["reviewed contracts"],
            problems=["no issue"],
            tomorrow_plan=["follow up"],
        ),
        reminder_kind="second",
    )

    assert result["target_users"] == 1
    assert result["would_send"] == 1
    text = result["dry_run_messages"][0]["text"]
    assert "\u8fd8\u5dee\u6700\u540e\u786e\u8ba4" in text
    assert "\u8fd8\u6ca1\u6709\u6536\u5230" not in text


@pytest.mark.asyncio
async def test_first_reminder_collecting_report_lists_missing_fields(monkeypatch):
    result = await _run_first_reminder_dry_run(
        monkeypatch,
        _report(
            status="collecting",
            today_work=["审核合同"],
            problems=[],
            tomorrow_plan=[],
            section_status={"today_work": True},
        ),
    )

    assert result["target_users"] == 1
    assert result["would_send"] == 1
    text = result["dry_run_messages"][0]["text"]
    assert "\u5df2\u7ecf\u8bb0\u5f55\u4e86\u4e00\u90e8\u5206" in text
    assert "\u95ee\u9898/\u98ce\u9669" in text
    assert "\u660e\u65e5\u8ba1\u5212" in text
    assert "\u8fd8\u6ca1\u6709\u6536\u5230" not in text


@pytest.mark.asyncio
async def test_second_reminder_collecting_report_still_lists_missing_fields(monkeypatch):
    result = await _run_first_reminder_dry_run(
        monkeypatch,
        _report(
            status="collecting",
            today_work=["reviewed contracts"],
            problems=[],
            tomorrow_plan=[],
            section_status={"today_work": True},
        ),
        reminder_kind="second",
    )

    assert result["target_users"] == 1
    assert result["would_send"] == 1
    text = result["dry_run_messages"][0]["text"]
    assert "\u5982\u679c\u4eca\u665a\u4e0d\u518d\u8865\u5145" in text
    assert "\u95ee\u9898/\u98ce\u9669" in text
    assert "\u660e\u65e5\u8ba1\u5212" in text


@pytest.mark.asyncio
async def test_first_reminder_without_report_sends_start_prompt(monkeypatch):
    result = await _run_first_reminder_dry_run(monkeypatch, None)

    assert result["target_users"] == 1
    assert result["would_send"] == 1
    text = result["dry_run_messages"][0]["text"]
    assert "\u5230\u4e86\u4eca\u5929\u7684\u590d\u76d8\u65f6\u95f4" in text
    assert "\u4eca\u5929\u4e3b\u8981\u505a\u4e86\u4ec0\u4e48" in text


@pytest.mark.asyncio
async def test_reminder_without_test_user_ids_blocks_real_send(monkeypatch):
    class Team:
        name = "Team A"
        dingtalk_webhook_url = ""
        dingtalk_webhook_secret = None

        def __hash__(self):
            return id(self)

    user = _user()
    user.team = Team()

    async def fake_list_missing_users(session, report_date):
        return [user]

    async def fake_load_reports_by_user(session, report_date, user_ids):
        return {}

    class Robot:
        sent = False

        def has_enterprise_app(self):
            return True

        async def send_text(self, **kwargs):
            self.sent = True

        async def send_robot_direct_text(self, **kwargs):
            self.sent = True

    robot = Robot()
    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports_by_user)

    result = await remind_missing_reports(
        SimpleNamespace(),
        SimpleNamespace(
            timezone="Asia/Shanghai",
            dingtalk_default_robot_webhook="",
            dingtalk_default_robot_secret="",
            reminder_send_enabled=True,
            reminder_dry_run=False,
            reminder_test_user_ids="",
        ),
        robot,
        date(2026, 6, 15),
        dry_run=False,
    )

    assert robot.sent is False
    assert result["dry_run"] is True
    assert result["target_users"] == 0
    assert result["would_send"] == 0
    assert result["real_sent"] == 0
    assert result["skipped_real_users"] == 1
    assert "no_test_user_ids" in result["send_block_reasons"]


@pytest.mark.asyncio
async def test_reminder_real_send_is_limited_to_test_user_ids(monkeypatch):
    class Team:
        name = "Team A"
        dingtalk_webhook_url = ""
        dingtalk_webhook_secret = None

        def __hash__(self):
            return id(self)

    team = Team()
    test_user = _user()
    test_user.team = team
    real_user = _user("Real User")
    real_user.dingtalk_user_id = "user-2"
    real_user.team = team

    async def fake_list_missing_users(session, report_date):
        return [test_user, real_user]

    async def fake_load_reports_by_user(session, report_date, user_ids):
        return {}

    class Session:
        def __init__(self):
            self.added = []

        async def execute(self, query):
            class Result:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return Result()

        def add(self, value):
            self.added.append(value)

    class Robot:
        def __init__(self):
            self.direct_user_ids = []

        def has_enterprise_app(self):
            return True

        async def send_text(self, **kwargs):
            raise AssertionError("group robot should not be used for test-user sends")

        async def send_robot_direct_text(self, **kwargs):
            self.direct_user_ids.extend(kwargs["user_ids"])

    robot = Robot()
    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports_by_user)

    session = Session()
    result = await remind_missing_reports(
        session,
        SimpleNamespace(
            timezone="Asia/Shanghai",
            dingtalk_default_robot_webhook="https://example.invalid",
            dingtalk_default_robot_secret="",
            reminder_send_enabled=True,
            reminder_dry_run=False,
            reminder_test_user_ids="user-1",
        ),
        robot,
        date(2026, 6, 15),
        dry_run=False,
    )

    assert robot.direct_user_ids == ["user-1"]
    assert result["dry_run"] is False
    assert result["target_users"] == 1
    assert result["would_send"] == 0
    assert result["real_sent"] == 1
    assert result["skipped_real_users"] == 1
    assert len(session.added) == 1
    reminder_event = session.added[0]
    assert reminder_event.backend_action == "daily_report_reminder_sent"
    assert reminder_event.user_id == test_user.id
    assert reminder_event.report_date == date(2026, 6, 15)
    assert reminder_event.report_id is None
    assert reminder_event.llm_decision_json["reminder_kind"] == "daily"


@pytest.mark.asyncio
async def test_send_user_message_falls_back_to_work_notification():
    class Robot:
        def __init__(self):
            self.direct_called = False
            self.work_notification_user_ids = []

        async def send_robot_direct_text(self, **kwargs):
            self.direct_called = True
            raise RuntimeError("robot direct not enabled")

        async def send_work_notification(self, **kwargs):
            self.work_notification_user_ids.extend(kwargs["user_ids"])

    robot = Robot()

    channel = await send_user_message(robot, ["user-1"], "hello")

    assert channel == "work_notification"
    assert robot.direct_called is True
    assert robot.work_notification_user_ids == ["user-1"]


@pytest.mark.asyncio
async def test_auto_submit_submits_collecting_reports_with_content_only():
    due_pending = _report(
        status="pending_confirmation",
        today_work=["reviewed contracts"],
        auto_submit_at=datetime(2026, 6, 15, 21, 0, tzinfo=timezone.utc),
        id=uuid4(),
    )
    collecting_with_content = _report(status="collecting", tomorrow_plan=["follow up"], id=uuid4())
    blank_collecting = _report(status="collecting", id=uuid4())
    future_pending = _report(
        status="pending_confirmation",
        today_work=["reviewed contracts"],
        auto_submit_at=datetime(2026, 6, 16, 21, 0, tzinfo=timezone.utc),
        id=uuid4(),
    )

    class Session:
        async def execute(self, query):
            class Result:
                def scalars(self):
                    return self

                def all(self):
                    return [due_pending, collecting_with_content, blank_collecting, future_pending]

            return Result()

    now = datetime(2026, 6, 15, 22, 0, tzinfo=timezone.utc)
    result = await auto_submit_due_pending_reports(Session(), SimpleNamespace(timezone="Asia/Shanghai"), now=now)

    assert result["auto_submitted"] == 2
    assert due_pending.status == STATUS_COMPLETED
    assert collecting_with_content.status == STATUS_COMPLETED
    assert blank_collecting.status == "collecting"
    assert future_pending.status == "pending_confirmation"


@pytest.mark.asyncio
async def test_send_daily_briefings_targets_team_leader_recipients():
    class Robot:
        def __init__(self):
            self.sent = []

        async def send_robot_direct_text(self, **kwargs):
            self.sent.append((kwargs["user_ids"], kwargs["text"]))

    robot = Robot()
    briefings = {
        "team_messages": [
            {
                "recipients": [{"dingtalk_user_id": "leader-1"}],
                "text": "\u56e2\u961f\u65e5\u62a5\u6668\u62a5",
            },
            {
                "recipients": [],
                "text": "\u4e0d\u5e94\u53d1\u9001",
            },
        ]
    }

    sent = await _send_daily_briefings(robot, briefings)

    assert sent == 1
    assert robot.sent == [(["leader-1"], "\u56e2\u961f\u65e5\u62a5\u6668\u62a5")]
