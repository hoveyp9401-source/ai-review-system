from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.agent2.weekly_plan_collection import WeeklyPlanReminderCandidate
from app.agent2.weekly_plan_reminder_dispatch import (
    DingTalkWeeklyPlanReminderTransport,
    WeeklyPlanReminderDelivery,
    WeeklyPlanReminderDispatcher,
    WeeklyPlanReminderRecipient,
)
from app.agent2.weekly_plan_reminder_outbox import (
    claim_weekly_plan_reminder,
    create_weekly_plan_reminder_outbox,
    fail_weekly_plan_reminder,
    record_weekly_plan_delivery,
    record_weekly_plan_provider_acceptance,
)

NOW = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)


def _row():
    return create_weekly_plan_reminder_outbox(
        WeeklyPlanReminderCandidate(
            tenant_id="tenant-a",
            batch_id="90000000-0000-4000-8000-000000000001",
            plan_id="90000000-0000-4000-8000-000000000002",
            target_week_start=date(2026, 8, 17),
            recipient_internal_user_id="user-a",
            collection_state="unfilled",
            reminder_at=NOW,
            idempotency_key="weekly-plan-reminder:slot-a",
        ),
        created_at=NOW,
    )


class _MemoryOutbox:
    def __init__(self, row, *, events=None, persist_error=None):
        self.row = row
        self.transitions = []
        self.events = events
        self.persist_error = persist_error

    async def claim(self, **kwargs):
        self.transitions.append("claimed")
        self.row = claim_weekly_plan_reminder(
            self.row,
            claim_token=kwargs["claim_token"],
            changed_at=kwargs["changed_at"],
        )
        return self.row

    async def persist_claim(self):
        if self.persist_error is not None:
            raise self.persist_error
        if self.events is not None:
            self.events.append("claim_committed")

    async def record_provider_acceptance(self, **kwargs):
        self.transitions.append("accepted")
        if self.events is not None:
            self.events.append("acceptance_recorded")
        self.row = record_weekly_plan_provider_acceptance(
            self.row,
            provider_message_id=kwargs["provider_message_id"],
            expected_claim_token=kwargs["expected_claim_token"],
            changed_at=kwargs["changed_at"],
        )
        return self.row

    async def persist_provider_acceptance(self):
        if self.events is not None:
            self.events.append("acceptance_committed")

    async def record_delivery(self, **kwargs):
        self.transitions.append("delivered")
        self.row = record_weekly_plan_delivery(
            self.row,
            changed_at=kwargs["changed_at"],
        )
        return self.row

    async def record_failure(self, **kwargs):
        self.transitions.append("failed")
        self.row = fail_weekly_plan_reminder(
            self.row,
            error=kwargs["error"],
            changed_at=kwargs["changed_at"],
            expected_claim_token=kwargs.get("expected_claim_token"),
        )
        return self.row


class _Transport:
    def __init__(self, delivery=None, *, error=None, events=None, query_delivery=None):
        self.delivery = delivery
        self.error = error
        self.calls = []
        self.events = events
        self.query_delivery = query_delivery
        self.query_calls = []

    async def send_private_text_verified(self, *, dingtalk_user_id, text):
        if self.events is not None:
            self.events.append("send")
        self.calls.append((dingtalk_user_id, text))
        if self.error is not None:
            raise self.error
        return self.delivery

    async def query_private_delivery(self, *, provider_reference):
        self.query_calls.append(provider_reference)
        if self.error is not None:
            raise self.error
        return self.query_delivery


@pytest.mark.asyncio
async def test_verified_private_delivery_reaches_delivered_only_for_exact_recipient():
    row = _row()
    events = []
    outbox = _MemoryOutbox(row, events=events)
    transport = _Transport(
        WeeklyPlanReminderDelivery(
            provider_reference="provider-1",
            delivery_verified=True,
            delivered_dingtalk_user_ids=("ding-a",),
        ),
        events=events,
    )
    dispatcher = WeeklyPlanReminderDispatcher(
        outbox=outbox,
        transport=transport,
        tenant_allowlist=frozenset({"tenant-a"}),
        user_allowlist=frozenset({"user-a"}),
    )

    result = await dispatcher.dispatch(
        row=row,
        recipient=WeeklyPlanReminderRecipient(
            tenant_id="tenant-a",
            internal_user_id="user-a",
            dingtalk_user_id="ding-a",
        ),
        changed_at=NOW,
        claim_token="claim-1",
    )

    assert result.status == "delivered"
    assert outbox.transitions == ["claimed", "accepted", "delivered"]
    assert transport.calls[0][0] == "ding-a"
    assert events == [
        "claim_committed",
        "send",
        "acceptance_recorded",
        "acceptance_committed",
    ]


@pytest.mark.asyncio
async def test_reminder_is_not_sent_when_durable_claim_cannot_be_saved():
    row = _row()
    outbox = _MemoryOutbox(
        row, persist_error=RuntimeError("database commit failed")
    )
    transport = _Transport(
        WeeklyPlanReminderDelivery(
            provider_reference="provider-1",
            delivery_verified=True,
            delivered_dingtalk_user_ids=("ding-a",),
        )
    )
    dispatcher = WeeklyPlanReminderDispatcher(
        outbox=outbox,
        transport=transport,
        tenant_allowlist=frozenset({"tenant-a"}),
        user_allowlist=frozenset({"user-a"}),
    )

    with pytest.raises(RuntimeError, match="database commit failed"):
        await dispatcher.dispatch(
            row=row,
            recipient=WeeklyPlanReminderRecipient(
                "tenant-a", "user-a", "ding-a"
            ),
            changed_at=NOW,
            claim_token="claim-1",
        )

    assert outbox.transitions == ["claimed"]
    assert transport.calls == []


@pytest.mark.asyncio
async def test_expired_claim_without_provider_reference_can_be_reclaimed_and_sent():
    claimed = claim_weekly_plan_reminder(
        _row(),
        claim_token="abandoned-worker",
        changed_at=NOW,
    )
    recovered_at = NOW + timedelta(minutes=16)
    outbox = _MemoryOutbox(claimed)
    transport = _Transport(
        WeeklyPlanReminderDelivery(
            provider_reference="provider-after-recovery",
            delivery_verified=True,
            delivered_dingtalk_user_ids=("ding-a",),
        )
    )
    dispatcher = WeeklyPlanReminderDispatcher(
        outbox=outbox,
        transport=transport,
        tenant_allowlist=frozenset({"tenant-a"}),
        user_allowlist=frozenset({"user-a"}),
    )

    result = await dispatcher.dispatch(
        row=claimed,
        recipient=WeeklyPlanReminderRecipient("tenant-a", "user-a", "ding-a"),
        changed_at=recovered_at,
        claim_token="recovery-worker",
    )

    assert result.status == "delivered"
    assert result.claim_token == "recovery-worker"
    assert result.retry_count == 1
    assert outbox.transitions == ["claimed", "accepted", "delivered"]
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_unexpired_claim_cannot_be_reclaimed_or_sent():
    claimed = claim_weekly_plan_reminder(
        _row(),
        claim_token="active-worker",
        changed_at=NOW,
    )
    outbox = _MemoryOutbox(claimed)
    transport = _Transport(
        WeeklyPlanReminderDelivery(
            provider_reference="must-not-be-used",
            delivery_verified=True,
            delivered_dingtalk_user_ids=("ding-a",),
        )
    )
    dispatcher = WeeklyPlanReminderDispatcher(
        outbox=outbox,
        transport=transport,
        tenant_allowlist=frozenset({"tenant-a"}),
        user_allowlist=frozenset({"user-a"}),
    )

    with pytest.raises(ValueError, match="weekly_plan_reminder_not_claimable"):
        await dispatcher.dispatch(
            row=claimed,
            recipient=WeeklyPlanReminderRecipient(
                "tenant-a", "user-a", "ding-a"
            ),
            changed_at=NOW + timedelta(minutes=14),
            claim_token="competing-worker",
        )

    assert transport.calls == []


def test_reclaimed_reminder_rejects_the_abandoned_workers_provider_acceptance():
    first_claim = claim_weekly_plan_reminder(
        _row(), claim_token="abandoned-worker", changed_at=NOW
    )
    reclaimed = claim_weekly_plan_reminder(
        first_claim,
        claim_token="recovery-worker",
        changed_at=NOW + timedelta(minutes=16),
    )

    with pytest.raises(
        ValueError, match="weekly_plan_reminder_provider_acceptance_invalid"
    ):
        record_weekly_plan_provider_acceptance(
            reclaimed,
            provider_message_id="provider-from-abandoned-worker",
            expected_claim_token="abandoned-worker",
            changed_at=NOW + timedelta(minutes=17),
        )

    accepted = record_weekly_plan_provider_acceptance(
        reclaimed,
        provider_message_id="provider-from-recovery-worker",
        expected_claim_token="recovery-worker",
        changed_at=NOW + timedelta(minutes=17),
    )
    assert accepted.status == "delivery_pending"
    assert accepted.provider_message_id == "provider-from-recovery-worker"


def test_reclaimed_reminder_rejects_the_abandoned_workers_failure():
    first_claim = claim_weekly_plan_reminder(
        _row(), claim_token="abandoned-worker", changed_at=NOW
    )
    reclaimed = claim_weekly_plan_reminder(
        first_claim,
        claim_token="recovery-worker",
        changed_at=NOW + timedelta(minutes=16),
    )

    with pytest.raises(ValueError, match="weekly_plan_reminder_failure_invalid"):
        fail_weekly_plan_reminder(
            reclaimed,
            error="late failure from abandoned worker",
            expected_claim_token="abandoned-worker",
            changed_at=NOW + timedelta(minutes=17),
        )

    assert reclaimed.status == "claimed"
    assert reclaimed.claim_token == "recovery-worker"


@pytest.mark.asyncio
async def test_provider_acceptance_without_final_delivery_stays_pending_and_is_not_resent():
    row = _row()
    outbox = _MemoryOutbox(row)
    transport = _Transport(
        WeeklyPlanReminderDelivery(
            provider_reference="provider-pending",
            delivery_verified=False,
            delivered_dingtalk_user_ids=(),
        )
    )
    dispatcher = WeeklyPlanReminderDispatcher(
        outbox=outbox,
        transport=transport,
        tenant_allowlist=frozenset({"tenant-a"}),
        user_allowlist=frozenset({"user-a"}),
    )

    pending = await dispatcher.dispatch(
        row=row,
        recipient=WeeklyPlanReminderRecipient("tenant-a", "user-a", "ding-a"),
        changed_at=NOW,
        claim_token="claim-1",
    )
    replay = await dispatcher.dispatch(
        row=pending,
        recipient=WeeklyPlanReminderRecipient("tenant-a", "user-a", "ding-a"),
        changed_at=NOW,
        claim_token="claim-2",
    )

    assert pending.status == "delivery_pending"
    assert replay == pending
    assert len(transport.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recipient,tenant_allowlist,user_allowlist",
    [
        (WeeklyPlanReminderRecipient("tenant-b", "user-a", "ding-a"), frozenset({"tenant-a"}), frozenset({"user-a"})),
        (WeeklyPlanReminderRecipient("tenant-a", "user-b", "ding-b"), frozenset({"tenant-a"}), frozenset({"user-a"})),
        (WeeklyPlanReminderRecipient("tenant-a", "user-a", ""), frozenset({"tenant-a"}), frozenset({"user-a"})),
    ],
)
async def test_scope_or_binding_mismatch_fails_closed_before_claim(
    recipient, tenant_allowlist, user_allowlist
):
    row = _row()
    outbox = _MemoryOutbox(row)
    transport = _Transport()
    dispatcher = WeeklyPlanReminderDispatcher(
        outbox=outbox,
        transport=transport,
        tenant_allowlist=tenant_allowlist,
        user_allowlist=user_allowlist,
    )

    with pytest.raises(ValueError, match="weekly_plan_reminder_scope_invalid"):
        await dispatcher.dispatch(
            row=row,
            recipient=recipient,
            changed_at=NOW,
            claim_token="claim-1",
        )

    assert outbox.transitions == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_wrong_verified_recipient_is_recorded_as_failure_not_delivery():
    row = _row()
    outbox = _MemoryOutbox(row)
    transport = _Transport(
        WeeklyPlanReminderDelivery(
            provider_reference="provider-wrong-recipient",
            delivery_verified=True,
            delivered_dingtalk_user_ids=("ding-somebody-else",),
        )
    )
    dispatcher = WeeklyPlanReminderDispatcher(
        outbox=outbox,
        transport=transport,
        tenant_allowlist=frozenset({"tenant-a"}),
        user_allowlist=frozenset({"user-a"}),
    )

    result = await dispatcher.dispatch(
        row=row,
        recipient=WeeklyPlanReminderRecipient("tenant-a", "user-a", "ding-a"),
        changed_at=NOW,
        claim_token="claim-1",
    )

    assert result.status == "failed"
    assert outbox.transitions == ["claimed", "accepted", "failed"]


@pytest.mark.asyncio
async def test_transport_failure_is_recorded_and_never_claimed_as_sent():
    row = _row()
    outbox = _MemoryOutbox(row)
    transport = _Transport(error=RuntimeError("provider unavailable"))
    dispatcher = WeeklyPlanReminderDispatcher(
        outbox=outbox,
        transport=transport,
        tenant_allowlist=frozenset({"tenant-a"}),
        user_allowlist=frozenset({"user-a"}),
    )

    result = await dispatcher.dispatch(
        row=row,
        recipient=WeeklyPlanReminderRecipient("tenant-a", "user-a", "ding-a"),
        changed_at=NOW,
        claim_token="claim-1",
    )

    assert result.status == "failed"
    assert result.provider_message_id == ""
    assert outbox.transitions == ["claimed", "failed"]


@pytest.mark.asyncio
async def test_dingtalk_adapter_keeps_provider_acceptance_without_delivery_pending():
    class _Robot:
        async def send_robot_direct_text_verified(self, **kwargs):
            del kwargs
            return {
                "processQueryKey": "query-1",
                "deliveryVerified": False,
                "deliveryRecipientUserIds": [],
            }

    result = await DingTalkWeeklyPlanReminderTransport(
        _Robot()
    ).send_private_text_verified(dingtalk_user_id="ding-a", text="reminder")

    assert result.provider_reference == "query-1"
    assert result.delivery_verified is False
    assert result.delivered_dingtalk_user_ids == ()


@pytest.mark.asyncio
async def test_dingtalk_adapter_preserves_exact_verified_recipient():
    class _Robot:
        async def send_robot_direct_text_verified(self, **kwargs):
            del kwargs
            return {
                "processQueryKey": "query-2",
                "deliveryVerified": True,
                "deliveryRecipientUserIds": ["ding-a"],
            }

    result = await DingTalkWeeklyPlanReminderTransport(
        _Robot()
    ).send_private_text_verified(dingtalk_user_id="ding-a", text="reminder")

    assert result.delivery_verified is True
    assert result.delivered_dingtalk_user_ids == ("ding-a",)


@pytest.mark.asyncio
async def test_dingtalk_adapter_queries_existing_reference_without_sending():
    class _Robot:
        sent = False

        async def send_robot_direct_text_verified(self, **kwargs):
            del kwargs
            self.sent = True
            raise AssertionError("reconciliation must not resend")

        async def get_robot_direct_message_status(self, **kwargs):
            assert kwargs == {"process_query_key": "query-existing"}
            return {
                "sendStatus": "SUCCESS",
                "messageReadInfoList": [{"userId": "ding-a"}],
            }

    robot = _Robot()
    result = await DingTalkWeeklyPlanReminderTransport(
        robot
    ).query_private_delivery(provider_reference="query-existing")

    assert robot.sent is False
    assert result.delivery_verified is True
    assert result.delivered_dingtalk_user_ids == ("ding-a",)


@pytest.mark.asyncio
async def test_pending_acceptance_is_reconciled_without_resending():
    pending = record_weekly_plan_provider_acceptance(
        claim_weekly_plan_reminder(
            _row(), claim_token="claim-1", changed_at=NOW
        ),
        provider_message_id="provider-pending",
        expected_claim_token="claim-1",
        changed_at=NOW,
    )
    outbox = _MemoryOutbox(pending)
    transport = _Transport(
        query_delivery=WeeklyPlanReminderDelivery(
            provider_reference="provider-pending",
            delivery_verified=True,
            delivered_dingtalk_user_ids=("ding-a",),
        )
    )
    dispatcher = WeeklyPlanReminderDispatcher(
        outbox=outbox,
        transport=transport,
        tenant_allowlist=frozenset({"tenant-a"}),
        user_allowlist=frozenset({"user-a"}),
    )

    result = await dispatcher.reconcile_pending(
        row=pending,
        recipient=WeeklyPlanReminderRecipient("tenant-a", "user-a", "ding-a"),
        changed_at=NOW,
    )

    assert result.status == "delivered"
    assert transport.calls == []
    assert transport.query_calls == ["provider-pending"]
    assert outbox.transitions == ["delivered"]


@pytest.mark.asyncio
async def test_pending_acceptance_stays_pending_when_delivery_is_not_ready():
    pending = record_weekly_plan_provider_acceptance(
        claim_weekly_plan_reminder(
            _row(), claim_token="claim-1", changed_at=NOW
        ),
        provider_message_id="provider-pending",
        expected_claim_token="claim-1",
        changed_at=NOW,
    )
    outbox = _MemoryOutbox(pending)
    transport = _Transport(
        query_delivery=WeeklyPlanReminderDelivery(
            provider_reference="provider-pending",
            delivery_verified=False,
        )
    )
    dispatcher = WeeklyPlanReminderDispatcher(
        outbox=outbox,
        transport=transport,
        tenant_allowlist=frozenset({"tenant-a"}),
        user_allowlist=frozenset({"user-a"}),
    )

    result = await dispatcher.reconcile_pending(
        row=pending,
        recipient=WeeklyPlanReminderRecipient("tenant-a", "user-a", "ding-a"),
        changed_at=NOW,
    )

    assert result == pending
    assert transport.calls == []
    assert outbox.transitions == []
