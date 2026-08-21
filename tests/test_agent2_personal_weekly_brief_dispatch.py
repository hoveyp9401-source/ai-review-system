from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent2.personal_weekly_brief_delivery import (
    PersonalWeeklyBriefDelivery,
    PersonalWeeklyBriefDispatcher,
    PersonalWeeklyBriefRecipient,
)
from app.agent2.personal_weekly_brief_store import PersonalWeeklyBriefRecord


NOW = datetime(2026, 8, 22, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _record(status: str = "generated") -> PersonalWeeklyBriefRecord:
    return PersonalWeeklyBriefRecord(
        brief_id="11111111-1111-4111-8111-111111111111",
        tenant_id="tenant-a",
        owner_user_id="user-a",
        conversation_id="conversation-a",
        week_start=date(2026, 8, 17),
        week_end=date(2026, 8, 21),
        snapshot_at=NOW,
        source_snapshot={"sources": []},
        source_fingerprint="a" * 64,
        content_json={"completed": []},
        message_text="个人本周工作简报",
        llm_model="agent2-model",
        status=status,
        idempotency_key="personal-weekly:tenant-a:user-a:2026-08-17",
        created_at=NOW,
        updated_at=NOW,
    )


class _Store:
    def __init__(self) -> None:
        self.record = _record()
        self.claims = 0
        self.acceptances = 0
        self.deliveries = 0
        self.failures = 0

    async def claim(self, **kwargs):
        self.claims += 1
        self.record = replace(
            self.record,
            status="claimed",
            claim_token=kwargs["claim_token"],
        )
        return self.record

    async def persist_claim(self) -> None:
        return None

    async def record_provider_acceptance(self, **kwargs):
        self.acceptances += 1
        self.record = replace(
            self.record,
            status="delivery_pending",
            provider_message_id=kwargs["provider_message_id"],
            provider_accepted_at=kwargs["changed_at"],
        )
        return self.record

    async def persist_provider_acceptance(self) -> None:
        return None

    async def record_delivery(self, **kwargs):
        self.deliveries += 1
        self.record = replace(
            self.record,
            status="delivered",
            delivered_at=kwargs["changed_at"],
        )
        return self.record

    async def record_failure(self, **kwargs):
        self.failures += 1
        self.record = replace(
            self.record,
            status="failed",
            last_error=kwargs["error"],
        )
        return self.record


class _Transport:
    def __init__(self, delivery: PersonalWeeklyBriefDelivery) -> None:
        self.delivery = delivery
        self.sent_to: list[str] = []

    async def send_private_text_verified(self, *, dingtalk_user_id, text):
        assert text == "个人本周工作简报"
        self.sent_to.append(dingtalk_user_id)
        return self.delivery

    async def query_private_delivery(self, *, provider_reference):
        assert provider_reference == self.delivery.provider_reference
        return self.delivery


def _recipient(user_id: str = "user-a") -> PersonalWeeklyBriefRecipient:
    return PersonalWeeklyBriefRecipient(
        tenant_id="tenant-a",
        internal_user_id=user_id,
        dingtalk_user_id=f"ding-{user_id}",
        conversation_id=f"conversation-{user_id[-1]}",
    )


@pytest.mark.asyncio
async def test_private_dispatch_rejects_cross_user_recipient_before_send() -> None:
    store = _Store()
    transport = _Transport(
        PersonalWeeklyBriefDelivery(
            provider_reference="provider-1",
            delivery_verified=True,
            delivered_dingtalk_user_ids=("ding-user-b",),
        )
    )
    dispatcher = PersonalWeeklyBriefDispatcher(
        store=store,
        transport=transport,
        tenant_id="tenant-a",
        allowed_user_ids=frozenset({"user-a"}),
    )

    with pytest.raises(ValueError, match="personal_weekly_brief_scope_invalid"):
        await dispatcher.dispatch(
            row=_record(),
            recipient=_recipient("user-b"),
            changed_at=NOW,
            claim_token="claim-1",
        )

    assert transport.sent_to == []
    assert store.claims == 0


@pytest.mark.asyncio
async def test_provider_acceptance_is_pending_not_delivered_and_never_resent() -> None:
    store = _Store()
    transport = _Transport(
        PersonalWeeklyBriefDelivery(
            provider_reference="provider-1",
            delivery_verified=False,
        )
    )
    dispatcher = PersonalWeeklyBriefDispatcher(
        store=store,
        transport=transport,
        tenant_id="tenant-a",
        allowed_user_ids=frozenset({"user-a"}),
    )

    pending = await dispatcher.dispatch(
        row=_record(),
        recipient=_recipient(),
        changed_at=NOW,
        claim_token="claim-1",
    )
    duplicate = await dispatcher.dispatch(
        row=pending,
        recipient=_recipient(),
        changed_at=NOW,
        claim_token="claim-2",
    )

    assert pending.status == "delivery_pending"
    assert duplicate == pending
    assert transport.sent_to == ["ding-user-a"]
    assert store.acceptances == 1
    assert store.deliveries == 0


@pytest.mark.asyncio
async def test_only_exact_recipient_delivery_is_recorded_as_delivered() -> None:
    store = _Store()
    transport = _Transport(
        PersonalWeeklyBriefDelivery(
            provider_reference="provider-1",
            delivery_verified=True,
            delivered_dingtalk_user_ids=("ding-user-a",),
        )
    )
    dispatcher = PersonalWeeklyBriefDispatcher(
        store=store,
        transport=transport,
        tenant_id="tenant-a",
        allowed_user_ids=frozenset({"user-a"}),
    )

    delivered = await dispatcher.dispatch(
        row=_record(),
        recipient=_recipient(),
        changed_at=NOW,
        claim_token="claim-1",
    )

    assert delivered.status == "delivered"
    assert delivered.provider_message_id == "provider-1"
    assert store.deliveries == 1


@pytest.mark.asyncio
async def test_claimed_row_is_never_automatically_sent_again() -> None:
    store = _Store()
    transport = _Transport(
        PersonalWeeklyBriefDelivery(
            provider_reference="provider-1",
            delivery_verified=True,
            delivered_dingtalk_user_ids=("ding-user-a",),
        )
    )
    dispatcher = PersonalWeeklyBriefDispatcher(
        store=store,
        transport=transport,
        tenant_id="tenant-a",
        allowed_user_ids=frozenset({"user-a"}),
    )

    claimed = replace(_record(), status="claimed", claim_token="old-claim")
    result = await dispatcher.dispatch(
        row=claimed,
        recipient=_recipient(),
        changed_at=NOW,
        claim_token="new-claim",
    )

    assert result == claimed
    assert transport.sent_to == []
