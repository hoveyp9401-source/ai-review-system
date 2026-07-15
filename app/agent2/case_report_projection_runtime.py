from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select

from app.agent2.business.models import CaseFollowupTask, CaseReportProjection
from app.agent2.case_fact_validation import validate_case_fact_extraction
from app.agent2.daily_report_projection_executor import DailyReportProjectionExecutor
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeStateTransition,
)
from app.agent2.outcome_adapters import business_composition_outcomes
from app.agent2.report_projection_policy import (
    CaseFactExtraction,
    ReportProjectionContext,
    ReportProjectionPolicy,
)
from app.agent2.report_projection_sql_store import SqlReportProjectionStore
from app.models import User
from app.repositories import get_report


async def project_committed_case_followup_facts(
    *,
    business_result,
    business_context,
    source_session,
    session_factory,
    settings,
    report_date: date,
) -> tuple[OperationOutcome, ...]:
    """Project only receipt-backed lifecycle replies through the Report Domain."""
    if not bool(
        getattr(settings, "case_followup_report_projection_enabled", False)
    ):
        return ()
    tenant_allowlist = _csv(
        getattr(settings, "case_followup_tenant_allowlist", "")
    )
    user_allowlist = _csv(
        getattr(settings, "case_followup_user_allowlist", "")
    )
    if (
        business_context.tenant_id not in tenant_allowlist
        or business_context.actor_user_id not in user_allowlist
    ):
        return ()
    business_outcomes = business_composition_outcomes(business_result)
    projectable = tuple(
        (action, case_outcome)
        for action, case_outcome in zip(business_result.actions, business_outcomes)
        if action.compiled_command_type == "create_case_progress"
        and action.receipt is not None
        and action.receipt.status in {"executed", "duplicate"}
        and str((action.receipt.after or {}).get("content_origin") or "")
        == "robot_followup"
        and bool((action.outcome_context or {}).get("case_fact_extraction"))
    )
    if not projectable:
        return ()

    # Projection uses its own durable request/report transactions. The source
    # case receipt and effect must therefore be committed first; otherwise an
    # outer rollback could leave a report write for a case write that never
    # committed. Retried source messages remain safe through both receipts'
    # stable idempotency keys.
    await source_session.commit()
    projected: list[OperationOutcome] = []
    for action, case_outcome in projectable:
        store = None
        request_id = ""
        receipt = action.receipt
        extraction = dict(
            (action.outcome_context or {}).get("case_fact_extraction") or {}
        )
        if receipt is None or not extraction:  # guarded by ``projectable``
            continue
        try:
            fact = _fact_from_context(
                extraction,
                case_id=str((receipt.after or {}).get("case_id") or ""),
                user_id=business_context.actor_user_id,
            )
            validation = validate_case_fact_extraction(fact)
            if not validation.valid:
                projected.append(OperationOutcome(
                    domain="report", operation="create",
                    object_ref=OutcomeObjectRef(
                        "daily_report", "", f"{report_date.isoformat()} 日报"
                    ),
                    business_status="blocked", message_status="not_applicable",
                    changed_fields=(), user_visible_snapshot={
                        "report_type": "daily", "period_label": report_date.isoformat()
                    },
                    blocking_reason=",".join(validation.reason_codes),
                    receipt_refs=(),
                    state_transition=OutcomeStateTransition("planned", "blocked"),
                    actual_write=False,
                    source_turn_id=business_context.source_message_id,
                    tenant_id=business_context.tenant_id,
                    user_id=business_context.actor_user_id,
                    conversation_id=business_context.conversation_id,
                    metadata={"raw_text_hash": validation.raw_text_hash},
                ))
                continue
            async with session_factory() as read_session:
                user = await read_session.get(User, UUID(business_context.actor_user_id))
                if user is None or not user.active:
                    continue
                report = await get_report(read_session, user.id, report_date)
                duplicate = await read_session.scalar(
                    select(CaseReportProjection.projection_id).where(
                        CaseReportProjection.tenant_id == business_context.tenant_id,
                        CaseReportProjection.user_id == business_context.actor_user_id,
                        CaseReportProjection.case_progress_id == UUID(
                            case_outcome.object_ref.stable_id
                        ),
                        CaseReportProjection.status == "active",
                    )
                )
                task = await read_session.scalar(
                    select(CaseFollowupTask).where(
                        CaseFollowupTask.tenant_id == business_context.tenant_id,
                        CaseFollowupTask.source_progress_id == UUID(
                            case_outcome.object_ref.stable_id
                        ),
                    )
                )
                writable = report is not None and report.status in {
                    "collecting", "pending_confirmation"
                }
                decision = ReportProjectionPolicy().decide(
                    fact,
                    ReportProjectionContext(
                        tenant_id=business_context.tenant_id,
                        user_id=business_context.actor_user_id,
                        source_turn_id=business_context.source_message_id,
                        report_date=report_date,
                        report_exists=report is not None,
                        report_writable=writable,
                        duplicate_projection_id=str(duplicate or ""),
                        automatic_projection_enabled=bool(
                            getattr(
                                settings,
                                "case_followup_report_projection_enabled",
                                False,
                            )
                        ),
                        high_confidence_threshold=0.9,
                    ),
                    source_progress_id=case_outcome.object_ref.stable_id,
                    source_followup_id=str(task.followup_id) if task is not None else "",
                )
            if decision.projection_mode == "confirmation_required":
                store = SqlReportProjectionStore(session_factory)
                request_id, confirmation_outcome = (
                    await store.create_confirmation_request(decision, case_outcome)
                )
                projected.append(confirmation_outcome)
                continue
            if not decision.eligible:
                continue
            store = SqlReportProjectionStore(session_factory)
            request_id = await store.create_request(decision, case_outcome)
            async with session_factory() as report_session:
                async with report_session.begin():
                    report_user = await report_session.get(
                        User, UUID(business_context.actor_user_id)
                    )
                    if report_user is None or not report_user.active:
                        continue
                    executor = DailyReportProjectionExecutor(
                        session=report_session,
                        user=report_user,
                        settings=settings,
                        tenant_id=business_context.tenant_id,
                    )
                    report_outcome = await executor.execute_report_projection(
                        decision, request_id
                    )
            if (
                report_outcome.business_status == "succeeded"
                and report_outcome.actual_write
            ) or (
                report_outcome.business_status == "duplicate"
                and report_outcome.receipt_refs
            ):
                await store.mark_succeeded(request_id, report_outcome)
            else:
                await store.mark_failed(request_id, report_outcome)
            projected.append(report_outcome)
        except Exception as exc:
            failure = OperationOutcome(
                    domain="report", operation="create",
                    object_ref=OutcomeObjectRef(
                        "daily_report", "", f"{report_date.isoformat()} 日报"
                    ),
                    business_status="failed", message_status="not_applicable",
                    changed_fields=(), user_visible_snapshot={
                        "report_type": "daily", "period_label": report_date.isoformat()
                    },
                    blocking_reason=f"report_projection_failed:{type(exc).__name__}",
                    receipt_refs=(),
                    state_transition=OutcomeStateTransition("planned", "failed"),
                    actual_write=False,
                    source_turn_id=business_context.source_message_id,
                    tenant_id=business_context.tenant_id,
                    user_id=business_context.actor_user_id,
                )
            if store is not None and request_id:
                try:
                    await store.mark_failed(request_id, failure)
                except Exception:
                    pass
            projected.append(failure)
    return tuple(projected)


def _csv(raw: object) -> frozenset[str]:
    return frozenset(
        value.strip()
        for value in str(raw or "").replace(";", ",").split(",")
        if value.strip()
    )


def _fact_from_context(
    payload: dict,
    *,
    case_id: str,
    user_id: str,
) -> CaseFactExtraction:
    return CaseFactExtraction(
        case_id=case_id,
        actor_user_id=user_id,
        raw_text=str(payload.get("raw_text") or ""),
        normalized_fact=str(payload.get("normalized_fact") or ""),
        factual_progress=tuple(map(str, payload.get("factual_progress") or ())),
        completed_actions=tuple(map(str, payload.get("completed_actions") or ())),
        next_actions=tuple(map(str, payload.get("next_actions") or ())),
        action_time_scope=str(payload.get("action_time_scope") or "unknown"),
        report_preference=str(payload.get("report_preference") or "automatic"),
        confidence=float(payload.get("confidence") or 0),
        evidence_spans=tuple(
            (int(item[0]), int(item[1]))
            for item in (payload.get("evidence_spans") or ())
            if isinstance(item, (list, tuple)) and len(item) == 2
        ),
        current_status=str(payload.get("current_status") or ""),
        hearing_readiness=str(payload.get("hearing_readiness") or ""),
        blocking_issues=tuple(map(str, payload.get("blocking_issues") or ())),
        requested_snooze=str(payload.get("requested_snooze") or ""),
    )
