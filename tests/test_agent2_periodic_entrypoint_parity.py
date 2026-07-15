from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path
import sys
from types import SimpleNamespace
from types import ModuleType
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.outcome_adapters import text_outcome
from app.workflows.intake import IncomingMessageEnvelope


ROOT = Path(__file__).resolve().parents[1]


def _async_function(relative_path: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _calls(function: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next(
        (keyword.value for keyword in call.keywords if keyword.arg == name),
        None,
    )


def test_stream_executes_periodic_reports_with_verified_context_and_authority() -> None:
    function = _async_function(
        "app/stream_runner.py",
        "_process_stream_agent2_daily_if_enabled",
    )

    calls = _calls(function, "execute_periodic_report_commands")

    assert len(calls) == 1
    call = calls[0]
    assert ast.unparse(_keyword(call, "commands")) == (
        "cognitive_v3.command_plan.report_commands"
    )
    assert ast.unparse(_keyword(call, "context")) == "verified_execution_context"
    assert ast.unparse(_keyword(call, "execution_authority")) == (
        "turn_runtime_result.mutation_execution_authority"
    )


def test_stream_periodic_receipts_reach_state_outcome_store_and_reply() -> None:
    function = _async_function(
        "app/stream_runner.py",
        "_process_stream_agent2_daily_if_enabled",
    )
    source = ast.unparse(function)

    assert "report_results=[item.as_dict() for item in periodic_report_results]" in source
    assert _calls(function, "periodic_execution_outcomes")
    assert _calls(function, "persist_operation_outcomes")
    assert (
        "elif phase2_business_result is None and (not periodic_report_results)"
        in source
    )
    assert any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value == "agent2_periodic_report_processed"
        for node in ast.walk(function)
    )


def test_webhook_periodic_only_branch_persists_operation_outcome() -> None:
    function = _async_function(
        "app/api/webhook.py",
        "_submit_webhook_agent2_if_enabled",
    )
    periodic_branches = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "periodic_report_results"
    ]

    assert len(periodic_branches) == 1
    branch = periodic_branches[0]
    assert _calls(branch, "periodic_execution_outcomes")
    assert _calls(branch, "persist_operation_outcomes")


def test_both_dingtalk_transports_persist_route_claim_before_runtime() -> None:
    webhook_function = _async_function(
        "app/api/webhook.py",
        "_submit_webhook_agent2_if_enabled",
    )
    stream_function = _async_function(
        "app/stream_runner.py",
        "_process_stream_agent2_daily_if_enabled",
    )

    for function in (webhook_function, stream_function):
        source = ast.unparse(function)
        resolve_at = source.index("await resolve_agent2_entrypoint(")
        persist_at = source.index("await persist_runtime_owner_claim(")
        owner_at = source.index("decide_runtime_owner(")
        assert resolve_at < persist_at < owner_at


@pytest.mark.asyncio
async def test_stream_periodic_only_command_executes_and_commits_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_package = ModuleType("app.progress")
    outbox_module = ModuleType("app.progress.outbox")
    outbox_module.enqueue_daily_report_outbox_best_effort = (
        lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(sys.modules, "app.progress", progress_package)
    monkeypatch.setitem(sys.modules, "app.progress.outbox", outbox_module)
    from app import stream_runner

    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    report_id = uuid5(NAMESPACE_URL, "stream-periodic-only-report")
    context = BusinessCommandContext(
        tenant_id="sandbox-agent2-phase2-20260711",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="user-1",
        actor_role_ids=(),
        allowed_case_ids=(),
        source_message_id="stream-periodic-message",
        source_channel="dingtalk_stream",
        occurred_at=now,
        conversation_id="stream-periodic-conversation",
        conversation_state_version=4,
    )
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="dingtalk_stream_text",
        raw_text="写周报",
        message_id="stream-periodic-message",
        conversation_id=context.conversation_id,
    )
    report_command = SimpleNamespace(command_type="capture_report_event")
    orchestration = SimpleNamespace(
        command_plan=SimpleNamespace(
            daily_commands=(),
            business_commands=(),
            report_commands=(report_command,),
        ),
        decision=SimpleNamespace(),
    )
    runtime_result = SimpleNamespace(
        orchestration=orchestration,
        business_execution_context=context,
        mutation_execution_authority="semantic_ticket",
        selection_continuation=None,
    )
    runtime = SimpleNamespace()

    async def handle(_request):
        return runtime_result

    runtime.handle = handle
    entrypoint = SimpleNamespace(
        decision=SimpleNamespace(route="agent2_primary"),
        binding=SimpleNamespace(),
    )
    persisted = SimpleNamespace(
        execution=SimpleNamespace(
            after=SimpleNamespace(report_id=report_id),
        ),
        as_dict=lambda: {
            "validation_status": "executed",
            "receipt_id": "periodic-receipt-1",
            "actual_write": True,
        },
    )
    calls: dict[str, object] = {}

    async def execute_periodic(_session, **kwargs):
        calls["execute"] = kwargs
        return [persisted]

    async def finalize(**kwargs):
        calls["finalize"] = kwargs

    async def persist(_session, outcomes, **kwargs):
        calls["persist"] = (outcomes, kwargs)

    async def mark_processed(_session, _event, **kwargs):
        calls["mark"] = kwargs

    async def send_reply(_handler, _robot, _job, reply_text):
        calls["reply"] = reply_text
        return 0.0

    async def build_context(**_kwargs):
        return (
            envelope,
            SimpleNamespace(),
            SimpleNamespace(report_date=date(2026, 7, 14)),
            date(2026, 7, 14),
        )

    async def resolve_entrypoint(*_args, **_kwargs):
        return entrypoint

    monkeypatch.setattr(stream_runner, "resolve_agent2_entrypoint", resolve_entrypoint)
    monkeypatch.setattr(
        stream_runner,
        "build_business_command_context",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(stream_runner, "cognitive_core_v3_enabled", lambda _settings: True)
    monkeypatch.setattr(stream_runner, "semantic_admission_mode", lambda *_args, **_kwargs: "enforced")
    monkeypatch.setattr(stream_runner, "_build_stream_cognitive_context", build_context)
    monkeypatch.setattr(stream_runner, "production_agent2_turn_runtime", lambda: runtime)
    monkeypatch.setattr(stream_runner, "execute_periodic_report_commands", execute_periodic)
    monkeypatch.setattr(stream_runner, "finalize_cognitive_core_v3_execution", finalize)
    monkeypatch.setattr(
        stream_runner,
        "periodic_execution_outcomes",
        lambda *_args, **_kwargs: (
            text_outcome("周期报告结果已生成。", source_turn_id="stream-periodic-message"),
        ),
    )
    monkeypatch.setattr(stream_runner, "persist_operation_outcomes", persist)
    monkeypatch.setattr(stream_runner, "mark_webhook_event_processed", mark_processed)
    monkeypatch.setattr(stream_runner, "_reply", send_reply)

    class Session:
        commits = 0

        async def commit(self):
            self.commits += 1

    session = Session()
    job = stream_runner.StreamJob(
        message=SimpleNamespace(
            message_id="stream-periodic-message",
            conversation_id=context.conversation_id,
        ),
        text="写周报",
        payload={},
    )

    result = await stream_runner._process_stream_agent2_daily_if_enabled(
        session=session,
        user=SimpleNamespace(id="user-1", dingtalk_user_id="ding-user-1"),
        event=SimpleNamespace(
            id="event-1",
            external_message_id="stream-periodic-message",
            conversation_id=context.conversation_id,
        ),
        job=job,
        handler=SimpleNamespace(),
        robot=SimpleNamespace(),
        llm_client=SimpleNamespace(),
        settings=SimpleNamespace(
            timezone="Asia/Shanghai",
            stream_processing_timeout_seconds=5,
        ),
        performance_service=SimpleNamespace(),
        timings={},
    )

    assert result == "agent2_periodic_report_processed"
    assert calls["execute"]["commands"] == (report_command,)
    assert calls["execute"]["context"] == context
    assert calls["execute"]["execution_authority"] == "semantic_ticket"
    assert calls["finalize"]["report_results"] == [persisted.as_dict()]
    assert calls["persist"][1]["tenant_id"] == context.tenant_id
    # Periodic/typed Report IDs are not guaranteed to exist in the legacy
    # daily_reports table backing WebhookEvent.report_id.
    assert calls["mark"]["report_id"] is None
    assert calls["reply"] == "周期报告结果已生成。"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_webhook_read_only_reply_persists_the_exact_composed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import webhook

    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    context = BusinessCommandContext(
        tenant_id="sandbox-agent2-phase2-20260711",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="user-1",
        actor_role_ids=(),
        allowed_case_ids=(),
        source_message_id="webhook-read-only-message",
        source_channel="dingtalk_webhook",
        occurred_at=now,
        conversation_id="webhook-read-only-conversation",
        conversation_state_version=4,
    )
    entrypoint = SimpleNamespace(
        decision=SimpleNamespace(route="agent2_primary"),
        binding=SimpleNamespace(),
    )
    orchestration = SimpleNamespace(
        command_plan=SimpleNamespace(
            daily_commands=(), business_commands=(), report_commands=(),
        ),
        decision=SimpleNamespace(
            admission_mode="enforced",
            admission_selection_requests=(),
            clarification_need=None,
        ),
    )
    runtime_result = SimpleNamespace(
        orchestration=orchestration,
        business_execution_context=context,
        mutation_execution_authority="semantic_ticket",
        selection_continuation=None,
    )
    runtime = SimpleNamespace()

    async def handle(_request):
        return runtime_result

    runtime.handle = handle
    calls: dict[str, object] = {}

    async def resolve_entrypoint(*_args, **_kwargs):
        return entrypoint

    async def load_daily_context(*_args, **_kwargs):
        return SimpleNamespace(
            report=None,
            report_date=date(2026, 7, 14),
            active_task=None,
        )

    async def side_reply(**_kwargs):
        return "read-only answer"

    async def persist(_session, outcomes, **kwargs):
        calls["persist"] = (outcomes, kwargs)

    monkeypatch.setattr(webhook, "resolve_agent2_entrypoint", resolve_entrypoint)
    monkeypatch.setattr(
        webhook,
        "build_business_command_context",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(webhook, "load_live_daily_context", load_daily_context)
    monkeypatch.setattr(webhook, "cognitive_core_v3_enabled", lambda _settings: True)
    monkeypatch.setattr(
        webhook,
        "semantic_admission_mode",
        lambda *_args, **_kwargs: "enforced",
    )
    monkeypatch.setattr(webhook, "production_agent2_turn_runtime", lambda: runtime)
    monkeypatch.setattr(webhook, "build_cognitive_side_reply_v3", side_reply)
    monkeypatch.setattr(webhook, "persist_operation_outcomes", persist)

    result = await webhook._submit_webhook_agent2_if_enabled(
        session=SimpleNamespace(),
        user=SimpleNamespace(id="user-1", name="Test User"),
        incoming=SimpleNamespace(
            dingtalk_user_id="ding-user-1",
            source="dingtalk_webhook",
            text="hello",
            conversation_id=context.conversation_id,
        ),
        settings=SimpleNamespace(timezone="Asia/Shanghai"),
        message_id=context.source_message_id,
        llm_client=SimpleNamespace(),
    )

    assert result.read_only is True
    assert result.message == "read-only answer"
    outcomes, scope = calls["persist"]
    assert outcomes[0].user_visible_snapshot == {"text": "read-only answer"}
    assert scope["tenant_id"] == context.tenant_id
    assert scope["user_id"] == context.actor_user_id
    assert scope["conversation_id"] == context.conversation_id
    assert scope["source_turn_id"] == context.source_message_id


@pytest.mark.asyncio
async def test_stream_read_only_reply_persists_before_event_commit_and_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_package = ModuleType("app.progress")
    outbox_module = ModuleType("app.progress.outbox")
    outbox_module.enqueue_daily_report_outbox_best_effort = (
        lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(sys.modules, "app.progress", progress_package)
    monkeypatch.setitem(sys.modules, "app.progress.outbox", outbox_module)
    from app import stream_runner

    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    context = BusinessCommandContext(
        tenant_id="sandbox-agent2-phase2-20260711",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="user-1",
        actor_role_ids=(),
        allowed_case_ids=(),
        source_message_id="stream-read-only-message",
        source_channel="dingtalk_stream",
        occurred_at=now,
        conversation_id="stream-read-only-conversation",
        conversation_state_version=4,
    )
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="dingtalk_stream_text",
        raw_text="hello",
        message_id=context.source_message_id,
        conversation_id=context.conversation_id,
    )
    entrypoint = SimpleNamespace(
        decision=SimpleNamespace(route="agent2_primary"),
        binding=SimpleNamespace(),
    )
    orchestration = SimpleNamespace(
        command_plan=SimpleNamespace(
            daily_commands=(), business_commands=(), report_commands=(),
        ),
        decision=SimpleNamespace(
            admission_mode="enforced",
            admission_selection_requests=(),
            clarification_need=None,
        ),
    )
    runtime_result = SimpleNamespace(
        orchestration=orchestration,
        business_execution_context=context,
        mutation_execution_authority="semantic_ticket",
        selection_continuation=None,
    )
    runtime = SimpleNamespace()

    async def handle(_request):
        return runtime_result

    runtime.handle = handle
    order: list[str] = []
    calls: dict[str, object] = {}

    async def build_context(**_kwargs):
        return (
            envelope,
            SimpleNamespace(),
            SimpleNamespace(report_date=date(2026, 7, 14)),
            date(2026, 7, 14),
        )

    async def resolve_entrypoint(*_args, **_kwargs):
        return entrypoint

    async def side_reply(**_kwargs):
        return "read-only answer"

    async def persist(_session, outcomes, **kwargs):
        order.append("persist")
        calls["persist"] = (outcomes, kwargs)

    async def mark_processed(_session, _event, **_kwargs):
        order.append("mark")

    async def send_reply(_handler, _robot, _job, reply_text):
        order.append("send")
        calls["reply"] = reply_text
        return 0.0

    monkeypatch.setattr(stream_runner, "resolve_agent2_entrypoint", resolve_entrypoint)
    monkeypatch.setattr(
        stream_runner,
        "build_business_command_context",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(stream_runner, "cognitive_core_v3_enabled", lambda _settings: True)
    monkeypatch.setattr(
        stream_runner,
        "semantic_admission_mode",
        lambda *_args, **_kwargs: "enforced",
    )
    monkeypatch.setattr(stream_runner, "_build_stream_cognitive_context", build_context)
    monkeypatch.setattr(stream_runner, "production_agent2_turn_runtime", lambda: runtime)
    monkeypatch.setattr(stream_runner, "build_cognitive_side_reply_v3", side_reply)
    monkeypatch.setattr(stream_runner, "persist_operation_outcomes", persist)
    monkeypatch.setattr(stream_runner, "mark_webhook_event_processed", mark_processed)
    monkeypatch.setattr(stream_runner, "_reply", send_reply)

    class Session:
        async def commit(self):
            order.append("commit")

    job = stream_runner.StreamJob(
        message=SimpleNamespace(
            message_id=context.source_message_id,
            conversation_id=context.conversation_id,
        ),
        text="hello",
        payload={},
    )
    result = await stream_runner._process_stream_agent2_daily_if_enabled(
        session=Session(),
        user=SimpleNamespace(id="user-1", dingtalk_user_id="ding-user-1"),
        event=SimpleNamespace(
            id="event-1",
            external_message_id=context.source_message_id,
            conversation_id=context.conversation_id,
        ),
        job=job,
        handler=SimpleNamespace(),
        robot=SimpleNamespace(),
        llm_client=SimpleNamespace(),
        settings=SimpleNamespace(
            timezone="Asia/Shanghai",
            stream_processing_timeout_seconds=5,
        ),
        performance_service=SimpleNamespace(),
        timings={},
    )

    assert result == "agent2_cognitive_v3_read_only"
    assert order == ["persist", "mark", "commit", "send"]
    outcomes, scope = calls["persist"]
    assert outcomes[0].user_visible_snapshot == {"text": "read-only answer"}
    assert scope["tenant_id"] == context.tenant_id
    assert calls["reply"] == "read-only answer"
