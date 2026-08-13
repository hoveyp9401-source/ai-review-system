"""Fail-closed private delivery for one weekly-plan reminder outbox row."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.agent2.weekly_plan_reminder_outbox import WeeklyPlanReminderOutbox


@dataclass(frozen=True)
class WeeklyPlanReminderRecipient:
    tenant_id: str
    internal_user_id: str
    dingtalk_user_id: str


@dataclass(frozen=True)
class WeeklyPlanReminderDelivery:
    provider_reference: str
    delivery_verified: bool
    delivered_dingtalk_user_ids: tuple[str, ...] = ()


class WeeklyPlanReminderTransport(Protocol):
    async def send_private_text_verified(
        self, *, dingtalk_user_id: str, text: str
    ) -> WeeklyPlanReminderDelivery: ...

    async def query_private_delivery(
        self, *, provider_reference: str
    ) -> WeeklyPlanReminderDelivery: ...


class DingTalkWeeklyPlanReminderTransport:
    """Adapt DingTalk's verified direct-message result without hiding pending state."""

    def __init__(self, robot) -> None:
        self._robot = robot

    async def send_private_text_verified(
        self, *, dingtalk_user_id: str, text: str
    ) -> WeeklyPlanReminderDelivery:
        from app.services.dingtalk import DingTalkDeliveryError

        try:
            result = await self._robot.send_robot_direct_text_verified(
                user_ids=[dingtalk_user_id],
                text=text,
            )
        except DingTalkDeliveryError as exc:
            if exc.provider_reference and not exc.terminal_failure:
                return WeeklyPlanReminderDelivery(
                    provider_reference=exc.provider_reference,
                    delivery_verified=False,
                )
            raise
        payload = result if isinstance(result, dict) else {}
        provider_reference = str(payload.get("processQueryKey") or "").strip()
        delivered = tuple(
            str(value).strip()
            for value in payload.get("deliveryRecipientUserIds", ())
            if str(value).strip()
        )
        return WeeklyPlanReminderDelivery(
            provider_reference=provider_reference,
            delivery_verified=payload.get("deliveryVerified") is True,
            delivered_dingtalk_user_ids=delivered,
        )

    async def query_private_delivery(
        self, *, provider_reference: str
    ) -> WeeklyPlanReminderDelivery:
        from app.services.dingtalk import DingTalkDeliveryError, _delivery_user_ids

        try:
            payload = await self._robot.get_robot_direct_message_status(
                process_query_key=provider_reference,
            )
        except (OSError, RuntimeError, TimeoutError):
            return WeeklyPlanReminderDelivery(
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
        return WeeklyPlanReminderDelivery(
            provider_reference=provider_reference,
            delivery_verified=status == "SUCCESS" and bool(delivered),
            delivered_dingtalk_user_ids=delivered,
        )


class WeeklyPlanReminderOutboxPort(Protocol):
    async def claim(self, **kwargs) -> WeeklyPlanReminderOutbox: ...

    async def persist_claim(self) -> None: ...

    async def record_provider_acceptance(self, **kwargs) -> WeeklyPlanReminderOutbox: ...

    async def persist_provider_acceptance(self) -> None: ...

    async def record_delivery(self, **kwargs) -> WeeklyPlanReminderOutbox: ...

    async def record_failure(self, **kwargs) -> WeeklyPlanReminderOutbox: ...


class WeeklyPlanReminderDispatcher:
    """Send exactly once; provider acceptance is never reported as delivery."""

    def __init__(
        self,
        *,
        outbox: WeeklyPlanReminderOutboxPort,
        transport: WeeklyPlanReminderTransport,
        tenant_allowlist: frozenset[str],
        user_allowlist: frozenset[str],
    ) -> None:
        self._outbox = outbox
        self._transport = transport
        self._tenant_allowlist = tenant_allowlist
        self._user_allowlist = user_allowlist

    async def dispatch(
        self,
        *,
        row: WeeklyPlanReminderOutbox,
        recipient: WeeklyPlanReminderRecipient,
        changed_at: datetime,
        claim_token: str,
    ) -> WeeklyPlanReminderOutbox:
        self._validate_scope(row=row, recipient=recipient, changed_at=changed_at)
        # An accepted request may already be in flight.  Never send it again.
        if row.status in {"delivery_pending", "delivered", "failed", "cancelled"}:
            return row
        if row.status not in {"queued", "claimed"}:
            raise ValueError("weekly_plan_reminder_not_dispatchable")
        claimed = await self._outbox.claim(
            tenant_id=row.tenant_id,
            outbox_id=row.outbox_id,
            claim_token=claim_token,
            changed_at=changed_at,
        )
        await self._outbox.persist_claim()
        try:
            delivery = await self._transport.send_private_text_verified(
                dingtalk_user_id=recipient.dingtalk_user_id,
                text=_reminder_text(claimed),
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            return await self._outbox.record_failure(
                tenant_id=row.tenant_id,
                outbox_id=row.outbox_id,
                error=f"transport_error:{type(exc).__name__}",
                changed_at=changed_at,
                expected_claim_token=claim_token,
            )
        if not delivery.provider_reference.strip():
            return await self._outbox.record_failure(
                tenant_id=row.tenant_id,
                outbox_id=row.outbox_id,
                error="provider_reference_missing",
                changed_at=changed_at,
                expected_claim_token=claim_token,
            )
        pending = await self._outbox.record_provider_acceptance(
            tenant_id=row.tenant_id,
            outbox_id=row.outbox_id,
            provider_message_id=delivery.provider_reference,
            expected_claim_token=claim_token,
            changed_at=changed_at,
        )
        await self._outbox.persist_provider_acceptance()
        if not delivery.delivery_verified:
            return pending
        if set(delivery.delivered_dingtalk_user_ids) != {
            recipient.dingtalk_user_id
        }:
            return await self._outbox.record_failure(
                tenant_id=row.tenant_id,
                outbox_id=row.outbox_id,
                error="delivery_recipient_mismatch",
                changed_at=changed_at,
            )
        return await self._outbox.record_delivery(
            tenant_id=row.tenant_id,
            outbox_id=row.outbox_id,
            changed_at=changed_at,
        )

    async def reconcile_pending(
        self,
        *,
        row: WeeklyPlanReminderOutbox,
        recipient: WeeklyPlanReminderRecipient,
        changed_at: datetime,
    ) -> WeeklyPlanReminderOutbox:
        """Verify an accepted message later without ever sending it again."""

        self._validate_scope(row=row, recipient=recipient, changed_at=changed_at)
        if row.status != "delivery_pending" or not row.provider_message_id.strip():
            raise ValueError("weekly_plan_reminder_not_pending_verification")
        try:
            delivery = await self._transport.query_private_delivery(
                provider_reference=row.provider_message_id,
            )
        except RuntimeError as exc:  # provider marks explicit terminal failure
            if not bool(getattr(exc, "terminal_failure", False)):
                return row
            return await self._outbox.record_failure(
                tenant_id=row.tenant_id,
                outbox_id=row.outbox_id,
                error=f"delivery_query_terminal:{type(exc).__name__}",
                changed_at=changed_at,
            )
        if not delivery.delivery_verified:
            return row
        if (
            delivery.provider_reference != row.provider_message_id
            or set(delivery.delivered_dingtalk_user_ids)
            != {recipient.dingtalk_user_id}
        ):
            return await self._outbox.record_failure(
                tenant_id=row.tenant_id,
                outbox_id=row.outbox_id,
                error="delivery_recipient_mismatch",
                changed_at=changed_at,
            )
        return await self._outbox.record_delivery(
            tenant_id=row.tenant_id,
            outbox_id=row.outbox_id,
            changed_at=changed_at,
        )

    def _validate_scope(
        self,
        *,
        row: WeeklyPlanReminderOutbox,
        recipient: WeeklyPlanReminderRecipient,
        changed_at: datetime,
    ) -> None:
        if changed_at.tzinfo is None or changed_at.utcoffset() is None:
            raise ValueError("weekly_plan_reminder_scope_invalid")
        if (
            not self._tenant_allowlist
            or not self._user_allowlist
            or row.channel != "private_chat"
            or row.tenant_id not in self._tenant_allowlist
            or row.recipient_internal_user_id not in self._user_allowlist
            or recipient.tenant_id != row.tenant_id
            or recipient.internal_user_id != row.recipient_internal_user_id
            or not recipient.dingtalk_user_id.strip()
        ):
            raise ValueError("weekly_plan_reminder_scope_invalid")


def _reminder_text(row: WeeklyPlanReminderOutbox) -> str:
    state_hint = {
        "unfilled": "还没有开始填写",
        "draft": "已有草稿，但还没有确认提交",
        "pending_confirmation": "正在等待你确认提交",
    }.get(row.collection_state, "还没有确认提交")
    monday = row.target_week_start.strftime("%m月%d日")
    return (
        f"你从{monday}开始的周工作计划{state_hint}。"
        "可以直接一句话告诉我周一到周六的安排，我会整理后请你确认。"
    )


__all__ = [
    "DingTalkWeeklyPlanReminderTransport",
    "WeeklyPlanReminderDelivery",
    "WeeklyPlanReminderDispatcher",
    "WeeklyPlanReminderRecipient",
    "WeeklyPlanReminderTransport",
]
