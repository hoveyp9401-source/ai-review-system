from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.agent2.business.models import (
    Agent2Case,
    Agent2OperationOutcome,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseFollowupPending,
    CaseFollowupTask,
    CaseProgress,
    CaseReportProjection,
    ReportProjectionRequest,
)
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)
from app.agent2.report_projection_policy import ReportProjectionDecision
from app.models import Agent2ConversationState


def _projection_request_values(
    decision: ReportProjectionDecision,
    case_outcome: OperationOutcome,
    *,
    now: datetime,
) -> tuple[dict[str, Any], str]:
    case_receipt = next((
        item for item in case_outcome.receipt_refs
        if item.receipt_type == "database"
        and item.status == "executed" and item.actual_write
    ), None)
    if case_receipt is None:
        raise ValueError("projection request requires committed case receipt")
    tenant_id, user_id = case_outcome.tenant_id, case_outcome.user_id
    if not tenant_id or not user_id:
        raise ValueError("projection request requires tenant and user identity")
    idempotency_key = (
        f"report-projection:{tenant_id}:{user_id}:{decision.source_case_id}:"
        f"{decision.source_turn_id}:{decision.section}"
    )
    return ({
        "request_id": uuid5(
            NAMESPACE_URL, f"report-projection-request:{decision.decision_id}"
        ),
        "tenant_id": tenant_id, "user_id": user_id,
        "case_id": UUID(decision.source_case_id),
        "case_progress_id": UUID(decision.source_progress_id),
        "followup_id": (
            UUID(decision.source_followup_id) if decision.source_followup_id else None
        ),
        "source_turn_id": decision.source_turn_id,
        "source_message_id": str(
            case_outcome.metadata.get("source_message_id") or decision.source_turn_id
        ),
        "case_receipt_id": UUID(case_receipt.receipt_id),
        "decision_json": {
            "decision_id": decision.decision_id, "eligible": decision.eligible,
            "projection_mode": decision.projection_mode,
            "report_type": decision.report_type, "section": decision.section,
            "report_date": decision.report_date.isoformat(),
            "normalized_fact": decision.normalized_fact,
            "source_case_id": decision.source_case_id,
            "source_progress_id": decision.source_progress_id,
            "source_followup_id": decision.source_followup_id,
            "source_turn_id": decision.source_turn_id,
            "reason_code": decision.reason_code, "confidence": decision.confidence,
            "duplicate_of": decision.duplicate_of,
            "policy_version": decision.policy_version,
        },
        "status": "pending", "idempotency_key": idempotency_key,
        "attempt_count": 0, "last_error": "", "created_at": now,
        "updated_at": now,
    }, idempotency_key)


class SqlReportProjectionStore:
    def __init__(self, session_factory: Any):
        self._session_factory = session_factory

    async def create_request(
        self,
        decision: ReportProjectionDecision,
        case_outcome: OperationOutcome,
    ) -> str:
        now = datetime.now(timezone.utc)
        values, idempotency_key = _projection_request_values(
            decision, case_outcome, now=now
        )
        tenant_id = case_outcome.tenant_id
        request_id = values["request_id"]
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    insert(ReportProjectionRequest)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=[
                            ReportProjectionRequest.tenant_id,
                            ReportProjectionRequest.idempotency_key,
                        ]
                    )
                )
                stored = await session.scalar(
                    select(ReportProjectionRequest).where(
                        ReportProjectionRequest.tenant_id == tenant_id,
                        ReportProjectionRequest.idempotency_key == idempotency_key,
                    )
                )
                if stored is None:
                    raise RuntimeError("projection request cannot be loaded")
                request_id = stored.request_id
        return str(request_id)

    async def create_confirmation_request(
        self,
        decision: ReportProjectionDecision,
        case_outcome: OperationOutcome,
    ) -> tuple[str, OperationOutcome]:
        if decision.projection_mode != "confirmation_required":
            raise ValueError("confirmation request requires confirmation_required decision")
        now = datetime.now(timezone.utc)
        request_values, request_key = _projection_request_values(
            decision, case_outcome, now=now
        )
        request_id = str(request_values["request_id"])
        tenant_id = case_outcome.tenant_id
        user_id = case_outcome.user_id
        if not decision.source_followup_id:
            raise ValueError("projection confirmation requires lifecycle follow-up")
        followup_id = UUID(decision.source_followup_id)
        progress_id = UUID(decision.source_progress_id)
        case_id = UUID(decision.source_case_id)
        pending_key = f"report-projection-confirmation:{tenant_id}:{decision.decision_id}"
        pending_id = uuid5(NAMESPACE_URL, pending_key)
        receipt_id = uuid5(NAMESPACE_URL, f"{pending_key}:receipt")
        audit_id = uuid5(NAMESPACE_URL, f"{pending_key}:audit")
        outcome_id = uuid5(NAMESPACE_URL, f"{pending_key}:outcome")
        case_name = "案件"
        conversation_id = ""
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(insert(ReportProjectionRequest).values(
                    **request_values
                ).on_conflict_do_nothing(index_elements=[
                    ReportProjectionRequest.tenant_id,
                    ReportProjectionRequest.idempotency_key,
                ]))
                request = await session.scalar(select(ReportProjectionRequest).where(
                    ReportProjectionRequest.tenant_id == tenant_id,
                    ReportProjectionRequest.idempotency_key == request_key,
                ).with_for_update())
                task = await session.scalar(select(CaseFollowupTask).where(
                    CaseFollowupTask.tenant_id == tenant_id,
                    CaseFollowupTask.followup_id == followup_id,
                    CaseFollowupTask.assigned_user_id == user_id,
                    CaseFollowupTask.task_status == "answered",
                ))
                progress = await session.scalar(select(CaseProgress).where(
                    CaseProgress.tenant_id == tenant_id,
                    CaseProgress.progress_id == progress_id,
                    CaseProgress.reporter_id == user_id,
                    CaseProgress.deleted_at.is_(None),
                ))
                case = await session.scalar(select(Agent2Case).where(
                    Agent2Case.tenant_id == tenant_id,
                    Agent2Case.case_id == case_id,
                    Agent2Case.owner_user_id == user_id,
                ))
                if request is None or task is None or progress is None or case is None:
                    raise ValueError("projection confirmation scope is no longer valid")
                conversation_id = task.conversation_id
                case_name = case.case_name
                if not conversation_id:
                    raise ValueError("projection confirmation requires conversation scope")
                state = await session.scalar(select(Agent2ConversationState).where(
                    Agent2ConversationState.user_key == f"{tenant_id}:{user_id}",
                    Agent2ConversationState.conversation_id == conversation_id,
                ))
                expected_state_version = state.version if state is not None else 0
                await session.execute(insert(CaseFollowupPending).values(
                    pending_id=pending_id, pending_type="confirmation",
                    tenant_id=tenant_id, user_id=user_id,
                    conversation_id=conversation_id, domain="report",
                    operation="confirm_report_projection",
                    source_turn_id=decision.source_turn_id,
                    source_message_id=request.source_message_id,
                    task_id=followup_id, case_id=case_id, followup_id=followup_id,
                    candidate_refs_json=[request_id],
                    candidate_versions_json={str(progress_id): progress.version},
                    candidate_labels_json={request_id: decision.normalized_fact},
                    acceptable_answer_forms_json={
                        "confirm": ["需要", "确认", "加入日报"],
                        "decline": ["不需要", "只记案件", "这条别放日报"],
                    },
                    expected_state_version=expected_state_version,
                    expires_at=now + timedelta(minutes=15), status="awaiting_input",
                    idempotency_key=pending_key, version=1,
                    created_at=now, updated_at=now,
                ).on_conflict_do_nothing(index_elements=[
                    CaseFollowupPending.tenant_id,
                    CaseFollowupPending.idempotency_key,
                ]))
                receipt_after = {
                    "pending_id": str(pending_id), "request_id": request_id,
                    "case_id": str(case_id), "case_progress_id": str(progress_id),
                    "status": "awaiting_input", "actual_write": True,
                }
                await session.execute(insert(BusinessCommandReceipt).values(
                    receipt_id=receipt_id, tenant_id=tenant_id,
                    command_id=f"confirm-projection:{decision.decision_id}",
                    command_type="create_report_projection_confirmation",
                    actor_user_id=user_id, source_message_id=request.source_message_id,
                    idempotency_key=f"{pending_key}:receipt", status="executed",
                    resource_type="report_projection_confirmation",
                    resource_id=str(pending_id), before_json={}, after_json=receipt_after,
                    error_code="", failed_stage="", actual_write=True,
                    created_at=now, updated_at=now,
                ).on_conflict_do_nothing(index_elements=[
                    BusinessCommandReceipt.tenant_id,
                    BusinessCommandReceipt.idempotency_key,
                ]))
                await session.execute(insert(BusinessAuditEvent).values(
                    audit_id=audit_id, tenant_id=tenant_id, receipt_id=receipt_id,
                    actor_user_id=user_id, source_message_id=request.source_message_id,
                    source_channel="case_followup_report_projection",
                    command_type="create_report_projection_confirmation",
                    resource_type="report_projection_confirmation",
                    resource_id=str(pending_id), before_json={}, after_json=receipt_after,
                    created_at=now,
                ).on_conflict_do_nothing(index_elements=[BusinessAuditEvent.audit_id]))
                snapshot = {
                    "report_type": "daily", "case_name": case_name,
                    "content": decision.normalized_fact,
                }
                await session.execute(insert(Agent2OperationOutcome).values(
                    outcome_id=outcome_id, tenant_id=tenant_id, user_id=user_id,
                    conversation_id=conversation_id,
                    source_turn_id=decision.source_turn_id, domain="report",
                    operation="confirm_projection", object_type="report_projection",
                    object_id=str(pending_id), business_status="waiting_for_reply",
                    message_status="not_requested", actual_write=True, would_write=True,
                    changed_fields_json=["pending_status"],
                    user_visible_snapshot_json=snapshot, blocking_reason="",
                    receipt_refs_json=[{
                        "receipt_id": str(receipt_id), "receipt_type": "database",
                        "status": "executed", "actual_write": True,
                    }], audit_refs_json=[str(audit_id)],
                    state_transition_json={"from": "absent", "to": "awaiting_input"},
                    idempotency_key=f"{pending_key}:outcome",
                    created_at=now, updated_at=now,
                ).on_conflict_do_nothing(index_elements=[
                    Agent2OperationOutcome.tenant_id,
                    Agent2OperationOutcome.idempotency_key,
                ]))
        outcome = OperationOutcome(
            domain="report", operation="confirm_projection",
            object_ref=OutcomeObjectRef(
                "report_projection", str(pending_id), f"{case_name}的日报投影"
            ), business_status="waiting_for_reply", message_status="not_requested",
            changed_fields=("pending_status",),
            user_visible_snapshot={
                "report_type": "daily", "case_name": case_name,
                "content": decision.normalized_fact,
            }, blocking_reason="",
            receipt_refs=(OutcomeReceiptRef(
                str(receipt_id), "database", "executed", True
            ),), state_transition=OutcomeStateTransition("absent", "awaiting_input"),
            actual_write=True, would_write=True,
            source_turn_id=decision.source_turn_id, tenant_id=tenant_id,
            user_id=user_id, conversation_id=conversation_id,
            outcome_id=str(outcome_id), audit_refs=(str(audit_id),),
            idempotency_key=f"{pending_key}:outcome", created_at=now,
        )
        return request_id, outcome

    async def mark_succeeded(
        self, request_id: str, report_outcome: OperationOutcome
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            async with session.begin():
                request = await session.scalar(
                    select(ReportProjectionRequest)
                    .where(ReportProjectionRequest.request_id == UUID(request_id))
                    .with_for_update()
                )
                if request is None:
                    raise RuntimeError("projection request missing")
                if request.status == "succeeded":
                    return
                report_item_id = str(
                    report_outcome.user_visible_snapshot.get("report_item_id") or ""
                )
                if not report_item_id:
                    raise ValueError("successful report projection requires report item id")
                report_id = UUID(report_outcome.object_ref.stable_id)
                decision = dict(request.decision_json or {})
                projection_key = f"{request.idempotency_key}:relation"
                projection_id = uuid5(NAMESPACE_URL, projection_key)
                await session.execute(
                    insert(CaseReportProjection)
                    .values(
                        projection_id=projection_id,
                        tenant_id=request.tenant_id,
                        user_id=request.user_id,
                        case_id=request.case_id,
                        case_progress_id=request.case_progress_id,
                        report_id=report_id,
                        report_item_id=report_item_id,
                        report_type=str(decision.get("report_type") or "daily"),
                        projection_type=str(decision.get("section") or ""),
                        source_turn_id=request.source_turn_id,
                        source_followup_id=request.followup_id,
                        source_message_id=request.source_message_id,
                        status="active",
                        version=1,
                        idempotency_key=projection_key,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            CaseReportProjection.tenant_id,
                            CaseReportProjection.idempotency_key,
                        ]
                    )
                )
                if request.followup_id is not None:
                    task = await session.get(CaseFollowupTask, request.followup_id)
                    if task is not None and task.conversation_id:
                        state = await session.scalar(
                            select(Agent2ConversationState).where(
                                Agent2ConversationState.user_key
                                == f"{request.tenant_id}:{request.user_id}",
                                Agent2ConversationState.conversation_id
                                == task.conversation_id,
                            )
                        )
                        expected_version = state.version if state is not None else 0
                        correction_key = (
                            f"report-projection-correction:{request.tenant_id}:"
                            f"{projection_id}"
                        )
                        await session.execute(
                            insert(CaseFollowupPending)
                            .values(
                                pending_id=uuid5(NAMESPACE_URL, correction_key),
                                pending_type="information",
                                tenant_id=request.tenant_id, user_id=request.user_id,
                                conversation_id=task.conversation_id, domain="report",
                                operation="correct_report_projection",
                                source_turn_id=request.source_turn_id,
                                source_message_id=request.source_message_id,
                                task_id=request.followup_id, case_id=request.case_id,
                                followup_id=request.followup_id,
                                candidate_refs_json=[str(projection_id)],
                                candidate_versions_json={str(projection_id): 1},
                                candidate_labels_json={
                                    str(projection_id): str(
                                        decision.get("normalized_fact") or ""
                                    )
                                },
                                acceptable_answer_forms_json={
                                    "remove": ["这条别放日报", "只记案件"],
                                    "replace": ["日报里换个说法"],
                                    "move": ["这不是今天做的", "改成明天计划"],
                                },
                                expected_state_version=expected_version,
                                expires_at=now + timedelta(hours=24),
                                status="awaiting_input",
                                idempotency_key=correction_key,
                                version=1, created_at=now, updated_at=now,
                            )
                            .on_conflict_do_nothing(index_elements=[
                                CaseFollowupPending.tenant_id,
                                CaseFollowupPending.idempotency_key,
                            ])
                        )
                request.status = "succeeded"
                request.attempt_count += 1
                request.completed_at = now
                request.updated_at = now

    async def mark_failed(
        self, request_id: str, report_outcome: OperationOutcome
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            async with session.begin():
                request = await session.scalar(
                    select(ReportProjectionRequest)
                    .where(ReportProjectionRequest.request_id == UUID(request_id))
                    .with_for_update()
                )
                if request is None:
                    raise RuntimeError("projection request missing")
                request.status = "failed"
                request.attempt_count += 1
                request.last_error = (
                    report_outcome.blocking_reason or report_outcome.business_status
                )[:2000]
                request.completed_at = now
                request.updated_at = now
