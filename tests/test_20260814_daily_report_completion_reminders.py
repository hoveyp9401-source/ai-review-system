from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling.context import TrustedReportSnapshot
from app.agent2.tool_calling.production_daily_executor import ProductionDailyExecutor
from app.agent2.typed_daily_commands import TypedDailyCommand
from app.agent2.typed_daily_executor import (
    TypedDailyExecutionContext,
    build_typed_daily_snapshot,
    execute_typed_agent2_daily_commands,
)
from app.scheduler import jobs
from app.scheduler.jobs import build_report_reminder_text, remind_missing_reports
from app.services.state_machine import assess_daily_report_completeness


class _Session:
    def __init__(self) -> None:
        self.statements = []
        self.receipt_after_states: list[dict] = []

    async def execute(self, statement):
        self.statements.append(statement)
        parameters = statement.compile().params
        after_json = parameters.get("after_json")
        if isinstance(after_json, dict):
            self.receipt_after_states.append(after_json)
        return SimpleNamespace(rowcount=1)

    async def scalars(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=list)

    async def flush(self) -> None:
        return None


def _command(
    *,
    report_id,
    command_type: str,
    version: int,
    patch: dict,
    suffix: str,
) -> TypedDailyCommand:
    return TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, f"completion-command-{suffix}"),
        decision_id=uuid5(NAMESPACE_URL, "completion-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, f"completion-subdecision-{suffix}"),
        command_type=command_type,
        report_id=report_id,
        report_version=version,
        target_item_ids=(),
        patch=patch,
        idempotency_key=f"completion-message:daily:{suffix}",
    )


def _run_complete_report_turn(
    monkeypatch,
    *,
    report_date: date,
    occurred_at: datetime,
    suffix: str,
):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, f"completion-user-{suffix}"),
        team_id=uuid5(NAMESPACE_URL, f"completion-team-{suffix}"),
        timezone="Asia/Shanghai",
    )
    snapshot = build_typed_daily_snapshot(
        user=user,
        report_date=report_date,
        report=None,
    )
    commands = (
        _command(
            report_id=snapshot.report_id,
            command_type="replace_section",
            version=0,
            patch={
                "field": "today_work",
                "items": [f"今日工作{i}" for i in range(1, 6)],
            },
            suffix=f"{suffix}-today-work",
        ),
        _command(
            report_id=snapshot.report_id,
            command_type="acknowledge_empty_section",
            version=1,
            patch={"field": "problems"},
            suffix=f"{suffix}-no-problems",
        ),
        _command(
            report_id=snapshot.report_id,
            command_type="replace_section",
            version=2,
            patch={
                "field": "tomorrow_plan",
                "items": [f"明日计划{i}" for i in range(1, 5)],
            },
            suffix=f"{suffix}-tomorrow-plan",
        ),
    )
    captured: dict = {}

    async def fake_lock(*_args, **_kwargs) -> None:
        return None

    async def fake_get_report(*_args, **_kwargs):
        return None

    async def fake_upsert(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=kwargs["report_id_override"],
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
        )

    monkeypatch.setattr(
        "app.repositories.acquire_daily_report_advisory_lock",
        fake_lock,
    )
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", fake_upsert)

    session = _Session()
    result = asyncio.run(
        execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=commands,
            execution_context=TypedDailyExecutionContext(
                report_date=report_date,
                source="agent2_completion_test",
                source_text_hash="a" * 64,
                occurred_at=occurred_at,
            ),
            settings=SimpleNamespace(timezone="Asia/Shanghai"),
            execution_authority="authenticated_admin_command",
        )
    )
    return result, captured, session


def test_one_turn_complete_report_enters_pending_confirmation(monkeypatch) -> None:
    occurred_at = datetime(
        2026,
        8,
        13,
        21,
        57,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    result, captured, session = _run_complete_report_turn(
        monkeypatch,
        report_date=date(2026, 8, 13),
        occurred_at=occurred_at,
        suffix="same-day",
    )

    assert result.status == "pending_confirmation"
    assert captured["status"] == "pending_confirmation"
    assert captured["completeness_score"] == 1.0
    assert captured["section_status"]["problems_acknowledged_empty"] is True
    assert captured["confirmation_type"] == "none"
    assert captured["confirmed_by_user"] is False
    assert captured["pending_confirmation_at"] == occurred_at
    assert "待确认" in result.message
    assert "日报已提交" not in result.message
    assert session.receipt_after_states[-1]["status"] == "pending_confirmation"


def test_next_morning_complete_catchup_requires_user_confirmation(monkeypatch) -> None:
    occurred_at = datetime(
        2026,
        8,
        14,
        9,
        20,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    result, captured, _session = _run_complete_report_turn(
        monkeypatch,
        report_date=date(2026, 8, 13),
        occurred_at=occurred_at,
        suffix="next-morning",
    )

    assert result.status == "pending_confirmation"
    assert captured["status"] == "pending_confirmation"
    assert captured["confirmation_type"] == "none"
    assert captured["confirmed_by_user"] is False
    assert "历史日报已补充完整" in result.message
    assert "请确认无误后再提交" in result.message
    assert "没有代你确认或提交" in result.message
    assert "次日上午自动提交" not in result.message


def test_early_next_morning_catchup_keeps_the_existing_auto_submit_rule(
    monkeypatch,
) -> None:
    result, captured, _session = _run_complete_report_turn(
        monkeypatch,
        report_date=date(2026, 8, 13),
        occurred_at=datetime(
            2026,
            8,
            14,
            8,
            30,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        suffix="before-briefing",
    )

    assert captured["status"] == "pending_confirmation"
    assert "今天晨报前自动提交" in result.message
    assert "本次没有代你确认或提交" in result.message


def test_tool_receipt_exposes_the_safe_next_step_for_completed_catchup() -> None:
    executor = ProductionDailyExecutor.__new__(ProductionDailyExecutor)
    executor._context = SimpleNamespace(
        now=datetime(2026, 8, 14, 9, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
        principal=SimpleNamespace(timezone="Asia/Shanghai"),
    )
    executor._settings = SimpleNamespace(
        summary_cron_hour=9,
        summary_cron_minute=0,
    )
    executor._tool_idempotency_key = lambda _request: "daily:test"
    report = TrustedReportSnapshot(
        report_id=uuid5(NAMESPACE_URL, "completion-report"),
        tenant_id="tenant-test",
        owner_user_id=uuid5(NAMESPACE_URL, "completion-user"),
        report_date=date(2026, 8, 13),
        version=3,
        status="pending_confirmation",
        acknowledged_empty_fields=frozenset({"problems"}),
    )

    outcome = executor._outcome(
        SimpleNamespace(),
        before=None,
        after=report,
        typed_receipt_ids=(),
    )

    assert outcome.safe_user_facts is not None
    assert "请确认无误后再提交" in outcome.safe_user_facts["next_step"]
    assert "没有代你确认或提交" in outcome.safe_user_facts["next_step"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reminder_kind", ["daily", "second"])
async def test_complete_collecting_report_gets_no_missing_field_reminder(
    monkeypatch,
    reminder_kind: str,
) -> None:
    class Team:
        name = "Team A"
        dingtalk_webhook_url = ""
        dingtalk_webhook_secret = None

        def __hash__(self) -> int:
            return id(self)

    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, f"complete-reminder-user-{reminder_kind}"),
        name="Test User",
        dingtalk_user_id="test-user",
        team=Team(),
    )
    report = SimpleNamespace(
        status="collecting",
        today_work=[f"今日工作{i}" for i in range(1, 6)],
        problems=[],
        tomorrow_plan=[f"明日计划{i}" for i in range(1, 5)],
        section_status={"problems_acknowledged_empty": True},
    )

    async def fake_list_missing_users(_session, _report_date):
        return [user]

    async def fake_load_reports(_session, _report_date, user_ids):
        assert user_ids == [user.id]
        return {user.id: report}

    class Robot:
        def has_enterprise_app(self) -> bool:
            return True

    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports)

    result = await remind_missing_reports(
        SimpleNamespace(),
        SimpleNamespace(
            timezone="Asia/Shanghai",
            dingtalk_default_robot_webhook="",
            dingtalk_default_robot_secret="",
            reminder_send_enabled=False,
            reminder_dry_run=True,
            reminder_test_user_ids="test-user",
        ),
        Robot(),
        date(2026, 8, 13),
        reminder_kind=reminder_kind,
        dry_run=True,
    )

    assert result["target_users"] == 0
    assert result["missing_count"] == 0
    assert result["would_send"] == 0
    assert result["dry_run_messages"] == []


@pytest.mark.asyncio
async def test_the_47_candidate_shape_for_24_people_produces_zero_false_reminders(
    monkeypatch,
) -> None:
    class Team:
        name = "综合管理部"
        dingtalk_webhook_url = ""
        dingtalk_webhook_secret = None

        def __hash__(self) -> int:
            return id(self)

    team = Team()
    users = [
        SimpleNamespace(
            id=uuid5(NAMESPACE_URL, f"reminder-shape-user-{index}"),
            name=f"测试成员{index}",
            dingtalk_user_id=f"test-user-{index}",
            team=team,
        )
        for index in range(24)
    ]
    candidates_by_date = {
        date(2026, 8, 12): users,
        date(2026, 8, 13): users[:23],
    }

    async def fake_list_missing_users(_session, report_date):
        return candidates_by_date[report_date]

    async def fake_load_reports(_session, _report_date, user_ids):
        return {
            user.id: SimpleNamespace(
                status="collecting",
                today_work=["完成工作"],
                problems=[],
                tomorrow_plan=["继续跟进"],
                section_status={"problems_acknowledged_empty": True},
            )
            for user in users
            if user.id in user_ids
        }

    class Robot:
        def has_enterprise_app(self) -> bool:
            return True

    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports)
    settings = SimpleNamespace(
        timezone="Asia/Shanghai",
        dingtalk_default_robot_webhook="",
        dingtalk_default_robot_secret="",
        reminder_send_enabled=False,
        reminder_dry_run=True,
        reminder_test_user_ids=",".join(
            user.dingtalk_user_id for user in users
        ),
    )

    candidate_count = 0
    for report_date, candidate_users in candidates_by_date.items():
        candidate_count += len(candidate_users)
        result = await remind_missing_reports(
            SimpleNamespace(),
            settings,
            Robot(),
            report_date,
            dry_run=True,
        )
        assert result["missing_count"] == 0
        assert result["target_users"] == 0
        assert result["dry_run_messages"] == []

    assert candidate_count == 47
    assert len({user.id for user in users}) == 24


@pytest.mark.parametrize(
    ("report", "expected_label", "absent_labels"),
    [
        (
            SimpleNamespace(
                status="collecting",
                today_work=[],
                problems=[],
                tomorrow_plan=["继续跟进"],
                section_status={"problems_acknowledged_empty": True},
            ),
            "今日工作",
            ("问题/风险", "明日计划"),
        ),
        (
            SimpleNamespace(
                status="collecting",
                today_work=["完成审核"],
                problems=[],
                tomorrow_plan=["继续跟进"],
                section_status={},
            ),
            "问题/风险",
            ("今日工作", "明日计划"),
        ),
        (
            SimpleNamespace(
                status="collecting",
                today_work=["完成审核"],
                problems=[],
                tomorrow_plan=[],
                section_status={"problems_acknowledged_empty": True},
            ),
            "明日计划",
            ("今日工作", "问题/风险"),
        ),
    ],
)
def test_each_single_missing_section_is_named_exactly(
    report,
    expected_label: str,
    absent_labels: tuple[str, str],
) -> None:
    text = build_report_reminder_text(
        date(2026, 8, 13),
        SimpleNamespace(name="Test User"),
        report,
    )

    assert expected_label in text
    assert all(label not in text for label in absent_labels)
    assert "未完成部分" not in text


@pytest.mark.parametrize(
    "acknowledged_field",
    ["today_work", "problems", "tomorrow_plan"],
)
def test_each_model_acknowledged_empty_section_uses_the_same_completion_rule(
    acknowledged_field: str,
) -> None:
    values = {
        "today_work": ["完成审核"],
        "problems": ["发现风险"],
        "tomorrow_plan": ["继续跟进"],
    }
    values[acknowledged_field] = []

    assessment = assess_daily_report_completeness(
        **values,
        section_status={f"{acknowledged_field}_acknowledged_empty": True},
    )

    assert assessment.ready_for_confirmation is True
    assert assessment.missing_sections == ()
    assert assessment.completeness_score == 1.0
    assert assessment.draft_status == "pending_confirmation"
