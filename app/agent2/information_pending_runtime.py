from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from app.agent2.admission_contracts import InformationPending
from app.agent2.information_pending import (
    InformationAnswerCandidate,
    InformationAnswerValue,
    InformationContinuationRequest,
    InformationPendingContext,
    InformationPendingResolution,
    InformationPendingResolver,
    InformationPendingSettlement,
    InformationPendingValidation,
    settle_information_pending,
)
from app.agent2.operation_outcomes import OperationOutcome


InformationContinuationStatus = Literal[
    "no_pending",
    "ready_for_fresh_admission",
    "clarification_required",
    "duplicate_source_message",
    "expired",
    "conflicted",
    "permission_revoked",
    "invalidated",
]


@dataclass(frozen=True)
class InformationContinuationPreprocessRequest:
    """Trusted input for resolving one message against Information Pending.

    Identity and state values must come from the server-side turn scope.  The
    user's text supplies only the missing value and can never supply tenant,
    user, conversation, object, or operation identity.
    """

    session: Any
    tenant_id: str
    user_id: str
    conversation_id: str
    conversation_state_user_key: str
    conversation_state_version: int
    source_message_id: str
    source_text: str
    occurred_at: datetime
    timezone_name: str

    def __post_init__(self) -> None:
        if not all(
            str(value or "").strip()
            for value in (
                self.tenant_id,
                self.user_id,
                self.conversation_id,
                self.conversation_state_user_key,
                self.source_message_id,
            )
        ):
            raise ValueError("information continuation requires trusted scope")
        if self.conversation_state_version < 0:
            raise ValueError("conversation state version must be non-negative")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("information continuation time must be timezone-aware")


@dataclass(frozen=True)
class InformationContinuationPreprocessResult:
    handled: bool
    status: InformationContinuationStatus
    reason: str
    pending: InformationPending | None
    resolution: InformationPendingResolution | None
    fresh_admission_request: InformationContinuationRequest | None
    actual_write: bool = False

    def __post_init__(self) -> None:
        if self.actual_write:
            raise ValueError("information continuation preprocessing cannot write business data")
        if self.status == "ready_for_fresh_admission":
            if (
                not self.handled
                or self.pending is None
                or self.resolution is None
                or self.fresh_admission_request is None
                or not self.fresh_admission_request.requires_fresh_admission
                or self.fresh_admission_request.business_write_allowed
            ):
                raise ValueError("ready continuation requires a non-writable fresh admission request")
        elif self.fresh_admission_request is not None:
            raise ValueError("blocked continuation cannot expose a fresh admission request")


class InformationPendingContinuationRepository(Protocol):
    async def load_scoped(
        self,
        context: InformationPendingContext,
    ) -> tuple[InformationPending, ...]: ...

    async def source_message_processed(
        self,
        context: InformationPendingContext,
    ) -> bool: ...

    async def persist_resolution(
        self,
        *,
        resolution: InformationPendingResolution,
        context: InformationPendingContext,
    ) -> None: ...

    async def persist_settlement(
        self,
        *,
        pending: InformationPending,
        settlement: InformationPendingSettlement,
        settled_at: datetime,
    ) -> None: ...

    async def persist_failed_settlement(
        self,
        *,
        pending: InformationPending,
        reason: str,
        settled_at: datetime,
    ) -> None: ...


class InformationPendingContinuationValidator(Protocol):
    async def validate(
        self,
        pending: InformationPending,
        context: InformationPendingContext,
    ) -> InformationPendingValidation: ...


class InformationPendingContinuationCoordinator:
    """Resolve a short answer without choosing an object or authorizing a write."""

    def __init__(self, *, resolver: InformationPendingResolver | None = None) -> None:
        self._resolver = resolver or InformationPendingResolver()

    async def preprocess(
        self,
        request: InformationContinuationPreprocessRequest,
        *,
        repository: InformationPendingContinuationRepository,
        validator: InformationPendingContinuationValidator,
    ) -> InformationContinuationPreprocessResult:
        context = InformationPendingContext(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            source_message_id=request.source_message_id,
            occurred_at=request.occurred_at,
            conversation_state_version=request.conversation_state_version,
        )
        pendings = await repository.load_scoped(context)
        if await repository.source_message_processed(context):
            return InformationContinuationPreprocessResult(
                handled=True,
                status="duplicate_source_message",
                reason="source_message_already_processed",
                pending=None,
                resolution=None,
                fresh_admission_request=None,
            )
        if not pendings:
            return InformationContinuationPreprocessResult(
                handled=False,
                status="no_pending",
                reason="no_scoped_information_pending",
                pending=None,
                resolution=None,
                fresh_admission_request=None,
            )

        candidate = _answer_candidate(
            pendings,
            source_message_id=request.source_message_id,
            source_text=request.source_text,
            occurred_at=request.occurred_at,
            timezone_name=request.timezone_name,
        )
        async_validator = _AsyncValidatorBridge(validator)
        active = tuple(
            pending
            for pending in pendings
            if pending.pending_status in {"active", "awaiting_input"}
            and context.occurred_at < pending.expires_at
        )
        # The resolver only invokes object/permission validation after it has
        # proven one unique live Pending.  Do not touch an expired or ambiguous
        # object's repository as a side effect of a short answer.
        if len(active) == 1:
            await async_validator.prime(active, context)
        resolution = self._resolver.resolve(
            pendings,
            candidate=candidate,
            context=context,
            validator=async_validator,
        )
        await repository.persist_resolution(resolution=resolution, context=context)
        pending = next(
            (item for item in pendings if item.pending_id == resolution.pending_id),
            None,
        )
        status = _preprocess_status(resolution.status)
        return InformationContinuationPreprocessResult(
            handled=True,
            status=status,
            reason=resolution.reason,
            pending=pending,
            resolution=resolution,
            fresh_admission_request=resolution.continuation,
        )

    async def settle(
        self,
        result: InformationContinuationPreprocessResult,
        *,
        repository: InformationPendingContinuationRepository,
        outcome: OperationOutcome,
        settled_trace_id: str,
        settled_at: datetime,
    ) -> InformationPendingSettlement:
        if (
            result.status != "ready_for_fresh_admission"
            or result.pending is None
            or result.resolution is None
        ):
            raise ValueError("only a ready continuation can be settled")
        settlement = settle_information_pending(
            result.pending,
            result.resolution,
            outcome,
            settled_trace_id=settled_trace_id,
        )
        if settlement.status == "consumed":
            await repository.persist_settlement(
                pending=result.pending,
                settlement=settlement,
                settled_at=settled_at,
            )
        else:
            await repository.persist_failed_settlement(
                pending=result.pending,
                reason=settlement.reason,
                settled_at=settled_at,
            )
        return settlement


class _AsyncValidatorBridge:
    """Prime an async production validator for the existing pure resolver."""

    def __init__(self, validator: InformationPendingContinuationValidator) -> None:
        self._validator = validator
        self._results: dict[str, InformationPendingValidation] = {}

    async def prime(
        self,
        pendings: tuple[InformationPending, ...],
        context: InformationPendingContext,
    ) -> None:
        for pending in pendings:
            self._results[pending.pending_id] = await self._validator.validate(
                pending,
                context,
            )

    def validate(
        self,
        pending: InformationPending,
        context: InformationPendingContext,
    ) -> InformationPendingValidation:
        return self._results.get(
            pending.pending_id,
            InformationPendingValidation("operation_illegal", "validator_result_missing"),
        )


_RELATIVE_DATES: dict[str, int] = {
    "\u4eca\u5929": 0,
    "\u660e\u5929": 1,
    "\u540e\u5929": 2,
}
_DATE_ONLY = re.compile(
    r"^\s*(?P<value>\u4eca\u5929|\u660e\u5929|\u540e\u5929|\d{4}-\d{1,2}-\d{1,2})\s*[\u3002\uff01!\uff1f?]?\s*$"
)


def extract_exact_travel_date_answer(
    source_text: str,
    *,
    occurred_at: datetime,
    timezone_name: str,
) -> InformationAnswerValue | None:
    """Accept only a standalone, grounded relative date or ISO calendar date."""

    match = _DATE_ONLY.fullmatch(str(source_text or ""))
    if match is None:
        return None
    raw = match.group("value")
    try:
        local_date = occurred_at.astimezone(ZoneInfo(timezone_name)).date()
    except (KeyError, ValueError):
        return None
    if raw in _RELATIVE_DATES:
        normalized = (local_date + timedelta(days=_RELATIVE_DATES[raw])).isoformat()
    else:
        try:
            normalized = date.fromisoformat(raw).isoformat()
        except ValueError:
            return None
    return InformationAnswerValue(
        field="travel_date",
        raw_value=raw,
        normalized_value=normalized,
        evidence_start_offset=match.start("value"),
        evidence_end_offset=match.end("value"),
    )


def _answer_candidate(
    pendings: tuple[InformationPending, ...],
    *,
    source_message_id: str,
    source_text: str,
    occurred_at: datetime,
    timezone_name: str,
) -> InformationAnswerCandidate:
    values: tuple[InformationAnswerValue, ...] = ()
    active = tuple(
        pending
        for pending in pendings
        if pending.pending_status in {"active", "awaiting_input"}
        and occurred_at < pending.expires_at
    )
    if len(active) == 1 and active[0].missing_fields == ("travel_date",):
        value = extract_exact_travel_date_answer(
            source_text,
            occurred_at=occurred_at,
            timezone_name=timezone_name,
        )
        if value is not None:
            values = (value,)
    return InformationAnswerCandidate(
        source_message_id=source_message_id,
        source_text=source_text,
        values=values,
    )


def _preprocess_status(status: str) -> InformationContinuationStatus:
    return {
        "ready_for_fresh_admission": "ready_for_fresh_admission",
        "no_unique_pending": "clarification_required",
        "insufficient_information": "clarification_required",
        "expired": "expired",
        "inactive": "invalidated",
        "conflicted": "conflicted",
        "permission_revoked": "permission_revoked",
        "invalidated": "invalidated",
    }.get(status, "invalidated")  # type: ignore[return-value]
