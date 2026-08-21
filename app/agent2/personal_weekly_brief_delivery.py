from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.agent2.personal_weekly_brief_store import PersonalWeeklyBriefRecord


@dataclass(frozen=True)
class PersonalWeeklyBriefRecipient:
    tenant_id: str
    internal_user_id: str
    dingtalk_user_id: str
    conversation_id: str


@dataclass(frozen=True)
class PersonalWeeklyBriefDelivery:
    provider_reference: str
    delivery_verified: bool
    delivered_dingtalk_user_ids: tuple[str, ...] = ()


class PersonalWeeklyBriefTransport(Protocol):
    async def send_private_text_verified(
        self, *, dingtalk_user_id: str, text: str
    ) -> PersonalWeeklyBriefDelivery: ...

    async def query_private_delivery(
        self, *, provider_reference: str
    ) -> PersonalWeeklyBriefDelivery: ...


class DingTalkPersonalWeeklyBriefTransport:
    def __init__(self, robot) -> None:
        self._robot = robot

    async def send_private_text_verified(
        self, *, dingtalk_user_id: str, text: str
    ) -> PersonalWeeklyBriefDelivery:
        from app.services.dingtalk import DingTalkDeliveryError

        try:
            payload = await self._robot.send_robot_direct_text_verified(
                user_ids=[dingtalk_user_id],
                text=text,
            )
        except DingTalkDeliveryError as exc:
            if exc.provider_reference and not exc.terminal_failure:
                return PersonalWeeklyBriefDelivery(
                    provider_reference=exc.provider_reference,
                    delivery_verified=False,
                )
            raise
        result = payload if isinstance(payload, dict) else {}
        return PersonalWeeklyBriefDelivery(
            provider_reference=str(result.get("processQueryKey") or "").strip(),
            delivery_verified=result.get("deliveryVerified") is True,
            delivered_dingtalk_user_ids=tuple(
                str(value).strip()
                for value in result.get("deliveryRecipientUserIds", ())
                if str(value).strip()
            ),
        )

    async def query_private_delivery(
        self, *, provider_reference: str
    ) -> PersonalWeeklyBriefDelivery:
        from app.services.dingtalk import DingTalkDeliveryError, _delivery_user_ids

        try:
            payload = await self._robot.get_robot_direct_message_status(
                process_query_key=provider_reference,
            )
        except (OSError, RuntimeError, TimeoutError):
            return PersonalWeeklyBriefDelivery(
                provider_reference=provider_reference,
                delivery_verified=False,
            )
        status = str(payload.get("sendStatus") or "").upper()
        if status and status != "SUCCESS":
            raise DingTalkDeliveryError(
                f"DingTalk direct robot delivery failed with status={status}.",
                provider_reference=provider_reference,
                terminal_failure=True,
            )
        delivered = tuple(sorted(_delivery_user_ids(payload)))
        return PersonalWeeklyBriefDelivery(
            provider_reference=provider_reference,
            delivery_verified=status == "SUCCESS" and bool(delivered),
            delivered_dingtalk_user_ids=delivered,
        )


class PersonalWeeklyBriefStorePort(Protocol):
    async def claim(self, **kwargs) -> PersonalWeeklyBriefRecord: ...

    async def persist_claim(self) -> None: ...

    async def record_provider_acceptance(self, **kwargs) -> PersonalWeeklyBriefRecord: ...

    async def persist_provider_acceptance(self) -> None: ...

    async def record_delivery(self, **kwargs) -> PersonalWeeklyBriefRecord: ...

    async def record_failure(self, **kwargs) -> PersonalWeeklyBriefRecord: ...


class PersonalWeeklyBriefDispatcher:
    """Fail closed: an accepted or ambiguously claimed brief is never resent."""

    def __init__(
        self,
        *,
        store: PersonalWeeklyBriefStorePort,
        transport: PersonalWeeklyBriefTransport,
        tenant_id: str,
        allowed_user_ids: frozenset[str],
    ) -> None:
        self._store = store
        self._transport = transport
        self._tenant_id = tenant_id
        self._allowed_user_ids = allowed_user_ids

    async def dispatch(
        self,
        *,
        row: PersonalWeeklyBriefRecord,
        recipient: PersonalWeeklyBriefRecipient,
        changed_at: datetime,
        claim_token: str,
    ) -> PersonalWeeklyBriefRecord:
        self._validate_scope(row=row, recipient=recipient, changed_at=changed_at)
        if row.status in {
            "claimed",
            "delivery_pending",
            "delivered",
            "failed",
            "cancelled",
        }:
            return row
        if row.status != "generated":
            raise ValueError("personal_weekly_brief_not_dispatchable")
        claimed = await self._store.claim(
            tenant_id=row.tenant_id,
            brief_id=row.brief_id,
            claim_token=claim_token,
            changed_at=changed_at,
        )
        await self._store.persist_claim()
        try:
            delivery = await self._transport.send_private_text_verified(
                dingtalk_user_id=recipient.dingtalk_user_id,
                text=claimed.message_text,
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            return await self._store.record_failure(
                tenant_id=row.tenant_id,
                brief_id=row.brief_id,
                error=f"transport_error:{type(exc).__name__}",
                changed_at=changed_at,
                expected_claim_token=claim_token,
            )
        if not delivery.provider_reference.strip():
            return await self._store.record_failure(
                tenant_id=row.tenant_id,
                brief_id=row.brief_id,
                error="provider_reference_missing",
                changed_at=changed_at,
                expected_claim_token=claim_token,
            )
        pending = await self._store.record_provider_acceptance(
            tenant_id=row.tenant_id,
            brief_id=row.brief_id,
            provider_message_id=delivery.provider_reference,
            expected_claim_token=claim_token,
            changed_at=changed_at,
        )
        await self._store.persist_provider_acceptance()
        if not delivery.delivery_verified:
            return pending
        if set(delivery.delivered_dingtalk_user_ids) != {recipient.dingtalk_user_id}:
            return await self._store.record_failure(
                tenant_id=row.tenant_id,
                brief_id=row.brief_id,
                error="delivery_recipient_mismatch",
                changed_at=changed_at,
            )
        return await self._store.record_delivery(
            tenant_id=row.tenant_id,
            brief_id=row.brief_id,
            delivery_receipt=_verified_delivery_receipt(
                delivery,
                checked_at=changed_at,
                evidence_source="send_response",
            ),
            changed_at=changed_at,
        )

    async def reconcile_pending(
        self,
        *,
        row: PersonalWeeklyBriefRecord,
        recipient: PersonalWeeklyBriefRecipient,
        changed_at: datetime,
    ) -> PersonalWeeklyBriefRecord:
        self._validate_scope(row=row, recipient=recipient, changed_at=changed_at)
        if row.status != "delivery_pending" or not row.provider_message_id.strip():
            raise ValueError("personal_weekly_brief_not_pending_verification")
        try:
            delivery = await self._transport.query_private_delivery(
                provider_reference=row.provider_message_id,
            )
        except RuntimeError as exc:
            if not bool(getattr(exc, "terminal_failure", False)):
                return row
            return await self._store.record_failure(
                tenant_id=row.tenant_id,
                brief_id=row.brief_id,
                error=f"delivery_query_terminal:{type(exc).__name__}",
                changed_at=changed_at,
            )
        if not delivery.delivery_verified:
            return row
        if (
            delivery.provider_reference != row.provider_message_id
            or set(delivery.delivered_dingtalk_user_ids) != {recipient.dingtalk_user_id}
        ):
            return await self._store.record_failure(
                tenant_id=row.tenant_id,
                brief_id=row.brief_id,
                error="delivery_recipient_mismatch",
                changed_at=changed_at,
            )
        return await self._store.record_delivery(
            tenant_id=row.tenant_id,
            brief_id=row.brief_id,
            delivery_receipt=_verified_delivery_receipt(
                delivery,
                checked_at=changed_at,
                evidence_source="delivery_query",
            ),
            changed_at=changed_at,
        )

    def _validate_scope(
        self,
        *,
        row: PersonalWeeklyBriefRecord,
        recipient: PersonalWeeklyBriefRecipient,
        changed_at: datetime,
    ) -> None:
        if (
            changed_at.tzinfo is None
            or changed_at.utcoffset() is None
            or not self._tenant_id
            or not self._allowed_user_ids
            or row.tenant_id != self._tenant_id
            or row.owner_user_id not in self._allowed_user_ids
            or recipient.tenant_id != row.tenant_id
            or recipient.internal_user_id != row.owner_user_id
            or recipient.conversation_id != row.conversation_id
            or not recipient.dingtalk_user_id.strip()
        ):
            raise ValueError("personal_weekly_brief_scope_invalid")


def _verified_delivery_receipt(
    delivery: PersonalWeeklyBriefDelivery,
    *,
    checked_at: datetime,
    evidence_source: str,
) -> dict[str, object]:
    if (
        checked_at.tzinfo is None
        or checked_at.utcoffset() is None
        or not delivery.provider_reference.strip()
        or delivery.delivery_verified is not True
        or not delivery.delivered_dingtalk_user_ids
        or evidence_source not in {"send_response", "delivery_query"}
    ):
        raise ValueError("personal_weekly_brief_delivery_receipt_invalid")
    return {
        "schema_version": "agent2.personal_weekly_brief.delivery.v1",
        "provider_reference": delivery.provider_reference,
        "delivery_verified": True,
        "delivery_status": "SUCCESS",
        "delivered_dingtalk_user_ids": list(delivery.delivered_dingtalk_user_ids),
        "checked_at": checked_at.isoformat(),
        "evidence_source": evidence_source,
    }


__all__ = [
    "DingTalkPersonalWeeklyBriefTransport",
    "PersonalWeeklyBriefDelivery",
    "PersonalWeeklyBriefDispatcher",
    "PersonalWeeklyBriefRecipient",
]
