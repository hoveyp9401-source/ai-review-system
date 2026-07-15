from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import case as sql_case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.admission_store_sql import (
    AdmissionReceiptReference,
    SqlAdmissionTicketStore,
)
from app.agent2.admission_contracts import validate_mutation_execution_authority
from app.agent2.business.contracts import (
    BUSINESS_COMMAND_TYPES,
    BusinessCommand,
    BusinessCommandContext,
    BusinessCommandError,
    BusinessReceipt,
    CreateCaseProgress,
    CreateTravelIntent,
    DeleteCaseProgress,
    LinkCaseProgress,
    ListAssignedCases,
    QueryOperationStatus,
    QueryCaseProgress,
    QueryPartyCases,
    RespondTravelCollaboration,
    SnoozeCaseFollowup,
    UpdateCaseProgress,
    UpdateTravelIntent,
    business_command_fingerprint,
)
from app.agent2.business.admission import require_business_execution_admission
from app.agent2.case_followup_commands import (
    TriggerCaseFollowupNow,
    UpdateCaseFollowupPolicy,
    execute_policy_update,
)
from app.agent2.case_lifecycle_followup import (
    CaseFollowupPolicySnapshot,
    CaseFollowupSubject,
    CaseFollowupTaskSnapshot,
    CaseFollowupTrigger,
    FollowupPolicyEngine,
    TriggerPolicyMatrix,
    build_followup_task_plan,
    calculate_next_due_at,
)
from app.agent2.case_followup_invalidation import invalidate_stale_cadence_followups
from app.agent2.case_followup_task_ledger_sql import (
    complete_followup_and_restore_report,
)
from app.agent2.business.case_followups import active_case_progress_followup_resources
from app.agent2.business.models import (
    Agent2Case,
    Agent2OperationOutcome,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseProgress,
    CaseLifecycleState,
    CaseFollowupPolicy,
    CaseFollowupPending,
    CaseFollowupTask,
    Agent2TaskLedgerEntry,
    NotificationOutbox,
    PartyCaseClue,
    PartyCaseRole,
    PartyEntity,
    PartyRelation,
    TravelCollaborationCandidate,
    TravelIntent,
)
from app.agent2.business.policy import BusinessEffectPolicy
from app.models import Agent2ConversationState


logger = logging.getLogger(__name__)


_READ_ONLY_BUSINESS_COMMANDS = (
    QueryCaseProgress,
    ListAssignedCases,
    QueryOperationStatus,
    QueryPartyCases,
)


def _is_mutating_business_command(command: BusinessCommand) -> bool:
    return not isinstance(command, _READ_ONLY_BUSINESS_COMMANDS)


class SqlBusinessExecutor:
    """Typed PostgreSQL executor.

    The caller owns the outer transaction. In request code use it inside
    ``async with session.begin():`` so the domain write, receipt and audit
    commit atomically. A nested savepoint guarantees a failed command cannot
    leave a partial domain write behind while still allowing a failure receipt.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        execution_authority: str,
        effect_policy: BusinessEffectPolicy | None = None,
        admission_ticket_store: Any | None = None,
    ):
        self.session = session
        self.effect_policy = effect_policy or BusinessEffectPolicy()
        self.admission_ticket_store = (
            admission_ticket_store or SqlAdmissionTicketStore(session)
        )
        self.execution_authority = validate_mutation_execution_authority(
            execution_authority
        )

    async def execute(
        self, command: BusinessCommand, context: BusinessCommandContext
    ) -> BusinessReceipt:
        if not isinstance(command, BUSINESS_COMMAND_TYPES):
            raise TypeError("typed business command only; raw text is forbidden")
        fingerprint = business_command_fingerprint(command)
        ingress_scope_hash = hashlib.sha256(
            "\x1f".join(
                (
                    context.tenant_id,
                    context.actor_user_id,
                    context.conversation_id,
                    context.source_channel,
                    context.source_message_id,
                )
            ).encode("utf-8")
        ).hexdigest()
        key = ":".join(
            ("agent2-business-v2", ingress_scope_hash, fingerprint)
        )
        receipt_id = _stable_uuid("business-receipt", key)

        reserved = await self.session.scalar(
            insert(BusinessCommandReceipt)
            .values(
                receipt_id=receipt_id,
                tenant_id=context.tenant_id,
                command_id=command.command_id,
                command_type=command.command_type,
                actor_user_id=context.actor_user_id,
                source_message_id=context.source_message_id,
                idempotency_key=key,
                status="processing",
                actual_write=False,
                created_at=context.occurred_at,
                updated_at=context.occurred_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    BusinessCommandReceipt.tenant_id,
                    BusinessCommandReceipt.idempotency_key,
                ]
            )
            .returning(BusinessCommandReceipt.receipt_id)
        )
        if reserved is None:
            existing = await self.session.scalar(
                select(BusinessCommandReceipt).where(
                    BusinessCommandReceipt.tenant_id == context.tenant_id,
                    BusinessCommandReceipt.idempotency_key == key,
                )
            )
            if existing is None:
                raise RuntimeError("idempotency reservation disappeared")
            if (
                self.execution_authority == "semantic_ticket"
                and _is_mutating_business_command(command)
                and not context.admission_required
            ):
                return _blocked_existing_ingress(
                    existing,
                    command=command,
                    context=context,
                    error_code="admission_ticket_required",
                )
            if (
                self.execution_authority != "semantic_ticket"
                and context.admission_required
            ):
                return _blocked_existing_ingress(
                    existing,
                    command=command,
                    context=context,
                    error_code="execution_authority_mismatch",
                )
            if (
                self.execution_authority == "semantic_ticket"
                and _is_mutating_business_command(command)
                and existing.status in {"executed", "duplicate"}
            ):
                try:
                    await self.admission_ticket_store.validate_consumed_business_replay(
                        command,
                        context,
                        receipt_id=str(existing.receipt_id),
                    )
                except BusinessCommandError as exc:
                    return _blocked_existing_ingress(
                        existing,
                        command=command,
                        context=context,
                        error_code=exc.code,
                        failed_stage=exc.stage,
                    )
            return _receipt_from_existing_ingress(
                existing,
                command=command,
                context=context,
            )

        try:
            async with self.session.begin_nested():
                if (
                    self.execution_authority == "semantic_ticket"
                    and _is_mutating_business_command(command)
                    and not context.admission_required
                ):
                    raise BusinessCommandError(
                        "admission_ticket_required",
                        "admission",
                        "semantic mutation requires authoritative admission",
                    )
                if (
                    self.execution_authority != "semantic_ticket"
                    and context.admission_required
                ):
                    raise BusinessCommandError(
                        "execution_authority_mismatch",
                        "admission",
                        "non-semantic authority cannot consume Admission Tickets",
                    )
                require_business_execution_admission(command, context)
                ticket_lease = None
                if context.admission_required:
                    ticket_lease = await self.admission_ticket_store.lock_and_validate(
                        command,
                        context,
                    )
                outcome = await self._dispatch(command, context, key)
                if ticket_lease is not None and outcome.status == "executed":
                    # Stage the successful receipt inside the same savepoint as
                    # the domain write before the authoritative Ticket may be
                    # consumed. The outer transaction remains the sole commit
                    # boundary for domain effect, receipt, Ticket, and audit.
                    await self.session.execute(
                        update(BusinessCommandReceipt)
                        .where(BusinessCommandReceipt.receipt_id == receipt_id)
                        .values(
                            status=outcome.status,
                            resource_type=outcome.resource_type,
                            resource_id=outcome.resource_id,
                            before_json=outcome.before,
                            after_json=outcome.after,
                            error_code=outcome.error_code or "",
                            failed_stage=outcome.failed_stage or "",
                            actual_write=outcome.actual_write,
                            updated_at=context.occurred_at,
                        )
                    )
                    await self.admission_ticket_store.consume(
                        ticket_lease,
                        receipt=AdmissionReceiptReference(
                            receipt_kind="business",
                            receipt_id=str(receipt_id),
                            status=outcome.status,
                            actual_write=outcome.actual_write,
                        ),
                        consumed_at=(
                            context.execution_started_at or context.occurred_at
                        ),
                    )
        except BusinessCommandError as exc:
            outcome = _Outcome(
                status="blocked",
                error_code=exc.code,
                failed_stage=exc.stage,
            )
        except Exception:
            logger.exception(
                "Agent2 business command persistence failed",
                extra={
                    "tenant_id": context.tenant_id,
                    "actor_user_id": context.actor_user_id,
                    "source_message_id": context.source_message_id,
                    "command_type": command.command_type,
                    "receipt_id": str(receipt_id),
                },
            )
            outcome = _Outcome(
                status="failed",
                error_code="business_persistence_error",
                failed_stage="persistence",
            )

        await self.session.execute(
            update(BusinessCommandReceipt)
            .where(BusinessCommandReceipt.receipt_id == receipt_id)
            .values(
                status=outcome.status,
                resource_type=outcome.resource_type,
                resource_id=outcome.resource_id,
                before_json=outcome.before,
                after_json=outcome.after,
                error_code=outcome.error_code or "",
                failed_stage=outcome.failed_stage or "",
                actual_write=outcome.actual_write,
                updated_at=context.occurred_at,
            )
        )
        # Successful read-only business queries are decisions too: they carry
        # tenant/actor/source scope and must be auditable without pretending a
        # domain write occurred.  Duplicate replays return the original
        # receipt above and therefore never create a second audit row.
        if outcome.status in {"executed", "blocked", "failed"}:
            self.session.add(
                BusinessAuditEvent(
                    audit_id=_stable_uuid("business-audit", str(receipt_id)),
                    tenant_id=context.tenant_id,
                    receipt_id=receipt_id,
                    actor_user_id=context.actor_user_id,
                    source_message_id=context.source_message_id,
                    source_channel=context.source_channel,
                    command_type=command.command_type,
                    resource_type=outcome.resource_type,
                    resource_id=outcome.resource_id,
                    before_json=outcome.before,
                    after_json=outcome.after,
                    created_at=context.occurred_at,
                )
            )
        await self.session.flush()
        persisted = await self.session.get(BusinessCommandReceipt, receipt_id)
        if persisted is None:
            raise RuntimeError("business receipt was not persisted")
        return _receipt_from_model(persisted)

    async def _dispatch(
        self, command: BusinessCommand, context: BusinessCommandContext, key: str
    ) -> "_Outcome":
        self.effect_policy.require(command)
        if isinstance(command, CreateTravelIntent):
            return await self._create_travel(command, context, key)
        if isinstance(command, UpdateTravelIntent):
            return await self._update_travel(command, context)
        if isinstance(command, RespondTravelCollaboration):
            return await self._respond_travel(command, context)
        if isinstance(command, CreateCaseProgress):
            return await self._create_progress(command, context, key)
        if isinstance(command, SnoozeCaseFollowup):
            return await self._snooze_case_followup(command, context)
        if isinstance(command, UpdateCaseProgress):
            return await self._update_progress(command, context)
        if isinstance(command, DeleteCaseProgress):
            return await self._delete_progress(command, context)
        if isinstance(command, QueryCaseProgress):
            return await self._query_progress(command, context)
        if isinstance(command, ListAssignedCases):
            return await self._list_assigned_cases(command, context)
        if isinstance(command, QueryOperationStatus):
            return await self._query_operation_status(command, context)
        if isinstance(command, QueryPartyCases):
            return await self._query_party_cases(command, context)
        if isinstance(command, LinkCaseProgress):
            return await self._link_progress(command, context)
        if isinstance(command, UpdateCaseFollowupPolicy):
            return await self._update_case_followup_policy(command, context)
        if isinstance(command, TriggerCaseFollowupNow):
            return await self._trigger_case_followup_now(command, context)
        raise TypeError("typed business command only")

    async def _update_case_followup_policy(
        self,
        command: UpdateCaseFollowupPolicy,
        context: BusinessCommandContext,
    ) -> "_Outcome":
        if command.tenant_id != context.tenant_id:
            raise BusinessCommandError(
                "policy_tenant_mismatch", "authorization", "policy tenant mismatch"
            )
        if command.assigned_user_id != context.actor_user_id:
            raise BusinessCommandError(
                "policy_user_mismatch", "authorization", "policy user mismatch"
            )
        case_uuid = await self._assert_case_access(command.case_id, context)
        case = await self.session.scalar(
            select(Agent2Case).where(
                Agent2Case.tenant_id == context.tenant_id,
                Agent2Case.case_id == case_uuid,
                Agent2Case.owner_user_id == context.actor_user_id,
            )
        )
        if case is None:
            raise BusinessCommandError(
                "case_not_assigned", "authorization", "case owner changed"
            )
        policy = await self.session.scalar(
            select(CaseFollowupPolicy)
            .where(
                CaseFollowupPolicy.tenant_id == context.tenant_id,
                CaseFollowupPolicy.case_id == case_uuid,
                CaseFollowupPolicy.assigned_user_id == context.actor_user_id,
            )
            .with_for_update()
        )
        if policy is None:
            if command.expected_version != 0:
                raise BusinessCommandError(
                    "version_conflict", "optimistic_lock", "policy version changed"
                )
            last_progress = await self.session.scalar(
                select(func.max(CaseProgress.occurred_at)).where(
                    CaseProgress.tenant_id == context.tenant_id,
                    CaseProgress.case_id == case_uuid,
                    CaseProgress.deleted_at.is_(None),
                    CaseProgress.content_origin != "robot_followup",
                )
            )
            policy_id = _stable_uuid(
                "case-followup-policy", context.tenant_id, command.case_id,
                context.actor_user_id,
            )
            current = CaseFollowupPolicySnapshot(
                policy_id=str(policy_id), tenant_id=context.tenant_id,
                case_id=command.case_id, assigned_user_id=context.actor_user_id,
                enabled=False, cadence_type="event_only", timezone="Asia/Shanghai",
                last_meaningful_progress_at=last_progress, next_due_at=None,
                version=0,
            )
        else:
            current = CaseFollowupPolicySnapshot(
                policy_id=str(policy.policy_id), tenant_id=policy.tenant_id,
                case_id=str(policy.case_id), assigned_user_id=policy.assigned_user_id,
                enabled=policy.enabled, cadence_type=policy.cadence_type,
                timezone=policy.timezone,
                last_meaningful_progress_at=policy.last_meaningful_progress_at,
                next_due_at=policy.next_due_at, version=policy.version,
                policy_source=policy.policy_source,
                event_triggers_enabled=policy.event_triggers_enabled,
                hearing_reminders_enabled=policy.hearing_reminders_enabled,
                stage_transition_enabled=policy.stage_transition_enabled,
                node_transition_enabled=policy.node_transition_enabled,
                business_days_only=policy.business_days_only,
                custom_interval_days=policy.cadence_days,
                snoozed_until=policy.snoozed_until,
            )
        execution = execute_policy_update(
            current, command, actor_has_case_permission=True,
            now=context.occurred_at,
        )
        if not execution.actual_write:
            raise BusinessCommandError(
                execution.reason_code, "policy", "follow-up policy was not changed"
            )
        after = execution.after
        if policy is None:
            policy = CaseFollowupPolicy(
                policy_id=UUID(after.policy_id), tenant_id=after.tenant_id,
                case_id=case_uuid, assigned_user_id=after.assigned_user_id,
                custom_interval_json={}, timezone=after.timezone,
                event_triggers_enabled=after.event_triggers_enabled,
                max_unanswered_reminders=1, created_at=context.occurred_at,
            )
            self.session.add(policy)
        policy.enabled = after.enabled
        policy.policy_source = after.policy_source
        policy.cadence_type = after.cadence_type
        policy.cadence_days = after.custom_interval_days
        policy.business_days_only = after.business_days_only
        policy.last_meaningful_progress_at = after.last_meaningful_progress_at
        policy.next_due_at = after.next_due_at
        policy.snoozed_until = after.snoozed_until
        policy.hearing_reminders_enabled = after.hearing_reminders_enabled
        policy.stage_transition_enabled = after.stage_transition_enabled
        policy.node_transition_enabled = after.node_transition_enabled
        policy.version = after.version
        policy.updated_at = context.occurred_at
        before_json = _followup_policy_snapshot_json(current)
        after_json = _followup_policy_snapshot_json(after)
        return _Outcome.success(
            "case_followup_policy", str(policy.policy_id), before_json, after_json
        )

    async def _trigger_case_followup_now(
        self,
        command: TriggerCaseFollowupNow,
        context: BusinessCommandContext,
    ) -> "_Outcome":
        actor_is_admin = "tenant_admin" in set(context.actor_role_ids)
        target_user_id = command.assigned_user_id
        if (
            command.tenant_id != context.tenant_id
            or (target_user_id != context.actor_user_id and not actor_is_admin)
        ):
            raise BusinessCommandError(
                "followup_scope_mismatch", "authorization", "follow-up scope mismatch"
            )
        case_uuid = await self._assert_case_access(command.case_id, context)
        case = await self.session.scalar(
            select(Agent2Case).where(
                Agent2Case.tenant_id == context.tenant_id,
                Agent2Case.case_id == case_uuid,
                Agent2Case.owner_user_id == target_user_id,
            )
        )
        if case is None:
            raise BusinessCommandError(
                "case_not_assigned", "authorization", "case owner changed"
            )
        active = (
            await self.session.scalars(
                select(CaseFollowupTask).where(
                    CaseFollowupTask.tenant_id == context.tenant_id,
                    CaseFollowupTask.case_id == case_uuid,
                    CaseFollowupTask.assigned_user_id == target_user_id,
                    CaseFollowupTask.task_status.in_((
                        "scheduled", "queued", "sending", "waiting_for_reply"
                    )),
                )
            )
        ).all()
        existing = tuple(
            CaseFollowupTaskSnapshot(
                str(item.followup_id), item.tenant_id, str(item.case_id),
                item.assigned_user_id, item.trigger_type, item.question_type,
                item.task_status,
            )
            for item in active
        )
        source = dict(case.source_json or {})
        subject = CaseFollowupSubject(
            tenant_id=case.tenant_id, case_id=str(case.case_id),
            assigned_user_id=target_user_id, case_type=case.case_type,
            stage=str(source.get("major_stage") or source.get("stage") or ""),
            node=str(source.get("minor_stage") or source.get("node") or ""),
            case_version=case.version, case_name=case.case_name,
        )
        policy = await self.session.scalar(
            select(CaseFollowupPolicy).where(
                CaseFollowupPolicy.tenant_id == context.tenant_id,
                CaseFollowupPolicy.case_id == case_uuid,
                CaseFollowupPolicy.assigned_user_id == target_user_id,
            ).with_for_update()
        )
        current_policy_version = policy.version if policy is not None else 0
        if (
            command.expected_policy_version is not None
            and command.expected_policy_version != current_policy_version
        ):
            raise BusinessCommandError(
                "version_conflict", "optimistic_lock", "policy version changed"
            )
        policy_snapshot = CaseFollowupPolicySnapshot(
            policy_id=str(policy.policy_id) if policy is not None else "",
            tenant_id=context.tenant_id, case_id=command.case_id,
            assigned_user_id=target_user_id, enabled=True,
            cadence_type="manual_only", timezone=(policy.timezone if policy else "Asia/Shanghai"),
            last_meaningful_progress_at=(policy.last_meaningful_progress_at if policy else None),
            next_due_at=None, version=(policy.version if policy else 0),
            policy_source=(policy.policy_source if policy else "case_manual_override"),
            event_triggers_enabled=True,
        )
        trigger = CaseFollowupTrigger(
            trigger_type="manual",
            trigger_event_id=f"manual:{context.source_message_id}",
            question_type="meaningful_progress",
            due_at=context.occurred_at,
            priority=300,
            facts={"source_turn_id": context.source_message_id},
        )
        evaluation = FollowupPolicyEngine().evaluate_triggers(
            subject, policy_snapshot, triggers=(trigger,),
            now=context.occurred_at, existing_tasks=existing,
        )
        if not evaluation.eligible:
            raise BusinessCommandError(
                evaluation.reason_code, "followup_policy", "manual follow-up was blocked"
            )
        plan = build_followup_task_plan(
            subject, evaluation, expires_at=context.occurred_at + timedelta(days=7),
            policy_id=policy_snapshot.policy_id,
        )
        task = CaseFollowupTask(
            followup_id=UUID(plan.followup_id), tenant_id=context.tenant_id,
            case_id=case_uuid, assigned_user_id=target_user_id,
            policy_id=UUID(plan.policy_id) if plan.policy_id else None,
            trigger_type=plan.trigger_type,
            trigger_event_id=plan.trigger_event_ids[0],
            trigger_sources_json=[{
                "trigger_type": item.trigger_type,
                "trigger_event_id": item.trigger_event_id,
                "question_type": item.question_type,
                "due_at": item.due_at.isoformat(),
                "priority": item.priority,
                "facts": dict(item.facts),
            } for item in plan.trigger_sources],
            case_type=plan.case_type, stage=plan.stage, node=plan.node,
            case_version=plan.case_version, question_type=plan.question_type,
            question_text=plan.question_text, priority=plan.priority,
            task_status="scheduled", message_status="scheduled",
            response_status="not_requested", due_at=plan.due_at,
            expires_at=plan.expires_at, reminder_count=0, max_reminders=1,
            conversation_id=(
                context.conversation_id
                or f"agent2-direct:{target_user_id}"
            ),
            provider_message_id="", idempotency_key=plan.idempotency_key,
            version=1, created_at=context.occurred_at, updated_at=context.occurred_at,
        )
        ledger = Agent2TaskLedgerEntry(
            task_id=task.followup_id, tenant_id=context.tenant_id,
            user_id=target_user_id,
            conversation_id=task.conversation_id, domain="case_followup",
            operation="answer", object_ref_json={
                "case_id": command.case_id, "followup_id": plan.followup_id,
            },
            status="active", focus_state="active", version=1,
            source_turn_id=context.source_message_id,
            pending_requirements_json={"pending_type": "case_followup"},
            resume_policy_json={"mode": "restore_previous"},
            expires_at=plan.expires_at, created_at=context.occurred_at,
            updated_at=context.occurred_at,
        )
        self.session.add_all((task, ledger))
        return _Outcome.success(
            "case_followup_task", plan.followup_id, {}, {
                "followup_id": plan.followup_id, "case_id": command.case_id,
                "case_name": case.case_name, "task_status": "scheduled",
                "message_status": "scheduled", "question_summary": plan.question_text,
                "due_at": plan.due_at.isoformat(), "version": 1,
            }
        )

    async def _create_travel(
        self, command: CreateTravelIntent, context: BusinessCommandContext, key: str
    ) -> "_Outcome":
        if not command.city_code or not command.destination_normalized:
            raise BusinessCommandError("travel_location_ambiguous", "domain_policy", "city is required")
        if command.start_at > command.end_at:
            raise BusinessCommandError("travel_time_invalid", "domain_policy", "travel interval is invalid")
        if command.confidence < 0.85:
            raise BusinessCommandError("travel_confidence_too_low", "domain_policy", "travel needs clarification")
        for case_id in command.related_case_ids:
            await self._assert_case_access(case_id, context)
        travel_intent_id = _stable_uuid(
            "travel-intent",
            context.tenant_id,
            context.source_message_id,
            business_command_fingerprint(command),
        )
        if context.admission_required:
            object_ref = context.admission_ticket.get("object_ref")
            if not isinstance(object_ref, dict):
                raise BusinessCommandError(
                    "admission_ticket_object_mismatch",
                    "admission",
                    "travel Ticket object is missing",
                )
            travel_intent_id = _parse_uuid(
                str(object_ref.get("stable_id") or ""),
                "admission_ticket_object_mismatch",
            )
        intent = TravelIntent(
            travel_intent_id=travel_intent_id,
            tenant_id=context.tenant_id,
            company_id=context.company_id,
            department_id=context.department_id,
            team_id=context.team_id,
            user_id=context.actor_user_id,
            destination_raw=command.destination_raw,
            destination_normalized=command.destination_normalized,
            city_code=command.city_code,
            province_code=command.province_code,
            start_at=command.start_at,
            end_at=command.end_at,
            time_precision=command.time_precision,
            purpose_summary=command.purpose_summary,
            related_case_ids=list(command.related_case_ids),
            related_matter_ids=[],
            source_message_id=context.source_message_id,
            source_channel=context.source_channel,
            status="planned",
            confidence=Decimal(str(command.confidence)),
            idempotency_key=key,
            version=1,
            created_at=context.occurred_at,
            updated_at=context.occurred_at,
        )
        self.session.add(intent)
        await self.session.flush()
        return _Outcome.success("travel_intent", str(intent.travel_intent_id), {}, _model_json(intent))

    async def _update_travel(
        self, command: UpdateTravelIntent, context: BusinessCommandContext
    ) -> "_Outcome":
        intent_id = _parse_uuid(command.travel_intent_id, "travel_intent_not_found")
        current = await self.session.scalar(
            select(TravelIntent)
            .where(
                TravelIntent.travel_intent_id == intent_id,
                TravelIntent.tenant_id == context.tenant_id,
                TravelIntent.user_id == context.actor_user_id,
            )
            .with_for_update()
        )
        if current is None:
            raise BusinessCommandError("travel_intent_not_found", "authorization", "travel intent not found")
        if current.version != command.expected_version:
            raise BusinessCommandError("version_conflict", "optimistic_lock", "travel version conflict")
        before = _model_json(current)
        current.start_at = command.start_at or current.start_at
        current.end_at = command.end_at or current.end_at
        current.destination_normalized = command.destination_normalized or current.destination_normalized
        current.city_code = command.city_code or current.city_code
        current.status = command.status or "changed"
        current.version += 1
        current.updated_at = context.occurred_at
        if current.start_at > current.end_at:
            raise BusinessCommandError("travel_time_invalid", "domain_policy", "travel interval is invalid")
        candidate_ids = select(TravelCollaborationCandidate.candidate_id).where(
            TravelCollaborationCandidate.tenant_id == context.tenant_id,
            TravelCollaborationCandidate.travel_intent_ids.contains([command.travel_intent_id]),
            TravelCollaborationCandidate.status.not_in(("declined", "cancelled", "expired")),
        )
        await self.session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.candidate_id.in_(candidate_ids),
                NotificationOutbox.status.in_(("pending", "failed")),
            )
            .values(
                status="cancelled",
                error_message="travel_intent_changed",
                updated_at=context.occurred_at,
            )
        )
        await self.session.execute(
            update(TravelCollaborationCandidate)
            .where(TravelCollaborationCandidate.candidate_id.in_(candidate_ids))
            .values(
                status="cancelled",
                version=TravelCollaborationCandidate.version + 1,
                updated_at=context.occurred_at,
            )
        )
        await self.session.flush()
        return _Outcome.success("travel_intent", command.travel_intent_id, before, _model_json(current))

    async def _respond_travel(
        self, command: RespondTravelCollaboration, context: BusinessCommandContext
    ) -> "_Outcome":
        candidate_id = _parse_uuid(command.candidate_id, "travel_candidate_not_found")
        candidate = await self.session.scalar(
            select(TravelCollaborationCandidate)
            .where(
                TravelCollaborationCandidate.candidate_id == candidate_id,
                TravelCollaborationCandidate.tenant_id == context.tenant_id,
                TravelCollaborationCandidate.company_id == context.company_id,
                TravelCollaborationCandidate.department_id == context.department_id,
                TravelCollaborationCandidate.team_id == context.team_id,
            )
            .with_for_update()
        )
        if candidate is None:
            raise BusinessCommandError("travel_candidate_not_found", "authorization", "candidate not found")
        participants = tuple(candidate.participant_ids or [])
        if context.actor_user_id not in participants:
            raise BusinessCommandError("travel_candidate_forbidden", "authorization", "not a participant")
        if (
            command.expected_version is not None
            and candidate.version != command.expected_version
        ):
            raise BusinessCommandError(
                "version_conflict", "optimistic_lock", "candidate version conflict"
            )
        if candidate.status in {"accepted", "declined", "cancelled", "expired"}:
            raise BusinessCommandError(
                "travel_candidate_closed",
                "domain_policy",
                "travel collaboration candidate is already closed",
            )
        before = _model_json(candidate)
        responses = dict(candidate.responses_json or {})
        responses[context.actor_user_id] = command.response
        values = set(responses.values())
        if "decline" in values:
            status = "declined"
        elif "cancel" in values or "changed" in values:
            status = "cancelled"
        elif all(responses.get(user_id) == "accept" for user_id in participants):
            status = "accepted"
        elif "accept" in values:
            status = "accepted_by_one"
        else:
            status = "notified"
        candidate.responses_json = responses
        candidate.status = status
        candidate.version += 1
        candidate.updated_at = context.occurred_at
        if status in {"declined", "cancelled"}:
            await self.session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.tenant_id == context.tenant_id,
                    NotificationOutbox.candidate_id == candidate.candidate_id,
                    NotificationOutbox.status.in_(("pending", "failed")),
                )
                .values(
                    status="cancelled",
                    error_message="travel_candidate_closed",
                    updated_at=context.occurred_at,
                )
            )
        cancelled_intent_ids: list[str] = []
        if command.response in {"changed", "cancel"}:
            intent_ids = [
                UUID(value)
                for value in (candidate.travel_intent_ids or [])
                if _is_uuid(value)
            ]
            intents = list(
                (
                    await self.session.scalars(
                        select(TravelIntent)
                        .where(
                            TravelIntent.tenant_id == context.tenant_id,
                            TravelIntent.travel_intent_id.in_(intent_ids),
                            TravelIntent.user_id == context.actor_user_id,
                            TravelIntent.status.not_in(("cancelled", "completed")),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for intent in intents:
                intent.status = "cancelled"
                intent.version += 1
                intent.updated_at = context.occurred_at
                cancelled_intent_ids.append(str(intent.travel_intent_id))
            for intent_id in cancelled_intent_ids:
                affected_candidate_ids = select(TravelCollaborationCandidate.candidate_id).where(
                    TravelCollaborationCandidate.tenant_id == context.tenant_id,
                    TravelCollaborationCandidate.candidate_id != candidate.candidate_id,
                    TravelCollaborationCandidate.travel_intent_ids.contains([intent_id]),
                    TravelCollaborationCandidate.status.not_in(("declined", "cancelled", "expired")),
                )
                await self.session.execute(
                    update(NotificationOutbox)
                    .where(
                        NotificationOutbox.candidate_id.in_(affected_candidate_ids),
                        NotificationOutbox.status.in_(("pending", "failed")),
                    )
                    .values(
                        status="cancelled",
                        error_message="participant_travel_cancelled_or_changed",
                        updated_at=context.occurred_at,
                    )
                )
                await self.session.execute(
                    update(TravelCollaborationCandidate)
                    .where(TravelCollaborationCandidate.candidate_id.in_(affected_candidate_ids))
                    .values(
                        status="cancelled",
                        version=TravelCollaborationCandidate.version + 1,
                        updated_at=context.occurred_at,
                    )
                )
        await self.session.flush()
        after = _model_json(candidate)
        if cancelled_intent_ids:
            after["cancelled_travel_intent_ids"] = cancelled_intent_ids
        return _Outcome.success("travel_collaboration", command.candidate_id, before, after)

    async def _snooze_case_followup(
        self,
        command: SnoozeCaseFollowup,
        context: BusinessCommandContext,
    ) -> "_Outcome":
        case_id = await self._assert_case_access(command.case_id, context)
        conversation_id = (
            context.conversation_id or f"agent2-direct:{context.actor_user_id}"
        )
        active = tuple((await self.session.scalars(
            select(CaseFollowupPending)
            .where(
                CaseFollowupPending.pending_type == "case_followup",
                CaseFollowupPending.tenant_id == context.tenant_id,
                CaseFollowupPending.user_id == context.actor_user_id,
                CaseFollowupPending.conversation_id == conversation_id,
                CaseFollowupPending.status.in_(("active", "awaiting_input")),
                CaseFollowupPending.expires_at > context.occurred_at,
            )
            .with_for_update()
        )).all())
        if len(active) != 1:
            raise BusinessCommandError(
                "case_followup_target_needs_clarification", "domain_policy",
                "exactly one active case follow-up is required",
            )
        pending = active[0]
        if str(pending.pending_id) != command.pending_id:
            raise BusinessCommandError(
                "case_followup_target_mismatch", "authorization",
                "snooze target is not the unique active pending",
            )
        if pending.case_id != case_id:
            raise BusinessCommandError(
                "case_followup_case_mismatch", "authorization",
                "snooze case does not match pending",
            )
        if (
            command.snoozed_until.tzinfo is None
            or command.snoozed_until <= context.occurred_at
            or command.snoozed_until > context.occurred_at + timedelta(days=365)
        ):
            raise BusinessCommandError(
                "case_followup_snooze_time_invalid", "domain_policy",
                "snooze time must be a bounded future timestamp",
            )
        task = await self.session.scalar(
            select(CaseFollowupTask)
            .where(
                CaseFollowupTask.tenant_id == context.tenant_id,
                CaseFollowupTask.followup_id == pending.followup_id,
                CaseFollowupTask.assigned_user_id == context.actor_user_id,
            )
            .with_for_update()
        )
        if (
            task is None or task.task_status != "waiting_for_reply"
            or task.message_status not in {"accepted_by_provider", "delivery_confirmed"}
            or not task.provider_message_id
        ):
            raise BusinessCommandError(
                "case_followup_not_provider_accepted", "authorization",
                "follow-up has no provider acceptance evidence",
            )
        expected_case_version = int(
            (pending.candidate_versions_json or {}).get(str(case_id), -1)
        )
        if expected_case_version != task.case_version:
            raise BusinessCommandError(
                "case_followup_case_mismatch", "authorization",
                "follow-up case version changed",
            )
        policy = await self.session.scalar(
            select(CaseFollowupPolicy)
            .where(
                CaseFollowupPolicy.tenant_id == context.tenant_id,
                CaseFollowupPolicy.case_id == case_id,
                CaseFollowupPolicy.assigned_user_id == context.actor_user_id,
            )
            .with_for_update()
        )
        before = {
            "task_status": task.task_status,
            "response_status": task.response_status,
            "snoozed_until": (
                policy.snoozed_until.isoformat()
                if policy is not None and policy.snoozed_until else None
            ),
        }
        task.task_status = "snoozed"
        task.response_status = "snoozed"
        task.next_eligible_at = command.snoozed_until
        task.completed_at = context.occurred_at
        task.version += 1
        task.updated_at = context.occurred_at
        pending.status = "consumed"
        pending.consumed_at = context.occurred_at
        pending.version += 1
        pending.updated_at = context.occurred_at
        if policy is not None:
            policy.snoozed_until = command.snoozed_until
            policy.next_due_at = command.snoozed_until
            policy.version += 1
            policy.updated_at = context.occurred_at
        ledger_transition = await complete_followup_and_restore_report(
            self.session, followup_task_id=task.followup_id,
            now=context.occurred_at, case_receipt_succeeded=True,
        )
        if ledger_transition.status == "blocked":
            raise BusinessCommandError(
                "case_followup_task_state_conflict", "domain_policy",
                ledger_transition.reason_code,
            )
        await self.session.flush()
        after = {
            "followup_id": str(task.followup_id), "case_id": str(case_id),
            "task_status": task.task_status, "response_status": task.response_status,
            "snoozed_until": command.snoozed_until.isoformat(),
            "restored_task_id": ledger_transition.restored_task_id,
        }
        return _Outcome.success(
            "case_followup_task", str(task.followup_id), before, after
        )

    async def _create_progress(
        self, command: CreateCaseProgress, context: BusinessCommandContext, key: str
    ) -> "_Outcome":
        case_id = await self._assert_case_access(command.case_id, context)
        case_record = await self.session.scalar(
            select(Agent2Case)
            .where(
                Agent2Case.tenant_id == context.tenant_id,
                Agent2Case.case_id == case_id,
            )
            .with_for_update()
        )
        if case_record is None:
            raise BusinessCommandError(
                "case_not_found", "authorization", "case is not in tenant"
            )
        if not context.can_create_case_progress(
            str(case_record.case_id),
            owner_user_id=case_record.owner_user_id,
        ):
            raise BusinessCommandError(
                "case_not_writable",
                "authorization",
                "actor has no case progress create permission",
            )
        if not command.summary.strip():
            raise BusinessCommandError("progress_summary_required", "domain_policy", "summary is required")
        content_origin = "human_record"
        followup = None
        lifecycle_pending = None
        lifecycle_task = None
        if command.followup_notification_id:
            followup_id = _parse_uuid(
                command.followup_notification_id,
                "case_followup_not_found",
            )
            conversation_id = (
                context.conversation_id
                or f"agent2-direct:{context.actor_user_id}"
            )
            lifecycle_candidates = tuple(
                (
                    await self.session.scalars(
                        select(CaseFollowupPending).where(
                            CaseFollowupPending.pending_type == "case_followup",
                            CaseFollowupPending.tenant_id == context.tenant_id,
                            CaseFollowupPending.user_id == context.actor_user_id,
                            CaseFollowupPending.conversation_id == conversation_id,
                            CaseFollowupPending.domain == "case",
                            CaseFollowupPending.status.in_(("active", "awaiting_input")),
                            CaseFollowupPending.expires_at > context.occurred_at,
                        )
                        .order_by(CaseFollowupPending.created_at.desc())
                        .limit(5)
                        .with_for_update()
                    )
                ).all()
            )
            matched_lifecycle = tuple(
                item for item in lifecycle_candidates
                if item.pending_id == followup_id
            )
            if lifecycle_candidates and len(lifecycle_candidates) != 1:
                raise BusinessCommandError(
                    "case_followup_target_needs_clarification",
                    "domain_policy",
                    "exactly one active case follow-up is required",
                )
            if len(matched_lifecycle) == 1:
                lifecycle_pending = matched_lifecycle[0]
                lifecycle_task = await self.session.scalar(
                    select(CaseFollowupTask)
                    .where(
                        CaseFollowupTask.tenant_id == context.tenant_id,
                        CaseFollowupTask.followup_id == lifecycle_pending.followup_id,
                    )
                    .with_for_update()
                )
                if (
                    lifecycle_task is None
                    or lifecycle_task.task_status != "waiting_for_reply"
                    or lifecycle_task.message_status not in {
                        "accepted_by_provider", "delivery_confirmed"
                    }
                    or not lifecycle_task.provider_message_id
                ):
                    raise BusinessCommandError(
                        "case_followup_not_provider_accepted",
                        "authorization",
                        "follow-up has no provider acceptance evidence",
                    )
                if (
                    str(lifecycle_pending.case_id) != str(case_id)
                    or case_record.version != lifecycle_task.case_version
                    or int(
                        (lifecycle_pending.candidate_versions_json or {}).get(
                            str(case_id), -1
                        )
                    )
                    != lifecycle_task.case_version
                ):
                    raise BusinessCommandError(
                        "case_followup_case_mismatch", "authorization",
                        "follow-up case or version changed",
                    )
                conversation_state = await self.session.scalar(
                    select(Agent2ConversationState).where(
                        Agent2ConversationState.user_key
                        == f"{context.tenant_id}:{context.actor_user_id}",
                        Agent2ConversationState.conversation_id == conversation_id,
                    )
                )
                current_state_version = (
                    conversation_state.version if conversation_state is not None else 0
                )
                if current_state_version != lifecycle_pending.expected_state_version:
                    raise BusinessCommandError(
                        "case_followup_conversation_state_changed",
                        "authorization",
                        "conversation state changed after the follow-up was sent",
                    )
                content_origin = "robot_followup"
            else:
                followup_rows = tuple(
                    (
                        await self.session.scalars(
                            select(NotificationOutbox).where(
                                NotificationOutbox.tenant_id == context.tenant_id,
                                NotificationOutbox.recipient_user_id == context.actor_user_id,
                                NotificationOutbox.message_type == "case_progress_followup",
                                NotificationOutbox.status == "sent",
                            )
                            .order_by(NotificationOutbox.created_at.desc())
                            .limit(5)
                            .with_for_update()
                        )
                    ).all()
                )
                active_followups = active_case_progress_followup_resources(
                    followup_rows, allowed_case_ids=context.allowed_case_ids,
                    now=context.occurred_at,
                )
                if len(active_followups) != 1:
                    raise BusinessCommandError(
                        "case_followup_target_needs_clarification", "domain_policy",
                        "exactly one active case follow-up is required",
                    )
                if active_followups[0]["notification_id"] != str(followup_id):
                    raise BusinessCommandError(
                        "case_followup_target_mismatch", "authorization",
                        "follow-up target is not the unique active task",
                    )
                followup = next(
                    item for item in followup_rows if item.notification_id == followup_id
                )
                if not followup.external_message_id:
                    raise BusinessCommandError(
                        "case_followup_not_delivered", "authorization",
                        "a provider-accepted case follow-up is required",
                    )
                followup_payload = dict(followup.message_json or {})
                if str(followup_payload.get("case_id") or "") != str(case_id):
                    raise BusinessCommandError(
                        "case_followup_case_mismatch", "authorization",
                        "follow-up case does not match the progress target",
                    )
                try:
                    expires_at = datetime.fromisoformat(
                        str(followup_payload.get("expires_at") or "")
                    )
                except ValueError as exc:
                    raise BusinessCommandError(
                        "case_followup_expiry_invalid", "domain_policy",
                        "follow-up expiry is invalid",
                    ) from exc
                if expires_at.tzinfo is None or context.occurred_at >= expires_at:
                    raise BusinessCommandError(
                        "case_followup_expired", "domain_policy",
                        "follow-up is no longer active",
                    )
                if str((followup.response_json or {}).get("followup_status") or "") == "completed":
                    raise BusinessCommandError(
                        "case_followup_already_completed", "domain_policy",
                        "follow-up already has a response",
                    )
                content_origin = "robot_followup"
        progress = CaseProgress(
            progress_id=_stable_uuid(
                "case-progress",
                context.tenant_id,
                context.source_message_id,
                business_command_fingerprint(command),
            ),
            tenant_id=context.tenant_id,
            case_id=case_id,
            occurred_at=command.occurred_at,
            recorded_at=context.occurred_at,
            reporter_id=context.actor_user_id,
            progress_type=command.progress_type,
            summary=command.summary.strip(),
            details=command.details.strip(),
            source_message_id=context.source_message_id,
            source_channel=context.source_channel,
            content_origin=content_origin,
            related_party_ids=list(command.related_party_ids),
            related_document_ids=list(command.related_document_ids),
            related_travel_intent_ids=list(command.related_travel_intent_ids),
            confidence=Decimal(str(command.confidence)),
            confirmation_status="confirmed_by_reporter",
            version=1,
            idempotency_key=key,
            created_at=context.occurred_at,
            updated_at=context.occurred_at,
        )
        self.session.add(progress)
        lifecycle_snapshot, lifecycle_change, lifecycle_transition = await self._apply_lifecycle_patch(
            command=command,
            context=context,
            case=case_record,
            progress=progress,
        )
        if followup is not None:
            followup.response_json = {
                **dict(followup.response_json or {}),
                "followup_status": "completed",
                "response_source_message_id": context.source_message_id,
                "progress_id": str(progress.progress_id),
                "completed_at": context.occurred_at.isoformat(),
            }
            followup.updated_at = context.occurred_at
        if lifecycle_pending is not None and lifecycle_task is not None:
            lifecycle_pending.status = "consumed"
            lifecycle_pending.consumed_at = context.occurred_at
            lifecycle_pending.version += 1
            lifecycle_pending.updated_at = context.occurred_at
            lifecycle_task.task_status = "answered"
            lifecycle_task.response_status = "answered"
            lifecycle_task.source_progress_id = progress.progress_id
            lifecycle_task.completed_at = context.occurred_at
            lifecycle_task.version += 1
            lifecycle_task.updated_at = context.occurred_at
            ledger_transition = await complete_followup_and_restore_report(
                self.session,
                followup_task_id=lifecycle_task.followup_id,
                now=context.occurred_at,
                case_receipt_succeeded=True,
            )
            if ledger_transition.status == "blocked":
                raise BusinessCommandError(
                    "case_followup_task_state_conflict",
                    "domain_policy",
                    ledger_transition.reason_code,
                )
            policy = await self.session.scalar(
                select(CaseFollowupPolicy).where(
                    CaseFollowupPolicy.tenant_id == context.tenant_id,
                    CaseFollowupPolicy.case_id == case_id,
                    CaseFollowupPolicy.assigned_user_id == case_record.owner_user_id,
                )
            )
            if policy is not None:
                policy.last_meaningful_progress_at = context.occurred_at
                policy.last_followup_at = context.occurred_at
                if policy.cadence_type in {
                    "daily", "weekly", "every_15_days", "monthly", "custom_interval"
                }:
                    policy.next_due_at = calculate_next_due_at(
                        context.occurred_at, cadence_type=policy.cadence_type,
                        timezone_name=policy.timezone,
                        custom_interval_days=policy.cadence_days,
                        business_days_only=policy.business_days_only,
                    )
                policy.version += 1
                policy.updated_at = context.occurred_at
        await invalidate_stale_cadence_followups(
            self.session,
            tenant_id=context.tenant_id,
            case_id=case_id,
            assigned_user_id=case_record.owner_user_id,
            now=context.occurred_at,
            current_followup_id=(
                str(lifecycle_task.followup_id) if lifecycle_task is not None else ""
            ),
        )
        await self.session.flush()
        after = _model_json(progress)
        if lifecycle_snapshot:
            after["lifecycle_state"] = lifecycle_snapshot
        if lifecycle_change:
            after["lifecycle_change"] = lifecycle_change
        if lifecycle_transition:
            after["lifecycle_transition"] = lifecycle_transition
        return _Outcome.success("case_progress", str(progress.progress_id), {}, after)

    async def _apply_lifecycle_patch(
        self,
        *,
        command: CreateCaseProgress,
        context: BusinessCommandContext,
        case: Agent2Case,
        progress: CaseProgress,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        has_patch = bool(
            command.lifecycle_stage or command.lifecycle_node
            or command.current_status or command.next_actions
            or command.hearing_readiness or command.blocking_issues
        )
        if not has_patch:
            return {}, {}, {}
        plaintiff_stages = {"拟诉", "诉讼中", "执行中", "已结案"}
        defendant_stages = {"受理", "开庭", "审结", "履行", "已结案"}
        allowed_stages = (
            plaintiff_stages if case.case_type in {"plaintiff", "plaintiff_case"}
            else defendant_stages if case.case_type in {"defendant", "defendant_case"}
            else set()
        )
        if command.lifecycle_stage and command.lifecycle_stage not in allowed_stages:
            raise BusinessCommandError(
                "case_lifecycle_stage_invalid", "domain_policy",
                "stage is not valid for the case role",
            )
        if (
            command.lifecycle_node
            and command.lifecycle_node not in TriggerPolicyMatrix.NODE_ALLOWLIST
        ):
            raise BusinessCommandError(
                "case_lifecycle_node_invalid", "domain_policy",
                "node is not in the lifecycle allowlist",
            )
        source = dict(case.source_json or {})
        initial_stage = str(source.get("major_stage") or source.get("stage") or "")
        initial_node = str(source.get("minor_stage") or source.get("node") or "")
        state = await self.session.scalar(
            select(CaseLifecycleState)
            .where(
                CaseLifecycleState.tenant_id == context.tenant_id,
                CaseLifecycleState.case_id == case.case_id,
            )
            .with_for_update()
        )
        state_created = state is None
        state_before: dict[str, Any] = {}
        if state is None:
            state = CaseLifecycleState(
                lifecycle_state_id=_stable_uuid(
                    "case-lifecycle-state", context.tenant_id, str(case.case_id)
                ),
                tenant_id=context.tenant_id, case_id=case.case_id,
                assigned_user_id=case.owner_user_id, case_type=case.case_type,
                stage=initial_stage, node=initial_node, current_status="",
                next_actions_json=[], hearing_readiness="",
                blocking_issues_json=[], last_progress_id=None, version=1,
                created_at=context.occurred_at, updated_at=context.occurred_at,
            )
            self.session.add(state)
            before_stage, before_node = initial_stage, initial_node
        else:
            if state.assigned_user_id != case.owner_user_id:
                raise BusinessCommandError(
                    "case_lifecycle_owner_changed", "authorization",
                    "case lifecycle owner changed",
                )
            state_before = _model_json(state)
            before_stage, before_node = state.stage, state.node
            state.version += 1
        if command.lifecycle_stage:
            state.stage = command.lifecycle_stage
        if command.lifecycle_node:
            state.node = command.lifecycle_node
        if command.current_status:
            state.current_status = command.current_status
        if command.next_actions:
            state.next_actions_json = list(command.next_actions)
        if command.hearing_readiness:
            state.hearing_readiness = command.hearing_readiness
        if command.blocking_issues:
            state.blocking_issues_json = list(command.blocking_issues)
        state.last_progress_id = progress.progress_id
        state.updated_at = context.occurred_at
        stage_or_node_changed = (
            state.stage != before_stage or state.node != before_node
        )
        if stage_or_node_changed:
            case.source_json = {
                **source, "major_stage": state.stage, "minor_stage": state.node,
            }
            case.version += 1
            case.updated_at = context.occurred_at
        snapshot = _model_json(state)
        change = (
            {
                "from_stage": before_stage, "to_stage": state.stage,
                "from_node": before_node, "node": state.node,
                "case_type": case.case_type, "case_version": case.version,
                "occurred_at": context.occurred_at.isoformat(),
            }
            if stage_or_node_changed else {}
        )
        return snapshot, change, {"created": state_created, "before": state_before}

    async def _update_progress(
        self, command: UpdateCaseProgress, context: BusinessCommandContext
    ) -> "_Outcome":
        current = await self._progress_for_write(command.progress_id, command.expected_version, context)
        before = _model_json(current)
        if command.summary is not None:
            current.summary = command.summary.strip()
        if command.details is not None:
            current.details = command.details.strip()
        if not current.summary:
            raise BusinessCommandError("progress_summary_required", "domain_policy", "summary is required")
        current.version += 1
        current.updated_at = context.occurred_at
        await self.session.flush()
        # PostgreSQL/asyncpg may expire ORM attributes after a prior nested
        # command in the same outer transaction.  Refresh explicitly while
        # still inside the async greenlet before serializing the receipt.
        await self.session.refresh(current)
        return _Outcome.success("case_progress", command.progress_id, before, _model_json(current))

    async def _delete_progress(
        self, command: DeleteCaseProgress, context: BusinessCommandContext
    ) -> "_Outcome":
        current = await self._progress_for_write(command.progress_id, command.expected_version, context)
        before = _model_json(current)
        lifecycle_reversal = await self._reverse_lifecycle_patch_for_delete(
            current=current,
            context=context,
        )
        current.deleted_at = context.occurred_at
        current.deleted_by = context.actor_user_id
        current.delete_reason = command.reason
        current.version += 1
        current.updated_at = context.occurred_at
        await self.session.flush()
        await self.session.refresh(current)
        after = _model_json(current)
        if lifecycle_reversal:
            after["lifecycle_reversal"] = lifecycle_reversal
        return _Outcome.success("case_progress", command.progress_id, before, after)

    async def _reverse_lifecycle_patch_for_delete(
        self,
        *,
        current: CaseProgress,
        context: BusinessCommandContext,
    ) -> dict[str, Any]:
        state = await self.session.scalar(
            select(CaseLifecycleState)
            .where(
                CaseLifecycleState.tenant_id == context.tenant_id,
                CaseLifecycleState.case_id == current.case_id,
            )
            .with_for_update()
        )
        if state is None or state.last_progress_id != current.progress_id:
            return {}

        receipt = await self.session.scalar(
            select(BusinessCommandReceipt)
            .where(
                BusinessCommandReceipt.tenant_id == context.tenant_id,
                BusinessCommandReceipt.command_type == "create_case_progress",
                BusinessCommandReceipt.actor_user_id == current.reporter_id,
                BusinessCommandReceipt.status == "executed",
                BusinessCommandReceipt.actual_write.is_(True),
                BusinessCommandReceipt.resource_type == "case_progress",
                BusinessCommandReceipt.resource_id == str(current.progress_id),
            )
            .order_by(BusinessCommandReceipt.created_at.desc())
            .limit(1)
        )
        transition = (
            dict((receipt.after_json or {}).get("lifecycle_transition") or {})
            if receipt is not None
            else {}
        )
        if not transition:
            # Compatibility repair for progress records written before lifecycle
            # transition snapshots were added.  Only remove a lifecycle row when
            # it is provably the row created by this exact progress.  Any other
            # legacy shape fails closed instead of guessing a prior state.
            if (
                state.version == 1
                and state.created_at == current.created_at
                and state.last_progress_id == current.progress_id
            ):
                state_id = str(state.lifecycle_state_id)
                await self.session.delete(state)
                return {
                    "action": "deleted_created_state",
                    "lifecycle_state_id": state_id,
                    "compatibility_repair": True,
                }
            raise BusinessCommandError(
                "case_lifecycle_reversal_unavailable",
                "domain_policy",
                "lifecycle transition snapshot is unavailable",
            )

        if bool(transition.get("created")):
            state_id = str(state.lifecycle_state_id)
            await self.session.delete(state)
            return {
                "action": "deleted_created_state",
                "lifecycle_state_id": state_id,
                "compatibility_repair": False,
            }

        prior = transition.get("before")
        if not isinstance(prior, dict) or not prior:
            raise BusinessCommandError(
                "case_lifecycle_reversal_invalid",
                "domain_policy",
                "prior lifecycle snapshot is invalid",
            )
        last_progress_id = prior.get("last_progress_id")
        try:
            restored_last_progress_id = (
                UUID(str(last_progress_id)) if last_progress_id else None
            )
        except (TypeError, ValueError) as exc:
            raise BusinessCommandError(
                "case_lifecycle_reversal_invalid",
                "domain_policy",
                "prior lifecycle progress reference is invalid",
            ) from exc
        state.stage = str(prior.get("stage") or "")
        state.node = str(prior.get("node") or "")
        state.current_status = str(prior.get("current_status") or "")
        state.next_actions_json = [
            str(value) for value in prior.get("next_actions_json") or []
        ]
        state.hearing_readiness = str(prior.get("hearing_readiness") or "")
        state.blocking_issues_json = [
            str(value) for value in prior.get("blocking_issues_json") or []
        ]
        state.last_progress_id = restored_last_progress_id
        state.version += 1
        state.updated_at = context.occurred_at
        return {
            "action": "restored_prior_state",
            "lifecycle_state_id": str(state.lifecycle_state_id),
            "restored_last_progress_id": str(restored_last_progress_id or ""),
        }

    async def _query_progress(
        self, command: QueryCaseProgress, context: BusinessCommandContext
    ) -> "_Outcome":
        case_id = await self._assert_case_access(command.case_id, context)
        statement = select(CaseProgress).where(
            CaseProgress.tenant_id == context.tenant_id,
            CaseProgress.case_id == case_id,
            CaseProgress.deleted_at.is_(None),
        )
        if command.start_at is not None:
            statement = statement.where(CaseProgress.occurred_at >= command.start_at)
        if command.end_at is not None:
            statement = statement.where(CaseProgress.occurred_at <= command.end_at)
        rows = (
            await self.session.scalars(
                statement.order_by(CaseProgress.occurred_at.desc())
            )
        ).all()
        return _Outcome(
            status="executed",
            resource_type="case_progress_query",
            resource_id=command.case_id,
            after={"items": [_model_json(item) for item in rows]},
            actual_write=False,
        )

    async def _list_assigned_cases(
        self,
        command: ListAssignedCases,
        context: BusinessCommandContext,
    ) -> "_Outcome":
        allowed_case_ids = tuple(
            UUID(value)
            for value in context.allowed_case_ids
            if _is_uuid(value)
        )
        if not allowed_case_ids:
            return _Outcome(
                status="executed",
                resource_type="assigned_case_inventory",
                resource_id=context.actor_user_id,
                after={"case_count": 0, "cases": []},
                actual_write=False,
            )
        statement = (
            select(Agent2Case, CaseLifecycleState)
            .outerjoin(
                CaseLifecycleState,
                (CaseLifecycleState.tenant_id == Agent2Case.tenant_id)
                & (CaseLifecycleState.case_id == Agent2Case.case_id)
                & (CaseLifecycleState.assigned_user_id == context.actor_user_id),
            )
            .where(
                Agent2Case.tenant_id == context.tenant_id,
                Agent2Case.owner_user_id == context.actor_user_id,
                Agent2Case.case_id.in_(allowed_case_ids),
            )
        )
        if not command.include_closed:
            statement = statement.where(Agent2Case.status != "closed")
        rows = (
            await self.session.execute(
                statement.order_by(
                    Agent2Case.case_type,
                    Agent2Case.case_number,
                    Agent2Case.case_name,
                )
            )
        ).all()
        cases = [
            {
                "case_id": str(case.case_id),
                "case_number": case.case_number,
                "case_name": case.case_name,
                "case_type": case.case_type,
                "status": case.status,
                "stage": lifecycle.stage if lifecycle is not None else "",
                "node": lifecycle.node if lifecycle is not None else "",
            }
            for case, lifecycle in rows
        ]
        return _Outcome(
            status="executed",
            resource_type="assigned_case_inventory",
            resource_id=context.actor_user_id,
            after={"case_count": len(cases), "cases": cases},
            actual_write=False,
        )

    async def _query_operation_status(
        self,
        command: QueryOperationStatus,
        context: BusinessCommandContext,
    ) -> "_Outcome":
        if not context.conversation_id:
            raise BusinessCommandError(
                "operation_status_conversation_required",
                "authorization",
                "operation status requires an exact conversation",
            )
        if command.domain == "travel":
            return await self._query_travel_collaboration_status(context)
        rows = list(
            (
                await self.session.scalars(
                    select(Agent2OperationOutcome)
                    .where(
                        Agent2OperationOutcome.tenant_id == context.tenant_id,
                        Agent2OperationOutcome.user_id == context.actor_user_id,
                        Agent2OperationOutcome.conversation_id == context.conversation_id,
                        Agent2OperationOutcome.source_turn_id
                        != context.source_message_id,
                    )
                    .order_by(Agent2OperationOutcome.created_at.desc())
                    .limit(50)
                )
            ).all()
        )
        latest_turn_id = str(rows[0].source_turn_id) if rows else ""
        latest = tuple(
            item for item in rows if str(item.source_turn_id) == latest_turn_id
        )
        answer = _operation_status_answer(command.domain, latest)
        return _Outcome(
            status="executed",
            resource_type="operation_status_query",
            resource_id=context.actor_user_id,
            after={
                "requested_domain": command.domain,
                "answer": answer,
            },
            actual_write=False,
        )

    async def _query_travel_collaboration_status(
        self,
        context: BusinessCommandContext,
    ) -> "_Outcome":
        candidates = list(
            (
                await self.session.scalars(
                    select(TravelCollaborationCandidate)
                    .where(
                        TravelCollaborationCandidate.tenant_id == context.tenant_id,
                        TravelCollaborationCandidate.company_id == context.company_id,
                        TravelCollaborationCandidate.department_id == context.department_id,
                        TravelCollaborationCandidate.team_id == context.team_id,
                        TravelCollaborationCandidate.participant_ids.contains(
                            [context.actor_user_id]
                        ),
                        TravelCollaborationCandidate.expires_at >= context.occurred_at,
                        TravelCollaborationCandidate.status.notin_(
                            ("expired", "cancelled")
                        ),
                    )
                    .order_by(TravelCollaborationCandidate.created_at.desc())
                    .limit(5)
                )
            ).all()
        )
        candidate_ids = tuple(item.candidate_id for item in candidates)
        notifications = (
            list(
                (
                    await self.session.scalars(
                        select(NotificationOutbox)
                        .where(
                            NotificationOutbox.tenant_id == context.tenant_id,
                            NotificationOutbox.candidate_id.in_(candidate_ids),
                            NotificationOutbox.message_type
                            == "travel_collaboration_question",
                        )
                        .order_by(NotificationOutbox.created_at.desc())
                    )
                ).all()
            )
            if candidate_ids
            else []
        )
        answer = _travel_collaboration_status_answer(candidates, notifications)
        return _Outcome(
            status="executed",
            resource_type="operation_status_query",
            resource_id=context.actor_user_id,
            after={
                "requested_domain": "travel",
                "answer": answer,
            },
            actual_write=False,
        )

    async def _query_party_cases(
        self, command: QueryPartyCases, context: BusinessCommandContext
    ) -> "_Outcome":
        if command.match_basis not in {
            "exact_identifier",
            "exact_canonical_name",
            "confirmed_alias",
        }:
            raise BusinessCommandError(
                "party_match_unconfirmed",
                "domain_policy",
                "party query requires an exact confirmed resolution",
            )
        party_id = _parse_uuid(command.party_id, "party_not_found")
        allowed_case_ids = tuple(
            UUID(value)
            for value in context.allowed_case_ids
            if _is_uuid(value)
        )
        if not allowed_case_ids:
            raise BusinessCommandError("party_not_found", "authorization", "no visible cases")
        party = await self.session.scalar(
            select(PartyEntity).where(
                PartyEntity.party_id == party_id,
                PartyEntity.tenant_id == context.tenant_id,
            )
        )
        if party is None:
            raise BusinessCommandError("party_not_found", "authorization", "party not in tenant")
        statement = (
            select(PartyCaseRole, Agent2Case)
            .join(
                Agent2Case,
                (Agent2Case.tenant_id == PartyCaseRole.tenant_id)
                & (Agent2Case.case_id == PartyCaseRole.case_id),
            )
            .where(
                PartyCaseRole.tenant_id == context.tenant_id,
                PartyCaseRole.party_id == party_id,
                PartyCaseRole.case_id.in_(allowed_case_ids),
                PartyCaseRole.confirmation_status == "confirmed",
            )
        )
        if command.role_type:
            statement = statement.where(PartyCaseRole.role_type == command.role_type)
        rows = (await self.session.execute(statement.order_by(Agent2Case.updated_at.desc()))).all()
        if not rows:
            raise BusinessCommandError("party_not_visible", "authorization", "party has no visible matching cases")
        case_ids = tuple(dict.fromkeys(case.case_id for _role, case in rows))
        related_party_id = sql_case(
            (
                PartyRelation.from_party_id == party_id,
                PartyRelation.to_party_id,
            ),
            else_=PartyRelation.from_party_id,
        )
        relation_rows = (
            await self.session.execute(
                select(PartyRelation, PartyEntity)
                .join(
                    PartyEntity,
                    (PartyEntity.tenant_id == PartyRelation.tenant_id)
                    & (PartyEntity.party_id == related_party_id),
                )
                .join(
                    PartyCaseRole,
                    (PartyCaseRole.tenant_id == PartyEntity.tenant_id)
                    & (PartyCaseRole.party_id == PartyEntity.party_id)
                    & (PartyCaseRole.case_id == PartyRelation.case_id),
                )
                .where(
                    PartyRelation.tenant_id == context.tenant_id,
                    or_(
                        PartyRelation.from_party_id == party_id,
                        PartyRelation.to_party_id == party_id,
                    ),
                    PartyRelation.confirmation_status == "confirmed",
                    PartyRelation.case_id.in_(case_ids),
                    PartyCaseRole.case_id.in_(case_ids),
                    PartyCaseRole.confirmation_status == "confirmed",
                )
                .order_by(PartyRelation.relation_type, PartyEntity.canonical_name)
            )
        ).all()
        clue_rows = list(
            (
                await self.session.scalars(
                    select(PartyCaseClue)
                    .where(
                        PartyCaseClue.tenant_id == context.tenant_id,
                        PartyCaseClue.party_id == party_id,
                        PartyCaseClue.case_id.in_(case_ids),
                        PartyCaseClue.confirmation_status == "confirmed",
                    )
                    .order_by(
                        PartyCaseClue.clue_type,
                        PartyCaseClue.occurred_at.desc().nullslast(),
                        PartyCaseClue.clue_id,
                    )
                )
            ).all()
        )
        progress_rows: list[CaseProgress] = []
        if command.include_recent_progress:
            progress_rows = list(
                (
                    await self.session.scalars(
                        select(CaseProgress)
                        .where(
                            CaseProgress.tenant_id == context.tenant_id,
                            CaseProgress.case_id.in_(case_ids),
                            CaseProgress.deleted_at.is_(None),
                        )
                        .order_by(CaseProgress.occurred_at.desc())
                        .limit(50)
                    )
                ).all()
            )
        case_by_id: dict[str, dict[str, Any]] = {}
        for role, case in rows:
            item = case_by_id.setdefault(
                str(case.case_id),
                {
                    "case_id": str(case.case_id),
                    "external_case_id": case.external_case_id,
                    "case_number": case.case_number,
                    "case_name": case.case_name,
                    "case_type": case.case_type,
                    "status": case.status,
                    "role_types": [],
                    "role_sources": [],
                },
            )
            if role.role_type not in item["role_types"]:
                item["role_types"].append(role.role_type)
                item["role_sources"].append(_json_value(role.source_reference))
        case_items = list(case_by_id.values())
        for item in case_items:
            item["role_type"] = item["role_types"][0] if len(item["role_types"]) == 1 else ",".join(item["role_types"])
        status_counts: dict[str, int] = {}
        for item in case_items:
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
        visible_sources = [
            {
                "case_id": item["case_id"],
                "source_reference": source,
            }
            for item in case_items
            for source in item["role_sources"]
        ]
        relations: list[dict[str, Any]] = []
        seen_relation_ids: set[str] = set()
        for relation, related_party in relation_rows:
            relation_id = str(relation.relation_id)
            if relation_id in seen_relation_ids:
                continue
            seen_relation_ids.add(relation_id)
            relations.append(
                {
                    "relation_id": relation_id,
                    "case_id": str(relation.case_id),
                    "relation_type": relation.relation_type,
                    "direction": "outbound" if relation.from_party_id == party_id else "inbound",
                    "related_party": {
                        "party_id": str(related_party.party_id),
                        "party_type": related_party.party_type,
                        "canonical_name": related_party.canonical_name,
                    },
                    "source_reference": _json_value(relation.source_reference),
                }
            )
        business_clues = [
            {
                "clue_id": str(clue.clue_id),
                "case_id": str(clue.case_id),
                "clue_type": clue.clue_type,
                "label": clue.label,
                "summary": clue.summary,
                "amount": str(clue.amount) if clue.amount is not None else None,
                "currency": clue.currency,
                "occurred_at": clue.occurred_at.isoformat() if clue.occurred_at else None,
                "source_type": clue.source_type,
                "source_id": clue.source_id,
                "source_field": clue.source_field,
                "source_reference": _json_value(clue.source_reference),
            }
            for clue in clue_rows
        ]
        return _Outcome(
            status="executed",
            resource_type="party_case_query",
            resource_id=command.party_id,
            after={
                "party": {
                    "party_id": str(party.party_id),
                    "party_type": party.party_type,
                    "canonical_name": party.canonical_name,
                    "short_name": party.short_name,
                    "former_names": list(party.former_names or []),
                    "legal_representative": party.legal_representative,
                    "status": party.status,
                    "registered_address": party.registered_address,
                    "data_quality": party.data_quality,
                },
                "match_basis": command.match_basis,
                "case_count": len(case_items),
                "status_counts": status_counts,
                "cases": case_items,
                "source_references": visible_sources,
                "relations": relations,
                "business_clues": business_clues,
                "clue_counts": {
                    clue_type: sum(1 for clue in business_clues if clue["clue_type"] == clue_type)
                    for clue_type in ("person", "court", "payment", "asset", "document", "other")
                },
                "recent_progress": [_model_json(item) for item in progress_rows],
            },
            actual_write=False,
        )

    async def _link_progress(
        self, command: LinkCaseProgress, context: BusinessCommandContext
    ) -> "_Outcome":
        current = await self._progress_for_write(command.progress_id, command.expected_version, context)
        before = _model_json(current)
        current.related_party_ids = _merge(current.related_party_ids, command.related_party_ids)
        current.related_document_ids = _merge(current.related_document_ids, command.related_document_ids)
        current.related_travel_intent_ids = _merge(
            current.related_travel_intent_ids, command.related_travel_intent_ids
        )
        current.version += 1
        current.updated_at = context.occurred_at
        await self.session.flush()
        await self.session.refresh(current)
        return _Outcome.success("case_progress", command.progress_id, before, _model_json(current))

    async def _progress_for_write(
        self, progress_id: str, expected_version: int, context: BusinessCommandContext
    ) -> CaseProgress:
        progress_uuid = _parse_uuid(progress_id, "progress_not_found")
        current = await self.session.scalar(
            select(CaseProgress)
            .where(
                CaseProgress.progress_id == progress_uuid,
                CaseProgress.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if current is None:
            raise BusinessCommandError("progress_not_found", "authorization", "progress not found")
        await self._assert_case_access(str(current.case_id), context)
        if current.reporter_id != context.actor_user_id and "case_progress_admin" not in context.actor_role_ids:
            raise BusinessCommandError("progress_forbidden", "authorization", "progress owned by another reporter")
        if current.version != expected_version:
            raise BusinessCommandError("version_conflict", "optimistic_lock", "progress version conflict")
        if current.deleted_at is not None:
            raise BusinessCommandError("progress_deleted", "domain_policy", "progress is deleted")
        return current

    async def _assert_case_access(
        self, case_id: str, context: BusinessCommandContext
    ) -> UUID:
        if case_id not in set(context.allowed_case_ids):
            raise BusinessCommandError("case_forbidden", "authorization", "case outside permission scope")
        case_uuid = _parse_uuid(case_id, "case_not_found")
        visible = await self.session.scalar(
            select(Agent2Case.case_id).where(
                Agent2Case.case_id == case_uuid,
                Agent2Case.tenant_id == context.tenant_id,
            )
        )
        if visible is None:
            raise BusinessCommandError("case_not_found", "authorization", "case not in tenant")
        return case_uuid


class _Outcome:
    def __init__(
        self,
        *,
        status: str,
        resource_type: str = "",
        resource_id: str = "",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        error_code: str | None = None,
        failed_stage: str | None = None,
        actual_write: bool = False,
    ):
        self.status = status
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.before = before or {}
        self.after = after or {}
        self.error_code = error_code
        self.failed_stage = failed_stage
        self.actual_write = actual_write

    @classmethod
    def success(
        cls,
        resource_type: str,
        resource_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> "_Outcome":
        return cls(
            status="executed",
            resource_type=resource_type,
            resource_id=resource_id,
            before=before,
            after=after,
            actual_write=True,
        )


def _operation_status_answer(domain: str, rows: tuple[Any, ...]) -> str:
    if not rows:
        return "没有查到上一条可核验的操作回执，我不能说已经处理成功。"
    if domain == "case_progress":
        case_rows = [item for item in rows if str(item.domain) == "case_progress"]
        successful = next(
            (
                item
                for item in case_rows
                if bool(item.actual_write)
                and str(item.business_status)
                in {"succeeded", "duplicate", "registered"}
            ),
            None,
        )
        if successful is not None:
            snapshot = (
                successful.user_visible_snapshot_json
                if isinstance(successful.user_visible_snapshot_json, dict)
                else {}
            )
            case_name = str(snapshot.get("case_name") or "该案件").strip()
            content = str(snapshot.get("content") or "").strip()
            detail = f"：{case_name}，{content}" if content else f"：{case_name}"
            return f"是的，上一条已进入案件进展{detail}。"
        if case_rows:
            return "没有。上一条案件进展没有形成成功写入回执。"
        if any(str(item.domain) == "report" and bool(item.actual_write) for item in rows):
            return "没有。上一条只更新了日报，没有创建案件进展。"
        return "没有。上一条没有创建案件进展。"
    travel_rows = [item for item in rows if str(item.domain) == "travel"]
    if not travel_rows:
        return "目前没有查到可核验的出差协同结果。"
    latest = travel_rows[0]
    status = str(latest.business_status or "")
    snapshot = (
        latest.user_visible_snapshot_json
        if isinstance(latest.user_visible_snapshot_json, dict)
        else {}
    )
    destination = str(snapshot.get("destination") or "该地点").strip()
    status_text = {
        "matched": "已找到同期同地协同候选",
        "queued": "协同通知已排队，尚未发送",
        "accepted_by_provider": "钉钉接口已受理通知，但不能据此确认送达",
        "waiting_for_reply": "正在等待对方回复",
        "accepted_by_one_party": "已有一方接受，仍在等待另一方",
        "accepted_by_both": "双方都已接受协同",
        "declined": "协同已被拒绝",
        "failed": "协同通知发送失败",
    }.get(status, "已有出差记录，但没有查到有效协同状态")
    return f"{destination}：{status_text}。"


def _travel_collaboration_status_answer(
    candidates: list[Any],
    notifications: list[Any],
) -> str:
    if not candidates:
        return "目前没有查到有效的出差协同候选。"
    notifications_by_candidate: dict[str, list[Any]] = {}
    for item in notifications:
        notifications_by_candidate.setdefault(str(item.candidate_id or ""), []).append(item)
    lines: list[str] = []
    for candidate in candidates:
        status = str(candidate.status or "")
        date_label = (
            f"{candidate.overlap_start.month}月{candidate.overlap_start.day}日"
            if candidate.overlap_start is not None
            else ""
        )
        prefix = f"{candidate.destination}{f'（{date_label}）' if date_label else ''}"
        if status == "accepted":
            detail = "双方都已明确接受协同"
        elif status == "accepted_by_one":
            detail = "已有一方接受，仍在等待另一方回复"
        elif status == "declined":
            detail = "协同已被拒绝"
        else:
            transport = notifications_by_candidate.get(str(candidate.candidate_id), [])
            if any(
                str(item.status or "") == "sent"
                and str(item.external_message_id or "").strip()
                for item in transport
            ):
                detail = "已找到同期同地协同候选；钉钉接口已受理通知，但尚无可靠送达证据或对方回复"
            elif any(str(item.status or "") == "processing" for item in transport):
                detail = "已找到同期同地协同候选；通知正在发送，尚无平台受理结果"
            elif any(str(item.status or "") == "pending" for item in transport):
                detail = "已找到同期同地协同候选；通知已进入发送队列"
            elif any(str(item.status or "") in {"failed", "dead_letter"} for item in transport):
                detail = "已找到同期同地协同候选；通知发送失败"
            elif any(str(item.status or "") == "sent" for item in transport):
                detail = "已找到同期同地协同候选；缺少平台消息 ID，不能确认发送成功"
            else:
                detail = "已找到同期同地协同候选；尚未查到可核验的通知发送结果"
        lines.append(f"{prefix}：{detail}。")
    return "有。" + "\n".join(lines)


def _stable_uuid(namespace: str, *parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, ":".join((namespace, *parts)))


def _parse_uuid(value: str, error_code: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError) as exc:
        raise BusinessCommandError(error_code, "authorization", "resource not found") from exc


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _merge(left: list[str] | None, right: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys((*(left or []), *right)))


def _followup_policy_snapshot_json(
    snapshot: CaseFollowupPolicySnapshot,
) -> dict[str, Any]:
    return {
        "policy_id": snapshot.policy_id,
        "tenant_id": snapshot.tenant_id,
        "case_id": snapshot.case_id,
        "assigned_user_id": snapshot.assigned_user_id,
        "enabled": snapshot.enabled,
        "policy_source": snapshot.policy_source,
        "cadence_type": snapshot.cadence_type,
        "custom_interval_days": snapshot.custom_interval_days,
        "timezone": snapshot.timezone,
        "business_days_only": snapshot.business_days_only,
        "hearing_reminders_enabled": snapshot.hearing_reminders_enabled,
        "stage_transition_enabled": snapshot.stage_transition_enabled,
        "node_transition_enabled": snapshot.node_transition_enabled,
        "last_meaningful_progress_at": (
            snapshot.last_meaningful_progress_at.isoformat()
            if snapshot.last_meaningful_progress_at else None
        ),
        "next_due_at": snapshot.next_due_at.isoformat() if snapshot.next_due_at else None,
        "snoozed_until": snapshot.snoozed_until.isoformat() if snapshot.snoozed_until else None,
        "version": snapshot.version,
    }


def _model_json(model: Any) -> dict[str, Any]:
    return {
        column.name: _json_value(getattr(model, column.name))
        for column in model.__table__.columns
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _receipt_from_existing_ingress(
    model: BusinessCommandReceipt,
    *,
    command: BusinessCommand,
    context: BusinessCommandContext,
) -> BusinessReceipt:
    same_scope = (
        model.tenant_id == context.tenant_id
        and model.actor_user_id == context.actor_user_id
        and model.source_message_id == context.source_message_id
        and model.command_type == command.command_type
    )
    if not same_scope:
        return BusinessReceipt(
            receipt_id=str(model.receipt_id),
            command_id=command.command_id,
            command_type=command.command_type,
            tenant_id=context.tenant_id,
            actor_user_id=context.actor_user_id,
            source_message_id=context.source_message_id,
            idempotency_key="",
            status="blocked",
            resource_type="",
            resource_id="",
            before={},
            after={},
            error_code="idempotency_scope_conflict",
            failed_stage="idempotency",
            actual_write=False,
            created_at=model.created_at,
        )
    if model.status in {"executed", "duplicate"}:
        return _receipt_from_model(model, duplicate=True)
    if model.status in {"blocked", "failed"}:
        return _receipt_from_model(model)
    return BusinessReceipt(
        receipt_id=str(model.receipt_id),
        command_id=model.command_id,
        command_type=model.command_type,
        tenant_id=model.tenant_id,
        actor_user_id=model.actor_user_id,
        source_message_id=model.source_message_id,
        idempotency_key=model.idempotency_key,
        status="blocked",
        resource_type="",
        resource_id="",
        before={},
        after={},
        error_code="idempotency_in_progress",
        failed_stage="idempotency",
        actual_write=False,
        created_at=model.created_at,
    )


def _blocked_existing_ingress(
    model: BusinessCommandReceipt,
    *,
    command: BusinessCommand,
    context: BusinessCommandContext,
    error_code: str,
    failed_stage: str = "admission",
) -> BusinessReceipt:
    return BusinessReceipt(
        receipt_id=str(model.receipt_id),
        command_id=command.command_id,
        command_type=command.command_type,
        tenant_id=context.tenant_id,
        actor_user_id=context.actor_user_id,
        source_message_id=context.source_message_id,
        idempotency_key="",
        status="blocked",
        resource_type="",
        resource_id="",
        before={},
        after={},
        error_code=error_code,
        failed_stage=failed_stage,
        actual_write=False,
        created_at=model.created_at,
    )


def _receipt_from_model(model: BusinessCommandReceipt, *, duplicate: bool = False) -> BusinessReceipt:
    return BusinessReceipt(
        receipt_id=str(model.receipt_id),
        command_id=model.command_id,
        command_type=model.command_type,
        tenant_id=model.tenant_id,
        actor_user_id=model.actor_user_id,
        source_message_id=model.source_message_id,
        idempotency_key=model.idempotency_key,
        status="duplicate" if duplicate else model.status,  # type: ignore[arg-type]
        resource_type=model.resource_type,
        resource_id=model.resource_id,
        before=dict(model.before_json or {}),
        after=dict(model.after_json or {}),
        error_code=model.error_code or None,
        failed_stage=model.failed_stage or None,
        actual_write=False if duplicate else model.actual_write,
        created_at=model.created_at,
    )
