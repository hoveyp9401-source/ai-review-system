from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.agent2.business.case_progress import CaseRecord
from app.agent2.business.admission import require_business_execution_admission
from app.agent2.admission_store import AdmissionTicketStore
from app.agent2.business.contracts import (
    BUSINESS_COMMAND_TYPES,
    BusinessAuditEntry,
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
    RespondTravelCollaboration,
    SnoozeCaseFollowup,
    UpdateCaseProgress,
    UpdateTravelIntent,
    business_command_fingerprint,
    normalize_travel_fact_text,
    travel_intent_fact_fingerprint,
)
from app.agent2.case_followup_commands import (
    TriggerCaseFollowupNow,
    UpdateCaseFollowupPolicy,
    execute_policy_update,
)
from app.agent2.case_lifecycle_followup import CaseFollowupPolicySnapshot


@dataclass(frozen=True)
class TravelIntentRecord:
    travel_intent_id: str
    tenant_id: str
    company_id: str
    department_id: str
    team_id: str
    user_id: str
    destination_raw: str
    destination_normalized: str
    city_code: str
    province_code: str
    start_at: datetime
    end_at: datetime
    time_precision: str
    purpose_summary: str
    related_case_ids: tuple[str, ...]
    source_message_id: str
    source_channel: str
    status: str
    confidence: float
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CaseProgressRecord:
    progress_id: str
    tenant_id: str
    case_id: str
    occurred_at: datetime
    recorded_at: datetime
    reporter_id: str
    progress_type: str
    summary: str
    details: str
    source_message_id: str
    source_channel: str
    content_origin: str
    related_party_ids: tuple[str, ...]
    related_document_ids: tuple[str, ...]
    related_travel_intent_ids: tuple[str, ...]
    confidence: float
    confirmation_status: str
    version: int
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    deleted_by: str = ""
    delete_reason: str = ""


@dataclass
class TravelCollaborationRecord:
    candidate_id: str
    tenant_id: str
    travel_intent_ids: tuple[str, ...]
    participant_ids: tuple[str, ...]
    destination: str
    overlap_start: datetime
    overlap_end: datetime
    match_reason: str
    match_score: float
    status: str
    notification_ids: tuple[str, ...]
    responses: dict[str, str]
    created_at: datetime
    expires_at: datetime
    version: int = 1


@dataclass(frozen=True)
class TravelNotificationRecord:
    notification_id: str
    tenant_id: str
    candidate_id: str
    recipient_user_id: str
    message_text: str
    idempotency_key: str
    status: str
    retry_count: int
    created_at: datetime
    sent_at: datetime | None = None
    error_message: str = ""


class InMemoryBusinessExecutor:
    """Transactional reference executor for deterministic domain behavior.

    Production adapters must preserve this interface and its fail-closed semantics.
    """

    def __init__(
        self,
        *,
        cases: tuple[CaseRecord, ...] = (),
        admission_ticket_store: AdmissionTicketStore | None = None,
    ):
        self.cases = {item.case_id: item for item in cases}
        self.travel_intents_by_id: dict[str, TravelIntentRecord] = {}
        self.case_progress: dict[str, CaseProgressRecord] = {}
        self.travel_candidates: dict[str, TravelCollaborationRecord] = {}
        self.notifications: dict[str, TravelNotificationRecord] = {}
        self.audit_log: list[BusinessAuditEntry] = []
        self.followup_policies: dict[str, CaseFollowupPolicySnapshot] = {}
        self.followup_tasks: dict[str, dict[str, Any]] = {}
        self._receipts_by_key: dict[str, BusinessReceipt] = {}
        self._lock = threading.RLock()
        self._admission_ticket_store = admission_ticket_store

    @property
    def travel_intents(self) -> tuple[TravelIntentRecord, ...]:
        return tuple(self.travel_intents_by_id.values())

    def execute(self, command: BusinessCommand, context: BusinessCommandContext) -> BusinessReceipt:
        if not isinstance(command, BUSINESS_COMMAND_TYPES):
            raise TypeError("typed business command only; raw text is forbidden")
        key = self._idempotency_key(command, context)
        with self._lock:
            existing = self._receipts_by_key.get(key)
            if existing is not None:
                return replace(existing, status="duplicate", actual_write=False)
            try:
                require_business_execution_admission(command, context)
                if context.admission_required:
                    if self._admission_ticket_store is None:
                        raise BusinessCommandError(
                            "admission_ticket_repository_required",
                            "admission",
                            "enforced mutation requires an authoritative ticket store",
                        )
                    with self._admission_ticket_store.acquire(
                        context.admission_ticket
                    ) as lease:
                        receipt = self._dispatch(command, context, key)
                        if receipt.status in {"executed", "duplicate"}:
                            lease.consume(receipt.receipt_id)
                        return receipt
                return self._dispatch(command, context, key)
            except BusinessCommandError as exc:
                receipt = self._receipt(
                    command=command,
                    context=context,
                    idempotency_key=key,
                    status="blocked",
                    resource_type="",
                    resource_id="",
                    before={},
                    after={},
                    error_code=exc.code,
                    failed_stage=exc.stage,
                    actual_write=False,
                )
                self._receipts_by_key[key] = receipt
                return receipt

    def seed_travel_candidate(
        self,
        *,
        tenant_id: str,
        candidate_id: str,
        travel_intent_ids: tuple[str, ...],
        participant_ids: tuple[str, ...],
        destination: str,
        overlap_start: datetime,
        overlap_end: datetime,
    ) -> TravelCollaborationRecord:
        record = TravelCollaborationRecord(
            candidate_id=candidate_id,
            tenant_id=tenant_id,
            travel_intent_ids=travel_intent_ids,
            participant_ids=participant_ids,
            destination=destination,
            overlap_start=overlap_start,
            overlap_end=overlap_end,
            match_reason="same_city_and_overlapping_date",
            match_score=1.0,
            status="candidate",
            notification_ids=(),
            responses={},
            created_at=overlap_start,
            expires_at=overlap_end + timedelta(days=1),
        )
        self.travel_candidates[candidate_id] = record
        return record

    def dispatch_travel_notifications(self, candidate_id: str) -> tuple[TravelNotificationRecord, ...]:
        with self._lock:
            candidate = self.travel_candidates.get(candidate_id)
            if candidate is None:
                raise LookupError("travel collaboration candidate not found")
            if candidate.notification_ids:
                return tuple(self.notifications[item] for item in candidate.notification_ids)
            if candidate.status in {"accepted", "declined", "cancelled", "expired"}:
                return ()
            created: list[TravelNotificationRecord] = []
            for participant in candidate.participant_ids:
                peers = [item for item in candidate.participant_ids if item != participant]
                peer_text = "、".join(peers)
                notification_id = _stable_id("travel-notification", candidate_id, participant)
                record = TravelNotificationRecord(
                    notification_id=notification_id,
                    tenant_id=candidate.tenant_id,
                    candidate_id=candidate_id,
                    recipient_user_id=participant,
                    message_text=(
                        f"你计划前往{candidate.destination}，{peer_text}同期也有行程，是否需要协同安排？"
                    ),
                    idempotency_key=f"travel-collaboration:{candidate_id}:{participant}",
                    status="pending",
                    retry_count=0,
                    created_at=candidate.created_at,
                )
                self.notifications[notification_id] = record
                created.append(record)
            candidate.notification_ids = tuple(item.notification_id for item in created)
            candidate.status = "notified"
            candidate.version += 1
            return tuple(created)

    def _dispatch(
        self, command: BusinessCommand, context: BusinessCommandContext, key: str
    ) -> BusinessReceipt:
        if isinstance(command, CreateTravelIntent):
            return self._create_travel(command, context, key)
        if isinstance(command, UpdateTravelIntent):
            return self._update_travel(command, context, key)
        if isinstance(command, RespondTravelCollaboration):
            return self._respond_travel(command, context, key)
        if isinstance(command, CreateCaseProgress):
            return self._create_progress(command, context, key)
        if isinstance(command, SnoozeCaseFollowup):
            return self._snooze_followup(command, context, key)
        if isinstance(command, UpdateCaseProgress):
            return self._update_progress(command, context, key)
        if isinstance(command, DeleteCaseProgress):
            return self._delete_progress(command, context, key)
        if isinstance(command, QueryCaseProgress):
            return self._query_progress(command, context, key)
        if isinstance(command, ListAssignedCases):
            return self._list_assigned_cases(command, context, key)
        if isinstance(command, QueryOperationStatus):
            return self._success(
                command,
                context,
                key,
                "operation_status_query",
                context.actor_user_id,
                {},
                {
                    "requested_domain": command.domain,
                    "answer": "内存参考执行器没有持久化历史回执，无法确认上一条操作状态。",
                },
                False,
            )
        if isinstance(command, LinkCaseProgress):
            return self._link_progress(command, context, key)
        if isinstance(command, UpdateCaseFollowupPolicy):
            return self._update_followup_policy(command, context, key)
        if isinstance(command, TriggerCaseFollowupNow):
            return self._trigger_followup_now(command, context, key)
        raise TypeError("typed business command only")

    def _list_assigned_cases(
        self,
        command: ListAssignedCases,
        context: BusinessCommandContext,
        key: str,
    ) -> BusinessReceipt:
        visible = [
            case
            for case in self.cases.values()
            if case.tenant_id == context.tenant_id
            and case.case_id in context.allowed_case_ids
            and (not case.owner_user_id or case.owner_user_id == context.actor_user_id)
        ]
        visible.sort(key=lambda item: (item.case_number, item.case_name))
        return self._success(
            command,
            context,
            key,
            "assigned_case_inventory",
            context.actor_user_id,
            {},
            {
                "case_count": len(visible),
                "cases": [
                    {
                        "case_id": case.case_id,
                        "case_number": case.case_number,
                        "case_name": case.case_name,
                        "case_type": "",
                        "status": "",
                        "stage": "",
                        "node": "",
                    }
                    for case in visible
                ],
            },
            False,
        )

    def _snooze_followup(
        self,
        command: SnoozeCaseFollowup,
        context: BusinessCommandContext,
        key: str,
    ) -> BusinessReceipt:
        active = [
            item for item in self.followup_tasks.values()
            if item.get("case_id") == command.case_id
            and item.get("task_status") == "waiting_for_reply"
            and item.get("pending_id") == command.pending_id
        ]
        if len(active) != 1 or command.case_id not in context.allowed_case_ids:
            raise BusinessCommandError(
                "case_followup_target_needs_clarification", "domain_policy",
                "exactly one bound follow-up pending is required",
            )
        before = dict(active[0])
        active[0].update({
            "task_status": "snoozed", "response_status": "snoozed",
            "snoozed_until": command.snoozed_until.isoformat(),
        })
        return self._receipt(
            command=command, context=context, idempotency_key=key,
            status="executed", resource_type="case_followup_task",
            resource_id=str(active[0].get("followup_id") or ""),
            before=before, after=dict(active[0]), error_code=None,
            failed_stage=None, actual_write=True,
        )

    def _update_followup_policy(
        self,
        command: UpdateCaseFollowupPolicy,
        context: BusinessCommandContext,
        key: str,
    ) -> BusinessReceipt:
        if (
            command.tenant_id != context.tenant_id
            or command.assigned_user_id != context.actor_user_id
            or command.case_id not in context.allowed_case_ids
            or command.case_id not in self.cases
        ):
            raise BusinessCommandError(
                "case_forbidden", "authorization", "case outside permission scope"
            )
        current = self.followup_policies.get(command.case_id)
        if current is None:
            current = CaseFollowupPolicySnapshot(
                policy_id=_stable_id("case-followup-policy", context.tenant_id, command.case_id),
                tenant_id=context.tenant_id, case_id=command.case_id,
                assigned_user_id=context.actor_user_id, enabled=False,
                cadence_type="event_only", timezone="Asia/Shanghai",
                last_meaningful_progress_at=None, next_due_at=None, version=0,
            )
        execution = execute_policy_update(
            current, command, actor_has_case_permission=True, now=context.occurred_at
        )
        if not execution.actual_write:
            raise BusinessCommandError(
                execution.reason_code, "policy", "follow-up policy was not changed"
            )
        self.followup_policies[command.case_id] = execution.after
        return self._receipt(
            command=command, context=context, idempotency_key=key,
            status="executed", resource_type="case_followup_policy",
            resource_id=execution.after.policy_id,
            before=asdict(execution.before), after=asdict(execution.after),
            error_code=None, failed_stage=None, actual_write=True,
        )

    def _trigger_followup_now(
        self,
        command: TriggerCaseFollowupNow,
        context: BusinessCommandContext,
        key: str,
    ) -> BusinessReceipt:
        if (
            command.tenant_id != context.tenant_id
            or command.assigned_user_id != context.actor_user_id
            or command.case_id not in context.allowed_case_ids
            or command.case_id not in self.cases
        ):
            raise BusinessCommandError(
                "case_forbidden", "authorization", "case outside permission scope"
            )
        current_policy = self.followup_policies.get(command.case_id)
        current_policy_version = current_policy.version if current_policy is not None else 0
        if (
            command.expected_policy_version is not None
            and command.expected_policy_version != current_policy_version
        ):
            raise BusinessCommandError(
                "version_conflict", "optimistic_lock", "policy version changed"
            )
        if any(
            item["case_id"] == command.case_id
            and item["task_status"] in {"scheduled", "queued", "sending", "waiting_for_reply"}
            for item in self.followup_tasks.values()
        ):
            raise BusinessCommandError(
                "waiting_for_reply_exists", "followup_policy", "active follow-up exists"
            )
        followup_id = _stable_id(
            "case-followup", context.tenant_id, command.case_id,
            context.source_message_id,
        )
        after = {
            "followup_id": followup_id, "case_id": command.case_id,
            "task_status": "scheduled", "message_status": "scheduled",
            "due_at": context.occurred_at.isoformat(), "version": 1,
        }
        self.followup_tasks[followup_id] = after
        return self._receipt(
            command=command, context=context, idempotency_key=key,
            status="executed", resource_type="case_followup_task",
            resource_id=followup_id, before={}, after=after,
            error_code=None, failed_stage=None, actual_write=True,
        )

    def _create_travel(
        self, command: CreateTravelIntent, context: BusinessCommandContext, key: str
    ) -> BusinessReceipt:
        if not command.city_code or not command.destination_normalized:
            raise BusinessCommandError("travel_location_ambiguous", "domain_policy", "city is required")
        if command.start_at > command.end_at:
            raise BusinessCommandError("travel_time_invalid", "domain_policy", "travel interval is invalid")
        if command.confidence < 0.85:
            raise BusinessCommandError("travel_confidence_too_low", "domain_policy", "travel needs clarification")
        self._assert_allowed_cases(command.related_case_ids, context)
        existing = next(
            (
                item
                for item in self.travel_intents_by_id.values()
                if item.tenant_id == context.tenant_id
                and item.user_id == context.actor_user_id
                and item.status != "cancelled"
                and item.city_code == command.city_code
                and item.start_at == command.start_at
                and item.end_at == command.end_at
                and item.time_precision == command.time_precision
                and normalize_travel_fact_text(item.purpose_summary)
                == normalize_travel_fact_text(command.purpose_summary)
            ),
            None,
        )
        if existing is not None:
            return self._duplicate(
                command,
                context,
                key,
                "travel_intent",
                existing.travel_intent_id,
                _mapping(existing),
            )
        intent_id = _stable_id(
            "travel-intent",
            context.tenant_id,
            context.source_message_id,
            business_command_fingerprint(command),
        )
        if context.admission_required:
            object_ref = context.admission_ticket.get("object_ref")
            if not isinstance(object_ref, dict) or not str(
                object_ref.get("stable_id") or ""
            ).strip():
                raise BusinessCommandError(
                    "admission_ticket_object_mismatch",
                    "admission",
                    "travel Ticket object is missing",
                )
            intent_id = str(object_ref["stable_id"])
        record = TravelIntentRecord(
            travel_intent_id=intent_id,
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
            related_case_ids=command.related_case_ids,
            source_message_id=context.source_message_id,
            source_channel=context.source_channel,
            status="planned",
            confidence=command.confidence,
            version=1,
            created_at=context.occurred_at,
            updated_at=context.occurred_at,
        )
        self.travel_intents_by_id[intent_id] = record
        return self._success(command, context, key, "travel_intent", intent_id, {}, _mapping(record), True)

    def _update_travel(
        self, command: UpdateTravelIntent, context: BusinessCommandContext, key: str
    ) -> BusinessReceipt:
        current = self.travel_intents_by_id.get(command.travel_intent_id)
        if current is None or current.tenant_id != context.tenant_id or current.user_id != context.actor_user_id:
            raise BusinessCommandError("travel_intent_not_found", "authorization", "travel intent not found")
        if current.version != command.expected_version:
            raise BusinessCommandError("version_conflict", "optimistic_lock", "travel version conflict")
        before = _mapping(current)
        updated = replace(
            current,
            start_at=command.start_at or current.start_at,
            end_at=command.end_at or current.end_at,
            destination_normalized=command.destination_normalized or current.destination_normalized,
            city_code=command.city_code or current.city_code,
            status=command.status or "changed",
            version=current.version + 1,
            updated_at=context.occurred_at,
        )
        if updated.start_at > updated.end_at:
            raise BusinessCommandError("travel_time_invalid", "domain_policy", "travel interval is invalid")
        self.travel_intents_by_id[current.travel_intent_id] = updated
        self._invalidate_travel_candidates(current.travel_intent_id)
        return self._success(command, context, key, "travel_intent", current.travel_intent_id, before, _mapping(updated), True)

    def _respond_travel(
        self, command: RespondTravelCollaboration, context: BusinessCommandContext, key: str
    ) -> BusinessReceipt:
        candidate = self.travel_candidates.get(command.candidate_id)
        if candidate is None or candidate.tenant_id != context.tenant_id:
            raise BusinessCommandError("travel_candidate_not_found", "authorization", "candidate not found")
        if context.actor_user_id not in candidate.participant_ids:
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
        before = _mapping(candidate)
        candidate.responses[context.actor_user_id] = command.response
        responses = set(candidate.responses.values())
        if "decline" in responses:
            candidate.status = "declined"
        elif "cancel" in responses or "changed" in responses:
            candidate.status = "cancelled"
        elif all(candidate.responses.get(user_id) == "accept" for user_id in candidate.participant_ids):
            candidate.status = "accepted"
        elif "accept" in responses:
            candidate.status = "accepted_by_one"
        else:
            candidate.status = "notified"
        if candidate.status in {"declined", "cancelled"}:
            self._cancel_candidate_notifications(candidate, reason="travel_candidate_closed")
        if command.response in {"changed", "cancel"}:
            for intent_id in candidate.travel_intent_ids:
                intent = self.travel_intents_by_id.get(intent_id)
                if (
                    intent is not None
                    and intent.tenant_id == context.tenant_id
                    and intent.user_id == context.actor_user_id
                    and intent.status not in {"cancelled", "completed"}
                ):
                    self.travel_intents_by_id[intent_id] = replace(
                        intent,
                        status="cancelled",
                        version=intent.version + 1,
                        updated_at=context.occurred_at,
                    )
                    self._invalidate_travel_candidates(
                        intent_id,
                        exclude_candidate_id=candidate.candidate_id,
                    )
        candidate.version += 1
        return self._success(command, context, key, "travel_collaboration", candidate.candidate_id, before, _mapping(candidate), True)

    def _create_progress(
        self, command: CreateCaseProgress, context: BusinessCommandContext, key: str
    ) -> BusinessReceipt:
        self._assert_case_access(command.case_id, context)
        case = self.cases.get(command.case_id)
        owner_user_id = (
            case.owner_user_id
            if case is not None and case.owner_user_id
            else context.actor_user_id
        )
        if not context.can_create_case_progress(
            command.case_id,
            owner_user_id=owner_user_id,
        ):
            raise BusinessCommandError(
                "case_not_writable",
                "authorization",
                "actor has no case progress create permission",
            )
        if not command.summary.strip():
            raise BusinessCommandError("progress_summary_required", "domain_policy", "summary is required")
        progress_id = _stable_id(
            "case-progress",
            context.tenant_id,
            context.source_message_id,
            business_command_fingerprint(command),
        )
        record = CaseProgressRecord(
            progress_id=progress_id,
            tenant_id=context.tenant_id,
            case_id=command.case_id,
            occurred_at=command.occurred_at,
            recorded_at=context.occurred_at,
            reporter_id=context.actor_user_id,
            progress_type=command.progress_type,
            summary=command.summary.strip(),
            details=command.details.strip(),
            source_message_id=context.source_message_id,
            source_channel=context.source_channel,
            content_origin="human_record",
            related_party_ids=command.related_party_ids,
            related_document_ids=command.related_document_ids,
            related_travel_intent_ids=command.related_travel_intent_ids,
            confidence=command.confidence,
            confirmation_status="confirmed_by_reporter",
            version=1,
            idempotency_key=key,
            created_at=context.occurred_at,
            updated_at=context.occurred_at,
        )
        self.case_progress[progress_id] = record
        return self._success(command, context, key, "case_progress", progress_id, {}, _mapping(record), True)

    def _update_progress(
        self, command: UpdateCaseProgress, context: BusinessCommandContext, key: str
    ) -> BusinessReceipt:
        current = self._progress_for_write(command.progress_id, command.expected_version, context)
        before = _mapping(current)
        updated = replace(
            current,
            summary=command.summary.strip() if command.summary is not None else current.summary,
            details=command.details.strip() if command.details is not None else current.details,
            version=current.version + 1,
            updated_at=context.occurred_at,
        )
        if not updated.summary:
            raise BusinessCommandError("progress_summary_required", "domain_policy", "summary is required")
        self.case_progress[current.progress_id] = updated
        return self._success(command, context, key, "case_progress", current.progress_id, before, _mapping(updated), True)

    def _delete_progress(
        self, command: DeleteCaseProgress, context: BusinessCommandContext, key: str
    ) -> BusinessReceipt:
        current = self._progress_for_write(command.progress_id, command.expected_version, context)
        before = _mapping(current)
        updated = replace(
            current,
            version=current.version + 1,
            updated_at=context.occurred_at,
            deleted_at=context.occurred_at,
            deleted_by=context.actor_user_id,
            delete_reason=command.reason,
        )
        self.case_progress[current.progress_id] = updated
        return self._success(command, context, key, "case_progress", current.progress_id, before, _mapping(updated), True)

    def _query_progress(
        self, command: QueryCaseProgress, context: BusinessCommandContext, key: str
    ) -> BusinessReceipt:
        self._assert_case_access(command.case_id, context)
        records = [
            item
            for item in self.case_progress.values()
            if item.tenant_id == context.tenant_id
            and item.case_id == command.case_id
            and item.deleted_at is None
            and (command.start_at is None or item.occurred_at >= command.start_at)
            and (command.end_at is None or item.occurred_at <= command.end_at)
        ]
        return self._success(
            command,
            context,
            key,
            "case_progress_query",
            command.case_id,
            {},
            {"items": [_mapping(item) for item in records]},
            False,
            audit=False,
        )

    def _link_progress(
        self, command: LinkCaseProgress, context: BusinessCommandContext, key: str
    ) -> BusinessReceipt:
        current = self._progress_for_write(command.progress_id, command.expected_version, context)
        before = _mapping(current)
        updated = replace(
            current,
            related_party_ids=_merge(current.related_party_ids, command.related_party_ids),
            related_document_ids=_merge(current.related_document_ids, command.related_document_ids),
            related_travel_intent_ids=_merge(current.related_travel_intent_ids, command.related_travel_intent_ids),
            version=current.version + 1,
            updated_at=context.occurred_at,
        )
        self.case_progress[current.progress_id] = updated
        return self._success(command, context, key, "case_progress", current.progress_id, before, _mapping(updated), True)

    def _progress_for_write(
        self, progress_id: str, expected_version: int, context: BusinessCommandContext
    ) -> CaseProgressRecord:
        current = self.case_progress.get(progress_id)
        if current is None or current.tenant_id != context.tenant_id:
            raise BusinessCommandError("progress_not_found", "authorization", "progress not found")
        self._assert_case_access(current.case_id, context)
        if current.reporter_id != context.actor_user_id and "case_progress_admin" not in context.actor_role_ids:
            raise BusinessCommandError("progress_forbidden", "authorization", "progress is owned by another reporter")
        if current.version != expected_version:
            raise BusinessCommandError("version_conflict", "optimistic_lock", "progress version conflict")
        if current.deleted_at is not None:
            raise BusinessCommandError("progress_deleted", "domain_policy", "progress is deleted")
        return current

    def _assert_case_access(self, case_id: str, context: BusinessCommandContext) -> None:
        if case_id not in set(context.allowed_case_ids):
            raise BusinessCommandError("case_forbidden", "authorization", "case is outside permission scope")
        case = self.cases.get(case_id)
        if self.cases and (case is None or case.tenant_id != context.tenant_id):
            raise BusinessCommandError("case_not_found", "authorization", "case is not in tenant")

    def _assert_allowed_cases(self, case_ids: tuple[str, ...], context: BusinessCommandContext) -> None:
        for case_id in case_ids:
            self._assert_case_access(case_id, context)

    def _invalidate_travel_candidates(
        self,
        travel_intent_id: str,
        *,
        exclude_candidate_id: str = "",
    ) -> None:
        for candidate in self.travel_candidates.values():
            if (
                candidate.candidate_id != exclude_candidate_id
                and travel_intent_id in candidate.travel_intent_ids
                and candidate.status not in {"declined", "cancelled", "expired"}
            ):
                candidate.status = "cancelled"
                candidate.version += 1
                self._cancel_candidate_notifications(
                    candidate,
                    reason="travel_intent_changed_or_cancelled",
                )

    def _cancel_candidate_notifications(
        self,
        candidate: TravelCollaborationRecord,
        *,
        reason: str,
    ) -> None:
        for notification_id in candidate.notification_ids:
            notification = self.notifications.get(notification_id)
            if notification is not None and notification.status in {"pending", "failed"}:
                self.notifications[notification_id] = replace(
                    notification,
                    status="cancelled",
                    error_message=reason,
                )

    def _success(
        self,
        command: BusinessCommand,
        context: BusinessCommandContext,
        key: str,
        resource_type: str,
        resource_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
        actual_write: bool,
        *,
        audit: bool = True,
    ) -> BusinessReceipt:
        receipt = self._receipt(
            command=command,
            context=context,
            idempotency_key=key,
            status="executed",
            resource_type=resource_type,
            resource_id=resource_id,
            before=before,
            after=after,
            error_code=None,
            failed_stage=None,
            actual_write=actual_write,
        )
        self._receipts_by_key[key] = receipt
        if audit and actual_write:
            self.audit_log.append(
                BusinessAuditEntry(
                    audit_id=_stable_id("business-audit", receipt.receipt_id),
                    receipt_id=receipt.receipt_id,
                    tenant_id=context.tenant_id,
                    actor_user_id=context.actor_user_id,
                    source_message_id=context.source_message_id,
                    source_channel=context.source_channel,
                    command_type=command.command_type,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    before=deepcopy(before),
                    after=deepcopy(after),
                    occurred_at=context.occurred_at,
                )
            )
        return receipt

    def _duplicate(
        self,
        command: BusinessCommand,
        context: BusinessCommandContext,
        key: str,
        resource_type: str,
        resource_id: str,
        after: dict[str, Any],
    ) -> BusinessReceipt:
        receipt = self._receipt(
            command=command,
            context=context,
            idempotency_key=key,
            status="duplicate",
            resource_type=resource_type,
            resource_id=resource_id,
            before={},
            after=after,
            error_code=None,
            failed_stage=None,
            actual_write=False,
        )
        self._receipts_by_key[key] = receipt
        self.audit_log.append(
            BusinessAuditEntry(
                audit_id=_stable_id("business-audit", receipt.receipt_id),
                receipt_id=receipt.receipt_id,
                tenant_id=context.tenant_id,
                actor_user_id=context.actor_user_id,
                source_message_id=context.source_message_id,
                source_channel=context.source_channel,
                command_type=command.command_type,
                resource_type=resource_type,
                resource_id=resource_id,
                before={},
                after=deepcopy(after),
                occurred_at=context.occurred_at,
            )
        )
        return receipt

    def _receipt(
        self,
        *,
        command: BusinessCommand,
        context: BusinessCommandContext,
        idempotency_key: str,
        status: str,
        resource_type: str,
        resource_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
        error_code: str | None,
        failed_stage: str | None,
        actual_write: bool,
    ) -> BusinessReceipt:
        return BusinessReceipt(
            receipt_id=_stable_id("business-receipt", idempotency_key),
            command_id=command.command_id,
            command_type=command.command_type,
            tenant_id=context.tenant_id,
            actor_user_id=context.actor_user_id,
            source_message_id=context.source_message_id,
            idempotency_key=idempotency_key,
            status=status,  # type: ignore[arg-type]
            resource_type=resource_type,
            resource_id=resource_id,
            before=deepcopy(before),
            after=deepcopy(after),
            error_code=error_code,
            failed_stage=failed_stage,
            actual_write=actual_write,
            created_at=context.occurred_at,
        )

    @staticmethod
    def _idempotency_key(command: BusinessCommand, context: BusinessCommandContext) -> str:
        return (
            f"{context.tenant_id}:{context.source_message_id}:"
            f"{business_command_fingerprint(command)}"
        )


def _stable_id(namespace: str, *parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, ":".join((namespace, *parts))))


def _mapping(value: Any) -> dict[str, Any]:
    return deepcopy(asdict(value))


def _merge(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))
