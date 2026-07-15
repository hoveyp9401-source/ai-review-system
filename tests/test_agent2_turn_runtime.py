from __future__ import annotations

import asyncio
import ast
from datetime import UTC, date, datetime
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.turn_runtime import (
    Agent2TurnRuntime,
    Agent2TurnRuntimeResult,
    VerifiedTurnRejected,
    VerifiedTurnRequest,
)
from app.workflows.intake import IncomingMessageEnvelope


@pytest.mark.parametrize(
    ("admission_mode", "expected_authority"),
    (
        ("enforced", "semantic_ticket"),
        ("shadow", "legacy_user_compatibility"),
        ("disabled", "legacy_user_compatibility"),
    ),
)
def test_runtime_maps_trusted_admission_mode_to_execution_authority(
    admission_mode,
    expected_authority,
):
    result = object.__new__(Agent2TurnRuntimeResult)
    object.__setattr__(result, "admission_mode", admission_mode)

    assert result.mutation_execution_authority == expected_authority


def _business_context(*, source: str, message_id: str) -> BusinessCommandContext:
    return BusinessCommandContext(
        tenant_id="sandbox-agent2-phase2-20260711",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=("case-1",),
        source_message_id=message_id,
        source_channel=source,
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        conversation_id="conversation-1",
    )


def _information_pending(
    *,
    expected_state_version: int,
    source_message_id: str = "manual:pending-lifecycle",
) -> SimpleNamespace:
    return SimpleNamespace(
        pending_id="pending-1",
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id=source_message_id,
        source_message_id=source_message_id,
        expected_conversation_state_version=expected_state_version,
        pending_status="active",
        business_write_allowed=False,
    )


def test_webhook_stream_and_manual_share_one_verified_turn_runtime_contract():
    calls: list[dict] = []
    started_at = datetime(2026, 7, 14, 9, 1, tzinfo=UTC)

    async def evaluate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            base_state=SimpleNamespace(version=7),
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256(
                    kwargs["envelope"].raw_text.encode("utf-8")
                ).hexdigest()
            ),
        )

    runtime = Agent2TurnRuntime(evaluator=evaluate, clock=lambda: started_at)
    results = []
    for source in (
        "dingtalk_webhook_text",
        "dingtalk_stream_text",
        "manual_text",
    ):
        message_id = f"{source}:message-1"
        envelope = IncomingMessageEnvelope(
            sender_id="user-1",
            sender_name="Test User",
            dingtalk_user_id="ding-user-1",
            source=source,
            raw_text="今天联系法院推进了海花岛案。",
            message_id=message_id,
            conversation_id="conversation-1",
            received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        )
        results.append(
            asyncio.run(
                runtime.handle(
                    VerifiedTurnRequest(
                        session=object(),
                        user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                        envelope=envelope,
                        llm_client=object(),
                        daily_report=None,
                        report_date=date(2026, 7, 14),
                        settings=SimpleNamespace(
                            agent2_semantic_admission_enabled=False
                        ),
                        business_context=_business_context(
                            source=source,
                            message_id=message_id,
                        ),
                    )
                )
            )
        )

    assert len(calls) == 3
    assert [call["envelope"].source for call in calls] == [
        "dingtalk_webhook_text",
        "dingtalk_stream_text",
        "manual_text",
    ]
    for result in results:
        assert result.business_execution_context is not None
        assert result.business_execution_context.conversation_state_version == 7
        assert result.business_execution_context.execution_started_at == started_at
        daily_context = result.daily_execution_context()
        assert daily_context.tenant_id == "sandbox-agent2-phase2-20260711"
        assert daily_context.conversation_id == "conversation-1"
        assert daily_context.source_turn_id.endswith(":message-1")
        assert daily_context.occurred_at == datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
        assert daily_context.execution_started_at == started_at
        assert daily_context.conversation_state_version == 7
        assert len(daily_context.source_text_hash) == 64


def test_runtime_rejects_source_scope_mismatch_before_semantic_interpretation():
    calls = 0

    async def evaluate(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("semantic interpreter must not run")

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text="记录到日报",
        message_id="manual:message-1",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )
    context = _business_context(
        source="manual_text",
        message_id="different-message",
    )

    with pytest.raises(VerifiedTurnRejected, match="verified_source_message_mismatch"):
        asyncio.run(
            Agent2TurnRuntime(evaluator=evaluate).handle(
                VerifiedTurnRequest(
                    session=object(),
                    user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                    envelope=envelope,
                    llm_client=object(),
                    daily_report=None,
                    report_date=date(2026, 7, 14),
                    settings=SimpleNamespace(
                        agent2_semantic_admission_enabled=False
                    ),
                    business_context=context,
                )
            )
        )

    assert calls == 0


def test_allowlisted_admission_user_cannot_bypass_verified_business_context():
    calls = 0

    async def evaluate(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("semantic interpreter must not run")

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text="今天联系法院推进了海花岛案。",
        message_id="manual:message-2",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )
    settings = SimpleNamespace(
        agent2_semantic_admission_enabled=True,
        agent2_semantic_admission_tenant_allowlist=(
            "sandbox-agent2-phase2-20260711"
        ),
        agent2_semantic_admission_user_allowlist="user-1",
    )

    with pytest.raises(VerifiedTurnRejected, match="verified_business_context_required"):
        asyncio.run(
            Agent2TurnRuntime(evaluator=evaluate).handle(
                VerifiedTurnRequest(
                    session=object(),
                    user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                    envelope=envelope,
                    llm_client=object(),
                    daily_report=None,
                    report_date=date(2026, 7, 14),
                    settings=settings,
                )
            )
        )

    assert calls == 0


def test_empty_admission_allowlists_preserve_disabled_legacy_runtime():
    async def evaluate(**kwargs):
        return SimpleNamespace(
            base_state=SimpleNamespace(version=1),
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256(
                    kwargs["envelope"].raw_text.encode("utf-8")
                ).hexdigest(),
                admission_mode="disabled",
            ),
        )

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text="写个日报",
        message_id="manual:empty-admission-allowlists",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )
    result = asyncio.run(
        Agent2TurnRuntime(evaluator=evaluate).handle(
            VerifiedTurnRequest(
                session=object(),
                user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                envelope=envelope,
                llm_client=object(),
                daily_report=None,
                report_date=date(2026, 7, 14),
                settings=SimpleNamespace(
                    agent2_semantic_admission_enabled=True,
                    agent2_semantic_admission_tenant_allowlist="",
                    agent2_semantic_admission_user_allowlist="user-1",
                ),
            )
        )
    )

    assert result.business_execution_context is None
    assert result.admission_artifact_persistence_status == "not_applicable"


def test_runtime_returns_information_pending_as_audit_artifact_not_a_command():
    pending = object()

    async def evaluate(**kwargs):
        return SimpleNamespace(
            base_state=SimpleNamespace(version=3),
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256(
                    kwargs["envelope"].raw_text.encode("utf-8")
                ).hexdigest(),
                admission_tickets=(),
                admission_information_pendings=(pending,),
                admission_trace=SimpleNamespace(trace_id="trace-1"),
            ),
        )

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text="记录这个案件的进展",
        message_id="manual:information-pending",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )
    result = asyncio.run(
        Agent2TurnRuntime(evaluator=evaluate).handle(
            VerifiedTurnRequest(
                session=object(),
                user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                envelope=envelope,
                llm_client=object(),
                daily_report=None,
                report_date=date(2026, 7, 14),
                settings=SimpleNamespace(agent2_semantic_admission_enabled=False),
                business_context=_business_context(
                    source="manual_text",
                    message_id=envelope.message_id,
                ),
            )
        )
    )

    assert result.information_pendings == (pending,)
    assert result.admission_tickets == ()
    assert result.admission_trace.trace_id == "trace-1"


def test_runtime_rejects_semantic_result_for_different_source_text():
    async def evaluate(**kwargs):
        return SimpleNamespace(
            base_state=SimpleNamespace(version=3),
            decision=SimpleNamespace(source_text_hash="0" * 64),
        )

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="dingtalk_webhook_text",
        raw_text="明天去南京出差",
        message_id="webhook:source-hash",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )

    with pytest.raises(
        VerifiedTurnRejected, match="semantic_result_source_hash_mismatch"
    ):
        asyncio.run(
            Agent2TurnRuntime(evaluator=evaluate).handle(
                VerifiedTurnRequest(
                    session=object(),
                    user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                    envelope=envelope,
                    llm_client=object(),
                    daily_report=None,
                    report_date=date(2026, 7, 14),
                    settings=SimpleNamespace(
                        agent2_semantic_admission_enabled=False
                    ),
                    business_context=_business_context(
                        source="dingtalk_webhook_text",
                        message_id=envelope.message_id,
                    ),
                )
            )
        )


def test_enforced_admission_requires_authoritative_artifact_sink_before_execution_binding():
    async def evaluate(**kwargs):
        return SimpleNamespace(
            base_state=SimpleNamespace(version=3),
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256(
                    kwargs["envelope"].raw_text.encode("utf-8")
                ).hexdigest(),
                admission_mode="enforced",
                admission_tickets=(SimpleNamespace(ticket_id="ticket-1"),),
                admission_information_pendings=(),
                admission_trace=SimpleNamespace(trace_id="trace-1"),
            ),
        )

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="dingtalk_webhook_text",
        raw_text="明天去南京出差",
        message_id="webhook:enforced-no-sink",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )

    with pytest.raises(
        VerifiedTurnRejected, match="admission_artifact_sink_required"
    ):
        asyncio.run(
            Agent2TurnRuntime(evaluator=evaluate).handle(
                VerifiedTurnRequest(
                    session=object(),
                    user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                    envelope=envelope,
                    llm_client=object(),
                    daily_report=None,
                    report_date=date(2026, 7, 14),
                    settings=SimpleNamespace(
                        agent2_semantic_admission_enabled=True,
                        agent2_semantic_admission_enforce=True,
                        agent2_semantic_admission_tenant_allowlist=(
                            "sandbox-agent2-phase2-20260711"
                        ),
                        agent2_semantic_admission_user_allowlist="user-1",
                    ),
                    business_context=_business_context(
                        source="dingtalk_webhook_text",
                        message_id=envelope.message_id,
                    ),
                )
            )
        )


def test_runtime_rejects_admission_mode_downgrade_from_trusted_feature_flags():
    sink_calls = 0

    class Sink:
        async def persist(self, request):
            nonlocal sink_calls
            sink_calls += 1

    async def evaluate(**kwargs):
        return SimpleNamespace(
            base_state=SimpleNamespace(version=3),
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256(
                    kwargs["envelope"].raw_text.encode("utf-8")
                ).hexdigest(),
                admission_mode="disabled",
                admission_tickets=(),
                admission_information_pendings=(),
                admission_trace=None,
            ),
        )

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text="记录案件进展",
        message_id="manual:mode-downgrade",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )
    settings = SimpleNamespace(
        agent2_semantic_admission_enabled=True,
        agent2_semantic_admission_enforce=True,
        agent2_semantic_admission_tenant_allowlist=(
            "sandbox-agent2-phase2-20260711"
        ),
        agent2_semantic_admission_user_allowlist="user-1",
    )

    with pytest.raises(
        VerifiedTurnRejected, match="semantic_result_admission_mode_mismatch"
    ):
        asyncio.run(
            Agent2TurnRuntime(
                evaluator=evaluate,
                admission_artifact_sink=Sink(),
            ).handle(
                VerifiedTurnRequest(
                    session=object(),
                    user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                    envelope=envelope,
                    llm_client=object(),
                    daily_report=None,
                    report_date=date(2026, 7, 14),
                    settings=settings,
                    business_context=_business_context(
                        source="manual_text",
                        message_id=envelope.message_id,
                    ),
                )
            )
        )

    assert sink_calls == 0


def test_enforced_admission_persists_decision_and_artifacts_before_returning_context():
    ticket = SimpleNamespace(ticket_id="ticket-1", status="issued")
    pending = _information_pending(
        expected_state_version=4,
        source_message_id="manual:persist-artifacts",
    )
    trace = SimpleNamespace(trace_id="trace-1")
    persisted = []

    class Sink:
        async def persist(self, request):
            persisted.append(request)

    async def evaluate(**kwargs):
        return SimpleNamespace(
            base_state=SimpleNamespace(version=4),
            decision=SimpleNamespace(
                decision_id="decision-1",
                source_text_hash=hashlib.sha256(
                    kwargs["envelope"].raw_text.encode("utf-8")
                ).hexdigest(),
                admission_mode="enforced",
                admission_tickets=(ticket,),
                admission_information_pendings=(pending,),
                admission_trace=trace,
            ),
        )

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text="记录这个案件的进展",
        message_id="manual:persist-artifacts",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )
    result = asyncio.run(
        Agent2TurnRuntime(
            evaluator=evaluate,
            admission_artifact_sink=Sink(),
        ).handle(
            VerifiedTurnRequest(
                session=object(),
                user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                envelope=envelope,
                llm_client=object(),
                daily_report=None,
                report_date=date(2026, 7, 14),
                settings=SimpleNamespace(
                    agent2_semantic_admission_enabled=True,
                    agent2_semantic_admission_enforce=True,
                    agent2_semantic_admission_tenant_allowlist=(
                        "sandbox-agent2-phase2-20260711"
                    ),
                    agent2_semantic_admission_user_allowlist="user-1",
                ),
                business_context=_business_context(
                    source="manual_text",
                    message_id=envelope.message_id,
                ),
            )
        )
    )

    assert len(persisted) == 1
    persistence = persisted[0]
    assert persistence.tenant_id == "sandbox-agent2-phase2-20260711"
    assert persistence.user_id == "user-1"
    assert persistence.decision.decision_id == "decision-1"
    assert persistence.trace is trace
    assert persistence.tickets == (ticket,)
    assert persistence.information_pendings == (pending,)
    assert result.admission_artifact_persistence_status == "persisted"
    assert result.admission_artifact_persistence_error == ""
    assert result.business_execution_context.conversation_state_version == 4
    assert result.information_pendings[0].pending_status == "active"


def test_enforced_runtime_rejects_information_pending_not_bound_to_base_state_before_sink():
    sink_calls = 0

    class Sink:
        async def persist(self, request):
            nonlocal sink_calls
            sink_calls += 1

    async def evaluate(**kwargs):
        return SimpleNamespace(
            base_state=SimpleNamespace(version=4),
            state_persisted=False,
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256(
                    kwargs["envelope"].raw_text.encode("utf-8")
                ).hexdigest(),
                admission_mode="enforced",
                admission_tickets=(),
                admission_information_pendings=(
                    _information_pending(expected_state_version=5),
                ),
                admission_trace=SimpleNamespace(
                    decisions=(SimpleNamespace(status="information_required"),)
                ),
            ),
        )

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text="明天",
        message_id="manual:pending-lifecycle",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )

    with pytest.raises(
        VerifiedTurnRejected,
        match="information_pending_state_version_mismatch",
    ):
        asyncio.run(
            Agent2TurnRuntime(
                evaluator=evaluate,
                admission_artifact_sink=Sink(),
            ).handle(
                VerifiedTurnRequest(
                    session=object(),
                    user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                    envelope=envelope,
                    llm_client=object(),
                    daily_report=None,
                    report_date=date(2026, 7, 14),
                    settings=SimpleNamespace(
                        agent2_semantic_admission_enabled=True,
                        agent2_semantic_admission_enforce=True,
                        agent2_semantic_admission_tenant_allowlist=(
                            "sandbox-agent2-phase2-20260711"
                        ),
                        agent2_semantic_admission_user_allowlist="user-1",
                    ),
                    business_context=_business_context(
                        source="manual_text",
                        message_id=envelope.message_id,
                    ),
                )
            )
        )

    assert sink_calls == 0


@pytest.mark.parametrize(
    ("decision_status", "include_pending"),
    (("information_required", True), ("blocked", False)),
)
def test_enforced_runtime_rejects_state_advance_for_pending_or_blocked_admission(
    decision_status: str,
    include_pending: bool,
):
    sink_calls = 0

    class Sink:
        async def persist(self, request):
            nonlocal sink_calls
            sink_calls += 1

    async def evaluate(**kwargs):
        return SimpleNamespace(
            base_state=SimpleNamespace(version=4),
            state_persisted=True,
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256(
                    kwargs["envelope"].raw_text.encode("utf-8")
                ).hexdigest(),
                admission_mode="enforced",
                admission_tickets=(),
                admission_information_pendings=(
                    (_information_pending(expected_state_version=4),)
                    if include_pending
                    else ()
                ),
                admission_trace=SimpleNamespace(
                    decisions=(SimpleNamespace(status=decision_status),)
                ),
            ),
        )

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="manual_text",
        raw_text="确认",
        message_id="manual:pending-lifecycle",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )

    with pytest.raises(
        VerifiedTurnRejected,
        match="admission_nonadvancing_turn_persisted_state",
    ):
        asyncio.run(
            Agent2TurnRuntime(
                evaluator=evaluate,
                admission_artifact_sink=Sink(),
            ).handle(
                VerifiedTurnRequest(
                    session=object(),
                    user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                    envelope=envelope,
                    llm_client=object(),
                    daily_report=None,
                    report_date=date(2026, 7, 14),
                    settings=SimpleNamespace(
                        agent2_semantic_admission_enabled=True,
                        agent2_semantic_admission_enforce=True,
                        agent2_semantic_admission_tenant_allowlist=(
                            "sandbox-agent2-phase2-20260711"
                        ),
                        agent2_semantic_admission_user_allowlist="user-1",
                    ),
                    business_context=_business_context(
                        source="manual_text",
                        message_id=envelope.message_id,
                    ),
                )
            )
        )

    assert sink_calls == 0


def test_shadow_artifact_sink_failure_is_observable_without_changing_legacy_result():
    class FailingSink:
        async def persist(self, request):
            raise RuntimeError("database unavailable")

    async def evaluate(**kwargs):
        return SimpleNamespace(
            base_state=SimpleNamespace(version=2),
            decision=SimpleNamespace(
                source_text_hash=hashlib.sha256(
                    kwargs["envelope"].raw_text.encode("utf-8")
                ).hexdigest(),
                admission_mode="shadow",
                admission_tickets=(),
                admission_information_pendings=(),
                admission_trace=SimpleNamespace(trace_id="shadow-trace"),
            ),
        )

    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="dingtalk_stream_text",
        raw_text="没其他风险",
        message_id="stream:shadow-sink-failure",
        conversation_id="conversation-1",
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
    )
    result = asyncio.run(
        Agent2TurnRuntime(
            evaluator=evaluate,
            admission_artifact_sink=FailingSink(),
        ).handle(
            VerifiedTurnRequest(
                session=object(),
                user=SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
                envelope=envelope,
                llm_client=object(),
                daily_report=None,
                report_date=date(2026, 7, 14),
                settings=SimpleNamespace(
                    agent2_semantic_admission_enabled=True,
                    agent2_semantic_admission_enforce=False,
                    agent2_semantic_admission_tenant_allowlist=(
                        "sandbox-agent2-phase2-20260711"
                    ),
                    agent2_semantic_admission_user_allowlist="user-1",
                ),
                business_context=_business_context(
                    source="dingtalk_stream_text",
                    message_id=envelope.message_id,
                ),
            )
        )
    )

    assert result.orchestration.base_state.version == 2
    assert result.admission_artifact_persistence_status == "failed_shadow"
    assert result.admission_artifact_persistence_error == "RuntimeError"


@pytest.mark.parametrize(
    "relative_path",
    ("app/api/webhook.py", "app/stream_runner.py", "app/api/reports.py"),
)
def test_production_entrypoints_use_verified_turn_runtime_without_direct_cognitive_bypass(
    relative_path: str,
):
    tree = ast.parse(Path(relative_path).read_text(encoding="utf-8"))
    verified_requests = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "VerifiedTurnRequest"
    ]
    runtime_handles = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "handle"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "production_agent2_turn_runtime"
    ]
    direct_evaluations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "evaluate_cognitive_core_v3"
    ]

    assert verified_requests
    assert runtime_handles
    assert not direct_evaluations
    for handle in runtime_handles:
        constructor = handle.func.value
        assert constructor.args == []
        assert constructor.keywords == []
    for request in verified_requests:
        assert "business_context" in {keyword.arg for keyword in request.keywords}


def test_manual_phase2_route_builds_verified_scope_and_never_falls_back_when_core_is_off():
    tree = ast.parse(Path("app/api/reports.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_submit_manual_agent2_if_applicable"
    )
    source = ast.unparse(function)

    assert "build_business_command_context" in source
    assert "phase2_primary and (not cognitive_core_v3_enabled(settings))" in source
    assert "entrypoint.decision.route == 'blocked'" in source


def test_production_interpreter_disables_legacy_semantic_enforcers_only_in_enforce_mode():
    tree = ast.parse(
        Path("app/agent2/cognitive_runtime_v3.py").read_text(encoding="utf-8")
    )
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "evaluate_cognitive_core_v3"
    )
    interpreter_call = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "LLMCognitiveSemanticInterpreter"
    )
    keyword = next(
        item
        for item in interpreter_call.keywords
        if item.arg == "legacy_semantic_enforcers_enabled"
    )

    assert ast.unparse(keyword.value) == "admission_mode != 'enforced'"
