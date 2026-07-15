from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import re
from typing import Literal, Protocol

from app.agent2.selection_continuation import selected_business_command_snapshot
from app.agent2.selection_pending import (
    SelectionCandidate,
    SelectionCandidateValidator,
    SelectionContext,
    SelectionPending,
    SelectionPendingResolver,
    SelectionResolution,
)


SelectionContinuationStatus = Literal[
    "no_pending",
    "ready_for_fresh_admission",
    "clarification_required",
    "duplicate_source_message",
    "expired",
    "already_consumed",
    "invalidated",
    "permission_revoked",
]


@dataclass(frozen=True)
class SelectionAnswerEvidence:
    source_message_id: str
    text: str
    text_sha256: str
    start_offset: int
    end_offset: int

    def __post_init__(self) -> None:
        if not self.source_message_id.strip() or not self.text:
            raise ValueError("selection evidence requires current source text")
        if self.start_offset < 0 or self.end_offset <= self.start_offset:
            raise ValueError("selection evidence offsets are invalid")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("selection evidence digest is invalid")

    def as_dict(self, *, segment_id: str = "") -> dict[str, object]:
        result: dict[str, object] = {
            "source_message_id": self.source_message_id,
            "text": self.text,
            "text_sha256": self.text_sha256,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
        }
        if segment_id:
            result["segment_id"] = segment_id
        return result


@dataclass(frozen=True)
class SelectionContinuationRequest:
    pending_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    domain: str
    operation: str
    candidate_stable_id: str
    candidate_version: int
    evidence: SelectionAnswerEvidence
    original_source_digest: str
    original_continuation_sha256: str
    bound_payload_sha256: str
    command_type: str
    requires_fresh_admission: bool = True
    business_write_allowed: bool = False

    def __post_init__(self) -> None:
        required = (
            self.pending_id,
            self.tenant_id,
            self.user_id,
            self.conversation_id,
            self.source_message_id,
            self.domain,
            self.operation,
            self.candidate_stable_id,
            self.original_source_digest,
            self.original_continuation_sha256,
            self.bound_payload_sha256,
            self.command_type,
        )
        if not all(str(value or "").strip() for value in required):
            raise ValueError("selection continuation request is incomplete")
        if self.candidate_version < 0:
            raise ValueError("selection continuation requires candidate version")
        if self.evidence.source_message_id != self.source_message_id:
            raise ValueError("selection evidence source changed")
        if not self.requires_fresh_admission or self.business_write_allowed:
            raise ValueError("selection continuation cannot authorize a business write")


@dataclass(frozen=True)
class SelectionContinuationPreprocessRequest:
    tenant_id: str
    user_id: str
    conversation_id: str
    conversation_state_version: int
    source_message_id: str
    source_text: str
    occurred_at: datetime
    pendings: tuple[SelectionPending, ...]

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
            raise ValueError("selection continuation requires trusted scope")
        if self.conversation_state_version < 0:
            raise ValueError("selection state version must be non-negative")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("selection continuation time must be timezone-aware")


@dataclass(frozen=True)
class SelectionContinuationPreprocessResult:
    handled: bool
    status: SelectionContinuationStatus
    reason: str
    pending: SelectionPending | None
    candidate: SelectionCandidate | None
    resolution: SelectionResolution | None
    fresh_admission_request: SelectionContinuationRequest | None
    actual_write: bool = False

    def __post_init__(self) -> None:
        if self.actual_write:
            raise ValueError("selection preprocessing cannot write business data")
        if self.status == "ready_for_fresh_admission":
            fresh = self.fresh_admission_request
            if (
                not self.handled
                or self.pending is None
                or self.candidate is None
                or self.resolution is None
                or fresh is None
                or not fresh.requires_fresh_admission
                or fresh.business_write_allowed
            ):
                raise ValueError("ready selection requires fresh non-writable admission")
        elif self.candidate is not None or self.fresh_admission_request is not None:
            raise ValueError("blocked selection cannot expose a candidate or admission request")


class SelectionContinuationSourceLedger(Protocol):
    async def source_message_processed(self, context: SelectionContext) -> bool: ...


class SelectionContinuationCoordinator:
    """Resolve one exact scoped answer; never issue a Ticket or perform a write."""

    def __init__(self, *, resolver: SelectionPendingResolver | None = None) -> None:
        self._resolver = resolver or SelectionPendingResolver()

    async def preprocess(
        self,
        request: SelectionContinuationPreprocessRequest,
        *,
        validator: SelectionCandidateValidator,
        source_ledger: SelectionContinuationSourceLedger,
    ) -> SelectionContinuationPreprocessResult:
        context = SelectionContext(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            conversation_state_version=request.conversation_state_version,
            source_turn_id=request.source_message_id,
            now=request.occurred_at,
        )
        if await source_ledger.source_message_processed(context):
            return _blocked(
                "duplicate_source_message",
                "source_message_already_processed",
                handled=True,
            )

        local = tuple(
            pending
            for pending in request.pendings
            if pending.tenant_id == request.tenant_id
            and pending.user_id == request.user_id
            and pending.conversation_id == request.conversation_id
        )
        if not local:
            return _blocked("no_pending", "no_scoped_selection_pending", handled=False)

        active = tuple(item for item in local if item.status == "active")
        evidence: SelectionAnswerEvidence | None = None
        expected_candidate_id = ""
        if len(active) == 1:
            exact = _exact_answer_evidence(
                active[0],
                source_message_id=request.source_message_id,
                source_text=request.source_text,
            )
            if exact is not None:
                expected_candidate_id, evidence = exact

        # An empty answer deliberately prevents the existing Resolver's unique
        # label-fragment/ordinal regex fallback.  Fresh Admission accepts only
        # the exact answer form proved above.
        resolution = await self._resolver.resolve(
            request.pendings,
            answer=request.source_text if evidence is not None else "",
            context=context,
            validator=validator,
        )
        pending = next(
            (item for item in local if item.pending_id == resolution.pending_id),
            local[0],
        )
        if resolution.status != "selected":
            return SelectionContinuationPreprocessResult(
                handled=True,
                status=_status_from_resolution(resolution),
                reason=resolution.reason,
                pending=pending,
                candidate=None,
                resolution=resolution,
                fresh_admission_request=None,
            )
        if evidence is None or resolution.selected_candidate_id != expected_candidate_id:
            return _blocked(
                "clarification_required",
                "selection_answer_not_exact",
                handled=True,
                pending=pending,
                resolution=resolution,
            )
        candidate = next(
            (
                item
                for item in pending.candidates
                if item.stable_id == resolution.selected_candidate_id
                and item.version == resolution.selected_candidate_version
            ),
            None,
        )
        if candidate is None:
            return _blocked(
                "invalidated",
                "selection_candidate_snapshot_changed",
                handled=True,
                pending=pending,
                resolution=resolution,
            )
        try:
            snapshot = selected_business_command_snapshot(pending, candidate)
        except (TypeError, ValueError):
            invalid = replace(
                pending,
                status="invalidated",
                invalidation_reason="selection_continuation_contract_invalid",
            )
            invalid_resolution = replace(
                resolution,
                status="invalidated",
                reason="selection_continuation_contract_invalid",
                pending_after=invalid,
            )
            return _blocked(
                "invalidated",
                "selection_continuation_contract_invalid",
                handled=True,
                pending=pending,
                resolution=invalid_resolution,
            )
        raw = snapshot.raw_command
        fresh = SelectionContinuationRequest(
            pending_id=pending.pending_id,
            tenant_id=pending.tenant_id,
            user_id=pending.user_id,
            conversation_id=pending.conversation_id,
            source_message_id=request.source_message_id,
            domain=pending.domain,
            operation=pending.operation,
            candidate_stable_id=candidate.stable_id,
            candidate_version=candidate.version,
            evidence=evidence,
            original_source_digest=snapshot.original_source_digest,
            original_continuation_sha256=snapshot.original_continuation_sha256,
            bound_payload_sha256=snapshot.bound_payload_sha256,
            command_type=str(raw.get("command_type") or ""),
        )
        return SelectionContinuationPreprocessResult(
            handled=True,
            status="ready_for_fresh_admission",
            reason="fresh_admission_required",
            pending=pending,
            candidate=candidate,
            resolution=resolution,
            fresh_admission_request=fresh,
        )


_EDGE_NOISE = frozenset(" \t\r\n。！？!?，,；;：:")


def _exact_answer_evidence(
    pending: SelectionPending,
    *,
    source_message_id: str,
    source_text: str,
) -> tuple[str, SelectionAnswerEvidence] | None:
    start = 0
    end = len(source_text)
    while start < end and source_text[start] in _EDGE_NOISE:
        start += 1
    while end > start and source_text[end - 1] in _EDGE_NOISE:
        end -= 1
    surface = source_text[start:end]
    if not surface:
        return None
    compact = _compact(surface)
    matches = {
        candidate_id
        for form, candidate_id in pending.acceptable_answer_forms.items()
        if _compact(form) == compact
    }
    matches.update(
        candidate.stable_id
        for candidate in pending.candidates
        if _compact(candidate.label) == compact
    )
    if len(matches) != 1:
        return None
    evidence = SelectionAnswerEvidence(
        source_message_id=source_message_id,
        text=surface,
        text_sha256=hashlib.sha256(surface.encode("utf-8")).hexdigest(),
        start_offset=start,
        end_offset=end,
    )
    return next(iter(matches)), evidence


def _compact(value: object) -> str:
    return re.sub(r"[\s，。！？；：,.!?;:]", "", str(value or "")).casefold()


def _status_from_resolution(resolution: SelectionResolution) -> SelectionContinuationStatus:
    if resolution.status == "clarification_required":
        return "clarification_required"
    if resolution.status == "expired":
        return "expired"
    if resolution.status == "already_consumed":
        return "already_consumed"
    if resolution.reason in {"forbidden", "permission_revoked", "candidate_missing_or_forbidden"}:
        return "permission_revoked"
    return "invalidated"


def _blocked(
    status: SelectionContinuationStatus,
    reason: str,
    *,
    handled: bool,
    pending: SelectionPending | None = None,
    resolution: SelectionResolution | None = None,
) -> SelectionContinuationPreprocessResult:
    return SelectionContinuationPreprocessResult(
        handled=handled,
        status=status,
        reason=reason,
        pending=pending,
        candidate=None,
        resolution=resolution,
        fresh_admission_request=None,
    )

