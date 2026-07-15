from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, Mapping, Protocol

from app.agent2.admission_contracts import InformationPending
from app.agent2.operation_outcomes import OperationOutcome


InformationValidationStatus = Literal[
    "valid",
    "object_missing",
    "version_conflict",
    "permission_revoked",
    "operation_illegal",
]


@dataclass(frozen=True)
class InformationPendingContext:
    tenant_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    occurred_at: datetime
    conversation_state_version: int

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
            raise ValueError("information pending context requires trusted scope")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("information pending context time must be timezone-aware")
        if self.conversation_state_version < 0:
            raise ValueError("conversation state version must be non-negative")


@dataclass(frozen=True)
class InformationAnswerValue:
    field: str
    raw_value: str
    normalized_value: str
    evidence_start_offset: int
    evidence_end_offset: int

    def validate_grounding(self, source_text: str) -> bool:
        return bool(
            self.field.strip()
            and self.raw_value.strip()
            and self.normalized_value.strip()
            and self.evidence_start_offset >= 0
            and self.evidence_end_offset > self.evidence_start_offset
            and source_text[
                self.evidence_start_offset : self.evidence_end_offset
            ]
            == self.raw_value
        )


@dataclass(frozen=True)
class InformationAnswerCandidate:
    source_message_id: str
    source_text: str
    values: tuple[InformationAnswerValue, ...]

    def __post_init__(self) -> None:
        if not self.source_message_id.strip() or not self.source_text.strip():
            raise ValueError("information answer requires source identity and text")
        fields = tuple(value.field for value in self.values)
        if len(fields) != len(set(fields)):
            raise ValueError("information answer contains duplicate fields")


@dataclass(frozen=True)
class InformationPendingValidation:
    status: InformationValidationStatus
    reason: str = ""


class InformationPendingValidator(Protocol):
    def validate(
        self,
        pending: InformationPending,
        context: InformationPendingContext,
    ) -> InformationPendingValidation: ...


@dataclass(frozen=True)
class InformationContinuationRequest:
    pending_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    domain: str
    operation: str
    object_ref: Mapping[str, Any] | None
    field_values: Mapping[str, str]
    raw_values: Mapping[str, str]
    evidence_spans: Mapping[str, tuple[int, int]]
    requires_fresh_admission: bool = True
    business_write_allowed: bool = False


@dataclass(frozen=True)
class InformationPendingResolution:
    status: Literal[
        "ready_for_fresh_admission",
        "no_unique_pending",
        "insufficient_information",
        "expired",
        "inactive",
        "conflicted",
        "permission_revoked",
        "invalidated",
    ]
    pending_id: str
    reason: str
    pending_after: InformationPending | None
    continuation: InformationContinuationRequest | None
    actual_write: bool = False


class InformationPendingResolver:
    """Resolve exact evidence into a fresh-admission request, never a write."""

    def resolve(
        self,
        pendings: tuple[InformationPending, ...],
        *,
        candidate: InformationAnswerCandidate,
        context: InformationPendingContext,
        validator: InformationPendingValidator,
    ) -> InformationPendingResolution:
        scoped = tuple(
            pending
            for pending in pendings
            if pending.tenant_id == context.tenant_id
            and pending.user_id == context.user_id
            and pending.conversation_id == context.conversation_id
        )
        active = tuple(
            pending
            for pending in scoped
            if pending.pending_status in {"active", "awaiting_input"}
            and context.occurred_at < pending.expires_at
        )
        if len(active) != 1:
            if not active and len(scoped) == 1:
                pending = scoped[0]
                if (
                    pending.pending_status in {"active", "awaiting_input"}
                    and context.occurred_at >= pending.expires_at
                ):
                    expired = replace(pending, pending_status="expired")
                    return InformationPendingResolution(
                        "expired", pending.pending_id, "pending_expired", expired, None
                    )
                return InformationPendingResolution(
                    "inactive",
                    pending.pending_id,
                    f"pending_{pending.pending_status}",
                    pending,
                    None,
                )
            return InformationPendingResolution(
                "no_unique_pending", "", "unique_active_pending_required", None, None
            )
        pending = active[0]
        if candidate.source_message_id != context.source_message_id:
            return InformationPendingResolution(
                "invalidated",
                pending.pending_id,
                "source_message_scope_mismatch",
                replace(pending, pending_status="conflicted"),
                None,
            )
        if candidate.source_message_id == pending.source_message_id:
            return InformationPendingResolution(
                "invalidated",
                pending.pending_id,
                "source_message_reused",
                replace(pending, pending_status="conflicted"),
                None,
            )
        if (
            context.conversation_state_version
            != pending.expected_conversation_state_version
        ):
            return InformationPendingResolution(
                "conflicted",
                pending.pending_id,
                "conversation_state_version_conflict",
                replace(pending, pending_status="conflicted"),
                None,
            )
        validation = validator.validate(pending, context)
        if validation.status != "valid":
            status, pending_status = {
                "permission_revoked": ("permission_revoked", "permission_revoked"),
                "version_conflict": ("conflicted", "conflicted"),
                "object_missing": ("invalidated", "cancelled"),
                "operation_illegal": ("invalidated", "cancelled"),
            }[validation.status]
            return InformationPendingResolution(
                status,  # type: ignore[arg-type]
                pending.pending_id,
                validation.reason or validation.status,
                replace(pending, pending_status=pending_status),
                None,
            )
        values = {value.field: value for value in candidate.values}
        required = set(pending.missing_fields)
        if set(values) != required or any(
            not value.validate_grounding(candidate.source_text)
            for value in values.values()
        ):
            return InformationPendingResolution(
                "insufficient_information",
                pending.pending_id,
                "exact_missing_fields_with_grounded_evidence_required",
                pending,
                None,
            )
        continuation = InformationContinuationRequest(
            pending_id=pending.pending_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            conversation_id=context.conversation_id,
            source_message_id=context.source_message_id,
            domain=pending.domain,
            operation=pending.operation,
            object_ref=pending.object_ref,
            field_values={
                field: value.normalized_value for field, value in values.items()
            },
            raw_values={field: value.raw_value for field, value in values.items()},
            evidence_spans={
                field: (
                    value.evidence_start_offset,
                    value.evidence_end_offset,
                )
                for field, value in values.items()
            },
        )
        return InformationPendingResolution(
            "ready_for_fresh_admission",
            pending.pending_id,
            "fresh_admission_required",
            pending,
            continuation,
        )


@dataclass(frozen=True)
class InformationPendingSettlement:
    status: Literal["consumed", "retained"]
    pending_after: InformationPending
    reason: str
    receipt_refs: tuple[str, ...]


def settle_information_pending(
    pending: InformationPending,
    resolution: InformationPendingResolution,
    outcome: OperationOutcome,
    *,
    settled_trace_id: str,
) -> InformationPendingSettlement:
    if (
        resolution.status != "ready_for_fresh_admission"
        or resolution.pending_id != pending.pending_id
        or resolution.continuation is None
    ):
        raise ValueError("information pending settlement requires a ready resolution")
    receipt_refs = tuple(receipt.receipt_id for receipt in outcome.receipt_refs)
    committed = any(
        receipt.receipt_type == "database"
        and receipt.status in {"executed", "duplicate"}
        and (receipt.actual_write or outcome.business_status == "duplicate")
        for receipt in outcome.receipt_refs
    )
    succeeded = outcome.business_status in {
        "succeeded",
        "duplicate",
        "registered",
        "matched",
    } and committed
    if succeeded:
        if not settled_trace_id.strip():
            raise ValueError("consumed information pending requires settled trace id")
        return InformationPendingSettlement(
            "consumed",
            replace(
                pending,
                pending_status="consumed",
                consumed_by_trace_id=settled_trace_id,
            ),
            "committed_receipt_succeeded",
            receipt_refs,
        )
    return InformationPendingSettlement(
        "retained",
        pending,
        outcome.blocking_reason or outcome.business_status,
        receipt_refs,
    )
