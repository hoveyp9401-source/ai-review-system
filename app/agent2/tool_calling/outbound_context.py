from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from typing import Any
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.tool_calling.context import TrustedRecentMessage
from app.models import ReportInteractionEvent, User
from app.services.dingtalk import validate_dingtalk_outbound_text


OUTBOUND_CONTEXT_BACKEND_ACTION = "agent2_outbound_context_sent"
OUTBOUND_CONTEXT_INTERACTION_TYPE = "outbound_conversation_message"
OUTBOUND_CONTEXT_MAX_AGE = timedelta(hours=16)

_OUTBOUND_CONTEXT_SCHEMA_VERSION = 1
_OUTBOUND_CONTEXT_EVENT_NAMESPACE = uuid.UUID(
    "a9962419-d6bb-445a-85f6-bb6666fdfbd6"
)
_MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class OutboundContextRecordResult:
    event: ReportInteractionEvent
    created: bool


def build_verified_outbound_context_event(
    *,
    user: User,
    conversation_id: str,
    message_text: str,
    source_message_id: str,
    delivery_receipt: Mapping[str, Any],
    sent_at: datetime,
) -> ReportInteractionEvent:
    """Build an append-only context record after DingTalk confirms delivery."""

    if not bool(getattr(user, "active", False)):
        raise ValueError("outbound context recipient must be an active user")
    user_id = getattr(user, "id", None)
    if not isinstance(user_id, uuid.UUID):
        raise ValueError("outbound context recipient requires a UUID user id")
    dingtalk_user_id = _required_text(
        getattr(user, "dingtalk_user_id", ""),
        field="dingtalk_user_id",
        max_length=128,
    )
    conversation_id = _required_text(
        conversation_id,
        field="conversation_id",
        max_length=256,
    )
    source_message_id = _required_text(
        source_message_id,
        field="source_message_id",
        max_length=480,
    )
    message_text = validate_dingtalk_outbound_text(message_text).strip()
    if not message_text or len(message_text) > 4000:
        raise ValueError(
            "outbound context message must contain 1 to 4000 characters"
        )
    sent_at = _aware_utc(sent_at, field="sent_at")
    provider_reference = _verified_provider_reference(
        delivery_receipt,
        recipient_dingtalk_user_id=dingtalk_user_id,
    )
    timezone_name = _required_text(
        getattr(user, "timezone", ""),
        field="timezone",
        max_length=64,
    )
    try:
        local_report_date = sent_at.astimezone(
            ZoneInfo(timezone_name)
        ).date()
    except ZoneInfoNotFoundError as exc:
        raise ValueError("outbound context recipient timezone is invalid") from exc

    expires_at = sent_at + OUTBOUND_CONTEXT_MAX_AGE
    event_id = outbound_context_event_id(
        user_id=user_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    return ReportInteractionEvent(
        id=event_id,
        user_id=user_id,
        report_id=None,
        dingtalk_user_id=dingtalk_user_id,
        report_date=local_report_date,
        message_text=message_text,
        llm_decision_json={
            "schema_version": _OUTBOUND_CONTEXT_SCHEMA_VERSION,
            "interaction_type": OUTBOUND_CONTEXT_INTERACTION_TYPE,
            "conversation_id": conversation_id,
            "source_message_id": source_message_id,
            "recipient_dingtalk_user_id": dingtalk_user_id,
            "business_write": False,
            "message_status": "delivery_confirmed",
            "provider_reference": provider_reference,
            "message_sha256": _message_sha256(message_text),
            "sent_at": sent_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
        backend_action=OUTBOUND_CONTEXT_BACKEND_ACTION,
        before_snapshot_json={},
        after_snapshot_json={},
        created_at=sent_at,
    )


async def record_verified_outbound_context_message(
    session: Any,
    *,
    user: User,
    conversation_id: str,
    message_text: str,
    source_message_id: str,
    delivery_receipt: Mapping[str, Any],
    sent_at: datetime,
) -> OutboundContextRecordResult:
    """Record a verified proactive message exactly once within the caller transaction."""

    candidate = build_verified_outbound_context_event(
        user=user,
        conversation_id=conversation_id,
        message_text=message_text,
        source_message_id=source_message_id,
        delivery_receipt=delivery_receipt,
        sent_at=sent_at,
    )
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:lock_key, 0))"
        ),
        {"lock_key": f"agent2-outbound-context:{candidate.id}"},
    )
    rows = list(
        (
            await session.scalars(
                select(ReportInteractionEvent)
                .where(ReportInteractionEvent.id == candidate.id)
                .with_for_update()
            )
        ).all()
    )
    if len(rows) > 1:
        raise RuntimeError("outbound context identity is not unique")
    if rows:
        existing = rows[0]
        if not _same_verified_record(existing, candidate):
            raise RuntimeError("outbound context identity collision")
        return OutboundContextRecordResult(event=existing, created=False)

    session.add(candidate)
    await session.flush()
    return OutboundContextRecordResult(event=candidate, created=True)


def trusted_recent_outbound_message(
    event: ReportInteractionEvent,
    *,
    request: TrustedContextRequest,
    dingtalk_user_id: str,
) -> TrustedRecentMessage | None:
    """Return trusted assistant context only for the exact verified recipient/chat."""

    try:
        metadata = (
            event.llm_decision_json
            if isinstance(event.llm_decision_json, dict)
            else {}
        )
        conversation_id = _required_text(
            metadata.get("conversation_id"),
            field="conversation_id",
            max_length=256,
        )
        source_message_id = _required_text(
            metadata.get("source_message_id"),
            field="source_message_id",
            max_length=480,
        )
        recipient_id = _required_text(
            metadata.get("recipient_dingtalk_user_id"),
            field="recipient_dingtalk_user_id",
            max_length=128,
        )
        provider_reference = _required_text(
            metadata.get("provider_reference"),
            field="provider_reference",
            max_length=256,
        )
        del provider_reference
        created_at = _aware_utc(event.created_at, field="created_at")
        sent_at = _parse_aware_datetime(
            metadata.get("sent_at"),
            field="sent_at",
        )
        expires_at = _parse_aware_datetime(
            metadata.get("expires_at"),
            field="expires_at",
        )
        server_now = _aware_utc(request.server_now, field="server_now")
        message_text = validate_dingtalk_outbound_text(
            str(event.message_text or "")
        ).strip()
        if not message_text or len(message_text) > 4000:
            return None
        expected_id = outbound_context_event_id(
            user_id=event.user_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
        )
    except (TypeError, ValueError, AttributeError):
        return None

    if not (
        event.backend_action == OUTBOUND_CONTEXT_BACKEND_ACTION
        and event.id == expected_id
        and event.user_id == request.user_id
        and event.report_id is None
        and event.dingtalk_user_id == dingtalk_user_id
        and recipient_id == dingtalk_user_id
        and conversation_id == request.conversation_id
        and metadata.get("schema_version")
        == _OUTBOUND_CONTEXT_SCHEMA_VERSION
        and metadata.get("interaction_type")
        == OUTBOUND_CONTEXT_INTERACTION_TYPE
        and metadata.get("message_status") == "delivery_confirmed"
        and metadata.get("business_write") is False
        and metadata.get("message_sha256") == _message_sha256(message_text)
        and sent_at == created_at
        and created_at <= server_now + _MAX_FUTURE_CLOCK_SKEW
        and created_at >= server_now - OUTBOUND_CONTEXT_MAX_AGE
        and created_at < expires_at
        and expires_at <= created_at + OUTBOUND_CONTEXT_MAX_AGE
        and server_now < expires_at
    ):
        return None
    return TrustedRecentMessage(
        role="assistant",
        content=message_text,
        source_message_id=f"outbound:{source_message_id}",
    )


def outbound_context_event_id(
    *,
    user_id: uuid.UUID,
    conversation_id: str,
    source_message_id: str,
) -> uuid.UUID:
    return uuid.uuid5(
        _OUTBOUND_CONTEXT_EVENT_NAMESPACE,
        f"{user_id}\x1f{conversation_id}\x1f{source_message_id}",
    )


def _same_verified_record(
    existing: ReportInteractionEvent,
    candidate: ReportInteractionEvent,
) -> bool:
    existing_metadata = (
        existing.llm_decision_json
        if isinstance(existing.llm_decision_json, dict)
        else {}
    )
    candidate_metadata = candidate.llm_decision_json
    stable_metadata_keys = (
        "schema_version",
        "interaction_type",
        "conversation_id",
        "source_message_id",
        "recipient_dingtalk_user_id",
        "business_write",
        "message_status",
        "provider_reference",
        "message_sha256",
    )
    return (
        existing.id == candidate.id
        and existing.user_id == candidate.user_id
        and existing.report_id is None
        and existing.dingtalk_user_id == candidate.dingtalk_user_id
        and existing.message_text == candidate.message_text
        and existing.backend_action == candidate.backend_action
        and all(
            existing_metadata.get(key) == candidate_metadata.get(key)
            for key in stable_metadata_keys
        )
    )


def _verified_provider_reference(
    receipt: Mapping[str, Any],
    *,
    recipient_dingtalk_user_id: str,
) -> str:
    if not isinstance(receipt, Mapping):
        raise ValueError("outbound context requires a delivery receipt")
    if receipt.get("deliveryVerified") is not True:
        raise ValueError("outbound context requires verified delivery")
    if str(receipt.get("deliveryStatus") or "").strip().upper() != "SUCCESS":
        raise ValueError("outbound context requires successful delivery")
    delivered = _receipt_user_ids(
        receipt.get("deliveryRecipientUserIds"),
        field="deliveryRecipientUserIds",
    )
    if recipient_dingtalk_user_id not in delivered:
        raise ValueError("outbound context recipient delivery was not confirmed")
    for field in (
        "invalidStaffIdList",
        "filteredStaffIdList",
        "flowControlledStaffIdList",
    ):
        blocked = _receipt_user_ids(receipt.get(field, []), field=field)
        if recipient_dingtalk_user_id in blocked:
            raise ValueError("outbound context recipient was rejected by provider")
    references = {
        str(receipt.get(field) or "").strip()
        for field in ("processQueryKey", "task_id", "taskId")
        if str(receipt.get(field) or "").strip()
    }
    if len(references) != 1:
        raise ValueError("outbound context requires one provider reference")
    return _required_text(
        references.pop(),
        field="provider_reference",
        max_length=256,
    )


def _receipt_user_ids(value: Any, *, field: str) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"outbound context {field} must be a list")
    return {
        _required_text(item, field=field, max_length=128)
        for item in value
    }


def _required_text(value: Any, *, field: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(
            f"outbound context {field} must contain 1 to {max_length} characters"
        )
    return normalized


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"outbound context {field} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_aware_datetime(value: Any, *, field: str) -> datetime:
    normalized = _required_text(value, field=field, max_length=64)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"outbound context {field} is invalid") from exc
    return _aware_utc(parsed, field=field)


def _message_sha256(message_text: str) -> str:
    return hashlib.sha256(message_text.encode("utf-8")).hexdigest()
