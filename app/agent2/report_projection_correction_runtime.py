from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.agent2.business.models import (
    Agent2IdentityBinding,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseFollowupPending,
    CaseReportProjection,
)
from app.agent2.daily_report_projection_correction_executor import (
    DailyReportProjectionCorrectionExecutor,
)
from app.agent2.operation_outcomes import OutcomeReceiptRef, OutcomeReplyComposer
from app.agent2.report_projection_corrections import (
    ProjectionCorrectionCommand,
    ProjectionSnapshot,
    parse_projection_correction,
    plan_projection_correction,
)
from app.models import Agent2ConversationState, User


@dataclass(frozen=True)
class ProjectionCorrectionTurnResult:
    handled: bool
    reply: str
    outcomes: tuple


async def execute_report_projection_correction_turn(
    *, session_factory, envelope, business_context, settings
) -> ProjectionCorrectionTurnResult | None:
    operation, replacement, target_section = parse_projection_correction(
        str(envelope.raw_text or "")
    )
    if not operation:
        return None
    conversation_id = str(envelope.conversation_id or "").strip() or (
        business_context.conversation_id
        or f"agent2-direct:{business_context.actor_user_id}"
    )
    now = envelope.received_at or business_context.occurred_at
    async with session_factory() as session:
        async with session.begin():
            pendings = tuple((await session.scalars(
                select(CaseFollowupPending).where(
                    CaseFollowupPending.pending_type == "information",
                    CaseFollowupPending.tenant_id == business_context.tenant_id,
                    CaseFollowupPending.user_id == business_context.actor_user_id,
                    CaseFollowupPending.conversation_id == conversation_id,
                    CaseFollowupPending.domain == "report",
                    CaseFollowupPending.operation == "correct_report_projection",
                    CaseFollowupPending.status.in_(("active", "awaiting_input")),
                ).order_by(CaseFollowupPending.created_at).with_for_update()
            )).all())
            if len(pendings) != 1:
                return ProjectionCorrectionTurnResult(
                    True,
                    "现在没有唯一可修改的日报投影。请说明案件和具体内容，我不会按最后一条猜。",
                    (),
                )
            pending = pendings[0]
            state = await session.scalar(select(Agent2ConversationState).where(
                Agent2ConversationState.user_key == (
                    f"{business_context.tenant_id}:{business_context.actor_user_id}"
                ),
                Agent2ConversationState.conversation_id == conversation_id,
            ))
            state_version = state.version if state is not None else 0
            if now >= pending.expires_at or state_version != pending.expected_state_version:
                pending.status = "expired" if now >= pending.expires_at else "conflicted"
                pending.version += 1
                pending.updated_at = now
                return ProjectionCorrectionTurnResult(
                    True, "这次日报修改上下文已经失效，我没有改动日报或案件进展。", ()
                )
            projection_id = str((pending.candidate_refs_json or [""])[0])
            projection = await session.scalar(
                select(CaseReportProjection)
                .where(
                    CaseReportProjection.projection_id == UUID(projection_id),
                    CaseReportProjection.tenant_id == business_context.tenant_id,
                    CaseReportProjection.user_id == business_context.actor_user_id,
                )
                .with_for_update()
            )
            binding = await session.scalar(select(Agent2IdentityBinding).where(
                Agent2IdentityBinding.tenant_id == business_context.tenant_id,
                Agent2IdentityBinding.user_id == business_context.actor_user_id,
                Agent2IdentityBinding.active.is_(True),
            ))
            permitted = set(
                str(value)
                for value in ((binding.permission_scope_json if binding else {}) or {}).get(
                    "allowed_case_ids", []
                )
            )
            if projection is None or str(projection.case_id) not in permitted:
                pending.status = "permission_revoked"
                pending.version += 1
                pending.updated_at = now
                return ProjectionCorrectionTurnResult(
                    True, "案件权限已经变化，本次没有改动日报或案件进展。", ()
                )
            expected_version = int(
                (pending.candidate_versions_json or {}).get(projection_id, -1)
            )
            command = ProjectionCorrectionCommand(
                command_id=f"projection-correction:{envelope.message_id}",
                tenant_id=business_context.tenant_id,
                user_id=business_context.actor_user_id,
                projection_id=projection_id, expected_version=expected_version,
                operation=operation, replacement_text=replacement,
                target_section=target_section,
                idempotency_key=(
                    f"projection-correction:{business_context.tenant_id}:"
                    f"{business_context.actor_user_id}:{envelope.message_id}:{projection_id}"
                ),
            )
            plan = plan_projection_correction(command, _snapshot(projection))
            if not plan.allowed:
                pending.status = "conflicted"
                pending.version += 1
                pending.updated_at = now
                return ProjectionCorrectionTurnResult(
                    True, "日报投影版本已经变化，本次没有改动。", ()
                )
            user = await session.get(User, UUID(business_context.actor_user_id))
            if user is None or not user.active:
                pending.status = "permission_revoked"
                pending.version += 1
                pending.updated_at = now
                return ProjectionCorrectionTurnResult(
                    True, "当前用户权限不可用，本次没有改动。", ()
                )
            execution = await DailyReportProjectionCorrectionExecutor(
                session=session, user=user, settings=settings,
                tenant_id=business_context.tenant_id,
            ).execute(
                plan, source_message_id=str(envelope.message_id or ""),
                idempotency_key=command.idempotency_key,
            )
            outcome = execution.outcome
            if outcome.business_status not in {"succeeded", "duplicate"}:
                return ProjectionCorrectionTurnResult(
                    True, OutcomeReplyComposer().compose((outcome,)), (outcome,)
                )
            before = _projection_json(projection)
            if operation == "remove":
                projection.status = "removed"
                projection.removed_at = now
            elif operation == "replace_text":
                pass
            else:
                projection.projection_type = target_section
                projection.report_item_id = execution.report_item_id_after
            projection.version += 1
            projection.updated_at = now
            pending.status = "consumed"
            pending.consumed_at = now
            pending.version += 1
            pending.updated_at = now
            receipt_id, audit_id = await _record_relation_correction(
                session, command=command, projection=projection,
                before=before, now=now,
            )
            outcome = replace(
                outcome,
                receipt_refs=outcome.receipt_refs + (OutcomeReceiptRef(
                    str(receipt_id), "database", "executed", True
                ),),
                audit_refs=outcome.audit_refs + (str(audit_id),),
            )
            reply = OutcomeReplyComposer().compose((outcome,))
            if operation == "remove":
                reply = "日报中的对应条目已经删除，案件进展仍然保留。\n\n" + reply
            return ProjectionCorrectionTurnResult(True, reply, (outcome,))


def _snapshot(item: CaseReportProjection) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        projection_id=str(item.projection_id), tenant_id=item.tenant_id,
        user_id=item.user_id, case_id=str(item.case_id),
        case_progress_id=str(item.case_progress_id), report_id=str(item.report_id),
        report_item_id=item.report_item_id, report_type=item.report_type,
        projection_type=item.projection_type, status=item.status, version=item.version,
    )


def _projection_json(item):
    return {
        "projection_id": str(item.projection_id), "status": item.status,
        "version": item.version, "report_item_id": item.report_item_id,
        "projection_type": item.projection_type,
        "case_progress_id": str(item.case_progress_id),
    }


async def _record_relation_correction(session, *, command, projection, before, now):
    receipt_id = uuid5(NAMESPACE_URL, f"{command.idempotency_key}:receipt")
    audit_id = uuid5(NAMESPACE_URL, f"{command.idempotency_key}:audit")
    after = _projection_json(projection)
    await session.execute(insert(BusinessCommandReceipt).values(
        receipt_id=receipt_id, tenant_id=command.tenant_id,
        command_id=command.command_id, command_type="correct_report_projection",
        actor_user_id=command.user_id,
        source_message_id=command.command_id.removeprefix("projection-correction:"),
        idempotency_key=f"{command.idempotency_key}:receipt", status="executed",
        resource_type="case_report_projection", resource_id=command.projection_id,
        before_json=before, after_json=after, error_code="", failed_stage="",
        actual_write=True, created_at=now, updated_at=now,
    ).on_conflict_do_nothing(index_elements=[
        BusinessCommandReceipt.tenant_id, BusinessCommandReceipt.idempotency_key,
    ]))
    await session.execute(insert(BusinessAuditEvent).values(
        audit_id=audit_id, tenant_id=command.tenant_id, receipt_id=receipt_id,
        actor_user_id=command.user_id,
        source_message_id=command.command_id.removeprefix("projection-correction:"),
        source_channel="dingtalk", command_type="correct_report_projection",
        resource_type="case_report_projection", resource_id=command.projection_id,
        before_json=before, after_json=after, created_at=now,
    ).on_conflict_do_nothing(index_elements=[BusinessAuditEvent.audit_id]))
    return receipt_id, audit_id
