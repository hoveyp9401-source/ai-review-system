from datetime import date
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

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
from app.scheduler.runner import _auto_submit_report_date
from app.scheduler.runner import _catchup_reminder_report_date
from app.scheduler.runner import _daily_briefing_report_date
from app.scheduler.runner import _reporting_required_on
from app.scheduler.runner import _scheduler_paused
from app.scheduler.runner import _scheduler_pause_dates
from app.services.state_machine import CONFIRMATION_AUTO_SUBMITTED_TIMEOUT, STATUS_COMPLETED


async def _no_daily_reminder_preferences(*_args, **_kwargs):
    return {}


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


def test_eight_am_auto_submit_targets_previous_reporting_day():
    assert _auto_submit_report_date(date(2026, 6, 16)) == date(2026, 6, 15)
    assert _auto_submit_report_date(date(2026, 6, 27)) == date(2026, 6, 26)
    assert _auto_submit_report_date(date(2026, 6, 29)) is None


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


def test_reminder_text_for_pending_confirmation_does_not_claim_a_missing_section():
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

    assert "\u7cfb\u7edf\u4f1a\u6309\u65f6\u81ea\u52a8\u63d0\u4ea4" in text
    assert "\u65e0\u9700\u518d\u786e\u8ba4" in text
    assert "\u8fd8\u5dee\u6700\u540e\u786e\u8ba4" not in text


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

    assert "\u6628\u5929\u7684\u590d\u76d8\u5df2\u7ecf\u8865\u5145\u5b8c\u6574" in text
    assert "\u5f85\u786e\u8ba4\u72b6\u6001" in text
    assert "\u8bf7\u786e\u8ba4\u65e0\u8bef\u540e\u518d\u63d0\u4ea4" in text
    assert "\u6211\u4e0d\u4f1a\u4ee3\u4f60\u786e\u8ba4\u6216\u63d0\u4ea4" in text
    assert "\u4eca\u5929\u7684\u590d\u76d8" not in text


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
    monkeypatch.setattr(
        jobs,
        "_load_daily_reminder_preferences",
        _no_daily_reminder_preferences,
    )

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


@pytest.mark.asyncio
async def test_reminder_scope_excludes_active_accounts_outside_formal_roster(monkeypatch):
    class Team:
        name = "Team A"
        dingtalk_webhook_url = ""
        dingtalk_webhook_secret = None

        def __hash__(self):
            return id(self)

    roster_user = _user("Roster User")
    roster_user.team = Team()
    outside_user = _user("Outside User")
    outside_user.id = UUID("20000000-0000-0000-0000-000000000099")
    outside_user.dingtalk_user_id = "outside-user"
    outside_user.team = Team()

    async def fake_list_missing_users(session, report_date):
        return [roster_user, outside_user]

    async def fake_load_roster(*_args, **_kwargs):
        return SimpleNamespace(
            user_ids=(str(roster_user.id),),
            dingtalk_user_ids=(roster_user.dingtalk_user_id,),
            member_count=1,
            members=(
                SimpleNamespace(
                    user_id=str(roster_user.id),
                    user_name=roster_user.name,
                    dingtalk_user_id=roster_user.dingtalk_user_id,
                ),
            ),
        )

    async def fake_load_reports_by_user(session, report_date, user_ids):
        assert user_ids == [roster_user.id]
        return {}

    class Robot:
        def has_enterprise_app(self):
            return True

    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(
        jobs,
        "load_formal_legal_daily_roster",
        fake_load_roster,
        raising=False,
    )
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports_by_user)
    monkeypatch.setattr(
        jobs,
        "_load_daily_reminder_preferences",
        _no_daily_reminder_preferences,
    )

    result = await remind_missing_reports(
        SimpleNamespace(),
        SimpleNamespace(
            timezone="Asia/Shanghai",
            legal_daily_dashboard_tenant_id="legal-daily-production-v1",
            dingtalk_default_robot_webhook="",
            dingtalk_default_robot_secret="",
            reminder_send_enabled=False,
            reminder_dry_run=True,
            reminder_test_user_ids="user-1",
        ),
        Robot(),
        date(2026, 8, 9),
        dry_run=True,
    )

    assert result["target_users"] == 1
    assert result["skipped_real_users"] == 0


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
    monkeypatch.setattr(
        jobs,
        "_load_daily_reminder_preferences",
        _no_daily_reminder_preferences,
    )

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

    assert result["missing_count"] == 0
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

    assert result["missing_count"] == 0
    assert result["target_users"] == 0
    assert result["would_send"] == 0
    assert result["dry_run_messages"] == []


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
async def test_second_reminder_skips_complete_pending_confirmation(monkeypatch):
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

    assert result["missing_count"] == 0
    assert result["target_users"] == 0
    assert result["would_send"] == 0
    assert result["dry_run_messages"] == []


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
    assert "\u5982\u679c\u660e\u65e98\u70b9\u524d\u4e0d\u518d\u8865\u5145" in text
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
    monkeypatch.setattr(
        jobs,
        "_load_daily_reminder_preferences",
        _no_daily_reminder_preferences,
    )

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
            return {"processQueryKey": "provider-query-1"}

    robot = Robot()
    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports_by_user)
    monkeypatch.setattr(
        jobs,
        "_load_daily_reminder_preferences",
        _no_daily_reminder_preferences,
    )

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
    assert reminder_event.backend_action == "daily_report_reminder_delivery_pending"
    assert reminder_event.user_id == test_user.id
    assert reminder_event.report_date == date(2026, 6, 15)
    assert reminder_event.report_id is None
    assert reminder_event.llm_decision_json["reminder_kind"] == "daily"
    assert reminder_event.llm_decision_json["message_status"] == "accepted_by_provider"
    assert reminder_event.llm_decision_json["provider_reference"] == "provider-query-1"
    assert reminder_event.llm_decision_json["provider_reference_available"] is True
    assert reminder_event.llm_decision_json["provider_message_id_available"] is False
    assert reminder_event.llm_decision_json["transport"] == "direct_robot"
    assert reminder_event.llm_decision_json["delivery_verified"] is False


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
            return {"task_id": "work-task-1"}

    robot = Robot()

    evidence = await send_user_message(robot, ["user-1"], "hello")

    assert evidence.channel == "work_notification"
    assert evidence.provider_reference == "work-task-1"
    assert evidence.message_status == "accepted_by_provider"
    assert robot.direct_called is True
    assert robot.work_notification_user_ids == ["user-1"]


@pytest.mark.asyncio
async def test_send_user_message_fails_closed_without_provider_reference():
    class Robot:
        async def send_robot_direct_text(self, **kwargs):
            return {}

        async def send_work_notification(self, **kwargs):
            return {}

    with pytest.raises(RuntimeError, match="provider reference"):
        await send_user_message(Robot(), ["user-1"], "hello")


@pytest.mark.asyncio
async def test_auto_submit_locks_reloads_and_submits_reports_with_content_only(
    monkeypatch,
):
    report_date = date(2026, 6, 15)
    due_pending = _report(
        status="pending_confirmation",
        today_work=["reviewed contracts"],
        auto_submit_at=datetime(2026, 6, 15, 21, 0, tzinfo=timezone.utc),
        id=uuid4(),
        user_id=uuid4(),
        report_date=report_date,
    )
    collecting_with_content = _report(
        status="collecting",
        tomorrow_plan=["follow up"],
        id=uuid4(),
        user_id=uuid4(),
        report_date=report_date,
    )
    blank_collecting = _report(
        status="collecting",
        id=uuid4(),
        user_id=uuid4(),
        report_date=report_date,
    )
    future_pending = _report(
        status="pending_confirmation",
        today_work=["reviewed contracts"],
        auto_submit_at=datetime(2026, 6, 16, 21, 0, tzinfo=timezone.utc),
        id=uuid4(),
        user_id=uuid4(),
        report_date=report_date,
    )

    class Session:
        async def execute(self, query):
            class Result:
                def all(self):
                    return [
                        (report.user_id, report.report_date)
                        for report in (
                            due_pending,
                            collecting_with_content,
                            blank_collecting,
                            future_pending,
                        )
                    ]

            return Result()

    reports = {
        (report.user_id, report.report_date): report
        for report in (
            due_pending,
            collecting_with_content,
            blank_collecting,
            future_pending,
        )
    }
    locked = []

    async def fake_lock(session, user_id, report_date):
        del session
        locked.append((user_id, report_date))

    async def fake_get_report(session, user_id, report_date):
        del session
        assert (user_id, report_date) in locked
        return reports[(user_id, report_date)]

    monkeypatch.setattr(jobs, "acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr(jobs, "get_report", fake_get_report)
    now = datetime(2026, 6, 15, 22, 0, tzinfo=timezone.utc)
    result = await auto_submit_due_pending_reports(Session(), SimpleNamespace(timezone="Asia/Shanghai"), now=now)

    assert result["auto_submitted"] == 3
    assert len(locked) == 4
    assert due_pending.status == STATUS_COMPLETED
    assert collecting_with_content.status == STATUS_COMPLETED
    assert blank_collecting.status == "collecting"
    assert future_pending.status == STATUS_COMPLETED


@pytest.mark.asyncio
async def test_auto_submit_reloads_after_lock_and_never_overwrites_newer_state(
    monkeypatch,
):
    report_date = date(2026, 6, 15)
    user_id = uuid4()
    candidate = _report(
        id=uuid4(),
        user_id=user_id,
        report_date=report_date,
        status="collecting",
        today_work=["reviewed contracts"],
    )
    reloaded = _report(
        id=candidate.id,
        user_id=user_id,
        report_date=report_date,
        status=STATUS_COMPLETED,
        today_work=["reviewed final contracts"],
    )

    class Session:
        async def execute(self, query):
            del query

            class Result:
                def all(self):
                    return [(user_id, report_date)]

            return Result()

    lock_acquired = False

    async def fake_lock(session, candidate_user_id, candidate_report_date):
        nonlocal lock_acquired
        del session
        assert (candidate_user_id, candidate_report_date) == (
            user_id,
            report_date,
        )
        lock_acquired = True

    async def fake_get_report(session, candidate_user_id, candidate_report_date):
        del session
        assert lock_acquired is True
        assert (candidate_user_id, candidate_report_date) == (
            user_id,
            report_date,
        )
        return reloaded

    monkeypatch.setattr(jobs, "acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr(jobs, "get_report", fake_get_report)

    result = await auto_submit_due_pending_reports(
        Session(),
        SimpleNamespace(timezone="Asia/Shanghai"),
        now=datetime(2026, 6, 15, 22, 0, tzinfo=timezone.utc),
        report_date=report_date,
    )

    assert result["auto_submitted"] == 0
    assert reloaded.status == STATUS_COMPLETED
    assert reloaded.today_work == ["reviewed final contracts"]


@pytest.mark.asyncio
async def test_send_daily_briefings_targets_team_leader_recipients():
    class Robot:
        def __init__(self):
            self.sent = []

        async def send_robot_direct_text(self, **kwargs):
            self.sent.append((kwargs["user_ids"], kwargs["text"]))
            return {"processQueryKey": "briefing-query-1"}

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
