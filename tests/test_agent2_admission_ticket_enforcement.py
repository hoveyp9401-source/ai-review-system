from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from app.agent2.admission_contracts import (
    ADMISSION_CONTRACT_VERSION,
    AdmissionExecutionScope,
)
from app.agent2.cognitive_core_v3 import (
    CognitiveCoreV3,
    CognitiveTurn,
    SemanticInterpretation,
)
from app.agent2.command_planner_v3 import CognitiveCommandPlanner, CommandPlanningContext
from app.agent2.business.case_progress import CaseRecord
from app.agent2.business.compiler import Phase2BusinessCommandCompiler
from app.agent2.business.composition import Phase2BusinessComposer
from app.agent2.business.admission import bind_business_execution_context
from app.agent2.business.contracts import BusinessCommandContext, CreateCaseProgress
from app.agent2.business.executor import InMemoryBusinessExecutor
from app.agent2.admission_store import InMemoryAdmissionTicketStore
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.typed_daily_commands import (
    DailyReportMutationSnapshot,
    TypedDailyCommand,
    execute_typed_daily_command,
)


class _StaticInterpreter:
    def __init__(self, proposal: SemanticInterpretation) -> None:
        self._proposal = proposal

    async def interpret(self, turn, state):
        return self._proposal


def _required_daily_command(
    *,
    ticket_overrides: dict | None = None,
) -> tuple[object, DailyReportMutationSnapshot, TypedDailyCommand]:
    actor_id = uuid5(NAMESPACE_URL, "required-ticket-helper-actor")
    report_id = uuid5(NAMESPACE_URL, "required-ticket-helper-report")
    ticket = {
        "ticket_id": "ticket-required-helper",
        "tenant_id": "sandbox-agent2-phase2-20260711",
        "user_id": str(actor_id),
        "conversation_id": "conversation-required-helper",
        "source_message_id": "message-required-helper",
        "action_id": "append-daily",
        "segment_id": "daily-segment",
        "domain": "report",
        "operation": "capture_daily_event",
        "object_ref": {
            "object_type": "daily_report",
            "stable_id": str(report_id),
            "version": 2,
        },
        "expected_conversation_state_version": 0,
        "authority_scope": {
            "report_id": str(report_id),
            "version": 2,
            "field": "today_work",
            "raw_fact": "完成合同审核",
            "segment_text": "日报记：完成合同审核",
        },
        "allowed_changed_fields": ["section", "items"],
        "ticket_status": "issued",
        "executor_revalidation_required": True,
        "proves_business_write": False,
        "contract_version": ADMISSION_CONTRACT_VERSION,
        "issued_at": "2026-07-14T09:00:00+00:00",
        "expires_at": "2026-07-14T09:05:00+00:00",
        "idempotency_key": "admission-required-helper-key",
    }
    ticket.update(ticket_overrides or {})
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=actor_id,
        version=2,
        status="collecting",
    )
    command = TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "required-ticket-helper-command"),
        decision_id=uuid5(NAMESPACE_URL, "required-ticket-helper-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, "required-ticket-helper-subdecision"),
        command_type="append_item",
        report_id=report_id,
        report_version=2,
        target_item_ids=(),
        patch={"field": "today_work", "items": ["完成合同审核"]},
        idempotency_key="required-ticket-helper-command-key",
        admission_required=True,
        admission_ticket=ticket,
        admission_action_id="append-daily",
        admission_operation="capture_daily_event",
    )
    return actor_id, snapshot, command


def test_enforced_decision_without_admission_ticket_cannot_plan_mutation():
    report_id = uuid5(NAMESPACE_URL, "admission-ticket-report")
    actor_id = uuid5(NAMESPACE_URL, "admission-ticket-actor")
    text = "日报记：完成合同审核"
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append"],
            "segments": [
                {
                    "segment_id": "daily-segment",
                    "text": text,
                    "intents": ["daily_append"],
                    "entity_ids": ["daily-event"],
                    "action_ids": ["append-daily"],
                }
            ],
            "entities": [
                {
                    "entity_id": "daily-event",
                    "entity_type": "daily_event",
                    "value": "完成合同审核",
                    "confidence": 0.99,
                    "attributes": {"field": "today_work"},
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "append-daily",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-event"],
                }
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "daily_append"},
        }
    )
    turn = CognitiveTurn(
        user_id=str(actor_id),
        conversation_id="conversation-ticket-enforcement",
        message_id="message-ticket-enforcement",
        text=text,
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
    )
    decision = asyncio.run(
        CognitiveCoreV3(_StaticInterpreter(proposal)).process(
            turn,
            ConversationState.empty(
                user_id=turn.user_id,
                conversation_id=turn.conversation_id,
            ),
        )
    ).decision
    decision = replace(decision, admission_mode="enforced")

    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=actor_id,
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=report_id,
                owner_user_id=actor_id,
                version=1,
                status="collecting",
            ),
        ),
    )

    assert plan.daily_commands == ()
    assert [(block.action_id, block.reason_code) for block in plan.blocked_actions] == [
        ("append-daily", "missing_admission_ticket")
    ]


def test_admitted_ticket_is_carried_by_the_planned_daily_mutation():
    report_id = uuid5(NAMESPACE_URL, "admitted-ticket-report")
    actor_id = uuid5(NAMESPACE_URL, "admitted-ticket-actor")
    text = "日报记：完成合同审核"
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["daily_append"],
            "segments": [
                {
                    "segment_id": "daily-segment",
                    "text": text,
                    "intents": ["daily_append"],
                    "entity_ids": ["daily-event"],
                    "action_ids": ["append-daily"],
                }
            ],
            "entities": [
                {
                    "entity_id": "daily-event",
                    "entity_type": "daily_event",
                    "value": "完成合同审核",
                    "confidence": 0.99,
                    "attributes": {"field": "today_work"},
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "append-daily",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-event"],
                }
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "daily_append"},
        }
    )
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id=str(actor_id),
        conversation_id="conversation-ticket-carry",
        message_id="message-ticket-carry",
        text=text,
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        resources={
            "active_tasks": [
                {
                    "workflow": "daily_report",
                    "task_id": "daily-task",
                    "status": "collecting",
                    "reply_candidate": True,
                    "metadata": {"report_date": "2026-07-14"},
                }
            ],
            "daily_draft": {
                "report_id": str(report_id),
                "status": "collecting",
                "version": 1,
            },
            "daily_policy": {"current_report_date": "2026-07-14"},
        },
    )
    decision = asyncio.run(
        CognitiveCoreV3(
            _StaticInterpreter(proposal),
            admission_engine=DomainAdmissionEngine(),
        ).process(
            turn,
            ConversationState.empty(
                user_id=turn.user_id,
                conversation_id=turn.conversation_id,
            ),
        )
    ).decision

    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=actor_id,
            daily_snapshot=DailyReportMutationSnapshot(
                report_id=report_id,
                owner_user_id=actor_id,
                version=1,
                status="collecting",
            ),
        ),
    )

    assert plan.blocked_actions == ()
    assert len(plan.daily_commands) == 1
    assert plan.daily_commands[0].admission_ticket["ticket_id"] == (
        decision.admission_tickets[0].ticket_id
    )
    assert plan.daily_commands[0].admission_required is True

    altered = replace(
        plan.daily_commands[0],
        patch={"field": "today_work", "items": ["已经完成全部案件结案"]},
    )
    execution = execute_typed_daily_command(
        altered,
        snapshot=DailyReportMutationSnapshot(
            report_id=report_id,
            owner_user_id=actor_id,
            version=1,
            status="collecting",
        ),
        actor_user_id=actor_id,
        admission_scope=AdmissionExecutionScope(
            tenant_id=turn.tenant_id,
            user_id=turn.user_id,
            conversation_id=turn.conversation_id,
            source_message_id=turn.message_id,
            executed_at=datetime(2026, 7, 14, 9, 1, tzinfo=timezone.utc),
            conversation_state_version=0,
        ),
    )
    assert execution.validation.status == "blocked"
    assert execution.validation.reason_code == "admission_ticket_claims_mismatch"
    assert execution.after.today_work == ()


def test_executor_fails_closed_if_required_admission_ticket_is_missing():
    actor_id = uuid5(NAMESPACE_URL, "missing-executor-ticket-actor")
    report_id = uuid5(NAMESPACE_URL, "missing-executor-ticket-report")
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=actor_id,
        version=2,
        status="collecting",
    )
    command = TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "missing-executor-ticket-command"),
        decision_id=uuid5(NAMESPACE_URL, "missing-executor-ticket-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, "missing-executor-ticket-subdecision"),
        command_type="append_item",
        report_id=report_id,
        report_version=2,
        target_item_ids=(),
        patch={"field": "today_work", "items": ["完成合同审核"]},
        idempotency_key="missing-executor-ticket-key",
        admission_required=True,
        admission_ticket={},
    )

    execution = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=actor_id,
    )

    assert execution.validation.status == "blocked"
    assert execution.validation.reason_code == "missing_admission_ticket"
    assert execution.should_write_db is False
    assert execution.after == snapshot


def test_executor_rejects_admission_ticket_for_another_user():
    actor_id = uuid5(NAMESPACE_URL, "ticket-scope-actor")
    report_id = uuid5(NAMESPACE_URL, "ticket-scope-report")
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=actor_id,
        version=2,
        status="collecting",
    )
    command = TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "ticket-scope-command"),
        decision_id=uuid5(NAMESPACE_URL, "ticket-scope-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, "ticket-scope-subdecision"),
        command_type="append_item",
        report_id=report_id,
        report_version=2,
        target_item_ids=(),
        patch={"field": "today_work", "items": ["完成合同审核"]},
        idempotency_key="ticket-scope-key",
        admission_required=True,
        admission_ticket={
            "ticket_id": "ticket-other-user",
            "tenant_id": "sandbox-agent2-phase2-20260711",
            "user_id": "other-user",
            "conversation_id": "conversation-ticket-scope",
            "source_message_id": "message-ticket-scope",
            "action_id": "append-daily",
            "segment_id": "daily-segment",
            "domain": "report",
            "operation": "capture_daily_event",
            "object_ref": {
                "object_type": "daily_report",
                "stable_id": str(report_id),
                "version": 2,
            },
            "contract_version": ADMISSION_CONTRACT_VERSION,
            "issued_at": "2026-07-14T09:00:00+00:00",
            "expires_at": "2026-07-14T09:05:00+00:00",
            "idempotency_key": "admission-ticket-scope-key",
        },
    )

    execution = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=actor_id,
    )

    assert execution.validation.status == "blocked"
    assert execution.validation.reason_code == "admission_ticket_scope_mismatch"
    assert execution.after == snapshot


def test_executor_rejects_admission_ticket_from_another_tenant():
    actor_id = uuid5(NAMESPACE_URL, "ticket-tenant-actor")
    report_id = uuid5(NAMESPACE_URL, "ticket-tenant-report")
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=actor_id,
        version=2,
        status="collecting",
    )
    command = TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "ticket-tenant-command"),
        decision_id=uuid5(NAMESPACE_URL, "ticket-tenant-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, "ticket-tenant-subdecision"),
        command_type="append_item",
        report_id=report_id,
        report_version=2,
        target_item_ids=(),
        patch={"field": "today_work", "items": ["完成合同审核"]},
        idempotency_key="ticket-tenant-key",
        admission_required=True,
        admission_ticket={
            "ticket_id": "ticket-other-tenant",
            "tenant_id": "other-tenant",
            "user_id": str(actor_id),
            "conversation_id": "conversation-ticket-tenant",
            "source_message_id": "message-ticket-tenant",
            "action_id": "append-daily",
            "segment_id": "daily-segment",
            "domain": "report",
            "operation": "capture_daily_event",
            "object_ref": {
                "object_type": "daily_report",
                "stable_id": str(report_id),
                "version": 2,
            },
            "contract_version": ADMISSION_CONTRACT_VERSION,
            "issued_at": "2026-07-14T09:00:00+00:00",
            "expires_at": "2026-07-14T09:05:00+00:00",
            "idempotency_key": "admission-ticket-tenant-key",
        },
    )

    execution = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=actor_id,
        admission_scope=AdmissionExecutionScope(
            tenant_id="sandbox-agent2-phase2-20260711",
            user_id=str(actor_id),
            conversation_id="conversation-ticket-tenant",
            source_message_id="message-ticket-tenant",
            executed_at=datetime(2026, 7, 14, 9, 1, tzinfo=timezone.utc),
            conversation_state_version=0,
        ),
    )

    assert execution.validation.status == "blocked"
    assert execution.validation.reason_code == "admission_ticket_scope_mismatch"
    assert execution.after == snapshot


def test_executor_rejects_expired_admission_ticket():
    actor_id, snapshot, command = _required_daily_command()

    execution = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=actor_id,
        admission_scope=AdmissionExecutionScope(
            tenant_id="sandbox-agent2-phase2-20260711",
            user_id=str(actor_id),
            conversation_id="conversation-required-helper",
            source_message_id="message-required-helper",
            executed_at=datetime(2026, 7, 14, 9, 6, tzinfo=timezone.utc),
            conversation_state_version=0,
        ),
    )

    assert execution.validation.status == "blocked"
    assert execution.validation.reason_code == "admission_ticket_expired"
    assert execution.after == snapshot


def test_executor_rejects_ticket_for_a_different_report_version():
    actor_id, snapshot, command = _required_daily_command(
        ticket_overrides={
            "object_ref": {
                "object_type": "daily_report",
                "stable_id": str(uuid5(NAMESPACE_URL, "required-ticket-helper-report")),
                "version": 1,
            }
        }
    )

    execution = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=actor_id,
        admission_scope=AdmissionExecutionScope(
            tenant_id="sandbox-agent2-phase2-20260711",
            user_id=str(actor_id),
            conversation_id="conversation-required-helper",
            source_message_id="message-required-helper",
            executed_at=datetime(2026, 7, 14, 9, 1, tzinfo=timezone.utc),
            conversation_state_version=0,
        ),
    )

    assert execution.validation.status == "blocked"
    assert execution.validation.reason_code == "admission_ticket_object_mismatch"
    assert execution.after == snapshot


def test_executor_rejects_unknown_admission_contract():
    actor_id, snapshot, command = _required_daily_command(
        ticket_overrides={"contract_version": "agent2.domain_admission.unknown"}
    )

    execution = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=actor_id,
        admission_scope=AdmissionExecutionScope(
            tenant_id="sandbox-agent2-phase2-20260711",
            user_id=str(actor_id),
            conversation_id="conversation-required-helper",
            source_message_id="message-required-helper",
            executed_at=datetime(2026, 7, 14, 9, 1, tzinfo=timezone.utc),
            conversation_state_version=0,
        ),
    )

    assert execution.validation.status == "blocked"
    assert execution.validation.reason_code == "unknown_admission_contract"
    assert execution.after == snapshot


def test_executor_rejects_ticket_for_a_different_domain_operation():
    actor_id, snapshot, command = _required_daily_command(
        ticket_overrides={"domain": "case", "operation": "record_case_progress"}
    )

    execution = execute_typed_daily_command(
        command,
        snapshot=snapshot,
        actor_user_id=actor_id,
        admission_scope=AdmissionExecutionScope(
            tenant_id="sandbox-agent2-phase2-20260711",
            user_id=str(actor_id),
            conversation_id="conversation-required-helper",
            source_message_id="message-required-helper",
            executed_at=datetime(2026, 7, 14, 9, 1, tzinfo=timezone.utc),
            conversation_state_version=0,
        ),
    )

    assert execution.validation.status == "blocked"
    assert execution.validation.reason_code == "admission_ticket_operation_mismatch"
    assert execution.after == snapshot


def test_admitted_case_ticket_is_carried_by_business_command():
    actor_id = uuid5(NAMESPACE_URL, "admitted-case-ticket-actor")
    text = "云璟府案今天与法院沟通了执行进展"
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "case-segment",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref"],
                    "action_ids": ["record-case"],
                }
            ],
            "entities": [
                {
                    "entity_id": "case-ref",
                    "entity_type": "case_ref",
                    "value": "云璟府案",
                    "confidence": 0.99,
                    "attributes": {
                        "normalized_fact": "今天与法院沟通了执行进展",
                        "statement_mode": "asserted",
                        "evidence_spans": [[4, len(text)]],
                    },
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref"],
                }
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "case_progress"},
        }
    )
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id=str(actor_id),
        conversation_id="conversation-case-ticket-carry",
        message_id="message-case-ticket-carry",
        text=text,
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        resources={
            "visible_cases": [
                {
                    "case_id": "case-1",
                    "case_name": "云璟府物业服务合同纠纷案",
                    "confirmed_aliases": ["云璟府案"],
                    "version": 4,
                }
            ]
        },
    )
    decision = asyncio.run(
        CognitiveCoreV3(
            _StaticInterpreter(proposal),
            admission_engine=DomainAdmissionEngine(),
        ).process(
            turn,
            ConversationState.empty(
                user_id=turn.user_id,
                conversation_id=turn.conversation_id,
            ),
        )
    ).decision

    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=actor_id,
        ),
    )

    assert plan.blocked_actions == ()
    assert len(plan.business_commands) == 1
    command = plan.business_commands[0]
    assert command.admission_required is True
    assert command.admission_action_id == "record-case"
    assert command.admission_operation == "record_case_progress"
    assert command.admission_ticket["ticket_id"] == decision.admission_tickets[0].ticket_id


def _required_case_candidate(
    *,
    ticket_overrides: dict | None = None,
    case_version: int = 4,
):
    actor_id = uuid5(NAMESPACE_URL, "required-case-ticket-actor")
    case_id = "case-required-ticket"
    text = "云璟府案今天与法院沟通了执行进展"
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "case-segment",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref"],
                    "action_ids": ["record-case"],
                }
            ],
            "entities": [
                {
                    "entity_id": "case-ref",
                    "entity_type": "case_ref",
                    "value": "云璟府案",
                    "confidence": 0.99,
                    "attributes": {
                        "stage": "execution",
                        "normalized_fact": "今天与法院沟通了执行进展",
                        "statement_mode": "asserted",
                        "evidence_spans": [[4, len(text)]],
                    },
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref"],
                }
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "case_progress"},
        }
    )
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id=str(actor_id),
        conversation_id="conversation-required-case",
        message_id="message-required-case",
        text=text,
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        resources={
            "visible_cases": [
                {
                    "case_id": case_id,
                    "case_name": "云璟府物业服务合同纠纷案",
                    "confirmed_aliases": ["云璟府案"],
                    "version": 4,
                }
            ]
        },
    )
    decision = asyncio.run(
        CognitiveCoreV3(
            _StaticInterpreter(proposal),
            admission_engine=DomainAdmissionEngine(),
        ).process(
            turn,
            ConversationState.empty(
                user_id=turn.user_id,
                conversation_id=turn.conversation_id,
            ),
        )
    ).decision
    command = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(message_id=turn.message_id, actor_user_id=actor_id),
    ).business_commands[0]
    if ticket_overrides:
        command = replace(
            command,
            admission_ticket={**command.admission_ticket, **ticket_overrides},
        )
    context = BusinessCommandContext(
        tenant_id=turn.tenant_id,
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id=turn.user_id,
        actor_role_ids=("lawyer",),
        allowed_case_ids=(case_id,),
        source_message_id=turn.message_id,
        source_channel="dingtalk",
        occurred_at=turn.occurred_at,
        conversation_id=turn.conversation_id,
        execution_started_at=datetime(2026, 7, 14, 9, 1, tzinfo=timezone.utc),
        conversation_state_version=0,
    )
    cases = (
        CaseRecord(
            case_id=case_id,
            tenant_id=turn.tenant_id,
            case_number="（2026）苏01执1号",
            case_name="云璟府物业服务合同纠纷案",
            party_names=("云璟府物业",),
            confirmed_aliases=("云璟府案",),
            version=case_version,
        ),
    )
    return command, context, cases


def _required_travel_candidate():
    actor_id = uuid5(NAMESPACE_URL, "required-travel-ticket-actor")
    text = "明天去南京出差"
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["travel_event"],
            "segments": [
                {
                    "segment_id": "travel-segment",
                    "text": text,
                    "intents": ["travel_event"],
                    "entity_ids": ["travel-event"],
                    "action_ids": ["record-travel"],
                }
            ],
            "entities": [
                {
                    "entity_id": "travel-event",
                    "entity_type": "travel_event",
                    "value": text,
                    "confidence": 0.99,
                    "attributes": {
                        "destination": "南京",
                        "date_hint": "明天",
                        "purpose": "出差",
                        "statement_mode": "asserted",
                        "traveler_scope": "self",
                        "evidence_spans": [[0, len(text)]],
                    },
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-travel",
                    "action_type": "record_travel_event",
                    "intent": "travel_event",
                    "entity_ids": ["travel-event"],
                }
            ],
            "clarification_need": None,
            "context_update": {"preserve_current_goal": True},
        }
    )
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id=str(actor_id),
        actor_user_id=str(actor_id),
        conversation_id="conversation-required-travel",
        message_id="message-required-travel",
        text=text,
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        resources={"timezone": "Asia/Shanghai"},
    )
    decision = asyncio.run(
        CognitiveCoreV3(
            _StaticInterpreter(proposal),
            admission_engine=DomainAdmissionEngine(),
        ).process(
            turn,
            ConversationState.empty(
                user_id=turn.user_id,
                conversation_id=turn.conversation_id,
            ),
        )
    ).decision
    candidate = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(message_id=turn.message_id, actor_user_id=actor_id),
    ).business_commands[0]
    context = BusinessCommandContext(
        tenant_id=turn.tenant_id,
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id=turn.user_id,
        actor_role_ids=("lawyer",),
        allowed_case_ids=(),
        source_message_id=turn.message_id,
        source_channel="dingtalk",
        occurred_at=turn.occurred_at,
        conversation_id=turn.conversation_id,
        execution_started_at=datetime(2026, 7, 14, 9, 1, tzinfo=timezone.utc),
        conversation_state_version=0,
    )
    return candidate, context


def test_business_compiler_rejects_cross_tenant_admission_ticket():
    command, context, cases = _required_case_candidate(
        ticket_overrides={"tenant_id": "other-tenant"}
    )

    result = Phase2BusinessCommandCompiler().compile(command, context, cases=cases)

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "admission_ticket_scope_mismatch"


def test_business_compiler_rejects_case_version_changed_after_admission():
    command, context, cases = _required_case_candidate(case_version=5)

    result = Phase2BusinessCommandCompiler().compile(command, context, cases=cases)

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "admission_ticket_object_version_conflict"


def test_business_compiler_rejects_conversation_state_version_change():
    command, context, cases = _required_case_candidate()

    result = Phase2BusinessCommandCompiler().compile(
        command,
        replace(context, conversation_state_version=1),
        cases=cases,
    )

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "admission_ticket_state_version_conflict"


def test_business_compiler_rejects_segment_text_changed_after_admission():
    command, context, cases = _required_case_candidate()
    changed_payload = {
        **command.payload,
        "source_segments": [
            {
                **command.payload["source_segments"][0],
                "text": "云璟府案今天与法院沟通了执行进展，并确认已经结案",
            }
        ],
    }

    result = Phase2BusinessCommandCompiler().compile(
        replace(command, payload=changed_payload),
        context,
        cases=cases,
    )

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "admission_ticket_segment_mismatch"


def test_business_compiler_rejects_case_claims_changed_after_admission():
    command, context, cases = _required_case_candidate()
    changed_entity = {
        **command.payload["entities"][0],
        "attributes": {"stage": "closed"},
    }

    result = Phase2BusinessCommandCompiler().compile(
        replace(
            command,
            payload={**command.payload, "entities": [changed_entity]},
        ),
        context,
        cases=cases,
    )

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "admission_ticket_claims_mismatch"


def test_business_executor_rejects_required_mutation_without_ticket():
    case_id = "case-executor-admission-required"
    case = CaseRecord(
        case_id=case_id,
        tenant_id="sandbox-agent2-phase2-20260711",
        case_number="（2026）苏01执2号",
        case_name="执行端票据校验案",
        party_names=("测试公司",),
        version=1,
    )
    context = BusinessCommandContext(
        tenant_id=case.tenant_id,
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-executor-admission",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(case_id,),
        source_message_id="message-executor-admission",
        source_channel="dingtalk",
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        conversation_id="conversation-executor-admission",
        execution_started_at=datetime(2026, 7, 14, 9, 1, tzinfo=timezone.utc),
        admission_required=True,
        admission_action_id="record-case",
        admission_operation="record_case_progress",
    )
    command = CreateCaseProgress(
        command_id="command-executor-admission",
        case_id=case_id,
        occurred_at=context.occurred_at,
        progress_type="general_update",
        summary="今天联系法院推进了执行",
        details="",
        related_party_ids=(),
        related_document_ids=(),
        related_travel_intent_ids=(),
        confidence=0.99,
    )
    executor = InMemoryBusinessExecutor(cases=(case,))

    receipt = executor.execute(command, context)

    assert receipt.status == "blocked"
    assert receipt.actual_write is False
    assert receipt.error_code == "missing_admission_ticket"
    assert executor.case_progress == {}


def test_business_composer_binds_ticket_to_executor_context():
    command, context, cases = _required_case_candidate()

    class _Cases:
        async def list_visible(self, _context):
            return cases

    class _CapturingExecutor:
        def __init__(self):
            self.context = None
            self.store = InMemoryAdmissionTicketStore((command.admission_ticket,))
            self.delegate = InMemoryBusinessExecutor(
                cases=cases,
                admission_ticket_store=self.store,
            )

        async def execute(self, compiled, execution_context):
            self.context = execution_context
            return self.delegate.execute(compiled, execution_context)

    executor = _CapturingExecutor()
    composer = Phase2BusinessComposer(
        case_repository=_Cases(),  # type: ignore[arg-type]
        executor=executor,
    )

    result = asyncio.run(composer.execute((command,), context))

    assert result.executed_count == 1
    assert executor.context is not None
    assert executor.context.admission_required is True
    assert executor.context.admission_action_id == "record-case"
    assert executor.context.admission_ticket["ticket_id"] == command.admission_ticket["ticket_id"]
    assert executor.store.status(command.admission_ticket["ticket_id"]) == "consumed"


def test_business_executor_rejects_valid_looking_ticket_missing_from_authoritative_store():
    command, context, cases = _required_case_candidate()

    class _Cases:
        async def list_visible(self, _context):
            return cases

    class _Executor:
        def __init__(self):
            self.delegate = InMemoryBusinessExecutor(
                cases=cases,
                admission_ticket_store=InMemoryAdmissionTicketStore(),
            )

        async def execute(self, compiled, execution_context):
            return self.delegate.execute(compiled, execution_context)

    executor = _Executor()
    result = asyncio.run(
        Phase2BusinessComposer(
            case_repository=_Cases(),  # type: ignore[arg-type]
            executor=executor,
        ).execute((command,), context)
    )

    assert result.executed_count == 0
    assert result.blocked_count == 1
    receipt = result.actions[0].receipt
    assert receipt is not None
    assert receipt.error_code == "admission_ticket_not_found"
    assert receipt.actual_write is False
    assert executor.delegate.case_progress == {}


def test_business_executor_rejects_ticket_that_differs_from_authoritative_claims():
    original, context, cases = _required_case_candidate()
    forged = replace(
        original,
        admission_ticket={
            **original.admission_ticket,
            "policy_version": "forged-policy-version",
        },
    )

    class _Cases:
        async def list_visible(self, _context):
            return cases

    class _Executor:
        def __init__(self):
            self.delegate = InMemoryBusinessExecutor(
                cases=cases,
                admission_ticket_store=InMemoryAdmissionTicketStore(
                    (original.admission_ticket,)
                ),
            )

        async def execute(self, compiled, execution_context):
            return self.delegate.execute(compiled, execution_context)

    executor = _Executor()
    result = asyncio.run(
        Phase2BusinessComposer(
            case_repository=_Cases(),  # type: ignore[arg-type]
            executor=executor,
        ).execute((forged,), context)
    )

    assert result.executed_count == 0
    assert result.blocked_count == 1
    receipt = result.actions[0].receipt
    assert receipt is not None
    assert receipt.error_code == "admission_ticket_authority_mismatch"
    assert receipt.actual_write is False
    assert executor.delegate.case_progress == {}


def test_authoritative_ticket_can_be_consumed_by_only_one_concurrent_executor():
    candidate, context, cases = _required_case_candidate()
    execution_context = bind_business_execution_context(candidate, context)
    compilation = Phase2BusinessCommandCompiler().compile(
        candidate,
        execution_context,
        cases=cases,
    )
    assert compilation.command is not None
    store = InMemoryAdmissionTicketStore((candidate.admission_ticket,))
    executors = (
        InMemoryBusinessExecutor(cases=cases, admission_ticket_store=store),
        InMemoryBusinessExecutor(cases=cases, admission_ticket_store=store),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(
            pool.map(
                lambda executor: executor.execute(
                    compilation.command, execution_context
                ),
                executors,
            )
        )

    assert sum(receipt.actual_write for receipt in receipts) == 1
    assert sorted(receipt.status for receipt in receipts) == ["blocked", "executed"]
    assert {
        receipt.error_code for receipt in receipts if receipt.status == "blocked"
    } == {"admission_ticket_inactive"}
    assert store.status(candidate.admission_ticket["ticket_id"]) == "consumed"


def test_compiled_case_command_cannot_strengthen_fact_after_admission():
    candidate, context, cases = _required_case_candidate()
    execution_context = bind_business_execution_context(candidate, context)
    compilation = Phase2BusinessCommandCompiler().compile(
        candidate,
        execution_context,
        cases=cases,
    )
    assert isinstance(compilation.command, CreateCaseProgress)
    tampered = replace(
        compilation.command,
        summary="云璟府案今天与法院沟通，并确认案件已经结案",
    )
    store = InMemoryAdmissionTicketStore((candidate.admission_ticket,))
    executor = InMemoryBusinessExecutor(
        cases=cases,
        admission_ticket_store=store,
    )

    receipt = executor.execute(tampered, execution_context)

    assert receipt.status == "blocked"
    assert receipt.actual_write is False
    assert receipt.error_code == "admission_ticket_claims_mismatch"
    assert executor.case_progress == {}
    assert store.status(candidate.admission_ticket["ticket_id"]) == "issued"


def test_compiled_travel_command_cannot_change_purpose_after_admission():
    candidate, context = _required_travel_candidate()
    execution_context = bind_business_execution_context(candidate, context)
    compilation = Phase2BusinessCommandCompiler().compile(
        candidate,
        execution_context,
        cases=(),
    )
    assert compilation.command is not None
    tampered = replace(compilation.command, purpose_summary="参加未经用户陈述的庭审")
    store = InMemoryAdmissionTicketStore((candidate.admission_ticket,))
    executor = InMemoryBusinessExecutor(admission_ticket_store=store)

    receipt = executor.execute(tampered, execution_context)

    assert receipt.status == "blocked"
    assert receipt.actual_write is False
    assert receipt.error_code == "admission_ticket_claims_mismatch"
    assert executor.travel_intents == ()
    assert store.status(candidate.admission_ticket["ticket_id"]) == "issued"


def test_admitted_travel_write_uses_the_ticket_preallocated_object_id():
    candidate, context = _required_travel_candidate()
    execution_context = bind_business_execution_context(candidate, context)
    compilation = Phase2BusinessCommandCompiler().compile(
        candidate,
        execution_context,
        cases=(),
    )
    assert compilation.command is not None
    store = InMemoryAdmissionTicketStore((candidate.admission_ticket,))
    executor = InMemoryBusinessExecutor(admission_ticket_store=store)

    receipt = executor.execute(compilation.command, execution_context)

    expected_id = candidate.admission_ticket["object_ref"]["stable_id"]
    assert receipt.status == "executed"
    assert receipt.actual_write is True
    assert receipt.resource_id == expected_id
    assert executor.travel_intents[0].travel_intent_id == expected_id
    assert store.status(candidate.admission_ticket["ticket_id"]) == "consumed"
