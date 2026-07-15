from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping
from uuid import UUID


ADMISSION_CONTRACT_VERSION = "agent2.domain_admission.v1"
ADMISSION_POLICY_VERSION = "agent2.domain_admission.policy.v1"
INFORMATION_PENDING_STATUSES = frozenset(
    {
        "active",
        "awaiting_input",
        "consumed",
        "expired",
        "cancelled",
        "conflicted",
        "permission_revoked",
    }
)
AdmissionStatus = Literal[
    "admitted",
    "blocked",
    "no_op",
    "information_required",
    "review_only",
    "deferred_audit_only",
]
AdmissionMode = Literal["disabled", "shadow", "enforced"]
SemanticReviewStatus = Literal[
    "pending_human_review", "resolved", "dismissed", "expired"
]
DeferredSemanticEventStatus = Literal[
    "recorded", "superseded", "reviewed", "cancelled", "expired"
]
MutationExecutionAuthority = Literal[
    "semantic_ticket",
    "legacy_user_compatibility",
    "authenticated_admin_command",
    "derived_committed_receipt",
]
MUTATION_EXECUTION_AUTHORITIES = frozenset(
    {
        "semantic_ticket",
        "legacy_user_compatibility",
        "authenticated_admin_command",
        "derived_committed_receipt",
    }
)
SEMANTIC_REVIEW_STATUSES = frozenset(
    {"pending_human_review", "resolved", "dismissed", "expired"}
)
DEFERRED_SEMANTIC_EVENT_STATUSES = frozenset(
    {"recorded", "superseded", "reviewed", "cancelled", "expired"}
)
ADMISSION_DOMAINS = frozenset(
    {"report", "case", "travel", "knowledge", "chat", "runtime"}
)
ADMISSION_STATUSES = frozenset(
    {
        "admitted",
        "blocked",
        "no_op",
        "information_required",
        "review_only",
        "deferred_audit_only",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_AUDIT_ACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def validate_mutation_execution_authority(value: object) -> str:
    authority = str(value or "").strip()
    if authority not in MUTATION_EXECUTION_AUTHORITIES:
        raise ValueError("unknown mutation execution authority")
    return authority


@dataclass(frozen=True)
class AdmissionExecutionScope:
    tenant_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    executed_at: datetime
    conversation_state_version: int | None = None

    def __post_init__(self) -> None:
        if not all(
            str(value or "").strip()
            for value in (
                self.tenant_id,
                self.user_id,
                self.conversation_id,
                self.source_message_id,
            )
        ):
            raise ValueError(
                "admission execution scope requires tenant, user, conversation, and source"
            )
        if self.executed_at.tzinfo is None or self.executed_at.utcoffset() is None:
            raise ValueError("admission execution time must be timezone-aware")
        if self.conversation_state_version is not None and self.conversation_state_version < 0:
            raise ValueError("conversation state version must be non-negative")


@dataclass(frozen=True)
class AdmissionDecision:
    action_id: str
    segment_id: str
    domain: str
    operation: str
    status: AdmissionStatus
    reason_code: str
    ticket_id: str = ""
    decision_id: str = ""
    trace_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    conversation_id: str = ""
    source_turn_id: str = ""
    source_message_id: str = ""
    segment_text_sha256: str = ""
    segment_start_offset: int = 0
    segment_end_offset: int = 0
    object_ref: Mapping[str, Any] | None = None
    expected_conversation_state_version: int = 0
    evidence_refs: tuple[str, ...] = ()
    pending_id: str = ""
    idempotency_key: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.object_ref is not None:
            object.__setattr__(self, "object_ref", _freeze_mapping(self.object_ref))

    @property
    def verdict(self) -> AdmissionStatus:
        return self.status

    def as_dict(self) -> dict[str, Any]:
        if self.created_at is None:
            raise ValueError("persistable admission decision requires created_at")
        return {
            "decision_id": self.decision_id,
            "trace_id": self.trace_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "source_turn_id": self.source_turn_id,
            "source_message_id": self.source_message_id,
            "action_id": self.action_id,
            "segment_id": self.segment_id,
            "segment_text_sha256": self.segment_text_sha256,
            "segment_start_offset": self.segment_start_offset,
            "segment_end_offset": self.segment_end_offset,
            "domain": self.domain,
            "operation": self.operation,
            "object_ref": _thaw(self.object_ref) if self.object_ref is not None else None,
            "expected_conversation_state_version": self.expected_conversation_state_version,
            "verdict": self.status,
            "reason_code": self.reason_code,
            "evidence_refs": list(self.evidence_refs),
            "ticket_id": self.ticket_id or None,
            "pending_id": self.pending_id or None,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class AdmissionTicket:
    ticket_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    action_id: str
    segment_id: str
    domain: str
    operation: str
    object_ref: Mapping[str, Any]
    contract_version: str
    issued_at: datetime
    expires_at: datetime
    idempotency_key: str
    trace_id: str = ""
    decision_id: str = ""
    source_turn_id: str = ""
    segment_text_sha256: str = ""
    segment_start_offset: int = 0
    segment_end_offset: int = 0
    expected_conversation_state_version: int = 0
    authority_scope: Mapping[str, Any] = field(default_factory=dict)
    allowed_changed_fields: tuple[str, ...] = ()
    fact_claims_sha256: str = ""
    authorized_command_sha256: str = ""
    policy_version: str = ADMISSION_POLICY_VERSION
    ticket_status: str = "issued"
    executor_revalidation_required: bool = True
    proves_business_write: bool = False
    consumed_receipt_ref: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "object_ref", _freeze_mapping(self.object_ref))
        object.__setattr__(self, "authority_scope", _freeze_mapping(self.authority_scope))

    @property
    def ttl_seconds(self) -> int:
        return int((self.expires_at - self.issued_at).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "trace_id": self.trace_id,
            "decision_id": self.decision_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "source_turn_id": self.source_turn_id,
            "source_message_id": self.source_message_id,
            "action_id": self.action_id,
            "segment_id": self.segment_id,
            "segment_text_sha256": self.segment_text_sha256,
            "segment_start_offset": self.segment_start_offset,
            "segment_end_offset": self.segment_end_offset,
            "domain": self.domain,
            "operation": self.operation,
            "object_ref": _thaw(self.object_ref),
            "expected_conversation_state_version": self.expected_conversation_state_version,
            "authority_scope": _thaw(self.authority_scope),
            "allowed_changed_fields": list(self.allowed_changed_fields),
            "fact_claims_sha256": self.fact_claims_sha256,
            "authorized_command_sha256": self.authorized_command_sha256,
            "policy_version": self.policy_version,
            "ticket_status": self.ticket_status,
            "contract_version": self.contract_version,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "ttl_seconds": self.ttl_seconds,
            "executor_revalidation_required": self.executor_revalidation_required,
            "proves_business_write": self.proves_business_write,
            "consumed_receipt_ref": self.consumed_receipt_ref or None,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class InformationPending:
    pending_id: str
    trace_id: str
    decision_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    source_turn_id: str
    source_message_id: str
    segment_id: str
    segment_text_sha256: str
    segment_start_offset: int
    segment_end_offset: int
    domain: str
    operation: str
    object_ref: Mapping[str, Any] | None
    expected_conversation_state_version: int
    missing_fields: tuple[str, ...]
    question_snapshot: Mapping[str, Any]
    acceptable_answer_forms: Mapping[str, Any]
    created_at: datetime
    expires_at: datetime
    idempotency_key: str
    pending_type: str = "information"
    pending_status: str = "active"
    consumed_by_trace_id: str = ""
    business_write_allowed: bool = False

    def __post_init__(self) -> None:
        if self.object_ref is not None:
            object.__setattr__(self, "object_ref", _freeze_mapping(self.object_ref))
        object.__setattr__(self, "question_snapshot", _freeze_mapping(self.question_snapshot))
        object.__setattr__(
            self,
            "acceptable_answer_forms",
            _freeze_mapping(self.acceptable_answer_forms),
        )
        if not self.missing_fields:
            raise ValueError("information pending requires at least one missing field")
        if self.pending_type != "information":
            raise ValueError("information pending has an invalid pending type")
        if self.pending_status not in INFORMATION_PENDING_STATUSES:
            raise ValueError("information pending has an unknown status")
        if (self.pending_status == "consumed") != bool(
            self.consumed_by_trace_id.strip()
        ):
            raise ValueError(
                "information pending consumption requires exactly one consuming trace"
            )
        if self.expires_at <= self.created_at:
            raise ValueError("information pending expiry must follow creation")
        if self.business_write_allowed:
            raise ValueError("information pending cannot authorize business writes")

    @property
    def ttl_seconds(self) -> int:
        return int((self.expires_at - self.created_at).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending_id": self.pending_id,
            "pending_type": self.pending_type,
            "trace_id": self.trace_id,
            "decision_id": self.decision_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "source_turn_id": self.source_turn_id,
            "source_message_id": self.source_message_id,
            "segment_id": self.segment_id,
            "segment_text_sha256": self.segment_text_sha256,
            "segment_start_offset": self.segment_start_offset,
            "segment_end_offset": self.segment_end_offset,
            "domain": self.domain,
            "operation": self.operation,
            "object_ref": (
                _thaw(self.object_ref) if self.object_ref is not None else None
            ),
            "expected_conversation_state_version": self.expected_conversation_state_version,
            "missing_fields": list(self.missing_fields),
            "question_snapshot": _thaw(self.question_snapshot),
            "acceptable_answer_forms": _thaw(self.acceptable_answer_forms),
            "pending_status": self.pending_status,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "ttl_seconds": self.ttl_seconds,
            "consumed_by_trace_id": self.consumed_by_trace_id or None,
            "business_write_allowed": self.business_write_allowed,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class TrustedSelectionCandidateRef:
    stable_id: str
    version: int
    label: str

    def __post_init__(self) -> None:
        if not self.stable_id.strip() or not self.label.strip() or self.version < 0:
            raise ValueError("trusted selection candidate is incomplete")

    def as_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "version": self.version,
            "label": self.label,
        }


@dataclass(frozen=True)
class TrustedSelectionRequest:
    """Admission-created request to persist one SelectionPending via CAS.

    This artifact carries trusted candidate snapshots and protected facts, but
    intentionally cannot authorize a business write and is not an
    InformationPending foreign-key target.
    """

    selection_request_id: str
    trace_id: str
    decision_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    source_turn_id: str
    source_message_id: str
    action_id: str
    segment_id: str
    segment_text_sha256: str
    segment_start_offset: int
    segment_end_offset: int
    domain: str
    operation: str
    expected_conversation_state_version: int
    candidates: tuple[TrustedSelectionCandidateRef, ...]
    acceptable_answer_forms: Mapping[str, str]
    continuation_payload: Mapping[str, Any]
    created_at: datetime
    expires_at: datetime
    idempotency_key: str
    business_write_allowed: bool = False

    def __post_init__(self) -> None:
        required = (
            self.selection_request_id,
            self.trace_id,
            self.decision_id,
            self.tenant_id,
            self.user_id,
            self.conversation_id,
            self.source_turn_id,
            self.source_message_id,
            self.action_id,
            self.segment_id,
            self.segment_text_sha256,
            self.domain,
            self.operation,
            self.idempotency_key,
        )
        if not all(str(value or "").strip() for value in required):
            raise ValueError("trusted selection request is incomplete")
        if self.domain != "case" or self.operation != "record_case_progress":
            raise ValueError("trusted selection request has unsupported contract")
        if self.expected_conversation_state_version < 0:
            raise ValueError("trusted selection state version is invalid")
        if self.segment_start_offset < 0 or self.segment_end_offset <= self.segment_start_offset:
            raise ValueError("trusted selection segment offsets are invalid")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("trusted selection timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("trusted selection expiry must follow creation")
        if len(self.candidates) < 2 or len({item.stable_id for item in self.candidates}) != len(
            self.candidates
        ):
            raise ValueError("trusted selection requires multiple unique candidates")
        candidate_ids = {item.stable_id for item in self.candidates}
        if any(value not in candidate_ids for value in self.acceptable_answer_forms.values()):
            raise ValueError("trusted selection answer references unknown candidate")
        if self.business_write_allowed:
            raise ValueError("trusted selection request cannot authorize a business write")
        object.__setattr__(
            self,
            "acceptable_answer_forms",
            _freeze_mapping(self.acceptable_answer_forms),
        )
        object.__setattr__(
            self,
            "continuation_payload",
            _freeze_mapping(self.continuation_payload),
        )

    @property
    def ttl_seconds(self) -> int:
        return int((self.expires_at - self.created_at).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        return {
            "selection_request_id": self.selection_request_id,
            "trace_id": self.trace_id,
            "decision_id": self.decision_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "source_turn_id": self.source_turn_id,
            "source_message_id": self.source_message_id,
            "action_id": self.action_id,
            "segment_id": self.segment_id,
            "segment_text_sha256": self.segment_text_sha256,
            "segment_start_offset": self.segment_start_offset,
            "segment_end_offset": self.segment_end_offset,
            "domain": self.domain,
            "operation": self.operation,
            "expected_conversation_state_version": self.expected_conversation_state_version,
            "candidates": [item.as_dict() for item in self.candidates],
            "acceptable_answer_forms": _thaw(self.acceptable_answer_forms),
            "continuation_payload": _thaw(self.continuation_payload),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "ttl_seconds": self.ttl_seconds,
            "business_write_allowed": self.business_write_allowed,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class SemanticReviewItem:
    """Immutable, audit-only snapshot for Shadow or explicit review verdicts."""

    review_id: str
    trace_id: str
    decision_id: str | None
    tenant_id: str
    user_id: str
    conversation_id: str
    source_turn_id: str
    source_message_id: str
    segment_id: str
    segment_text_sha256: str
    segment_start_offset: int
    segment_end_offset: int
    domain: str
    operation: str
    object_ref: Mapping[str, Any] | None
    reason_code: str
    candidate_snapshot: Mapping[str, Any]
    resolution: Mapping[str, Any]
    idempotency_key: str
    created_at: datetime
    review_status: SemanticReviewStatus = "pending_human_review"
    audit_only: bool = True
    business_write_allowed: bool = False
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_audit_artifact_common(
            artifact_id=self.review_id,
            trace_id=self.trace_id,
            decision_id=self.decision_id,
            scope=(
                self.tenant_id,
                self.user_id,
                self.conversation_id,
                self.source_turn_id,
                self.source_message_id,
                self.segment_id,
            ),
            segment_text_sha256=self.segment_text_sha256,
            segment_start_offset=self.segment_start_offset,
            segment_end_offset=self.segment_end_offset,
            domain=self.domain,
            operation=self.operation,
            reason_code=self.reason_code,
            object_ref=self.object_ref,
            idempotency_key=self.idempotency_key,
            created_at=self.created_at,
        )
        if self.review_status not in SEMANTIC_REVIEW_STATUSES:
            raise ValueError("semantic review item has an unknown status")
        if self.audit_only is not True or self.business_write_allowed is not False:
            raise ValueError("semantic review item must remain audit-only")
        _validate_aware(self.resolved_at, field_name="review resolved_at", optional=True)
        if self.resolved_at is not None and self.resolved_at < self.created_at:
            raise ValueError("semantic review resolution cannot precede creation")
        if not isinstance(self.resolution, Mapping):
            raise ValueError("semantic review resolution must be a mapping")
        if self.review_status == "pending_human_review" and (
            self.resolution or self.resolved_at is not None
        ):
            raise ValueError("pending semantic review cannot have a resolution")
        if self.review_status != "pending_human_review":
            if self.resolved_at is None:
                raise ValueError("terminal semantic review requires resolved_at")
            resolution_keys = {"resolution_status", "resolution_sha256"}
            if set(self.resolution) != resolution_keys:
                raise ValueError("semantic review resolution has invalid schema")
            if self.resolution.get("resolution_status") != self.review_status:
                raise ValueError(
                    "semantic review resolution status does not match review status"
                )
            resolution_sha256 = self.resolution.get("resolution_sha256")
            if not isinstance(
                resolution_sha256, str
            ) or not _SHA256_PATTERN.fullmatch(resolution_sha256):
                raise ValueError("semantic review resolution digest is invalid")
        _validate_digest_only_candidate_mapping(
            self.candidate_snapshot,
            field_name="candidate_snapshot",
        )
        object.__setattr__(
            self,
            "candidate_snapshot",
            _freeze_mapping(self.candidate_snapshot),
        )
        object.__setattr__(self, "resolution", _freeze_mapping(self.resolution))
        if self.object_ref is not None:
            object.__setattr__(self, "object_ref", _freeze_mapping(self.object_ref))

    def as_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "trace_id": self.trace_id,
            "decision_id": self.decision_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "source_turn_id": self.source_turn_id,
            "source_message_id": self.source_message_id,
            "segment_id": self.segment_id,
            "segment_text_sha256": self.segment_text_sha256,
            "segment_start_offset": self.segment_start_offset,
            "segment_end_offset": self.segment_end_offset,
            "domain": self.domain,
            "operation": self.operation,
            "object_ref": (
                _thaw(self.object_ref) if self.object_ref is not None else None
            ),
            "reason_code": self.reason_code,
            "review_status": self.review_status,
            "candidate_snapshot": _thaw(self.candidate_snapshot),
            "resolution": _thaw(self.resolution),
            "audit_only": self.audit_only,
            "business_write_allowed": self.business_write_allowed,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat(),
            "resolved_at": (
                self.resolved_at.isoformat() if self.resolved_at is not None else None
            ),
        }


@dataclass(frozen=True)
class DeferredSemanticEvent:
    """Immutable signal that can only be reconsidered by a fresh turn."""

    deferred_event_id: str
    trace_id: str
    decision_id: str | None
    tenant_id: str
    user_id: str
    conversation_id: str
    source_turn_id: str
    source_message_id: str
    segment_id: str
    segment_text_sha256: str
    segment_start_offset: int
    segment_end_offset: int
    domain: str
    operation: str
    object_ref: Mapping[str, Any] | None
    reason_code: str
    payload: Mapping[str, Any]
    not_before: datetime | None
    expires_at: datetime | None
    idempotency_key: str
    created_at: datetime
    event_status: DeferredSemanticEventStatus = "recorded"
    audit_only: bool = True
    business_write_allowed: bool = False
    requires_fresh_admission: bool = True

    def __post_init__(self) -> None:
        _validate_audit_artifact_common(
            artifact_id=self.deferred_event_id,
            trace_id=self.trace_id,
            decision_id=self.decision_id,
            scope=(
                self.tenant_id,
                self.user_id,
                self.conversation_id,
                self.source_turn_id,
                self.source_message_id,
                self.segment_id,
            ),
            segment_text_sha256=self.segment_text_sha256,
            segment_start_offset=self.segment_start_offset,
            segment_end_offset=self.segment_end_offset,
            domain=self.domain,
            operation=self.operation,
            reason_code=self.reason_code,
            object_ref=self.object_ref,
            idempotency_key=self.idempotency_key,
            created_at=self.created_at,
        )
        if self.event_status not in DEFERRED_SEMANTIC_EVENT_STATUSES:
            raise ValueError("deferred semantic event has an unknown status")
        if (
            self.audit_only is not True
            or self.business_write_allowed is not False
            or self.requires_fresh_admission is not True
        ):
            raise ValueError(
                "deferred semantic event must require fresh admission and remain audit-only"
            )
        _validate_aware(
            self.not_before,
            field_name="deferred not_before",
            optional=True,
        )
        _validate_aware(
            self.expires_at,
            field_name="deferred expires_at",
            optional=True,
        )
        if self.not_before is not None and self.not_before < self.created_at:
            raise ValueError("deferred not_before cannot precede creation")
        window_start = self.not_before or self.created_at
        if self.expires_at is not None and self.expires_at <= window_start:
            raise ValueError("deferred expiry must follow its active window")
        _validate_digest_only_candidate_mapping(self.payload, field_name="payload")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))
        if self.object_ref is not None:
            object.__setattr__(self, "object_ref", _freeze_mapping(self.object_ref))

    def as_dict(self) -> dict[str, Any]:
        return {
            "deferred_event_id": self.deferred_event_id,
            "trace_id": self.trace_id,
            "decision_id": self.decision_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "source_turn_id": self.source_turn_id,
            "source_message_id": self.source_message_id,
            "segment_id": self.segment_id,
            "segment_text_sha256": self.segment_text_sha256,
            "segment_start_offset": self.segment_start_offset,
            "segment_end_offset": self.segment_end_offset,
            "domain": self.domain,
            "operation": self.operation,
            "object_ref": (
                _thaw(self.object_ref) if self.object_ref is not None else None
            ),
            "reason_code": self.reason_code,
            "event_status": self.event_status,
            "payload": _thaw(self.payload),
            "not_before": (
                self.not_before.isoformat() if self.not_before is not None else None
            ),
            "expires_at": (
                self.expires_at.isoformat() if self.expires_at is not None else None
            ),
            "audit_only": self.audit_only,
            "business_write_allowed": self.business_write_allowed,
            "requires_fresh_admission": self.requires_fresh_admission,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class AdmissionTrace:
    trace_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    contract_version: str
    decisions: tuple[AdmissionDecision, ...]
    source_turn_id: str = ""
    expected_conversation_state_version: int = 0
    proposal_sha256: str = ""
    policy_version: str = ADMISSION_POLICY_VERSION
    trace_status: str = "evaluated"
    admission_summary: str = "blocked"
    failure_reason: str = ""
    idempotency_key: str = ""
    created_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.created_at is None:
            raise ValueError("persistable admission trace requires created_at")
        return {
            "trace_id": self.trace_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "source_turn_id": self.source_turn_id,
            "source_message_id": self.source_message_id,
            "expected_conversation_state_version": self.expected_conversation_state_version,
            "proposal_sha256": self.proposal_sha256,
            "contract_version": self.contract_version,
            "policy_version": self.policy_version,
            "trace_status": self.trace_status,
            "admission_summary": self.admission_summary,
            "failure_reason": self.failure_reason,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat(),
        }


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _validate_audit_artifact_common(
    *,
    artifact_id: str,
    trace_id: str,
    decision_id: str | None,
    scope: tuple[str, ...],
    segment_text_sha256: str,
    segment_start_offset: int,
    segment_end_offset: int,
    domain: str,
    operation: str,
    reason_code: str,
    object_ref: Mapping[str, Any] | None,
    idempotency_key: str,
    created_at: datetime,
) -> None:
    _validate_uuid5(artifact_id, field_name="artifact id")
    _validate_uuid5(trace_id, field_name="trace id")
    if decision_id is not None:
        _validate_uuid5(decision_id, field_name="decision id")
    if not all(str(value or "").strip() for value in scope):
        raise ValueError("audit artifact scope is incomplete")
    if not _SHA256_PATTERN.fullmatch(str(segment_text_sha256 or "")):
        raise ValueError("audit artifact segment hash must be lowercase SHA-256")
    if (
        not isinstance(segment_start_offset, int)
        or isinstance(segment_start_offset, bool)
        or not isinstance(segment_end_offset, int)
        or isinstance(segment_end_offset, bool)
        or segment_start_offset < 0
        or segment_end_offset < segment_start_offset
    ):
        raise ValueError("audit artifact segment offsets are invalid")
    if domain not in ADMISSION_DOMAINS:
        raise ValueError("audit artifact has an unknown domain")
    if not str(operation or "").strip() or not str(reason_code or "").strip():
        raise ValueError("audit artifact operation and reason are required")
    _validate_object_ref(object_ref)
    if not str(idempotency_key or "").strip():
        raise ValueError("audit artifact idempotency key is required")
    _validate_aware(created_at, field_name="audit artifact created_at")


def _validate_uuid5(value: object, *, field_name: str) -> None:
    try:
        parsed = UUID(str(value or ""))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field_name} must be UUIDv5") from exc
    if parsed.version != 5 or parsed.variant != "specified in RFC 4122":
        raise ValueError(f"{field_name} must be UUIDv5")


def _validate_aware(
    value: datetime | None,
    *,
    field_name: str,
    optional: bool = False,
) -> None:
    if value is None:
        if optional:
            return
        raise ValueError(f"{field_name} is required")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_object_ref(value: Mapping[str, Any] | None) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError("audit artifact object_ref must be a mapping")
    allowed_keys = {"object_type", "stable_id", "version", "label"}
    if set(value) - allowed_keys:
        raise ValueError("audit artifact object_ref has unknown fields")
    if not str(value.get("object_type") or "").strip() or not str(
        value.get("stable_id") or ""
    ).strip():
        raise ValueError("audit artifact object_ref is incomplete")
    version = value.get("version")
    if version is not None and (
        not isinstance(version, int) or isinstance(version, bool) or version < 0
    ):
        raise ValueError("audit artifact object version is invalid")
    label = value.get("label")
    if label is not None and not isinstance(label, str):
        raise ValueError("audit artifact object label must be text")


def _validate_digest_only_candidate_mapping(
    value: Mapping[str, Any],
    *,
    field_name: str,
) -> None:
    """Accept only the production Admission decision digest projection."""

    required_keys = {"action_id", "decision_verdict", "evidence_refs"}
    if not isinstance(value, Mapping) or set(value) != required_keys:
        raise ValueError(f"{field_name} has invalid schema")
    action_id = value.get("action_id")
    if not isinstance(action_id, str) or not _AUDIT_ACTION_ID_PATTERN.fullmatch(
        action_id
    ):
        raise ValueError(f"{field_name} action_id is not a stable identifier")
    verdict = value.get("decision_verdict")
    if not isinstance(verdict, str) or verdict not in ADMISSION_STATUSES:
        raise ValueError(f"{field_name} decision_verdict is unknown")
    evidence_refs = value.get("evidence_refs")
    if not isinstance(evidence_refs, (list, tuple)) or not evidence_refs:
        raise ValueError(f"{field_name} evidence_refs are invalid")
    if any(not _is_stable_audit_evidence_ref(item) for item in evidence_refs):
        raise ValueError(f"{field_name} evidence_refs are invalid")
    if len(set(evidence_refs)) != len(evidence_refs):
        raise ValueError(f"{field_name} evidence_refs are invalid")


def _is_stable_audit_evidence_ref(value: object) -> bool:
    if not isinstance(value, str):
        return False
    prefix, separator, identifier = value.partition(":")
    if not separator:
        return False
    if prefix == "segment_sha256":
        return bool(_SHA256_PATTERN.fullmatch(identifier))
    if prefix == "selection_request":
        try:
            parsed = UUID(identifier)
        except (ValueError, AttributeError, TypeError):
            return False
        return parsed.version == 5 and parsed.variant == "specified in RFC 4122"
    return False
