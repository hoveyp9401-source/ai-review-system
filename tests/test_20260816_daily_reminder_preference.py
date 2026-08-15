from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.tool_calling.context import (
    SHADOW_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
)
from app.agent2.tool_calling.contracts import ReceiptStatus
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.agent2.tool_calling.runtime import ShadowRuntime
from app.agent2.tool_calling.sandbox_contracts import SandboxExecutionContext
from app.agent2.tool_calling.sandbox_handlers import SandboxHandlerRequest
from app.agent2.tool_calling.sandbox_memory_executor import (
    SandboxPersonalMemoryExecutor,
    sandbox_personal_memory_id,
)
from app.agent2.tool_calling.validation import NativeToolCall
from app.scheduler import jobs
from app.scheduler.jobs import (
    _load_daily_reminder_preferences,
    remind_missing_reports,
)


class _MemorySession:
    """Small database-boundary stand-in for the public memory tool handler."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict[str, object]]] = {
            "personal_memory": {},
            "personal_memory_audit": {},
        }

    async def read_record(
        self,
        table_name: str,
        record_id: str,
    ) -> dict[str, object] | None:
        row = self.rows[table_name].get(record_id)
        return dict(row) if row is not None else None

    async def read_table_rows(
        self,
        table_name: str,
    ) -> tuple[dict[str, object], ...]:
        return tuple(dict(row) for row in self.rows[table_name].values())

    async def upsert_record(
        self,
        table_name: str,
        record_id: str,
        payload: dict[str, object],
        *,
        create_only: bool = False,
    ) -> None:
        if create_only and record_id in self.rows[table_name]:
            raise AssertionError("append-only audit was overwritten")
        self.rows[table_name][record_id] = dict(payload)


def _context(
    *,
    source_message_id: str,
    turn_id: str,
    now: datetime,
) -> SandboxExecutionContext:
    return SandboxExecutionContext(
        tenant_id="daily-reminder-sandbox",
        user_id=UUID("10000000-0000-4000-8000-000000000016"),
        now=now,
        conversation_id="direct-conversation",
        source_message_id=source_message_id,
        turn_id=turn_id,
        timezone="Asia/Shanghai",
    )


async def _remember_daily_reminders(
    session: _MemorySession,
    *,
    context: SandboxExecutionContext,
    user_message: str,
    enabled: bool,
    call_id: str,
):
    definition = TOOL_REGISTRY["remember_personal_memory"]
    arguments = {
        "memory_key": "report.daily_reminders_enabled",
        "value": {"enabled": enabled},
        "source_evidence": {
            "source_message_index": 1,
            "intent": "explicit_preference",
        },
    }
    bound = CurrentTurnSource((user_message,)).bind_tool_arguments(
        "remember_personal_memory",
        arguments,
    )
    typed = definition.input_model.model_validate(bound)
    memory_executor = SandboxPersonalMemoryExecutor(
        session=session,
        context=context,
    )
    return await definition.sandbox_handler(
        SandboxHandlerRequest(
            tool_call_id=call_id,
            tool_name="remember_personal_memory",
            arguments=typed,
            executor=object(),
            memory_executor=memory_executor,
        )
    )


@pytest.mark.asyncio
async def test_authenticated_user_can_disable_then_restore_daily_reminders_with_audit() -> None:
    session = _MemorySession()
    now = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
    first_context = _context(
        source_message_id="message-disable",
        turn_id="turn-disable",
        now=now,
    )

    disabled = await _remember_daily_reminders(
        session,
        context=first_context,
        user_message="请关闭我自己的日报提醒。",
        enabled=False,
        call_id="call-disable",
    )

    memory_id = sandbox_personal_memory_id(
        first_context,
        "report.daily_reminders_enabled",
    )
    assert disabled.status_if_unchanged == ReceiptStatus.NO_OP
    assert session.rows["personal_memory"][memory_id]["value"] == {
        "enabled": False
    }
    first_audit = next(
        iter(session.rows["personal_memory_audit"].values())
    )
    assert first_audit["action"] == "create"

    restored = await _remember_daily_reminders(
        session,
        context=_context(
            source_message_id="message-restore",
            turn_id="turn-restore",
            now=now + timedelta(minutes=1),
        ),
        user_message="请恢复我自己的日报提醒。",
        enabled=True,
        call_id="call-restore",
    )

    assert restored.status_if_unchanged == ReceiptStatus.NO_OP
    assert session.rows["personal_memory"][memory_id]["value"] == {
        "enabled": True
    }
    audits = list(session.rows["personal_memory_audit"].values())
    assert [audit["action"] for audit in audits] == ["create", "replace"]
    assert [audit["source_message_id"] for audit in audits] == [
        "message-disable",
        "message-restore",
    ]


@pytest.mark.parametrize("conversation_kind", ["group", "unknown"])
@pytest.mark.asyncio
async def test_non_direct_conversation_cannot_change_a_personal_reminder_preference(
    conversation_kind: str,
) -> None:
    user_id = UUID("10000000-0000-4000-8000-000000000016")
    context = TrustedContext(
        namespace=SHADOW_STATE_NAMESPACE,
        now=datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="daily-reminder-sandbox",
            user_id=user_id,
            conversation_id="group-conversation",
            source_message_id="group-message",
            timezone="Asia/Shanghai",
            conversation_kind=conversation_kind,
        ),
        allowed_tool_names=frozenset({"remember_personal_memory"}),
        gate_decisions={"remember_personal_memory": True},
    )
    plan = await ShadowRuntime().open_session(context).propose(
        (
            NativeToolCall(
                tool_call_id="group-call",
                tool_name="remember_personal_memory",
                arguments={
                    "memory_key": "report.daily_reminders_enabled",
                    "value": {"enabled": False},
                    "source_evidence": {
                        "source_message_index": 1,
                        "intent": "explicit_preference",
                    },
                },
            ),
        )
    )

    assert plan.shadow_handler_call_count == 0
    assert plan.receipts[0].status == ReceiptStatus.BLOCKED
    assert plan.receipts[0].error_code == (
        "PERSONAL_MEMORY_DIRECT_CONVERSATION_REQUIRED"
    )


@pytest.mark.parametrize(
    ("memory_key", "value", "intent"),
    [
        (
            "response.preferred_salutation",
            {"salutation": "四哥"},
            "user_salutation_assignment",
        ),
        (
            "assistant.preferred_name",
            {"name": "小律"},
            "assistant_name_assignment",
        ),
    ],
)
@pytest.mark.asyncio
async def test_group_conversation_keeps_existing_name_memory_behavior(
    memory_key: str,
    value: dict[str, str],
    intent: str,
) -> None:
    context = TrustedContext(
        namespace=SHADOW_STATE_NAMESPACE,
        now=datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="daily-reminder-sandbox",
            user_id=UUID("10000000-0000-4000-8000-000000000016"),
            conversation_id="group-conversation",
            source_message_id="group-salutation-message",
            timezone="Asia/Shanghai",
            conversation_kind="group",
        ),
        allowed_tool_names=frozenset({"remember_personal_memory"}),
        gate_decisions={"remember_personal_memory": True},
    )
    plan = await ShadowRuntime().open_session(context).propose(
        (
            NativeToolCall(
                tool_call_id="group-existing-memory-call",
                tool_name="remember_personal_memory",
                arguments={
                    "memory_key": memory_key,
                    "value": value,
                    "source_evidence": {
                        "source_message_index": 1,
                        "intent": intent,
                    },
                },
            ),
        )
    )

    assert plan.shadow_handler_call_count == 1
    assert plan.receipts[0].status == ReceiptStatus.SUCCESS


def test_memory_tool_exposes_the_daily_reminder_scope_to_agent2() -> None:
    description = TOOL_REGISTRY["remember_personal_memory"].description
    forget_description = TOOL_REGISTRY["forget_personal_memory"].description

    assert "report.daily_reminders_enabled" in description
    assert "scheduled Daily Report reminders" in description
    assert "direct conversation" in description
    assert "third party" in description
    assert "ambiguous" in description
    assert "ask for clarification without writing" in description
    assert (
        "current user_message itself must unambiguously identify scheduled Daily Report"
        in description
    )
    assert "delivery records" in description
    assert "never supply that missing current-message scope" in description
    assert "only says not to remind the user again" in description
    assert "verified final delivery" not in description
    assert "Do not use this tool for report.daily_reminders_enabled" in (
        forget_description
    )
    assert "remember_personal_memory with enabled=true" in forget_description
    assert "source_evidence" in forget_description


@pytest.mark.parametrize("conversation_kind", ["direct", "group"])
@pytest.mark.asyncio
async def test_daily_reminder_preference_cannot_be_forgotten(
    conversation_kind: str,
) -> None:
    context = TrustedContext(
        namespace=SHADOW_STATE_NAMESPACE,
        now=datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="daily-reminder-sandbox",
            user_id=UUID("10000000-0000-4000-8000-000000000016"),
            conversation_id=f"{conversation_kind}-conversation",
            source_message_id=f"{conversation_kind}-forget-message",
            timezone="Asia/Shanghai",
            conversation_kind=conversation_kind,
        ),
        allowed_tool_names=frozenset({"forget_personal_memory"}),
        gate_decisions={"forget_personal_memory": True},
    )

    plan = await ShadowRuntime().open_session(context).propose(
        (
            NativeToolCall(
                tool_call_id=f"{conversation_kind}-forget-call",
                tool_name="forget_personal_memory",
                arguments={
                    "memory_key": "report.daily_reminders_enabled",
                },
            ),
        )
    )

    assert plan.shadow_handler_call_count == 0
    assert plan.receipts[0].status == ReceiptStatus.FAILED
    assert plan.receipts[0].error_code == "INVALID_TOOL_ARGUMENTS"


@pytest.mark.parametrize(
    "memory_key",
    [
        "assistant.preferred_name",
        "response.verbosity",
        "response.output_format",
        "response.preferred_salutation",
        "report.show_updated_snapshot",
        "report.show_item_numbers",
    ],
)
def test_existing_personal_memories_remain_forgettable(memory_key: str) -> None:
    typed = TOOL_REGISTRY["forget_personal_memory"].input_model.model_validate(
        {"memory_key": memory_key}
    )

    assert typed.memory_key == memory_key


@pytest.mark.parametrize("enabled", ["false", 0, 1, None])
def test_daily_reminder_memory_accepts_only_a_real_boolean(enabled: object) -> None:
    with pytest.raises(ValueError):
        TOOL_REGISTRY["remember_personal_memory"].input_model.model_validate(
            {
                "memory_key": "report.daily_reminders_enabled",
                "value": {"enabled": enabled},
                "source_evidence": {
                    "source_message_index": 1,
                    "intent": "explicit_preference",
                },
            }
        )


@pytest.mark.parametrize("reminder_kind", ["daily", "second", "catchup"])
@pytest.mark.asyncio
async def test_all_daily_reminder_jobs_skip_only_the_user_who_disabled_them(
    monkeypatch: pytest.MonkeyPatch,
    reminder_kind: str,
) -> None:
    class Team:
        name = "法务一部"
        dingtalk_webhook_url = ""
        dingtalk_webhook_secret = None

        def __hash__(self) -> int:
            return id(self)

    team = Team()
    disabled_user = SimpleNamespace(
        id=UUID("20000000-0000-4000-8000-000000000016"),
        dingtalk_user_id="disabled-user",
        name="关闭提醒用户",
        team=team,
    )
    default_user = SimpleNamespace(
        id=UUID("30000000-0000-4000-8000-000000000016"),
        dingtalk_user_id="default-user",
        name="默认提醒用户",
        team=team,
    )

    async def fake_list_missing_users(_session, _report_date):
        return [disabled_user, default_user]

    async def fake_load_reports(_session, _report_date, _user_ids):
        return {}

    async def fake_load_roster(*_args, **_kwargs):
        return SimpleNamespace(
            user_ids=(str(disabled_user.id), str(default_user.id)),
            dingtalk_user_ids=("disabled-user", "default-user"),
            member_count=2,
            members=(
                SimpleNamespace(
                    user_id=str(disabled_user.id),
                    dingtalk_user_id="disabled-user",
                ),
                SimpleNamespace(
                    user_id=str(default_user.id),
                    dingtalk_user_id="default-user",
                ),
            ),
        )

    class Result:
        def all(self):
            return [(disabled_user.id, {"enabled": False})]

    class Session:
        async def execute(self, _statement):
            return Result()

    class Robot:
        def has_enterprise_app(self) -> bool:
            return True

        async def send_robot_direct_text(self, **_kwargs):
            raise AssertionError("dry-run must not call DingTalk")

    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports)
    monkeypatch.setattr(
        jobs,
        "load_formal_legal_daily_roster",
        fake_load_roster,
    )

    result = await remind_missing_reports(
        Session(),
        SimpleNamespace(
            timezone="Asia/Shanghai",
            legal_daily_dashboard_tenant_id="daily-reminder-sandbox",
            dingtalk_default_robot_webhook="",
            dingtalk_default_robot_secret="",
            reminder_send_enabled=False,
            reminder_dry_run=True,
            reminder_test_user_ids="disabled-user,default-user",
        ),
        Robot(),
        datetime(2026, 8, 16, tzinfo=UTC).date(),
        reminder_kind=reminder_kind,
        dry_run=True,
    )

    assert result["target_users"] == 1
    assert result["would_send"] == 1
    assert result["skipped_by_preference"] == 1
    assert result["missing_count"] == 2
    assert result["dry_run_messages"][0]["user_ids"] == ["default-user"]


@pytest.mark.asyncio
async def test_reminder_preference_read_failure_stops_before_any_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team = SimpleNamespace(
        name="法务一部",
        dingtalk_webhook_url="",
        dingtalk_webhook_secret=None,
    )
    user = SimpleNamespace(
        id=UUID("40000000-0000-4000-8000-000000000016"),
        dingtalk_user_id="would-send-user",
        name="待读取偏好用户",
        team=team,
    )

    async def fake_list_missing_users(_session, _report_date):
        return [user]

    async def fake_load_reports(_session, _report_date, _user_ids):
        return {}

    async def fake_load_roster(*_args, **_kwargs):
        return SimpleNamespace(
            user_ids=(str(user.id),),
            dingtalk_user_ids=(user.dingtalk_user_id,),
            member_count=1,
            members=(
                SimpleNamespace(
                    user_id=str(user.id),
                    dingtalk_user_id=user.dingtalk_user_id,
                ),
            ),
        )

    class Session:
        async def execute(self, _statement):
            raise RuntimeError("memory unavailable")

    class Robot:
        sent = False

        def has_enterprise_app(self) -> bool:
            return True

        async def send_robot_direct_text(self, **_kwargs):
            self.sent = True
            return {"processQueryKey": "must-not-exist"}

    robot = Robot()
    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports)
    monkeypatch.setattr(
        jobs,
        "load_formal_legal_daily_roster",
        fake_load_roster,
    )

    with pytest.raises(RuntimeError, match="memory unavailable"):
        await remind_missing_reports(
            Session(),
            SimpleNamespace(
                timezone="Asia/Shanghai",
                legal_daily_dashboard_tenant_id="daily-reminder-sandbox",
                dingtalk_default_robot_webhook="",
                dingtalk_default_robot_secret="",
                reminder_send_enabled=True,
                reminder_dry_run=False,
                reminder_test_user_ids="would-send-user",
            ),
            robot,
            datetime(2026, 8, 16, tzinfo=UTC).date(),
            reminder_kind="daily",
            dry_run=False,
        )

    assert robot.sent is False


@pytest.mark.asyncio
async def test_preference_read_rejects_a_session_without_database_access() -> None:
    with pytest.raises(RuntimeError, match="database session"):
        await _load_daily_reminder_preferences(
            SimpleNamespace(),
            [UUID("50000000-0000-4000-8000-000000000016")],
            tenant_id="daily-reminder-sandbox",
            now=datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_preference_read_rejects_a_missing_tenant_scope() -> None:
    class Result:
        def all(self):
            return []

    class Session:
        async def execute(self, _statement):
            return Result()

    with pytest.raises(RuntimeError, match="tenant"):
        await _load_daily_reminder_preferences(
            Session(),
            [UUID("60000000-0000-4000-8000-000000000016")],
            tenant_id="",
            now=datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_preference_read_sql_has_complete_scope_and_expiry_conditions() -> None:
    user_ids = [
        UUID("70000000-0000-4000-8000-000000000016"),
        UUID("70000000-0000-4000-8000-000000000017"),
    ]
    tenant_id = "daily-reminder-sandbox"
    now = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)

    class Result:
        def all(self):
            return [(user_ids[0], {"enabled": False})]

    class RecordingSession:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return Result()

    session = RecordingSession()
    preferences = await _load_daily_reminder_preferences(
        session,
        user_ids,
        tenant_id=tenant_id,
        now=now,
    )

    assert preferences == {user_ids[0]: False}
    assert session.statement is not None
    compiled = session.statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"render_postcompile": True},
    )
    sql = " ".join(str(compiled).lower().split())
    parameters = tuple(compiled.params.values())
    assert "agent2_personal_memories.tenant_id =" in sql
    assert "agent2_personal_memories.user_id in" in sql
    assert "agent2_personal_memories.memory_type =" in sql
    assert "agent2_personal_memories.memory_key =" in sql
    assert "agent2_personal_memories.status =" in sql
    assert "agent2_personal_memories.expires_at is null" in sql
    assert "agent2_personal_memories.expires_at >" in sql
    assert tenant_id in parameters
    assert set(user_ids).issubset(set(parameters))
    assert "response_preference" in parameters
    assert "report.daily_reminders_enabled" in parameters
    assert "active" in parameters
    assert now in parameters


@pytest.mark.parametrize(
    ("stored_rows", "error_pattern"),
    [
        (
            (
                {"enabled": False},
                {"enabled": True},
            ),
            "duplicate active rows",
        ),
        (({"enabled": "false"},), "invalid value"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_preference_rows_stop_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    stored_rows: tuple[dict[str, object], ...],
    error_pattern: str,
) -> None:
    team = SimpleNamespace(
        name="Legal Team",
        dingtalk_webhook_url="",
        dingtalk_webhook_secret=None,
    )
    user = SimpleNamespace(
        id=UUID("80000000-0000-4000-8000-000000000016"),
        dingtalk_user_id="bad-preference-user",
        name="Preference User",
        team=team,
    )

    async def fake_list_missing_users(_session, _report_date):
        return [user]

    async def fake_load_reports(_session, _report_date, _user_ids):
        return {}

    async def fake_load_roster(*_args, **_kwargs):
        return SimpleNamespace(
            user_ids=(str(user.id),),
            dingtalk_user_ids=(user.dingtalk_user_id,),
            member_count=1,
            members=(
                SimpleNamespace(
                    user_id=str(user.id),
                    dingtalk_user_id=user.dingtalk_user_id,
                ),
            ),
        )

    class Result:
        def all(self):
            return [(user.id, value) for value in stored_rows]

    class Session:
        async def execute(self, _statement):
            return Result()

    class Robot:
        sent = False

        def has_enterprise_app(self) -> bool:
            return True

        async def send_robot_direct_text(self, **_kwargs):
            self.sent = True
            return {"processQueryKey": "must-not-exist"}

    robot = Robot()
    monkeypatch.setattr(jobs, "list_missing_users", fake_list_missing_users)
    monkeypatch.setattr(jobs, "_load_reports_by_user", fake_load_reports)
    monkeypatch.setattr(
        jobs,
        "load_formal_legal_daily_roster",
        fake_load_roster,
    )

    with pytest.raises(RuntimeError, match=error_pattern):
        await remind_missing_reports(
            Session(),
            SimpleNamespace(
                timezone="Asia/Shanghai",
                legal_daily_dashboard_tenant_id="daily-reminder-sandbox",
                dingtalk_default_robot_webhook="",
                dingtalk_default_robot_secret="",
                reminder_send_enabled=True,
                reminder_dry_run=False,
                reminder_test_user_ids=user.dingtalk_user_id,
            ),
            robot,
            datetime(2026, 8, 16, tzinfo=UTC).date(),
            reminder_kind="daily",
            dry_run=False,
        )

    assert robot.sent is False
