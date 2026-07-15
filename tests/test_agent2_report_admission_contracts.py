from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from app.agent2.admission_contracts import (
    ADMISSION_CONTRACT_VERSION,
    AdmissionExecutionScope,
    AdmissionTicket,
)
from app.agent2.admission_hashes import compute_admission_claim_hashes
from app.agent2.cognitive_core_v3 import (
    CognitiveCoreV3,
    CognitiveTurn,
    SemanticInterpretation,
)
from app.agent2.command_planner_v3 import (
    CognitiveCommandPlanner,
    CommandPlanningContext,
    DailySnapshotReference,
)
from app.agent2.conversation_state import ConversationState
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    TypedDailyCommand,
    execute_typed_daily_command,
)
from app.agent2.report_domain import (
    PeriodicReportSnapshot,
    TypedPeriodicReportCommand,
    execute_periodic_report_command,
)


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


class _StaticInterpreter:
    def __init__(self, interpretation: SemanticInterpretation) -> None:
        self.interpretation = interpretation

    async def interpret(self, turn, state):
        return self.interpretation


def _uuid(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"report-admission-contract:{label}")


def _daily_snapshot() -> DailyReportMutationSnapshot:
    return DailyReportMutationSnapshot(
        report_id=_uuid("daily-report"),
        owner_user_id=_uuid("actor"),
        version=4,
        status="collecting",
        today_work=("完成合同审核", "提交补充材料"),
        problems=("暂无",),
        tomorrow_plan=("跟进付款",),
        item_ids={
            "today_work": ("today-1", "today-2"),
            "problems": ("problem-1",),
            "tomorrow_plan": ("plan-1",),
        },
    )


def _scope() -> AdmissionExecutionScope:
    return AdmissionExecutionScope(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id=str(_uuid("actor")),
        conversation_id="conversation-report-admission",
        source_message_id="message-report-admission",
        executed_at=NOW + timedelta(seconds=5),
        conversation_state_version=7,
    )


def _periodic_snapshot() -> PeriodicReportSnapshot:
    return PeriodicReportSnapshot(
        report_id=_uuid("weekly-report"),
        owner_user_id=_uuid("actor"),
        report_type="weekly",
        period_key="2026-W29",
        version=2,
        status="collecting",
        sections={"accomplishments": ("完成初稿",)},
        item_ids={"accomplishments": ("weekly-item-1",)},
    )


def _periodic_ticket(
    *,
    operation: str,
    command_type: str,
    target_item_ids: tuple[str, ...] = (),
    patch: dict | None = None,
    allowed_changed_fields: tuple[str, ...],
) -> dict:
    snapshot = _periodic_snapshot()
    ticket = {
        "ticket_id": f"ticket-{operation}",
        "tenant_id": "sandbox-agent2-phase2-20260711",
        "user_id": str(snapshot.owner_user_id),
        "conversation_id": "conversation-report-admission",
        "source_message_id": "message-report-admission",
        "action_id": f"action-{operation}",
        "segment_id": f"segment-{operation}",
        "segment_text_sha256": "a" * 64,
        "domain": "report",
        "operation": operation,
        "object_ref": {
            "object_type": "periodic_report",
            "stable_id": str(snapshot.report_id),
            "version": snapshot.version,
        },
        "expected_conversation_state_version": 7,
        "authority_scope": {
            "report_type": snapshot.report_type,
            "period_key": snapshot.period_key,
            "report_id": str(snapshot.report_id),
            "report_version": snapshot.version,
            "command_type": command_type,
            "target_item_ids": list(target_item_ids),
            "patch": dict(patch or {}),
        },
        "allowed_changed_fields": list(allowed_changed_fields),
        "ticket_status": "issued",
        "executor_revalidation_required": True,
        "proves_business_write": False,
        "contract_version": ADMISSION_CONTRACT_VERSION,
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "idempotency_key": f"admission-{operation}",
    }
    fact_hash, command_hash = compute_admission_claim_hashes(
        action_id=ticket["action_id"],
        operation=ticket["operation"],
        segment_text_sha256=ticket["segment_text_sha256"],
        domain=ticket["domain"],
        object_ref=ticket["object_ref"],
        authority_scope=ticket["authority_scope"],
        allowed_changed_fields=ticket["allowed_changed_fields"],
    )
    ticket["fact_claims_sha256"] = fact_hash
    ticket["authorized_command_sha256"] = command_hash
    return ticket


def _periodic_command(
    *,
    operation: str,
    command_type: str,
    target_item_ids: tuple[str, ...] = (),
    patch: dict | None = None,
    allowed_changed_fields: tuple[str, ...],
) -> TypedPeriodicReportCommand:
    snapshot = _periodic_snapshot()
    return TypedPeriodicReportCommand(
        command_id=_uuid(f"command-{operation}"),
        decision_id=_uuid(f"decision-{operation}"),
        sub_decision_id=_uuid(f"sub-decision-{operation}"),
        command_type=command_type,
        report_type=snapshot.report_type,
        period_key=snapshot.period_key,
        report_id=snapshot.report_id,
        report_version=snapshot.version,
        target_item_ids=target_item_ids,
        patch=dict(patch or {}),
        idempotency_key=f"command-{operation}",
        admission_ticket=_periodic_ticket(
            operation=operation,
            command_type=command_type,
            target_item_ids=target_item_ids,
            patch=patch,
            allowed_changed_fields=allowed_changed_fields,
        ),
        admission_required=True,
        admission_action_id=f"action-{operation}",
        admission_operation=operation,
    )


def _daily_ticket(
    *,
    operation: str,
    command_type: str,
    target_item_ids: tuple[str, ...] = (),
    patch: dict | None = None,
    allowed_changed_fields: tuple[str, ...] = (),
) -> dict:
    snapshot = _daily_snapshot()
    ticket = {
        "ticket_id": f"ticket-{operation}",
        "tenant_id": "sandbox-agent2-phase2-20260711",
        "user_id": str(snapshot.owner_user_id),
        "conversation_id": "conversation-report-admission",
        "source_message_id": "message-report-admission",
        "action_id": f"action-{operation}",
        "segment_id": f"segment-{operation}",
        "segment_text_sha256": "a" * 64,
        "domain": "report",
        "operation": operation,
        "object_ref": {
            "object_type": "daily_report",
            "stable_id": str(snapshot.report_id),
            "version": snapshot.version,
        },
        "expected_conversation_state_version": 7,
        "authority_scope": {
            "report_type": "daily",
            "report_id": str(snapshot.report_id),
            "report_version": snapshot.version,
            "command_type": command_type,
            "target_item_ids": list(target_item_ids),
            "patch": dict(patch or {}),
        },
        "allowed_changed_fields": list(allowed_changed_fields),
        "ticket_status": "issued",
        "executor_revalidation_required": True,
        "proves_business_write": False,
        "contract_version": ADMISSION_CONTRACT_VERSION,
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "idempotency_key": f"admission-{operation}",
    }
    fact_hash, command_hash = compute_admission_claim_hashes(
        action_id=ticket["action_id"],
        operation=ticket["operation"],
        segment_text_sha256=ticket["segment_text_sha256"],
        domain=ticket["domain"],
        object_ref=ticket["object_ref"],
        authority_scope=ticket["authority_scope"],
        allowed_changed_fields=ticket["allowed_changed_fields"],
    )
    ticket["fact_claims_sha256"] = fact_hash
    ticket["authorized_command_sha256"] = command_hash
    return ticket


def _ticket_record(payload: dict) -> AdmissionTicket:
    return AdmissionTicket(
        ticket_id=payload["ticket_id"],
        tenant_id=payload["tenant_id"],
        user_id=payload["user_id"],
        conversation_id=payload["conversation_id"],
        source_message_id=payload["source_message_id"],
        action_id=payload["action_id"],
        segment_id=payload["segment_id"],
        segment_text_sha256=payload.get("segment_text_sha256", ""),
        domain=payload["domain"],
        operation=payload["operation"],
        object_ref=payload["object_ref"],
        contract_version=payload["contract_version"],
        issued_at=datetime.fromisoformat(payload["issued_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
        idempotency_key=payload["idempotency_key"],
        expected_conversation_state_version=payload[
            "expected_conversation_state_version"
        ],
        authority_scope=payload["authority_scope"],
        allowed_changed_fields=tuple(payload["allowed_changed_fields"]),
        fact_claims_sha256=payload.get("fact_claims_sha256", ""),
        authorized_command_sha256=payload.get("authorized_command_sha256", ""),
    )


def _daily_command(
    *,
    operation: str,
    command_type: str,
    target_item_ids: tuple[str, ...] = (),
    patch: dict | None = None,
    allowed_changed_fields: tuple[str, ...] = ("status",),
) -> TypedDailyCommand:
    snapshot = _daily_snapshot()
    ticket = _daily_ticket(
        operation=operation,
        command_type=command_type,
        target_item_ids=target_item_ids,
        patch=patch,
        allowed_changed_fields=allowed_changed_fields,
    )
    return TypedDailyCommand(
        command_id=_uuid(f"command-{operation}"),
        decision_id=_uuid(f"decision-{operation}"),
        sub_decision_id=_uuid(f"sub-decision-{operation}"),
        command_type=command_type,
        report_id=snapshot.report_id,
        report_version=snapshot.version,
        target_item_ids=target_item_ids,
        patch=dict(patch or {}),
        idempotency_key=f"command-{operation}",
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id=f"action-{operation}",
        admission_operation=operation,
    )


def test_daily_submit_blocks_ticket_with_wrong_changed_field_claims() -> None:
    snapshot = _daily_snapshot()
    command = _daily_command(
        operation="submit_daily_report",
        command_type="submit_report",
    )
    forged = replace(
        command,
        admission_ticket={
            **command.admission_ticket,
            "allowed_changed_fields": ["items"],
        },
    )

    result = execute_typed_daily_command(
        forged,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.validation.reason_code == "admission_ticket_claims_mismatch"
    assert result.should_write_db is False
    assert result.after == snapshot


@pytest.mark.parametrize(
    ("operation", "command_type", "target_item_ids", "patch", "allowed_changed_fields"),
    [
        ("submit_daily_report", "submit_report", (), {}, ("status",)),
        ("delete_daily_item", "delete_item", ("today-1",), {}, ("items",)),
        (
            "edit_daily_item",
            "edit_item",
            ("today-1",),
            {"replacement": "完成合同终稿审核"},
            ("items",),
        ),
        (
            "merge_daily_items",
            "merge_items",
            ("today-1", "today-2"),
            {"replacement": "完成合同审核并提交补充材料"},
            ("items",),
        ),
        (
            "clear_daily_report",
            "clear_report",
            (),
            {"field": "all"},
            ("sections", "items"),
        ),
        (
            "clear_daily_section",
            "clear_report",
            (),
            {"field": "problems"},
            ("section", "items"),
        ),
        (
            "copy_previous_daily_report",
            "copy_report",
            (),
            {
                "sections": {"today_work": ["处理历史遗留事项"]},
                "source_report_date": "2026-07-13",
                "source_report_id": str(_uuid("source-daily-report")),
            },
            ("sections", "items"),
        ),
        (
            "copy_current_work_to_tomorrow",
            "copy_report",
            (),
            {
                "sections": {"tomorrow_plan": ["继续完成合同审核"]},
                "source_report_date": "2026-07-14",
                "source_report_id": str(_uuid("daily-report")),
            },
            ("section", "items"),
        ),
        (
            "complete_previous_daily_plan",
            "copy_report",
            (),
            {
                "sections": {"today_work": ["已跟进昨日付款计划"]},
                "source_report_date": "2026-07-13",
                "source_report_id": str(_uuid("source-daily-report")),
            },
            ("section", "items"),
        ),
    ],
)
def test_each_collecting_daily_mutation_requires_exact_operation_contract(
    operation: str,
    command_type: str,
    target_item_ids: tuple[str, ...],
    patch: dict,
    allowed_changed_fields: tuple[str, ...],
) -> None:
    snapshot = _daily_snapshot()
    command = _daily_command(
        operation=operation,
        command_type=command_type,
        target_item_ids=target_item_ids,
        patch=patch,
        allowed_changed_fields=allowed_changed_fields,
    )

    result = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.validation.status == "authorized"
    assert result.should_write_db is True
    assert result.after.version == snapshot.version + 1


def test_daily_reopen_requires_exact_status_ticket() -> None:
    snapshot = replace(_daily_snapshot(), status="completed")
    command = _daily_command(
        operation="reopen_daily_report",
        command_type="reopen_report",
        patch={"report_date": "2026-07-14"},
        allowed_changed_fields=("status",),
    )

    result = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.validation.status == "authorized"
    assert result.should_write_db is True
    assert result.after.status == "collecting"


@pytest.mark.parametrize(
    ("ticket_override", "command_override", "expected_reason"),
    [
        (
            {"expires_at": (NOW + timedelta(seconds=1)).isoformat()},
            {},
            "admission_ticket_expired",
        ),
        (
            {"expected_conversation_state_version": 8},
            {},
            "admission_ticket_state_version_conflict",
        ),
        (
            {
                "object_ref": {
                    "object_type": "daily_report",
                    "stable_id": str(_uuid("daily-report")),
                    "version": 5,
                }
            },
            {},
            "admission_ticket_object_mismatch",
        ),
        (
            {},
            {"patch": {"replacement": "篡改后的更强事实"}},
            "admission_ticket_claims_mismatch",
        ),
    ],
)
def test_daily_edit_revalidates_ttl_state_version_and_exact_fact_claims(
    ticket_override: dict,
    command_override: dict,
    expected_reason: str,
) -> None:
    snapshot = _daily_snapshot()
    command = _daily_command(
        operation="edit_daily_item",
        command_type="edit_item",
        target_item_ids=("today-1",),
        patch={"replacement": "完成合同终稿审核"},
        allowed_changed_fields=("items",),
    )
    candidate = replace(
        command,
        admission_ticket={**command.admission_ticket, **ticket_override},
        **command_override,
    )

    result = execute_typed_daily_command(
        candidate,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.validation.reason_code == expected_reason
    assert result.should_write_db is False
    assert result.after == snapshot


def test_enforced_daily_query_remains_read_only_without_execution_ticket() -> None:
    snapshot = _daily_snapshot()
    interpretation = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_report"],
            "segments": [],
            "entities": [
                {
                    "entity_id": "daily-report",
                    "entity_type": "daily_report",
                    "value": "今天的日报",
                    "confidence": 1.0,
                    "attributes": {
                        "report_id": str(snapshot.report_id),
                        "report_date": "2026-07-14",
                        "version": snapshot.version,
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "query-daily",
                    "action_type": "query_daily_report",
                    "intent": "daily_report",
                    "entity_ids": ["daily-report"],
                }
            ],
            "clarification_need": None,
            "context_update": {},
        }
    )
    turn = CognitiveTurn(
        user_id=str(snapshot.owner_user_id),
        conversation_id="conversation-report-admission",
        message_id="message-report-admission",
        text="看看今天的日报",
        occurred_at=NOW,
    )
    state = ConversationState.empty(
        user_id=str(snapshot.owner_user_id),
        conversation_id=turn.conversation_id,
    )
    decision = asyncio.run(
        CognitiveCoreV3(_StaticInterpreter(interpretation)).process(turn, state)
    ).decision
    decision = replace(decision, admission_mode="enforced")

    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=snapshot.owner_user_id,
            daily_snapshot=snapshot,
            daily_history=(
                DailySnapshotReference(
                    report_date=datetime(2026, 7, 14).date(),
                    snapshot=snapshot,
                ),
            ),
            current_report_date=datetime(2026, 7, 14).date(),
        ),
    )

    assert plan.blocked_actions == ()
    assert len(plan.daily_commands) == 1
    assert plan.daily_commands[0].command_type == "query_report"
    assert plan.daily_commands[0].admission_required is False
    assert plan.daily_commands[0].admission_ticket == {}


def test_enforced_periodic_query_remains_read_only_without_execution_ticket() -> None:
    snapshot = _periodic_snapshot()
    interpretation = SemanticInterpretation.from_payload(
        {
            "intents": ["weekly_report"],
            "segments": [],
            "entities": [
                {
                    "entity_id": "weekly-report",
                    "entity_type": "periodic_report",
                    "value": "本周周报",
                    "confidence": 1.0,
                    "attributes": {
                        "report_type": "weekly",
                        "field": "accomplishments",
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "query-weekly",
                    "action_type": "query_periodic_report",
                    "intent": "weekly_report",
                    "entity_ids": ["weekly-report"],
                }
            ],
            "clarification_need": None,
            "context_update": {},
        }
    )
    turn = CognitiveTurn(
        user_id=str(snapshot.owner_user_id),
        conversation_id="conversation-report-admission",
        message_id="message-report-admission",
        text="看看本周周报",
        occurred_at=NOW,
    )
    state = ConversationState.empty(
        user_id=str(snapshot.owner_user_id),
        conversation_id=turn.conversation_id,
    )
    decision = asyncio.run(
        CognitiveCoreV3(_StaticInterpreter(interpretation)).process(turn, state)
    ).decision
    decision = replace(decision, admission_mode="enforced")

    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=snapshot.owner_user_id,
            periodic_snapshot=snapshot,
        ),
    )

    assert plan.blocked_actions == ()
    assert len(plan.report_commands) == 1
    assert plan.report_commands[0].command_type == "query_report"
    assert plan.report_commands[0].admission_required is False
    assert plan.report_commands[0].admission_ticket == {}


def test_planner_binds_daily_mutation_ticket_to_typed_command() -> None:
    snapshot = _daily_snapshot()
    interpretation = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_report"],
            "segments": [],
            "entities": [],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "action-submit_daily_report",
                    "action_type": "submit_daily_report",
                    "intent": "daily_report",
                    "entity_ids": [],
                }
            ],
            "clarification_need": None,
            "context_update": {},
        }
    )
    turn = CognitiveTurn(
        user_id=str(snapshot.owner_user_id),
        conversation_id="conversation-report-admission",
        message_id="message-report-admission",
        text="提交今天的日报",
        occurred_at=NOW,
    )
    state = ConversationState.empty(
        user_id=str(snapshot.owner_user_id), conversation_id=turn.conversation_id
    )
    decision = asyncio.run(
        CognitiveCoreV3(_StaticInterpreter(interpretation)).process(turn, state)
    ).decision
    ticket = _ticket_record(
        _daily_ticket(
            operation="submit_daily_report",
            command_type="submit_report",
            allowed_changed_fields=("status",),
        )
    )
    decision = replace(
        decision,
        admission_mode="enforced",
        admission_tickets=(ticket,),
    )

    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=snapshot.owner_user_id,
            daily_snapshot=snapshot,
            current_report_date=datetime(2026, 7, 14).date(),
        ),
    )

    assert plan.blocked_actions == ()
    assert len(plan.daily_commands) == 1
    assert plan.daily_commands[0].admission_required is True
    assert plan.daily_commands[0].admission_operation == "submit_daily_report"
    assert plan.daily_commands[0].admission_ticket["ticket_id"] == ticket.ticket_id


def test_planner_binds_periodic_mutation_ticket_to_typed_command() -> None:
    snapshot = _periodic_snapshot()
    interpretation = SemanticInterpretation.from_payload(
        {
            "intents": ["weekly_report"],
            "segments": [],
            "entities": [
                {
                    "entity_id": "weekly-event",
                    "entity_type": "report_event",
                    "value": "完成合同审核",
                    "confidence": 1.0,
                    "attributes": {
                        "report_type": "weekly",
                        "field": "accomplishments",
                    },
                }
            ],
            "confidence": 1.0,
            "required_actions": [
                {
                    "action_id": "action-capture_report_event",
                    "action_type": "capture_report_event",
                    "intent": "weekly_report",
                    "entity_ids": ["weekly-event"],
                }
            ],
            "clarification_need": None,
            "context_update": {},
        }
    )
    turn = CognitiveTurn(
        user_id=str(snapshot.owner_user_id),
        conversation_id="conversation-report-admission",
        message_id="message-report-admission",
        text="周报记一条：完成合同审核",
        occurred_at=NOW,
    )
    state = ConversationState.empty(
        user_id=str(snapshot.owner_user_id), conversation_id=turn.conversation_id
    )
    decision = asyncio.run(
        CognitiveCoreV3(_StaticInterpreter(interpretation)).process(turn, state)
    ).decision
    ticket = _ticket_record(
        _periodic_ticket(
            operation="capture_report_event",
            command_type="append_item",
            patch={"field": "accomplishments", "value": "完成合同审核"},
            allowed_changed_fields=("section", "items"),
        )
    )
    decision = replace(
        decision,
        admission_mode="enforced",
        admission_tickets=(ticket,),
    )

    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=snapshot.owner_user_id,
            periodic_snapshot=snapshot,
        ),
    )

    assert plan.blocked_actions == ()
    assert len(plan.report_commands) == 1
    assert plan.report_commands[0].admission_required is True
    assert plan.report_commands[0].admission_operation == "capture_report_event"
    assert plan.report_commands[0].admission_ticket["ticket_id"] == ticket.ticket_id


def test_periodic_mutation_without_required_ticket_is_zero_write() -> None:
    snapshot = PeriodicReportSnapshot(
        report_id=_uuid("weekly-report"),
        owner_user_id=_uuid("actor"),
        report_type="weekly",
        period_key="2026-W29",
        version=2,
        status="collecting",
    )
    command = TypedPeriodicReportCommand(
        command_id=_uuid("weekly-append-command"),
        decision_id=_uuid("weekly-append-decision"),
        sub_decision_id=_uuid("weekly-append-sub-decision"),
        command_type="append_item",
        report_type="weekly",
        period_key="2026-W29",
        report_id=snapshot.report_id,
        report_version=snapshot.version,
        target_item_ids=(),
        patch={"field": "accomplishments", "value": "完成合同审核"},
        idempotency_key="weekly-append-idempotency",
        admission_required=True,
        admission_action_id="weekly-append-action",
        admission_operation="capture_report_event",
    )

    result = execute_periodic_report_command(
        command,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.reason_code == "missing_admission_ticket"
    assert result.should_write_db is False
    assert result.after == snapshot


def test_periodic_append_with_exact_ticket_claims_writes_once() -> None:
    snapshot = _periodic_snapshot()
    command = _periodic_command(
        operation="capture_report_event",
        command_type="append_item",
        patch={"field": "accomplishments", "value": "完成合同审核"},
        allowed_changed_fields=("section", "items"),
    )

    result = execute_periodic_report_command(
        command,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.validation_status == "authorized"
    assert result.should_write_db is True
    assert result.after.sections["accomplishments"] == ("完成初稿", "完成合同审核")


def test_periodic_append_blocks_tampered_fact_claims_without_write() -> None:
    snapshot = _periodic_snapshot()
    command = _periodic_command(
        operation="capture_report_event",
        command_type="append_item",
        patch={"field": "accomplishments", "value": "完成合同审核"},
        allowed_changed_fields=("section", "items"),
    )
    forged = replace(
        command,
        patch={"field": "accomplishments", "value": "已完成并提交全部材料"},
    )

    result = execute_periodic_report_command(
        forged,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.reason_code == "admission_ticket_claims_mismatch"
    assert result.should_write_db is False
    assert result.after == snapshot


def test_periodic_append_blocks_joint_command_and_ticket_claim_forgery() -> None:
    snapshot = _periodic_snapshot()
    command = _periodic_command(
        operation="capture_report_event",
        command_type="append_item",
        patch={"field": "accomplishments", "value": "完成合同审核"},
        allowed_changed_fields=("section", "items"),
    )
    forged_patch = {
        "field": "accomplishments",
        "value": "已完成并提交全部材料",
    }
    forged = replace(
        command,
        patch=forged_patch,
        admission_ticket={
            **command.admission_ticket,
            "authority_scope": {
                **command.admission_ticket["authority_scope"],
                "patch": forged_patch,
            },
        },
    )

    result = execute_periodic_report_command(
        forged,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.reason_code == "admission_ticket_claims_mismatch"
    assert result.should_write_db is False
    assert result.after == snapshot


def test_periodic_executor_fails_closed_for_non_object_ticket_payload() -> None:
    snapshot = _periodic_snapshot()
    command = _periodic_command(
        operation="capture_report_event",
        command_type="append_item",
        patch={"field": "accomplishments", "value": "完成合同审核"},
        allowed_changed_fields=("section", "items"),
    )
    malformed = replace(command, admission_ticket="forged")  # type: ignore[arg-type]

    result = execute_periodic_report_command(
        malformed,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.reason_code == "invalid_admission_ticket"
    assert result.should_write_db is False
    assert result.after == snapshot


@pytest.mark.parametrize(
    ("ticket_override", "command_override", "expected_reason"),
    [
        (
            {"expires_at": (NOW + timedelta(seconds=1)).isoformat()},
            {},
            "admission_ticket_expired",
        ),
        (
            {"expected_conversation_state_version": 8},
            {},
            "admission_ticket_state_version_conflict",
        ),
        (
            {
                "object_ref": {
                    "object_type": "periodic_report",
                    "stable_id": str(_uuid("weekly-report")),
                    "version": 3,
                }
            },
            {},
            "admission_ticket_object_mismatch",
        ),
        (
            {},
            {"report_version": 3},
            "admission_ticket_object_mismatch",
        ),
    ],
)
def test_periodic_append_revalidates_ttl_state_and_report_version(
    ticket_override: dict,
    command_override: dict,
    expected_reason: str,
) -> None:
    snapshot = _periodic_snapshot()
    command = _periodic_command(
        operation="capture_report_event",
        command_type="append_item",
        patch={"field": "accomplishments", "value": "完成合同审核"},
        allowed_changed_fields=("section", "items"),
    )
    candidate = replace(
        command,
        admission_ticket={**command.admission_ticket, **ticket_override},
        **command_override,
    )

    result = execute_periodic_report_command(
        candidate,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.reason_code == expected_reason
    assert result.should_write_db is False
    assert result.after == snapshot


@pytest.mark.parametrize(
    ("operation", "command_type", "target_item_ids", "patch", "allowed_changed_fields"),
    [
        ("submit_periodic_report", "submit_report", (), {}, ("status",)),
        (
            "edit_periodic_report_item",
            "edit_item",
            ("weekly-item-1",),
            {"replacement": "完成定稿"},
            ("items",),
        ),
        (
            "delete_periodic_report_item",
            "delete_item",
            ("weekly-item-1",),
            {},
            ("items",),
        ),
    ],
)
def test_each_periodic_report_mutation_requires_exact_operation_contract(
    operation: str,
    command_type: str,
    target_item_ids: tuple[str, ...],
    patch: dict,
    allowed_changed_fields: tuple[str, ...],
) -> None:
    snapshot = _periodic_snapshot()
    command = _periodic_command(
        operation=operation,
        command_type=command_type,
        target_item_ids=target_item_ids,
        patch=patch,
        allowed_changed_fields=allowed_changed_fields,
    )

    result = execute_periodic_report_command(
        command,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        admission_scope=_scope(),
    )

    assert result.validation_status == "authorized"
    assert result.should_write_db is True
    assert result.after.version == snapshot.version + 1
