from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from app.services.dingtalk import DingTalkOutboundContentError

from app.agent2.personal_weekly_brief_delivery import (
    DingTalkPersonalWeeklyBriefTransport,
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
            send_started_at=kwargs["changed_at"],
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
            final_verified_at=kwargs["changed_at"],
            delivery_receipt_json=kwargs["delivery_receipt"],
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

    async def send_private_text_accepted(self, *, dingtalk_user_id, text):
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
async def test_http_400_before_provider_acceptance_is_retry_safe_failure() -> None:
    store = _Store()

    class _RejectedTransport:
        async def send_private_text_accepted(self, **_kwargs):
            request = httpx.Request("POST", "https://api.dingtalk.invalid/send")
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError(
                "rejected before acceptance",
                request=request,
                response=response,
            )

    failed = await PersonalWeeklyBriefDispatcher(
        store=store,
        transport=_RejectedTransport(),
        tenant_id="tenant-a",
        allowed_user_ids=frozenset({"user-a"}),
    ).dispatch(
        row=_record(),
        recipient=_recipient(),
        changed_at=NOW,
        claim_token="claim-http-400",
    )

    assert failed.status == "failed"
    assert failed.last_error == "retry_safe_preacceptance:HTTPStatusError:400"
    assert store.acceptances == 0
    assert store.deliveries == 0


@pytest.mark.asyncio
async def test_dingtalk_transport_records_acceptance_before_separate_delivery_query() -> None:
    calls: list[str] = []

    class _Robot:
        async def send_robot_direct_text(self, **_kwargs):
            calls.append("accepted")
            return {"processQueryKey": "provider-two-phase"}

        async def send_robot_direct_text_verified(self, **_kwargs):
            raise AssertionError("weekly brief must not hide acceptance behind polling")

        async def get_robot_direct_message_status(self, **_kwargs):
                calls.append("verified")
                return {
                    "sendStatus": "SUCCESS",
                    "messageReadInfoList": [{"userId": "ding-user-a"}],
                }

    transport = DingTalkPersonalWeeklyBriefTransport(_Robot())
    accepted = await transport.send_private_text_accepted(
        dingtalk_user_id="ding-user-a",
        text="个人本周工作简报",
    )
    assert accepted.provider_reference == "provider-two-phase"
    assert accepted.delivery_verified is False
    assert calls == ["accepted"]

    delivered = await transport.query_private_delivery(
        provider_reference=accepted.provider_reference,
    )
    assert delivered.delivery_verified is True
    assert delivered.delivered_dingtalk_user_ids == ("ding-user-a",)
    assert calls == ["accepted", "verified"]


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
    assert delivered.delivery_receipt_json == {
        "schema_version": "agent2.personal_weekly_brief.delivery.v1",
        "provider_reference": "provider-1",
        "delivery_verified": True,
        "delivery_status": "SUCCESS",
        "delivered_dingtalk_user_ids": ["ding-user-a"],
        "checked_at": NOW.isoformat(),
        "evidence_source": "send_response",
    }
    assert store.deliveries == 1


@pytest.mark.asyncio
async def test_dispatch_records_distinct_observed_send_acceptance_and_verification_times() -> None:
    store = _Store()

    class _TwoPhaseTransport:
        async def send_private_text_accepted(self, **_kwargs):
            return PersonalWeeklyBriefDelivery(
                provider_reference="provider-timeline",
                delivery_verified=False,
            )

        async def query_private_delivery(self, *, provider_reference):
            assert provider_reference == "provider-timeline"
            return PersonalWeeklyBriefDelivery(
                provider_reference=provider_reference,
                delivery_verified=True,
                delivered_dingtalk_user_ids=("ding-user-a",),
            )

    transport = _TwoPhaseTransport()
    observed = iter(
        (
            NOW.replace(second=11),
            NOW.replace(second=19),
        )
    )
    dispatcher = PersonalWeeklyBriefDispatcher(
        store=store,
        transport=transport,
        tenant_id="tenant-a",
        allowed_user_ids=frozenset({"user-a"}),
        clock=lambda: next(observed),
    )

    delivered = await dispatcher.dispatch(
        row=_record(),
        recipient=_recipient(),
        changed_at=NOW.replace(second=3),
        claim_token="claim-timeline",
    )

    assert delivered.send_started_at == NOW.replace(second=3)
    assert delivered.provider_accepted_at == NOW.replace(second=11)
    assert delivered.delivered_at == NOW.replace(second=19)
    assert delivered.final_verified_at == NOW.replace(second=19)
    assert delivered.delivery_receipt_json["checked_at"] == NOW.replace(
        second=19
    ).isoformat()
    assert delivered.delivery_receipt_json["evidence_source"] == "delivery_query"


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


@pytest.mark.asyncio
async def test_local_pre_send_content_failure_is_marked_retry_safe() -> None:
    store = _Store()

    class _LocalFailureTransport:
        async def send_private_text_accepted(self, **_kwargs):
            raise DingTalkOutboundContentError("local validation blocked send")

    dispatcher = PersonalWeeklyBriefDispatcher(
        store=store,
        transport=_LocalFailureTransport(),
        tenant_id="tenant-a",
        allowed_user_ids=frozenset({"user-a"}),
    )

    failed = await dispatcher.dispatch(
        row=_record(),
        recipient=_recipient(),
        changed_at=NOW,
        claim_token="claim-safe-local-failure",
    )

    assert failed.status == "failed"
    assert failed.last_error == (
        "retry_safe_preacceptance:DingTalkOutboundContentError"
    )
