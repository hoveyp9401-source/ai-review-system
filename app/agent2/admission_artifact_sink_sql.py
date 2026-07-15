from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, insert

from app.agent2.admission_contracts import (
    ADMISSION_CONTRACT_VERSION,
    AdmissionDecision,
    AdmissionTicket,
    AdmissionTrace,
    DeferredSemanticEvent,
    InformationPending,
    SemanticReviewItem,
)
from app.agent2.turn_runtime import AdmissionArtifactPersistenceRequest


_metadata = MetaData()

_traces = Table(
    "agent2_semantic_admission_traces",
    _metadata,
    Column("trace_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("source_turn_id", String(256), nullable=False),
    Column("source_message_id", String(256), nullable=False),
    Column("expected_conversation_state_version", Integer, nullable=False),
    Column("proposal_sha256", String(64), nullable=False),
    Column("contract_version", String(128), nullable=False),
    Column("policy_version", String(128), nullable=False),
    Column("admission_mode", String(16), nullable=False),
    Column("trace_status", String(32), nullable=False),
    Column("admission_summary", String(32), nullable=False),
    Column("failure_reason", String(2048), nullable=False),
    Column("idempotency_key", String(512), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

_decisions = Table(
    "agent2_semantic_admission_decisions",
    _metadata,
    Column("decision_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("trace_id", PG_UUID(as_uuid=True), nullable=False),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("source_turn_id", String(256), nullable=False),
    Column("source_message_id", String(256), nullable=False),
    Column("action_id", String(256), nullable=False),
    Column("segment_id", String(256), nullable=False),
    Column("segment_text_sha256", String(64), nullable=False),
    Column("segment_start_offset", Integer, nullable=False),
    Column("segment_end_offset", Integer, nullable=False),
    Column("domain", String(32), nullable=False),
    Column("operation", String(128), nullable=False),
    Column("object_type", String(128)),
    Column("object_stable_id", String(512)),
    Column("object_version", Integer),
    Column("object_label", String(512)),
    Column("expected_conversation_state_version", Integer, nullable=False),
    Column("verdict", String(32), nullable=False),
    Column("reason_code", String(128), nullable=False),
    Column("evidence_refs_json", JSONB, nullable=False),
    Column("ticket_id", PG_UUID(as_uuid=True)),
    Column("pending_id", PG_UUID(as_uuid=True)),
    Column("idempotency_key", String(512), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

_tickets = Table(
    "agent2_semantic_admission_tickets",
    _metadata,
    Column("ticket_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("trace_id", PG_UUID(as_uuid=True), nullable=False),
    Column("decision_id", PG_UUID(as_uuid=True), nullable=False),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("source_turn_id", String(256), nullable=False),
    Column("source_message_id", String(256), nullable=False),
    Column("action_id", String(256), nullable=False),
    Column("segment_id", String(256), nullable=False),
    Column("segment_text_sha256", String(64), nullable=False),
    Column("segment_start_offset", Integer, nullable=False),
    Column("segment_end_offset", Integer, nullable=False),
    Column("domain", String(32), nullable=False),
    Column("operation", String(128), nullable=False),
    Column("object_type", String(128), nullable=False),
    Column("object_stable_id", String(512), nullable=False),
    Column("object_version", Integer),
    Column("object_label", String(512)),
    Column("expected_conversation_state_version", Integer, nullable=False),
    Column("authority_scope_json", JSONB, nullable=False),
    Column("allowed_changed_fields_json", JSONB, nullable=False),
    Column("fact_claims_sha256", String(64), nullable=False),
    Column("authorized_command_sha256", String(64), nullable=False),
    Column("policy_version", String(128), nullable=False),
    Column("ticket_status", String(32), nullable=False),
    Column("contract_version", String(128), nullable=False),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("ttl_seconds", Integer, nullable=False),
    Column("executor_revalidation_required", Boolean, nullable=False),
    Column("proves_business_write", Boolean, nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    Column("consumed_receipt_ref", String(512)),
    Column("invalidation_reason", String(2048), nullable=False),
    Column("idempotency_key", String(512), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

_information_pendings = Table(
    "agent2_information_pendings",
    _metadata,
    Column("pending_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("pending_type", String(32), nullable=False),
    Column("trace_id", PG_UUID(as_uuid=True), nullable=False),
    Column("decision_id", PG_UUID(as_uuid=True), nullable=False),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("source_turn_id", String(256), nullable=False),
    Column("source_message_id", String(256), nullable=False),
    Column("segment_id", String(256), nullable=False),
    Column("segment_text_sha256", String(64), nullable=False),
    Column("segment_start_offset", Integer, nullable=False),
    Column("segment_end_offset", Integer, nullable=False),
    Column("domain", String(32), nullable=False),
    Column("operation", String(128), nullable=False),
    Column("object_type", String(128)),
    Column("object_stable_id", String(512)),
    Column("object_version", Integer),
    Column("object_label", String(512)),
    Column("expected_conversation_state_version", Integer, nullable=False),
    Column("missing_fields_json", JSONB, nullable=False),
    Column("question_snapshot_json", JSONB, nullable=False),
    Column("acceptable_answer_forms_json", JSONB, nullable=False),
    Column("pending_status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("ttl_seconds", Integer, nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    Column("consumed_by_trace_id", PG_UUID(as_uuid=True)),
    Column("invalidation_reason", String(2048), nullable=False),
    Column("business_write_allowed", Boolean, nullable=False),
    Column("idempotency_key", String(512), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

_semantic_review_items = Table(
    "agent2_semantic_review_items",
    _metadata,
    Column("review_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("trace_id", PG_UUID(as_uuid=True), nullable=False),
    Column("decision_id", PG_UUID(as_uuid=True)),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("source_turn_id", String(256), nullable=False),
    Column("source_message_id", String(256), nullable=False),
    Column("segment_id", String(256), nullable=False),
    Column("segment_text_sha256", String(64), nullable=False),
    Column("segment_start_offset", Integer, nullable=False),
    Column("segment_end_offset", Integer, nullable=False),
    Column("domain", String(32), nullable=False),
    Column("operation", String(128), nullable=False),
    Column("object_type", String(128)),
    Column("object_stable_id", String(512)),
    Column("object_version", Integer),
    Column("object_label", String(512)),
    Column("reason_code", String(128), nullable=False),
    Column("review_status", String(32), nullable=False),
    Column("candidate_snapshot_json", JSONB, nullable=False),
    Column("resolution_json", JSONB, nullable=False),
    Column("reviewed_by", String(128), nullable=False),
    Column("audit_only", Boolean, nullable=False),
    Column("business_write_allowed", Boolean, nullable=False),
    Column("idempotency_key", String(512), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True)),
)

_deferred_semantic_events = Table(
    "agent2_deferred_semantic_events",
    _metadata,
    Column("deferred_event_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("trace_id", PG_UUID(as_uuid=True), nullable=False),
    Column("decision_id", PG_UUID(as_uuid=True)),
    Column("tenant_id", String(128), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(256), nullable=False),
    Column("source_turn_id", String(256), nullable=False),
    Column("source_message_id", String(256), nullable=False),
    Column("segment_id", String(256), nullable=False),
    Column("segment_text_sha256", String(64), nullable=False),
    Column("segment_start_offset", Integer, nullable=False),
    Column("segment_end_offset", Integer, nullable=False),
    Column("domain", String(32), nullable=False),
    Column("operation", String(128), nullable=False),
    Column("object_type", String(128)),
    Column("object_stable_id", String(512)),
    Column("object_version", Integer),
    Column("object_label", String(512)),
    Column("reason_code", String(128), nullable=False),
    Column("event_status", String(32), nullable=False),
    Column("payload_json", JSONB, nullable=False),
    Column("not_before", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("audit_only", Boolean, nullable=False),
    Column("business_write_allowed", Boolean, nullable=False),
    Column("requires_fresh_admission", Boolean, nullable=False),
    Column("idempotency_key", String(512), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

_READ_ONLY_ADMITTED_OPERATIONS = frozenset(
    {
        "query_daily_report",
        "query_periodic_report",
        "answer_case_query",
        "query_case_progress",
        "query_operation_status",
        "search_enterprise_knowledge",
    }
)
_REVIEW_STATUSES = frozenset(
    {"pending_human_review", "resolved", "dismissed", "expired"}
)
_DEFERRED_EVENT_STATUSES = frozenset(
    {"recorded", "superseded", "reviewed", "cancelled", "expired"}
)


class AdmissionArtifactPersistenceError(ValueError):
    """The issuance artifact set is unsafe or collides with persisted truth."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class SqlAdmissionArtifactSink:
    """Issue an exact Admission artifact set in the caller's transaction.

    The sink never consumes a Ticket.  Enforced artifacts become executable
    issuance state; Shadow Tickets and Pendings are retained for audit only and
    are forced into a cancelled state so no Executor can acquire them.
    """

    async def persist(self, request: AdmissionArtifactPersistenceRequest) -> None:
        artifact_set = _validate_artifact_set(request)
        async with request.session.begin_nested():
            await _insert_exact(
                request.session,
                _traces,
                _trace_values(
                    artifact_set.trace,
                    admission_mode=request.admission_mode,
                ),
            )
            # Decision -> Ticket/Pending references are deferred by the schema;
            # Ticket/Pending -> Decision references are immediate, so this order
            # is mandatory.
            for decision in artifact_set.decisions:
                await _insert_exact(
                    request.session,
                    _decisions,
                    _decision_values(decision),
                )
            for ticket in artifact_set.tickets:
                await _insert_exact(
                    request.session,
                    _tickets,
                    _ticket_values(ticket, admission_mode=request.admission_mode),
                )
            for pending in artifact_set.information_pendings:
                await _insert_exact(
                    request.session,
                    _information_pendings,
                    _pending_values(
                        pending,
                        admission_mode=request.admission_mode,
                    ),
                )
            for review in artifact_set.review_items:
                await _insert_exact(
                    request.session,
                    _semantic_review_items,
                    _review_values(review),
                )
            for deferred in artifact_set.deferred_events:
                await _insert_exact(
                    request.session,
                    _deferred_semantic_events,
                    _deferred_values(deferred),
                )
            await request.session.flush()


class _ArtifactSet:
    def __init__(
        self,
        *,
        trace: AdmissionTrace,
        decisions: tuple[AdmissionDecision, ...],
        tickets: tuple[AdmissionTicket, ...],
        information_pendings: tuple[InformationPending, ...],
        review_items: tuple[SemanticReviewItem, ...],
        deferred_events: tuple[DeferredSemanticEvent, ...],
    ) -> None:
        self.trace = trace
        self.decisions = decisions
        self.tickets = tickets
        self.information_pendings = information_pendings
        self.review_items = review_items
        self.deferred_events = deferred_events


def _validate_artifact_set(
    request: AdmissionArtifactPersistenceRequest,
) -> _ArtifactSet:
    if request.admission_mode not in {"shadow", "enforced"}:
        raise AdmissionArtifactPersistenceError("artifact_mode_invalid")
    if not isinstance(request.trace, AdmissionTrace):
        raise AdmissionArtifactPersistenceError("artifact_trace_required")
    trace = request.trace
    decisions = tuple(trace.decisions)
    tickets = tuple(request.tickets)
    pendings = tuple(request.information_pendings)
    reviews = tuple(getattr(request, "review_items", ()) or ())
    deferred_events = tuple(getattr(request, "deferred_events", ()) or ())
    if not all(isinstance(item, AdmissionDecision) for item in decisions):
        raise AdmissionArtifactPersistenceError("artifact_decision_invalid")
    if not all(isinstance(item, AdmissionTicket) for item in tickets):
        raise AdmissionArtifactPersistenceError("artifact_ticket_invalid")
    if not all(isinstance(item, InformationPending) for item in pendings):
        raise AdmissionArtifactPersistenceError("artifact_pending_invalid")
    if not all(isinstance(item, SemanticReviewItem) for item in reviews):
        raise AdmissionArtifactPersistenceError("artifact_review_invalid")
    if not all(isinstance(item, DeferredSemanticEvent) for item in deferred_events):
        raise AdmissionArtifactPersistenceError("artifact_deferred_invalid")
    if reviews and getattr(request, "review_capture_enabled", False) is not True:
        raise AdmissionArtifactPersistenceError("artifact_review_capture_disabled")
    if (
        deferred_events
        and getattr(request, "deferred_capture_enabled", False) is not True
    ):
        raise AdmissionArtifactPersistenceError("artifact_deferred_capture_disabled")

    envelope = request.decision
    if (
        getattr(envelope, "admission_trace", None) != trace
        or tuple(getattr(envelope, "admission_tickets", ()) or ()) != tickets
        or tuple(
            getattr(envelope, "admission_information_pendings", ()) or ()
        )
        != pendings
        or tuple(getattr(envelope, "admission_review_items", ()) or ()) != reviews
        or tuple(getattr(envelope, "admission_deferred_events", ()) or ())
        != deferred_events
        or str(getattr(envelope, "admission_mode", "") or "")
        != request.admission_mode
    ):
        raise AdmissionArtifactPersistenceError("artifact_envelope_mismatch")

    trusted_scope = (
        request.tenant_id,
        request.user_id,
        request.conversation_id,
        request.source_message_id,
    )
    if _artifact_scope(trace) != trusted_scope:
        raise AdmissionArtifactPersistenceError("artifact_scope_mismatch")
    if trace.source_turn_id != request.source_message_id:
        raise AdmissionArtifactPersistenceError("artifact_scope_mismatch")
    if trace.contract_version != ADMISSION_CONTRACT_VERSION:
        raise AdmissionArtifactPersistenceError("artifact_contract_mismatch")

    _require_unique((decision.decision_id for decision in decisions), "decision")
    _require_unique((ticket.ticket_id for ticket in tickets), "ticket")
    _require_unique((pending.pending_id for pending in pendings), "pending")
    _require_unique((review.review_id for review in reviews), "review")
    _require_unique(
        (event.deferred_event_id for event in deferred_events),
        "deferred",
    )
    decisions_by_id = {decision.decision_id: decision for decision in decisions}
    tickets_by_id = {ticket.ticket_id: ticket for ticket in tickets}
    pendings_by_id = {pending.pending_id: pending for pending in pendings}

    for decision in decisions:
        if (
            _artifact_scope(decision) != trusted_scope
            or decision.source_turn_id != request.source_message_id
            or decision.trace_id != trace.trace_id
        ):
            raise AdmissionArtifactPersistenceError("artifact_scope_mismatch")
        if decision.status == "admitted":
            if not decision.object_ref:
                raise AdmissionArtifactPersistenceError(
                    "artifact_decision_link_invalid"
                )
            if decision.operation in _READ_ONLY_ADMITTED_OPERATIONS:
                if decision.ticket_id or decision.pending_id:
                    raise AdmissionArtifactPersistenceError(
                        "artifact_decision_link_invalid"
                    )
            elif (
                not decision.ticket_id
                or decision.ticket_id not in tickets_by_id
                or decision.pending_id
            ):
                raise AdmissionArtifactPersistenceError(
                    "artifact_decision_link_invalid"
                )
        elif decision.status == "information_required":
            if (
                decision.ticket_id
                or not decision.pending_id
                or decision.pending_id not in pendings_by_id
            ):
                raise AdmissionArtifactPersistenceError(
                    "artifact_decision_link_invalid"
                )
        elif decision.ticket_id or decision.pending_id:
            raise AdmissionArtifactPersistenceError("artifact_decision_link_invalid")

    linked_ticket_ids = {
        decision.ticket_id for decision in decisions if decision.ticket_id
    }
    linked_pending_ids = {
        decision.pending_id for decision in decisions if decision.pending_id
    }
    if linked_ticket_ids != set(tickets_by_id) or linked_pending_ids != set(
        pendings_by_id
    ):
        raise AdmissionArtifactPersistenceError("artifact_orphan_invalid")

    for ticket in tickets:
        decision = decisions_by_id.get(ticket.decision_id)
        if (
            decision is None
            or decision.ticket_id != ticket.ticket_id
            or ticket.trace_id != trace.trace_id
            or _artifact_scope(ticket) != trusted_scope
            or ticket.source_turn_id != request.source_message_id
        ):
            raise AdmissionArtifactPersistenceError("artifact_ticket_link_invalid")
        if (
            ticket.ticket_status != "issued"
            or ticket.consumed_receipt_ref
            or not ticket.executor_revalidation_required
            or ticket.proves_business_write
        ):
            raise AdmissionArtifactPersistenceError("artifact_ticket_not_issuable")
        if ticket.contract_version != ADMISSION_CONTRACT_VERSION:
            raise AdmissionArtifactPersistenceError("artifact_contract_mismatch")

    for pending in pendings:
        decision = decisions_by_id.get(pending.decision_id)
        if (
            decision is None
            or decision.pending_id != pending.pending_id
            or pending.trace_id != trace.trace_id
            or _artifact_scope(pending) != trusted_scope
            or pending.source_turn_id != request.source_message_id
        ):
            raise AdmissionArtifactPersistenceError("artifact_pending_link_invalid")
        if (
            pending.pending_status not in {"active", "awaiting_input"}
            or pending.consumed_by_trace_id
            or pending.business_write_allowed
        ):
            raise AdmissionArtifactPersistenceError("artifact_pending_not_issuable")

    for review in reviews:
        decision = decisions_by_id.get(review.decision_id)
        if (
            decision is None
            or review.trace_id != trace.trace_id
            or _artifact_scope(review) != trusted_scope
            or review.source_turn_id != request.source_message_id
        ):
            raise AdmissionArtifactPersistenceError("artifact_review_scope_mismatch")
        if not _artifact_matches_decision(review, decision):
            raise AdmissionArtifactPersistenceError("artifact_review_link_invalid")
        if request.admission_mode != "shadow" and decision.status != "review_only":
            raise AdmissionArtifactPersistenceError("artifact_review_verdict_invalid")
        if review.review_status not in _REVIEW_STATUSES:
            raise AdmissionArtifactPersistenceError("artifact_review_status_invalid")
        if review.audit_only is not True:
            raise AdmissionArtifactPersistenceError("artifact_review_not_audit_only")
        if review.business_write_allowed is not False:
            raise AdmissionArtifactPersistenceError(
                "artifact_review_can_authorize_write"
            )

    for deferred in deferred_events:
        decision = decisions_by_id.get(deferred.decision_id)
        if (
            decision is None
            or deferred.trace_id != trace.trace_id
            or _artifact_scope(deferred) != trusted_scope
            or deferred.source_turn_id != request.source_message_id
        ):
            raise AdmissionArtifactPersistenceError("artifact_deferred_scope_mismatch")
        if not _artifact_matches_decision(deferred, decision):
            raise AdmissionArtifactPersistenceError("artifact_deferred_link_invalid")
        if decision.status != "deferred_audit_only":
            raise AdmissionArtifactPersistenceError("artifact_deferred_verdict_invalid")
        if deferred.event_status not in _DEFERRED_EVENT_STATUSES:
            raise AdmissionArtifactPersistenceError("artifact_deferred_status_invalid")
        if deferred.audit_only is not True:
            raise AdmissionArtifactPersistenceError("artifact_deferred_not_audit_only")
        if deferred.business_write_allowed is not False:
            raise AdmissionArtifactPersistenceError(
                "artifact_deferred_can_authorize_write"
            )
        if deferred.requires_fresh_admission is not True:
            raise AdmissionArtifactPersistenceError(
                "artifact_deferred_requires_fresh_admission"
            )
        if (
            deferred.not_before is not None
            and deferred.expires_at is not None
            and deferred.expires_at <= deferred.not_before
        ):
            raise AdmissionArtifactPersistenceError("artifact_deferred_window_invalid")

    return _ArtifactSet(
        trace=trace,
        decisions=decisions,
        tickets=tickets,
        information_pendings=pendings,
        review_items=reviews,
        deferred_events=deferred_events,
    )


def _artifact_scope(artifact: Any) -> tuple[str, str, str, str]:
    return (
        str(getattr(artifact, "tenant_id", "") or ""),
        str(getattr(artifact, "user_id", "") or ""),
        str(getattr(artifact, "conversation_id", "") or ""),
        str(getattr(artifact, "source_message_id", "") or ""),
    )


def _artifact_matches_decision(
    artifact: SemanticReviewItem | DeferredSemanticEvent,
    decision: AdmissionDecision,
) -> bool:
    candidate = (
        artifact.candidate_snapshot
        if isinstance(artifact, SemanticReviewItem)
        else artifact.payload
    )
    expected_candidate = {
        "action_id": decision.action_id,
        "decision_verdict": decision.status,
        "evidence_refs": decision.evidence_refs,
    }
    return (
        artifact.decision_id == decision.decision_id
        and artifact.segment_id == decision.segment_id
        and artifact.segment_text_sha256 == decision.segment_text_sha256
        and artifact.segment_start_offset == decision.segment_start_offset
        and artifact.segment_end_offset == decision.segment_end_offset
        and artifact.domain == decision.domain
        and artifact.operation == decision.operation
        and _canonical(_plain(artifact.object_ref))
        == _canonical(_plain(decision.object_ref))
        and _canonical(_plain(candidate))
        == _canonical(_plain(expected_candidate))
    )


def _require_unique(values: Any, artifact_type: str) -> None:
    normalized = tuple(str(value or "") for value in values)
    if not all(normalized) or len(normalized) != len(set(normalized)):
        raise AdmissionArtifactPersistenceError(
            "artifact_identifier_invalid", artifact_type
        )


def _trace_values(
    trace: AdmissionTrace,
    *,
    admission_mode: str,
) -> dict[str, Any]:
    if trace.created_at is None:
        raise AdmissionArtifactPersistenceError("artifact_timestamp_invalid")
    return {
        "trace_id": _uuid(trace.trace_id),
        "tenant_id": trace.tenant_id,
        "user_id": trace.user_id,
        "conversation_id": trace.conversation_id,
        "source_turn_id": trace.source_turn_id,
        "source_message_id": trace.source_message_id,
        "expected_conversation_state_version": (
            trace.expected_conversation_state_version
        ),
        "proposal_sha256": trace.proposal_sha256,
        "contract_version": trace.contract_version,
        "policy_version": trace.policy_version,
        "admission_mode": admission_mode,
        "trace_status": trace.trace_status,
        "admission_summary": trace.admission_summary,
        "failure_reason": trace.failure_reason,
        "idempotency_key": trace.idempotency_key,
        "created_at": _aware(trace.created_at),
    }


def _decision_values(decision: AdmissionDecision) -> dict[str, Any]:
    if decision.created_at is None:
        raise AdmissionArtifactPersistenceError("artifact_timestamp_invalid")
    return {
        "decision_id": _uuid(decision.decision_id),
        "trace_id": _uuid(decision.trace_id),
        "tenant_id": decision.tenant_id,
        "user_id": decision.user_id,
        "conversation_id": decision.conversation_id,
        "source_turn_id": decision.source_turn_id,
        "source_message_id": decision.source_message_id,
        "action_id": decision.action_id,
        "segment_id": decision.segment_id,
        "segment_text_sha256": decision.segment_text_sha256,
        "segment_start_offset": decision.segment_start_offset,
        "segment_end_offset": decision.segment_end_offset,
        "domain": decision.domain,
        "operation": decision.operation,
        **_object_values(decision.object_ref),
        "expected_conversation_state_version": (
            decision.expected_conversation_state_version
        ),
        "verdict": decision.status,
        "reason_code": decision.reason_code,
        "evidence_refs_json": list(decision.evidence_refs),
        "ticket_id": _optional_uuid(decision.ticket_id),
        "pending_id": _optional_uuid(decision.pending_id),
        "idempotency_key": decision.idempotency_key,
        "created_at": _aware(decision.created_at),
    }


def _ticket_values(
    ticket: AdmissionTicket,
    *,
    admission_mode: str,
) -> dict[str, Any]:
    shadow = admission_mode == "shadow"
    return {
        "ticket_id": _uuid(ticket.ticket_id),
        "trace_id": _uuid(ticket.trace_id),
        "decision_id": _uuid(ticket.decision_id),
        "tenant_id": ticket.tenant_id,
        "user_id": ticket.user_id,
        "conversation_id": ticket.conversation_id,
        "source_turn_id": ticket.source_turn_id,
        "source_message_id": ticket.source_message_id,
        "action_id": ticket.action_id,
        "segment_id": ticket.segment_id,
        "segment_text_sha256": ticket.segment_text_sha256,
        "segment_start_offset": ticket.segment_start_offset,
        "segment_end_offset": ticket.segment_end_offset,
        "domain": ticket.domain,
        "operation": ticket.operation,
        **_object_values(ticket.object_ref, required=True),
        "expected_conversation_state_version": (
            ticket.expected_conversation_state_version
        ),
        "authority_scope_json": _plain(ticket.authority_scope),
        "allowed_changed_fields_json": list(ticket.allowed_changed_fields),
        "fact_claims_sha256": ticket.fact_claims_sha256,
        "authorized_command_sha256": ticket.authorized_command_sha256,
        "policy_version": ticket.policy_version,
        "ticket_status": "cancelled" if shadow else "issued",
        "contract_version": ticket.contract_version,
        "issued_at": _aware(ticket.issued_at),
        "expires_at": _aware(ticket.expires_at),
        "ttl_seconds": ticket.ttl_seconds,
        "executor_revalidation_required": True,
        "proves_business_write": False,
        "consumed_at": None,
        "consumed_receipt_ref": None,
        "invalidation_reason": (
            "shadow_artifact_non_executable" if shadow else ""
        ),
        "idempotency_key": ticket.idempotency_key,
        "created_at": _aware(ticket.issued_at),
        "updated_at": _aware(ticket.issued_at),
    }


def _pending_values(
    pending: InformationPending,
    *,
    admission_mode: str,
) -> dict[str, Any]:
    shadow = admission_mode == "shadow"
    return {
        "pending_id": _uuid(pending.pending_id),
        "pending_type": pending.pending_type,
        "trace_id": _uuid(pending.trace_id),
        "decision_id": _uuid(pending.decision_id),
        "tenant_id": pending.tenant_id,
        "user_id": pending.user_id,
        "conversation_id": pending.conversation_id,
        "source_turn_id": pending.source_turn_id,
        "source_message_id": pending.source_message_id,
        "segment_id": pending.segment_id,
        "segment_text_sha256": pending.segment_text_sha256,
        "segment_start_offset": pending.segment_start_offset,
        "segment_end_offset": pending.segment_end_offset,
        "domain": pending.domain,
        "operation": pending.operation,
        **_object_values(pending.object_ref),
        "expected_conversation_state_version": (
            pending.expected_conversation_state_version
        ),
        "missing_fields_json": list(pending.missing_fields),
        "question_snapshot_json": _plain(pending.question_snapshot),
        "acceptable_answer_forms_json": _plain(
            pending.acceptable_answer_forms
        ),
        "pending_status": "cancelled" if shadow else "awaiting_input",
        "created_at": _aware(pending.created_at),
        "expires_at": _aware(pending.expires_at),
        "ttl_seconds": pending.ttl_seconds,
        "consumed_at": None,
        "consumed_by_trace_id": None,
        "invalidation_reason": (
            "shadow_artifact_non_executable" if shadow else ""
        ),
        "business_write_allowed": False,
        "idempotency_key": pending.idempotency_key,
        "updated_at": _aware(pending.created_at),
    }


def _review_values(review: SemanticReviewItem) -> dict[str, Any]:
    return {
        "review_id": _uuid(review.review_id),
        "trace_id": _uuid(review.trace_id),
        "decision_id": _uuid(review.decision_id),
        "tenant_id": review.tenant_id,
        "user_id": review.user_id,
        "conversation_id": review.conversation_id,
        "source_turn_id": review.source_turn_id,
        "source_message_id": review.source_message_id,
        "segment_id": review.segment_id,
        "segment_text_sha256": review.segment_text_sha256,
        "segment_start_offset": review.segment_start_offset,
        "segment_end_offset": review.segment_end_offset,
        "domain": review.domain,
        "operation": review.operation,
        **_object_values(review.object_ref),
        "reason_code": review.reason_code,
        "review_status": review.review_status,
        "candidate_snapshot_json": _plain(review.candidate_snapshot),
        "resolution_json": _plain(review.resolution),
        "reviewed_by": "",
        "audit_only": True,
        "business_write_allowed": False,
        "idempotency_key": review.idempotency_key,
        "created_at": _aware(review.created_at),
        "resolved_at": _optional_aware(review.resolved_at),
    }


def _deferred_values(deferred: DeferredSemanticEvent) -> dict[str, Any]:
    created_at = _aware(deferred.created_at)
    return {
        "deferred_event_id": _uuid(deferred.deferred_event_id),
        "trace_id": _uuid(deferred.trace_id),
        "decision_id": _uuid(deferred.decision_id),
        "tenant_id": deferred.tenant_id,
        "user_id": deferred.user_id,
        "conversation_id": deferred.conversation_id,
        "source_turn_id": deferred.source_turn_id,
        "source_message_id": deferred.source_message_id,
        "segment_id": deferred.segment_id,
        "segment_text_sha256": deferred.segment_text_sha256,
        "segment_start_offset": deferred.segment_start_offset,
        "segment_end_offset": deferred.segment_end_offset,
        "domain": deferred.domain,
        "operation": deferred.operation,
        **_object_values(deferred.object_ref),
        "reason_code": deferred.reason_code,
        "event_status": deferred.event_status,
        "payload_json": _plain(deferred.payload),
        "not_before": _optional_aware(deferred.not_before),
        "expires_at": _optional_aware(deferred.expires_at),
        "audit_only": True,
        "business_write_allowed": False,
        "requires_fresh_admission": True,
        "idempotency_key": deferred.idempotency_key,
        "created_at": created_at,
        "updated_at": created_at,
    }


def _object_values(
    object_ref: Mapping[str, Any] | None,
    *,
    required: bool = False,
) -> dict[str, Any]:
    if object_ref is None:
        if required:
            raise AdmissionArtifactPersistenceError("artifact_object_invalid")
        return {
            "object_type": None,
            "object_stable_id": None,
            "object_version": None,
            "object_label": None,
        }
    object_type = str(object_ref.get("object_type") or "").strip()
    stable_id = str(object_ref.get("stable_id") or "").strip()
    if not object_type or not stable_id:
        raise AdmissionArtifactPersistenceError("artifact_object_invalid")
    version = object_ref.get("version")
    if version is not None:
        try:
            version = int(version)
        except (TypeError, ValueError) as exc:
            raise AdmissionArtifactPersistenceError(
                "artifact_object_invalid"
            ) from exc
        if version < 0:
            raise AdmissionArtifactPersistenceError("artifact_object_invalid")
    label = object_ref.get("label")
    return {
        "object_type": object_type,
        "object_stable_id": stable_id,
        "object_version": version,
        "object_label": str(label) if label is not None else None,
    }


async def _insert_exact(session: Any, table: Table, values: dict[str, Any]) -> None:
    await session.execute(insert(table).values(**values).on_conflict_do_nothing())
    primary_key = next(iter(table.primary_key.columns))
    result = await session.execute(
        select(table)
        .where(
            table.c.tenant_id == values["tenant_id"],
            primary_key == values[primary_key.name],
        )
        .with_for_update()
    )
    persisted = result.mappings().one_or_none()
    actual = (
        {key: persisted.get(key) for key in values}
        if persisted is not None
        else None
    )
    if actual is None or _canonical(actual) != _canonical(values):
        raise AdmissionArtifactPersistenceError(
            "artifact_idempotency_collision", table.name
        )


def _uuid(value: Any) -> UUID:
    try:
        return UUID(str(value or ""))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AdmissionArtifactPersistenceError(
            "artifact_identifier_invalid"
        ) from exc


def _optional_uuid(value: Any) -> UUID | None:
    return _uuid(value) if str(value or "").strip() else None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AdmissionArtifactPersistenceError("artifact_timestamp_invalid")
    return value


def _optional_aware(value: datetime | None) -> datetime | None:
    return _aware(value) if value is not None else None


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, datetime):
        # PostgreSQL TIMESTAMPTZ preserves an instant, not the caller's original
        # UTC offset.  Normalize aware datetimes before comparing an INSERT
        # round-trip so 09:00+08:00 and 01:00+00:00 are not treated as an
        # idempotency collision.
        if value.tzinfo is not None:
            return value.astimezone(UTC).isoformat()
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


__all__ = [
    "AdmissionArtifactPersistenceError",
    "SqlAdmissionArtifactSink",
]
