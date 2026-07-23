from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.report_document_contract import (
    daily_section_cue_semantic_payload,
    parse_structured_daily_document,
)
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    TypedDailyCommand,
    execute_typed_daily_command,
)
from app.agent2.typed_daily_executor import (
    TypedDailyExecutionContext,
    execute_typed_agent2_daily_commands,
)
from app.scheduler import jobs


class _ReceiptSession:
    def __init__(self) -> None:
        self.statements: list[object] = []
        self.added: list[object] = []

    async def execute(self, statement: object):
        self.statements.append(statement)
        return SimpleNamespace(rowcount=1, scalars=lambda: SimpleNamespace(all=lambda: []))

    async def scalars(self, statement: object):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [])

    async def flush(self) -> None:
        return None

    def add(self, value: object) -> None:
        self.added.append(value)


def _command(
    *,
    command_type: str,
    report_id,
    version: int,
    patch: dict[str, object],
    suffix: str,
    target_item_ids: tuple[str, ...] = (),
) -> TypedDailyCommand:
    return TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, f"release-command:{suffix}"),
        decision_id=uuid5(NAMESPACE_URL, "release-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, f"release-subdecision:{suffix}"),
        command_type=command_type,
        report_id=report_id,
        report_version=version,
        target_item_ids=target_item_ids,
        patch=patch,
        idempotency_key=f"release-message:{suffix}",
    )


def test_missing_empty_and_values_are_distinct_daily_section_inputs():
    resources = {
        "daily_draft": {
            "report_id": str(uuid5(NAMESPACE_URL, "release-report")),
            "version": 7,
        }
    }

    missing = daily_section_cue_semantic_payload("明日计划", resources)
    empty = daily_section_cue_semantic_payload("明日计划改成   ", resources)
    values = daily_section_cue_semantic_payload("明日计划改成准备评审材料", resources)

    assert missing is None
    assert empty is None
    assert values is not None
    assert len(values["required_actions"]) == 1
    assert values["entities"][0]["attributes"] == {
        "report_id": resources["daily_draft"]["report_id"],
        "version": 7,
        "field": "tomorrow_plan",
        "items": ["准备评审材料"],
    }


@pytest.mark.parametrize(
    "text",
    (
        (
            "今日工作\n1. 同事说已经完成合同审核\n"
            "问题与风险\n1. 客户资料不全\n"
            "明日计划\n1. 明天整理评审材料"
        ),
        (
            "今日工作\n1. 完成合同审核\n"
            "问题与风险\n1. 客户资料是否齐全？\n"
            "明日计划\n1. 明天整理评审材料"
        ),
        (
            "今日工作\n1. 完成合同审核\n"
            "问题与风险\n1. 客户资料不全\n"
            "明日计划\n1. 南京出差取消"
        ),
        (
            "今日工作\n1. 如果收到确认再完成合同审核\n"
            "问题与风险\n1. 客户资料不全\n"
            "明日计划\n1. 明天整理评审材料"
        ),
    ),
)
def test_structured_daily_document_rejects_nonassertive_or_cancelled_items(
    text: str,
) -> None:
    assert parse_structured_daily_document(text) is None


def test_structured_daily_document_accepts_three_asserted_sections() -> None:
    document = parse_structured_daily_document(
        "今日工作\n1. 完成合同审核\n"
        "问题与风险\n1. 客户资料不全\n"
        "明日计划\n1. 明天整理评审材料"
    )

    assert document is not None
    assert document.fields == {"today_work", "problems", "tomorrow_plan"}


@pytest.mark.parametrize(
    "text",
    (
        "明日计划改成南京出差取消",
        "明日计划改成如果收到通知再去南京",
        "问题与风险：同事说客户资料不全",
        "问题与风险：客户资料是否齐全？",
    ),
)
def test_single_section_replacement_rejects_nonassertive_language(text: str) -> None:
    resources = {
        "daily_draft": {
            "report_id": str(uuid5(NAMESPACE_URL, "release-report")),
            "version": 7,
        }
    }

    assert daily_section_cue_semantic_payload(text, resources) is None


def test_empty_replacement_is_not_an_implicit_clear_operation():
    owner_id = uuid5(NAMESPACE_URL, "release-owner")
    report_id = uuid5(NAMESPACE_URL, "release-empty-report")
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=4,
        status="collecting",
        today_work=("整理评审材料",),
        problems=("暂无阻断",),
        tomorrow_plan=("准备复核",),
        item_ids={
            "today_work": ("work-1",),
            "problems": ("problem-1",),
            "tomorrow_plan": ("plan-1",),
        },
    )
    command = _command(
        command_type="replace_section",
        report_id=report_id,
        version=4,
        patch={"field": "tomorrow_plan", "items": []},
        suffix="empty",
    )

    execution = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=owner_id,
    )

    assert execution.validation.status == "blocked"
    assert execution.after == snapshot
    assert execution.should_write_db is False


@pytest.mark.asyncio
async def test_sql_executor_no_op_does_not_upsert_or_claim_success(monkeypatch):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "release-noop-user"),
        team_id=uuid5(NAMESPACE_URL, "release-noop-team"),
        timezone="Asia/Shanghai",
    )
    report_date = date(2026, 7, 21)
    report_id = uuid5(NAMESPACE_URL, "release-noop-report")
    existing = SimpleNamespace(
        id=report_id,
        status="collecting",
        today_work=["整理评审材料"],
        problems=["暂无阻断"],
        tomorrow_plan=["准备复核"],
        section_status={
            "_agent2_report_version": 4,
            "_agent2_draft_item_ids": {
                "today_work": ["work-1"],
                "problems": ["problem-1"],
                "tomorrow_plan": ["plan-1"],
            },
        },
    )
    command = _command(
        command_type="replace_section",
        report_id=report_id,
        version=4,
        patch={"field": "tomorrow_plan", "items": ["准备复核"]},
        suffix="noop",
    )
    session = _ReceiptSession()

    async def fake_lock(*args, **kwargs):
        return None

    async def fake_get_report(*args, **kwargs):
        return existing

    async def forbidden_upsert(*args, **kwargs):
        raise AssertionError("a no-op must not update the daily report row")

    async def forbidden_state_sync(*args, **kwargs):
        raise AssertionError("a no-op must not update conversation task state")

    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", forbidden_upsert)
    monkeypatch.setattr(
        "app.agent2.typed_daily_executor.sync_focused_report_task",
        forbidden_state_sync,
    )

    result = await execute_typed_agent2_daily_commands(
        session,
        user=user,
        commands=(command,),
        execution_context=TypedDailyExecutionContext(
            report_date=report_date,
            source="release_candidate_test",
            source_text_hash="a" * 64,
            tenant_id="sandbox-release",
            conversation_id="release-conversation",
            source_turn_id="release-noop-turn",
        ),
        settings=SimpleNamespace(timezone="Asia/Shanghai"),
        execution_authority="authenticated_admin_command",
    )

    assert result.report_saved is False
    assert result.today_work == existing.today_work
    assert result.problems == existing.problems
    assert result.tomorrow_plan == existing.tomorrow_plan
    assert "本次没有修改" in result.message
    assert "已更新" not in result.message
    assert result.command_results[0]["actual_write"] is False


class _SchedulerReportProxy:
    """A stale ORM-like row that only flushes fields explicitly dirtied by the job."""

    def __init__(self, backing: dict[str, object]) -> None:
        object.__setattr__(self, "_backing", backing)
        for name in (
            "id",
            "user_id",
            "status",
            "today_work",
            "problems",
            "tomorrow_plan",
            "section_status",
            "last_prompted_at",
        ):
            object.__setattr__(self, name, backing.get(name))

    def __setattr__(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)
        if name in {"last_prompted_at", "section_status"}:
            self._backing[name] = value
        elif name in {"today_work", "problems", "tomorrow_plan"}:
            raise AssertionError(f"scheduler attempted to write daily body field: {name}")


@pytest.mark.asyncio
async def test_actual_scheduler_entry_preserves_concurrent_daily_body(monkeypatch):
    class Team:
        name = "Release Test Team"
        dingtalk_webhook_url = ""
        dingtalk_webhook_secret = None

        def __hash__(self) -> int:
            return id(self)

    team = Team()
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "release-scheduler-user"),
        team_id=uuid5(NAMESPACE_URL, "release-scheduler-team"),
        team=team,
        name="reviewer-alias",
        dingtalk_user_id="release-user-1",
        timezone="Asia/Shanghai",
    )
    report_date = date(2026, 7, 21)
    report_id = uuid5(NAMESPACE_URL, "release-scheduler-report")
    backing: dict[str, object] = {
        "id": report_id,
        "user_id": user.id,
        "status": "collecting",
        "today_work": ["整理评审材料"],
        "problems": ["暂无阻断"],
        "tomorrow_plan": ["旧计划"],
        "section_status": {
            "_agent2_report_version": 2,
            "_agent2_draft_item_ids": {
                "today_work": ["work-1"],
                "problems": ["problem-1"],
                "tomorrow_plan": ["plan-1"],
            },
        },
        "last_prompted_at": None,
    }
    scheduler_loaded = asyncio.Event()
    allow_send = asyncio.Event()
    stale_scheduler_row: _SchedulerReportProxy | None = None

    async def fake_list_missing_users(session, target_date):
        assert target_date == report_date
        return [user]

    async def fake_load_reports(session, target_date, user_ids):
        nonlocal stale_scheduler_row
        assert user_ids == [user.id]
        stale_scheduler_row = _SchedulerReportProxy(backing)
        scheduler_loaded.set()
        return {user.id: stale_scheduler_row}

    class SchedulerSession(_ReceiptSession):
        async def execute(self, statement: object):
            self.statements.append(statement)
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [stale_scheduler_row])
            )

    class Robot:
        def has_enterprise_app(self):
            return True

        async def send_robot_direct_text(self, **kwargs):
            await allow_send.wait()
            return {"processQueryKey": "offline-provider-reference"}

    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports)

    scheduler_task = asyncio.create_task(
        jobs.remind_missing_reports(
            SchedulerSession(),
            SimpleNamespace(
                timezone="Asia/Shanghai",
                dingtalk_default_robot_webhook="",
                dingtalk_default_robot_secret="",
                reminder_send_enabled=True,
                reminder_dry_run=False,
                reminder_test_user_ids="release-user-1",
            ),
            Robot(),
            report_date,
            dry_run=False,
        )
    )
    await scheduler_loaded.wait()

    user_command = _command(
        command_type="replace_section",
        report_id=report_id,
        version=2,
        patch={"field": "tomorrow_plan", "items": ["新的复核计划"]},
        suffix="scheduler-concurrent",
    )
    user_session = _ReceiptSession()

    async def fake_lock(*args, **kwargs):
        return None

    async def fake_get_report(*args, **kwargs):
        return SimpleNamespace(**backing)

    async def fake_upsert(session, **kwargs):
        backing.update(
            today_work=list(kwargs["today_work"]),
            problems=list(kwargs["problems"]),
            tomorrow_plan=list(kwargs["tomorrow_plan"]),
            section_status=dict(kwargs["section_status"]),
        )
        return SimpleNamespace(
            id=report_id,
            today_work=backing["today_work"],
            problems=backing["problems"],
            tomorrow_plan=backing["tomorrow_plan"],
        )

    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", fake_upsert)

    user_result = await execute_typed_agent2_daily_commands(
        user_session,
        user=user,
        commands=(user_command,),
        execution_context=TypedDailyExecutionContext(
            report_date=report_date,
            source="release_candidate_test",
            source_text_hash="b" * 64,
            tenant_id="sandbox-release",
        ),
        settings=SimpleNamespace(timezone="Asia/Shanghai"),
        execution_authority="authenticated_admin_command",
    )
    allow_send.set()
    scheduler_result = await scheduler_task

    assert user_result.report_saved is True
    assert backing["today_work"] == ["整理评审材料"]
    assert backing["problems"] == ["暂无阻断"]
    assert backing["tomorrow_plan"] == ["新的复核计划"]
    assert backing["section_status"]["_agent2_report_version"] == 3
    assert backing["last_prompted_at"] is not None
    assert scheduler_result["real_sent"] == 1
