from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


REPLY_DELIVERY_KEY = "_agent2_reply_delivery_v1"
ReplyDeliveryStatus = Literal[
    "verified",
    "accepted_unverified",
    "delivery_failed",
    "failed",
    "unknown",
]
ReplyDeliveryChannel = Literal["direct_robot", "session_webhook"]

_SCHEMA_VERSION = "agent2.reply.delivery.v1"
_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "channel",
        "provider_reference",
        "provider_accepted",
        "delivery_verified",
        "status",
        "error",
        "checked_at",
    }
)
_OPTIONAL_KEYS = frozenset({"retry_safe_preacceptance_failure"})
_RETRY_SAFE_LOCAL_ERRORS = frozenset({"DingTalkOutboundContentError"})


@dataclass(frozen=True)
class ReplyDeliveryEvidence:
    channel: ReplyDeliveryChannel
    provider_reference: str | None
    provider_accepted: bool
    delivery_verified: bool
    status: ReplyDeliveryStatus
    error: str | None
    checked_at: datetime
    retry_safe_preacceptance_failure: bool = False


def build_reply_delivery_record(
    *,
    channel: ReplyDeliveryChannel,
    provider_reference: str | None,
    provider_accepted: bool,
    delivery_verified: bool,
    status: ReplyDeliveryStatus,
    error: str | None,
    checked_at: datetime,
    retry_safe_preacceptance_failure: bool = False,
) -> dict[str, Any]:
    """Build one strictly validated, server-owned delivery record."""

    record: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "channel": channel,
        "provider_reference": provider_reference,
        "provider_accepted": provider_accepted,
        "delivery_verified": delivery_verified,
        "status": status,
        "error": error,
        "checked_at": checked_at.isoformat(),
    }
    if retry_safe_preacceptance_failure:
        record["retry_safe_preacceptance_failure"] = True
    if validated_reply_delivery_record({REPLY_DELIVERY_KEY: record}) is None:
        raise ValueError("invalid Agent2 reply delivery record")
    return record


def validated_reply_delivery_record(
    response_payload: Mapping[str, Any] | None,
) -> ReplyDeliveryEvidence | None:
    """Read only a complete record with internally consistent evidence."""

    if not isinstance(response_payload, Mapping):
        return None
    raw = response_payload.get(REPLY_DELIVERY_KEY)
    if not isinstance(raw, Mapping):
        return None
    keys = frozenset(raw.keys())
    if not _REQUIRED_KEYS.issubset(keys) or not keys.issubset(
        _REQUIRED_KEYS | _OPTIONAL_KEYS
    ):
        return None
    if raw.get("schema_version") != _SCHEMA_VERSION:
        return None
    channel = raw.get("channel")
    status = raw.get("status")
    provider_reference = raw.get("provider_reference")
    provider_accepted = raw.get("provider_accepted")
    delivery_verified = raw.get("delivery_verified")
    error = raw.get("error")
    retry_safe = raw.get("retry_safe_preacceptance_failure", False)
    if channel not in {"direct_robot", "session_webhook"}:
        return None
    if status not in {
        "verified",
        "accepted_unverified",
        "delivery_failed",
        "failed",
        "unknown",
    }:
        return None
    if type(provider_accepted) is not bool or type(delivery_verified) is not bool:
        return None
    if type(retry_safe) is not bool:
        return None
    if provider_reference is not None and (
        not isinstance(provider_reference, str)
        or not provider_reference.strip()
        or provider_reference != provider_reference.strip()
        or len(provider_reference) > 512
    ):
        return None
    if error is not None and (
        not isinstance(error, str)
        or not error.strip()
        or error != error.strip()
        or len(error) > 128
    ):
        return None
    checked_at_raw = raw.get("checked_at")
    if not isinstance(checked_at_raw, str):
        return None
    try:
        checked_at = datetime.fromisoformat(checked_at_raw)
    except ValueError:
        return None
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        return None

    if status == "verified":
        valid_state = (
            channel == "direct_robot"
            and provider_accepted
            and delivery_verified
            and provider_reference is not None
            and error is None
            and not retry_safe
        )
    elif status == "accepted_unverified":
        valid_state = (
            provider_accepted
            and not delivery_verified
            and (
                (channel == "direct_robot" and provider_reference is not None)
                or (
                    channel == "session_webhook"
                    and provider_reference is None
                )
            )
            and not retry_safe
        )
    elif status == "delivery_failed":
        valid_state = (
            channel == "direct_robot"
            and provider_accepted
            and not delivery_verified
            and provider_reference is not None
            and error == "DingTalkDeliveryError"
            and not retry_safe
        )
    elif status == "failed":
        valid_state = (
            not provider_accepted
            and not delivery_verified
            and provider_reference is None
            and error is not None
            and (
                not retry_safe
                or error in _RETRY_SAFE_LOCAL_ERRORS
            )
        )
    else:
        valid_state = (
            not provider_accepted
            and not delivery_verified
            and provider_reference is None
            and not retry_safe
        )
    if not valid_state:
        return None
    return ReplyDeliveryEvidence(
        channel=channel,
        provider_reference=provider_reference,
        provider_accepted=provider_accepted,
        delivery_verified=delivery_verified,
        status=status,
        error=error,
        checked_at=checked_at,
        retry_safe_preacceptance_failure=retry_safe,
    )


def cached_reply_is_retry_safe(
    response_payload: Mapping[str, Any] | None,
) -> bool:
    evidence = validated_reply_delivery_record(response_payload)
    return bool(
        evidence is not None
        and evidence.status == "failed"
        and evidence.retry_safe_preacceptance_failure
    )
