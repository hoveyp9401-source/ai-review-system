from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseFollowupPending,
    CaseProgress,
    CaseReportProjection,
    ReportProjectionRequest,
)
from app.agent2.daily_report_projection_executor import DailyReportProjectionExecutor
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeReplyComposer,
    OutcomeStateTransition,
)
from app.agent2.report_projection_confirmation import (
    ProjectionConfirmationContext,
    ProjectionConfirmationPendingSnapshot,
    ProjectionConfirmationResolver,
    parse_projection_confirmation_answer,
)
from app.agent2.report_projection_policy import ReportProjectionDecision
from app.models import Agent2ConversationState, User


@dataclass(frozen=True)
class ProjectionConfirmationTurnResult:
    handled: bool
    reply: str
    outcomes: tuple[OperationOutcome, ...]


async def execute_report_projection_confirmation_turn(
    *,
    session_factory,
    envelope,
    business_context,
    settings,
) -> ProjectionConfirmationTurnResult | None:
    if parse_projection_confirmation_answer(str(envelope.raw_text or "")) == "unknown":
        return None
    conversation_id = str(envelope.conversation_id or "").strip() or (
        business_context.conversation_id
        or f"agent2-direct:{business_context.actor_user_id}"
    )
    now = envelope.received_at or business_context.occurred_at
    async with session_factory() as session:
        async with session.begin():
            rows = tuple((await session.scalars(
                select(CaseFollowupPending).where(
                    CaseFollowupPending.pending_type == "confirmation",
                    CaseFollowupPending.tenant_id == business_context.tenant_id,
                    CaseFollowupPending.user_id == business_context.actor_user_id,
                    CaseFollowupPending.conversation_id == conversation_id,
                    CaseFollowupPending.domain == "report",
                    CaseFollowupPending.operation == "confirm_report_projection",
                    CaseFollowupPending.status.in_(("active", "awaiting_input")),
                ).order_by(CaseFollowupPending.created_at).with_for_update()
            )).all())
            state = await session.scalar(select(Agent2ConversationState).where(
                Agent2ConversationState.user_key == (
                    f"{business_context.tenant_id}:{business_context.actor_user_id}"
                ),
                Agent2ConversationState.conversation_id == conversation_id,
            ))
            state_version = state.version if state is not None else 0
            snapshots = tuple(_pending_snapshot(item) for item in rows)
            resolution = ProjectionConfirmationResolver().resolve(
                snapshots, answer=str(envelope.raw_text or ""),
                context=ProjectionConfirmationContext(
                    tenant_id=business_context.tenant_id,
                    user_id=business_context.actor_user_id,
                    conversation_id=conversation_id,
                    conversation_state_version=state_version,
                    now=now, source_message_already_processed=False,
                ),
            )
            if resolution.status == "clarification_required":
                return ProjectionConfirmationTurnResult(
                    True,
                    "现在有多条日报投影在等你确认。请说明案件或具体内容，我不会替你猜。",
                    (),
                )
            if resolution.status in {"expired", "conflicted"}:
                if resolution.pending is not None:
                    row = next(item for item in rows if str(item.pending_id) == resolution.pending.pending_id)
                    row.status = resolution.status
                    row.version += 1
                    row.updated_at = now
                return ProjectionConfirmationTurnResult(
                    True,
                    "这次确认上下文已经失效，我没有写入日报。请重新说明要加入的案件工作。",
                    (),
                )
            if resolution.status != "bound" or resolution.pending is None:
                return None
            pending = next(
                item for item in rows
                if str(item.pending_id) == resolution.pending.pending_id
            )
            request_id = str((pending.candidate_refs_json or [""])[0])
            request = await session.scalar(
                select(ReportProjectionRequest)
                .where(
                    ReportProjectionRequest.request_id == UUID(request_id),
                    ReportProjectionRequest.tenant_id == business_context.tenant_id,
                    ReportProjectionRequest.user_id == business_context.actor_user_id,
                    ReportProjectionRequest.status == "pending",
                )
                .with_for_update()
            )
            if request is None:
                pending.status = "conflicted"
                pending.version += 1
                pending.updated_at = now
                return ProjectionConfirmationTurnResult(
                    True, "待确认的日报投影已经变化，本次没有写入。", ()
                )
            if resolution.action == "decline":
                outcome = await _decline_projection(
                    session, pending=pending, request=request,
                    source_message_id=str(envelope.message_id or ""), now=now,
                )
                return ProjectionConfirmationTurnResult(
                    True, OutcomeReplyComposer().compose((outcome,)), (outcome,)
                )
            validation_reason = await _validate_projection_scope(
                session, pending=pending, request=request,
                tenant_id=business_context.tenant_id,
                user_id=business_context.actor_user_id,
                allowed_case_ids=set(business_context.allowed_case_ids),
            )
            if validation_reason:
                pending.status = (
                    "permission_revoked"
                    if validation_reason == "permission_revoked" else "conflicted"
                )
                pending.version += 1
                pending.updated_at = now
                return ProjectionConfirmationTurnResult(
                    True, "案件或权限已经变化，本次没有写入日报。", ()
                )
            decision = _decision_from_request(request)
            user = await session.get(User, UUID(business_context.actor_user_id))
            if user is None or not user.active:
                pending.status = "permission_revoked"
                pending.version += 1
                pending.updated_at = now
                return ProjectionConfirmationTurnResult(
                    True, "当前用户权限不可用，本次没有写入日报。", ()
                )
            report_outcome = await DailyReportProjectionExecutor(
                session=session, user=user, settings=settings,
                tenant_id=business_context.tenant_id,
            ).execute_report_projection(decision, request_id)
            committed = (
                report_outcome.business_status in {"succeeded", "duplicate"}
                and bool(report_outcome.receipt_refs)
                and (
                    report_outcome.actual_write
                    or report_outcome.business_status == "duplicate"
                )
            )
            if not committed:
                request.attempt_count += 1
                request.last_error = report_outcome.blocking_reason[:2000]
                request.updated_at = now
                return ProjectionConfirmationTurnResult(
                    True, OutcomeReplyComposer().compose((report_outcome,)),
                    (report_outcome,),
                )
            await _settle_confirmed_projection(
                session, pending=pending, request=request,
                report_outcome=report_outcome, now=now,
            )
            return ProjectionConfirmationTurnResult(
                True, OutcomeReplyComposer().compose((report_outcome,)),
                (report_outcome,),
            )


def _pending_snapshot(row: CaseFollowupPending) -> ProjectionConfirmationPendingSnapshot:
    request_id = str((row.candidate_refs_json or [""])[0])
    progress_id = next(iter((row.candidate_versions_json or {}).keys()), "")
    return ProjectionConfirmationPendingSnapshot(
        pending_id=str(row.pending_id), tenant_id=row.tenant_id, user_id=row.user_id,
        conversation_id=row.conversation_id, request_id=request_id,
        case_id=str(row.case_id), case_progress_id=progress_id,
        expected_state_version=row.expected_state_version,
        expires_at=row.expires_at, status=row.status, version=row.version,
    )


async def _validate_projection_scope(
    session, *, pending, request, tenant_id, user_id, allowed_case_ids
) -> str:
    if str(request.case_id) not in allowed_case_ids:
        return "permission_revoked"
    binding = await session.scalar(select(Agent2IdentityBinding).where(
        Agent2IdentityBinding.tenant_id == tenant_id,
        Agent2IdentityBinding.user_id == user_id,
        Agent2IdentityBinding.active.is_(True),
    ))
    permitted = set(
        str(value) for value in ((binding.permission_scope_json if binding else {}) or {}).get(
            "allowed_case_ids", []
        )
    )
    if binding is None or str(request.case_id) not in permitted:
        return "permission_revoked"
    case = await session.scalar(select(Agent2Case).where(
        Agent2Case.tenant_id == tenant_id, Agent2Case.case_id == request.case_id,
        Agent2Case.owner_user_id == user_id,
    ))
    progress = await session.scalar(select(CaseProgress).where(
        CaseProgress.tenant_id == tenant_id,
        CaseProgress.progress_id == request.case_progress_id,
        CaseProgress.case_id == request.case_id,
        CaseProgress.deleted_at.is_(None),
    ))
    expected_version = int(
        (pending.candidate_versions_json or {}).get(str(request.case_progress_id), -1)
    )
    if case is None or progress is None or progress.version != expected_version:
        return "candidate_changed"
    return ""


def _decision_from_request(request: ReportProjectionRequest) -> ReportProjectionDecision:
    value = dict(request.decision_json or {})
    return ReportProjectionDecision(
        decision_id=str(value["decision_id"]), eligible=True,
        projection_mode="confirmed", report_type="daily",
        section=str(value.get("section") or (
            "today_work" if value.get("reason_code") == "completed_work_today"
            else "tomorrow_plan"
        )),
        report_date=date.fromisoformat(str(value["report_date"])),
        normalized_fact=str(value["normalized_fact"]),
        source_case_id=str(request.case_id),
        source_progress_id=str(request.case_progress_id),
        source_followup_id=str(request.followup_id or ""),
        source_turn_id=request.source_turn_id,
        reason_code=str(value.get("reason_code") or "confirmed_by_user"),
        confidence=float(value.get("confidence") or 0), duplicate_of="",
        policy_version=str(value.get("policy_version") or ""),
    )


async def _decline_projection(session, *, pending, request, source_message_id, now):
    key = f"report-projection-decline:{pending.tenant_id}:{pending.pending_id}"
    receipt_id = uuid5(NAMESPACE_URL, f"{key}:receipt")
    audit_id = uuid5(NAMESPACE_URL, f"{key}:audit")
    pending.status = "consumed"
    pending.consumed_at = now
    pending.version += 1
    pending.updated_at = now
    request.status = "cancelled"
    request.completed_at = now
    request.updated_at = now
    after = {"pending_id": str(pending.pending_id), "request_id": str(request.request_id), "status": "cancelled"}
    await session.execute(insert(BusinessCommandReceipt).values(
        receipt_id=receipt_id, tenant_id=pending.tenant_id,
        command_id=f"decline-projection:{pending.pending_id}",
        command_type="decline_report_projection", actor_user_id=pending.user_id,
        source_message_id=source_message_id, idempotency_key=f"{key}:receipt",
        status="executed", resource_type="report_projection_request",
        resource_id=str(request.request_id), before_json={"status": "pending"},
        after_json=after, error_code="", failed_stage="", actual_write=True,
        created_at=now, updated_at=now,
    ).on_conflict_do_nothing(index_elements=[
        BusinessCommandReceipt.tenant_id, BusinessCommandReceipt.idempotency_key,
    ]))
    await session.execute(insert(BusinessAuditEvent).values(
        audit_id=audit_id, tenant_id=pending.tenant_id, receipt_id=receipt_id,
        actor_user_id=pending.user_id, source_message_id=source_message_id,
        source_channel="dingtalk", command_type="decline_report_projection",
        resource_type="report_projection_request", resource_id=str(request.request_id),
        before_json={"status": "pending"}, after_json=after, created_at=now,
    ).on_conflict_do_nothing(index_elements=[BusinessAuditEvent.audit_id]))
    return OperationOutcome(
        domain="report", operation="decline_projection",
        object_ref=OutcomeObjectRef(
            "report_projection", str(request.request_id), "案件日报投影"
        ), business_status="cancelled", message_status="not_requested",
        changed_fields=("status",),
        user_visible_snapshot={"report_type": "daily", "case_only": True},
        blocking_reason="", receipt_refs=(OutcomeReceiptRef(
            str(receipt_id), "database", "executed", True
        ),), state_transition=OutcomeStateTransition("awaiting_input", "cancelled"),
        actual_write=True, source_turn_id=source_message_id,
        tenant_id=pending.tenant_id, user_id=pending.user_id,
        conversation_id=pending.conversation_id,
    )


async def _settle_confirmed_projection(session, *, pending, request, report_outcome, now):
    report_item_id = str(report_outcome.user_visible_snapshot.get("report_item_id") or "")
    if not report_item_id:
        raise ValueError("confirmed projection requires report item id")
    key = f"{request.idempotency_key}:relation"
    await session.execute(insert(CaseReportProjection).values(
        projection_id=uuid5(NAMESPACE_URL, key), tenant_id=request.tenant_id,
        user_id=request.user_id, case_id=request.case_id,
        case_progress_id=request.case_progress_id,
        report_id=UUID(report_outcome.object_ref.stable_id),
        report_item_id=report_item_id, report_type="daily",
        projection_type=str((request.decision_json or {}).get("section") or "today_work"),
        source_turn_id=request.source_turn_id,
        source_followup_id=request.followup_id,
        source_message_id=request.source_message_id,
        status="active", version=1, idempotency_key=key,
        created_at=now, updated_at=now,
    ).on_conflict_do_nothing(index_elements=[
        CaseReportProjection.tenant_id, CaseReportProjection.idempotency_key,
    ]))
    correction_key = f"report-projection-correction:{request.tenant_id}:{uuid5(NAMESPACE_URL, key)}"
    await session.execute(insert(CaseFollowupPending).values(
        pending_id=uuid5(NAMESPACE_URL, correction_key), pending_type="information",
        tenant_id=request.tenant_id, user_id=request.user_id,
        conversation_id=pending.conversation_id, domain="report",
        operation="correct_report_projection", source_turn_id=request.source_turn_id,
        source_message_id=request.source_message_id, task_id=request.followup_id,
        case_id=request.case_id, followup_id=request.followup_id,
        candidate_refs_json=[str(uuid5(NAMESPACE_URL, key))],
        candidate_versions_json={str(uuid5(NAMESPACE_URL, key)): 1},
        candidate_labels_json={str(uuid5(NAMESPACE_URL, key)): str(
            (request.decision_json or {}).get("normalized_fact") or ""
        )},
        acceptable_answer_forms_json={
            "remove": ["这条别放日报", "只记案件"],
            "replace": ["日报里换个说法"],
            "move": ["这不是今天做的", "改成明天计划"],
        }, expected_state_version=pending.expected_state_version,
        expires_at=now + timedelta(hours=24),
        status="awaiting_input", idempotency_key=correction_key,
        version=1, created_at=now, updated_at=now,
    ).on_conflict_do_nothing(index_elements=[
        CaseFollowupPending.tenant_id, CaseFollowupPending.idempotency_key,
    ]))
    request.status = "succeeded"
    request.attempt_count += 1
    request.completed_at = now
    request.updated_at = now
    pending.status = "consumed"
    pending.consumed_at = now
    pending.version += 1
    pending.updated_at = now
