from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Literal, Mapping, Protocol, get_args
from uuid import NAMESPACE_URL, uuid5

from app.agent2.operation_outcomes import OperationOutcome


SelectionPendingStatus = Literal[
    "active", "consumed", "expired", "invalidated", "cancelled"
]
SELECTION_PENDING_STATUSES = frozenset(get_args(SelectionPendingStatus))


@dataclass(frozen=True)
class SelectionCandidate:
    stable_id: str
    version: int
    label: str

    def __post_init__(self) -> None:
        if not self.stable_id.strip() or not self.label.strip() or self.version < 0:
            raise ValueError("selection candidate requires stable id, version, and label")

    def as_dict(self) -> dict[str, Any]:
        return {"stable_id": self.stable_id, "version": self.version, "label": self.label}


@dataclass(frozen=True)
class SelectionPending:
    pending_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    domain: str
    operation: str
    source_turn_id: str
    candidates: tuple[SelectionCandidate, ...]
    acceptable_answer_forms: dict[str, str]
    expected_conversation_state_version: int
    created_at: datetime
    expires_at: datetime
    status: SelectionPendingStatus
    continuation_payload: dict[str, Any] | None = None
    consumed_receipt_id: str = ""
    invalidation_reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in SELECTION_PENDING_STATUSES:
            raise ValueError("selection pending has an unknown status")
        required = (
            self.pending_id,
            self.tenant_id,
            self.user_id,
            self.conversation_id,
            self.domain,
            self.operation,
            self.source_turn_id,
        )
        if not all(str(value or "").strip() for value in required):
            raise ValueError("selection pending requires identity, domain, operation, and source turn")
        if not self.candidates or len({item.stable_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("selection pending requires unique candidates")
        candidate_ids = {item.stable_id for item in self.candidates}
        if any(value not in candidate_ids for value in self.acceptable_answer_forms.values()):
            raise ValueError("selection answer form must reference one candidate")
        if self.expected_conversation_state_version < 0:
            raise ValueError("selection pending state version must be non-negative")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("selection pending timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("selection pending expiry must be after creation")
        object.__setattr__(self, "acceptable_answer_forms", dict(self.acceptable_answer_forms))
        object.__setattr__(self, "continuation_payload", dict(self.continuation_payload or {}))

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending_id": self.pending_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "domain": self.domain,
            "operation": self.operation,
            "source_turn_id": self.source_turn_id,
            "candidates": [item.as_dict() for item in self.candidates],
            "acceptable_answer_forms": dict(self.acceptable_answer_forms),
            "expected_conversation_state_version": self.expected_conversation_state_version,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status,
            "continuation_payload": dict(self.continuation_payload or {}),
            "consumed_receipt_id": self.consumed_receipt_id,
            "invalidation_reason": self.invalidation_reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SelectionPending":
        return cls(
            pending_id=str(payload.get("pending_id") or ""),
            tenant_id=str(payload.get("tenant_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            conversation_id=str(payload.get("conversation_id") or ""),
            domain=str(payload.get("domain") or ""),
            operation=str(payload.get("operation") or ""),
            source_turn_id=str(payload.get("source_turn_id") or ""),
            candidates=tuple(
                SelectionCandidate(
                    stable_id=str(item.get("stable_id") or ""),
                    version=int(item.get("version", -1)),
                    label=str(item.get("label") or ""),
                )
                for item in payload.get("candidates", [])
                if isinstance(item, dict)
            ),
            acceptable_answer_forms={
                str(key): str(value)
                for key, value in dict(payload.get("acceptable_answer_forms") or {}).items()
            },
            expected_conversation_state_version=int(
                payload.get("expected_conversation_state_version", -1)
            ),
            created_at=_parse_datetime(payload.get("created_at")),
            expires_at=_parse_datetime(payload.get("expires_at")),
            status=str(payload.get("status") or ""),
            continuation_payload=dict(payload.get("continuation_payload") or {}),
            consumed_receipt_id=str(payload.get("consumed_receipt_id") or ""),
            invalidation_reason=str(payload.get("invalidation_reason") or ""),
        )


@dataclass(frozen=True)
class SelectionContext:
    tenant_id: str
    user_id: str
    conversation_id: str
    conversation_state_version: int
    source_turn_id: str
    now: datetime


@dataclass(frozen=True)
class SelectionValidation:
    status: Literal["valid", "not_found", "version_conflict", "forbidden", "illegal"]
    reason: str = ""

    @classmethod
    def valid(cls) -> "SelectionValidation":
        return cls("valid")


class SelectionCandidateValidator(Protocol):
    async def validate(
        self,
        pending: SelectionPending,
        candidate: SelectionCandidate,
        context: SelectionContext,
    ) -> SelectionValidation: ...


@dataclass(frozen=True)
class SelectionResolution:
    status: str
    pending_id: str
    selected_candidate_id: str
    selected_candidate_version: int | None
    reason: str
    actual_write: bool
    pending_after: SelectionPending


@dataclass(frozen=True)
class SelectionPendingAudit:
    pending_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    source_turn_id: str
    selected_candidate_id: str
    receipt_ids: tuple[str, ...]
    result: str
    reason: str
    occurred_at: datetime


@dataclass(frozen=True)
class SelectionSettlement:
    pending_after: SelectionPending
    audit: SelectionPendingAudit


class SelectionPendingResolver:
    """Resolve one bounded selection; this interface never executes business writes."""

    async def resolve(
        self,
        pendings: tuple[SelectionPending, ...],
        *,
        answer: str,
        context: SelectionContext,
        validator: SelectionCandidateValidator,
    ) -> SelectionResolution:
        local = tuple(
            item
            for item in pendings
            if item.tenant_id == context.tenant_id
            and item.user_id == context.user_id
            and item.conversation_id == context.conversation_id
        )
        active = tuple(item for item in local if item.status == "active")
        fallback = active[0] if active else local[0] if local else _empty_pending(context)
        if len(local) == 1 and local[0].status == "consumed":
            return SelectionResolution(
                "already_consumed",
                local[0].pending_id,
                "",
                None,
                "selection_pending_already_consumed",
                False,
                local[0],
            )
        if len(active) != 1:
            return SelectionResolution(
                "clarification_required", fallback.pending_id, "", None,
                "selection_pending_not_unique", False, fallback,
            )
        pending = active[0]
        if context.now >= pending.expires_at:
            expired = replace(pending, status="expired", invalidation_reason="expired")
            return SelectionResolution("expired", pending.pending_id, "", None, "expired", False, expired)
        if context.conversation_state_version != pending.expected_conversation_state_version:
            invalid = replace(
                pending,
                status="invalidated",
                invalidation_reason="conversation_state_version_changed",
            )
            return SelectionResolution(
                "invalidated", pending.pending_id, "", None,
                "conversation_state_version_changed", False, invalid,
            )
        candidate_id = _answer_candidate_id(pending, answer)
        candidate = next(
            (item for item in pending.candidates if item.stable_id == candidate_id), None
        )
        if candidate is None:
            return SelectionResolution(
                "clarification_required", pending.pending_id, "", None,
                "selection_answer_unresolved", False, pending,
            )
        validation = await validator.validate(pending, candidate, context)
        if validation.status != "valid":
            invalid = replace(
                pending,
                status="invalidated",
                invalidation_reason=validation.reason or validation.status,
            )
            return SelectionResolution(
                "invalidated", pending.pending_id, "", None,
                invalid.invalidation_reason, False, invalid,
            )
        return SelectionResolution(
            "selected",
            pending.pending_id,
            candidate.stable_id,
            candidate.version,
            "",
            False,
            pending,
        )


class SelectionPendingFactory:
    def from_trusted_request(self, request: Any) -> SelectionPending:
        """Convert one Admission artifact into ConversationState data.

        Persistence/CAS remains the runtime's responsibility.  This conversion
        neither consumes the request nor grants execution authority.
        """

        if (
            bool(getattr(request, "business_write_allowed", True))
            or str(getattr(request, "domain", "")) != "case"
            or str(getattr(request, "operation", "")) != "record_case_progress"
        ):
            raise ValueError("trusted selection request is not persistable")
        candidates = tuple(
            SelectionCandidate(
                stable_id=str(item.stable_id),
                version=int(item.version),
                label=str(item.label),
            )
            for item in tuple(getattr(request, "candidates", ()))
        )
        return SelectionPending(
            pending_id=str(request.selection_request_id),
            tenant_id=str(request.tenant_id),
            user_id=str(request.user_id),
            conversation_id=str(request.conversation_id),
            domain="case",
            operation="record_case_progress",
            source_turn_id=str(request.source_turn_id),
            candidates=candidates,
            acceptable_answer_forms={
                str(key): str(value)
                for key, value in dict(request.acceptable_answer_forms).items()
            },
            expected_conversation_state_version=int(
                request.expected_conversation_state_version
            ),
            created_at=request.created_at,
            expires_at=request.expires_at,
            status="active",
            continuation_payload=_thaw_selection_mapping(
                request.continuation_payload
            ),
        )

    def from_block(
        self,
        block: Any,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        source_turn_id: str,
        expected_conversation_state_version: int,
        now: datetime,
        expires_in_seconds: int,
    ) -> SelectionPending | None:
        metadata = getattr(block, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        selection = metadata.get("selection")
        if not isinstance(selection, dict):
            return None
        raw_candidates = selection.get("candidates")
        raw_candidates = raw_candidates if isinstance(raw_candidates, list) else []
        candidates = tuple(
            SelectionCandidate(
                stable_id=str(item.get("stable_id") or "").strip(),
                version=int(item.get("version", -1)),
                label=str(item.get("label") or "").strip(),
            )
            for item in raw_candidates
            if isinstance(item, dict)
            and str(item.get("stable_id") or "").strip()
            and str(item.get("label") or "").strip()
        )
        if not candidates:
            return None
        answer_forms: dict[str, str] = {}
        chinese_ordinals = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十")
        for index, candidate in enumerate(candidates, start=1):
            answer_forms[str(index)] = candidate.stable_id
            answer_forms[f"第{index}个"] = candidate.stable_id
            answer_forms[f"第{index}条"] = candidate.stable_id
            if index <= len(chinese_ordinals):
                ordinal = chinese_ordinals[index - 1]
                answer_forms[f"第{ordinal}个"] = candidate.stable_id
                answer_forms[f"第{ordinal}条"] = candidate.stable_id
            answer_forms[candidate.label] = candidate.stable_id
        declared = selection.get("acceptable_answer_forms")
        if isinstance(declared, dict):
            candidate_ids = {item.stable_id for item in candidates}
            answer_forms.update(
                {
                    str(key): str(value)
                    for key, value in declared.items()
                    if str(value) in candidate_ids
                }
            )
        pending_id = str(
            uuid5(
                NAMESPACE_URL,
                f"agent2-selection:{tenant_id}:{user_id}:{conversation_id}:"
                f"{source_turn_id}:{getattr(block, 'action_id', '')}",
            )
        )
        continuation_payload = dict(selection.get("continuation_payload") or {})
        if continuation_payload:
            try:
                continuation_payload = protect_selection_continuation_payload(
                    continuation_payload
                )
            except (TypeError, ValueError):
                # A malformed saved command must never become an active
                # executable Pending.  The clarification block remains visible
                # to the reply layer, but no continuation authority is minted.
                return None
        return SelectionPending(
            pending_id=pending_id,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            domain=str(selection.get("domain") or "").strip(),
            operation=str(selection.get("operation") or "").strip(),
            source_turn_id=source_turn_id,
            candidates=candidates,
            acceptable_answer_forms=answer_forms,
            expected_conversation_state_version=expected_conversation_state_version,
            created_at=now,
            expires_at=now + timedelta(seconds=max(1, expires_in_seconds)),
            status="active",
            continuation_payload=continuation_payload,
        )


def protect_selection_continuation_payload(
    continuation_payload: dict[str, Any],
) -> dict[str, Any]:
    """Freeze the original command facts copied into a Selection Pending.

    The snapshot is not execution authority.  It only lets a later fresh
    Admission prove that neither the original source fact nor its command
    envelope changed while the user was choosing a stable object.
    """

    result = deepcopy(dict(continuation_payload or {}))
    raw = result.get("typed_business_command")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("selection continuation command is missing")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("selection continuation payload is missing")
    segments = payload.get("source_segments")
    if (
        not isinstance(segments, list)
        or len(segments) != 1
        or not isinstance(segments[0], dict)
    ):
        raise ValueError("selection continuation requires one source segment")
    segment = segments[0]
    source_text = str(segment.get("text") or "")
    source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if not source_text.strip() or str(segment.get("text_hash") or "") != source_digest:
        raise ValueError("selection continuation source digest is invalid")
    try:
        start_offset = int(segment.get("start_offset"))
        end_offset = int(segment.get("end_offset"))
    except (TypeError, ValueError) as exc:
        raise ValueError("selection continuation source offsets are missing") from exc
    if start_offset < 0 or end_offset - start_offset != len(source_text):
        raise ValueError("selection continuation source offsets are invalid")
    result["protected_snapshot"] = {
        "original_continuation_sha256": _selection_sha256_json(raw),
        "original_payload_sha256": _selection_sha256_json(payload),
        "source_segment_id": str(segment.get("segment_id") or ""),
        "original_source_digest": source_digest,
        "source_start_offset": start_offset,
        "source_end_offset": end_offset,
    }
    return result


def settle_selection(
    pending: SelectionPending,
    resolution: SelectionResolution,
    outcome: OperationOutcome,
    *,
    settled_at: datetime,
) -> SelectionSettlement:
    if resolution.pending_id != pending.pending_id or resolution.status != "selected":
        raise ValueError("selection settlement requires its selected pending resolution")
    receipt_ids = tuple(item.receipt_id for item in outcome.receipt_refs)
    successful = outcome.business_status in {
        "succeeded",
        "duplicate",
        "registered",
        "matched",
        "accepted_by_one_party",
        "accepted_by_both",
        "declined",
        "cancelled",
    } and bool(receipt_ids)
    if successful:
        pending_after = replace(
            pending,
            status="consumed",
            consumed_receipt_id=receipt_ids[0],
            invalidation_reason="",
        )
        result = "consumed"
        reason = "receipt_succeeded"
    else:
        reason = outcome.blocking_reason or outcome.business_status
        pending_after = replace(
            pending,
            status="invalidated",
            invalidation_reason=reason,
        )
        result = "invalidated"
    return SelectionSettlement(
        pending_after=pending_after,
        audit=SelectionPendingAudit(
            pending_id=pending.pending_id,
            tenant_id=pending.tenant_id,
            user_id=pending.user_id,
            conversation_id=pending.conversation_id,
            source_turn_id=outcome.source_turn_id,
            selected_candidate_id=resolution.selected_candidate_id,
            receipt_ids=receipt_ids,
            result=result,
            reason=reason,
            occurred_at=settled_at,
        ),
    )


def _answer_candidate_id(pending: SelectionPending, answer: str) -> str:
    compact = _compact_selection_text(answer)
    normalized_forms = {
        _compact_selection_text(key): value
        for key, value in pending.acceptable_answer_forms.items()
    }
    if compact in normalized_forms:
        return normalized_forms[compact]
    fragment_candidate_id = _unique_label_fragment_candidate_id(pending, compact)
    if fragment_candidate_id:
        return fragment_candidate_id
    match = re.search(r"第?([一二两三四五六七八九十\d]+)(?:个|条)", compact)
    if match is None:
        return ""
    index = _ordinal_value(match.group(1)) - 1
    return pending.candidates[index].stable_id if 0 <= index < len(pending.candidates) else ""


def answer_may_target_selection(
    pendings: tuple[SelectionPending, ...], answer: str
) -> bool:
    """Grammar/contract gate; it does not infer a candidate or execute work."""
    compact = _compact_selection_text(answer)
    declared = {
        _compact_selection_text(form)
        for pending in pendings
        for form in pending.acceptable_answer_forms
    }
    if compact in declared:
        return True
    if any(_unique_label_fragment_candidate_id(pending, compact) for pending in pendings):
        return True
    return bool(re.search(r"第([一二两三四五六七八九十\d]+)(?:个|条)", compact))


def _unique_label_fragment_candidate_id(
    pending: SelectionPending,
    compact_answer: str,
) -> str:
    if len(compact_answer) < 4:
        return ""
    matches = [
        candidate.stable_id
        for candidate in pending.candidates
        if compact_answer in _compact_selection_text(candidate.label)
    ]
    unique = tuple(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else ""


def _compact_selection_text(value: object) -> str:
    return re.sub(
        r"[\s，。！？、,.!?‘’“”\"']",
        "",
        str(value or ""),
    ).casefold()


def _ordinal_value(value: str) -> int:
    if value.isdigit():
        return int(value)
    mapping = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    return mapping.get(value, -1)


def _parse_datetime(value: Any) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value or ""))
    if result.tzinfo is None:
        raise ValueError("selection pending timestamp must be timezone-aware")
    return result


def _selection_sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _thaw_selection_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    def thaw(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): thaw(child) for key, child in item.items()}
        if isinstance(item, tuple):
            return [thaw(child) for child in item]
        return item

    return thaw(value)


def _empty_pending(context: SelectionContext) -> SelectionPending:
    return SelectionPending(
        pending_id="selection-none",
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        conversation_id=context.conversation_id,
        domain="selection",
        operation="clarify",
        source_turn_id=context.source_turn_id,
        candidates=(SelectionCandidate("selection-none", 0, "待确认事项"),),
        acceptable_answer_forms={},
        expected_conversation_state_version=context.conversation_state_version,
        created_at=context.now,
        expires_at=context.now.replace(year=context.now.year + 1),
        status="invalidated",
        invalidation_reason="no_local_pending",
    )
