from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from app.agent2.business.models import (
    Agent2IdentityBinding,
    BusinessAuditEvent,
    NotificationOutbox,
    TravelCollaborationCandidate,
)
from app.agent2.business.notifications import (
    build_travel_notification_message,
    dispatch_notification_batch,
    mark_notification_failed,
    mark_notification_sent,
    reconcile_sent_notification_outcomes,
    recover_stale_notification_claims,
)
from app.agent2.business.travel_pipeline import _index_intents_by_id, _record_candidate_creation


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def test_travel_pipeline_indexes_postgres_uuid_ids_using_matcher_string_contract():
    intent_id = uuid4()
    intent = type("PersistedTravelIntent", (), {"travel_intent_id": intent_id})()

    indexed = _index_intents_by_id((intent,))

    assert indexed[str(intent_id)] is intent


class _FlushSession:
    def __init__(self):
        self.flush_count = 0
        self.commit_count = 0
        self.statements = []
        self.added = []

    async def scalar(self, statement):
        if isinstance(statement, Insert):
            self.statements.append(statement)
            return statement.compile(dialect=postgresql.dialect()).params["receipt_id"]
        return None

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1

    async def commit(self):
        self.commit_count += 1


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)


class _DispatchSession(_FlushSession):
    def __init__(self, event, binding):
        super().__init__()
        self.event = event
        self.binding = binding

    async def scalars(self, statement):
        return _Scalars((self.event,))

    async def scalar(self, statement):
        if isinstance(statement, Insert):
            return await super().scalar(statement)
        return self.binding


class _Transport:
    def __init__(self, response=None):
        self.response = response or {"processQueryKey": "delivery-123"}
        self.calls = []

    async def send_robot_direct_text(self, *, user_ids, text):
        self.calls.append({"user_ids": user_ids, "text": text})
        return self.response


class _CandidateReceiptSession:
    def __init__(self):
        self.statements = []
        self.added = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return statement.compile(dialect=postgresql.dialect()).params["receipt_id"]

    def add(self, value):
        self.added.append(value)


def _event() -> NotificationOutbox:
    return NotificationOutbox(
        notification_id=uuid4(),
        tenant_id="tenant-test",
        candidate_id=uuid4(),
        recipient_user_id="user-1",
        channel="dingtalk",
        message_type="travel_collaboration_question",
        message_json={"text": "测试通知"},
        idempotency_key="candidate:user-1",
        status="processing",
        retry_count=0,
        locked_by="worker-1",
        locked_at=NOW,
        response_json={},
        dispatch_history_json=[],
        created_at=NOW,
        updated_at=NOW,
    )


def test_travel_notification_discloses_only_peer_destination_dates_and_question():
    message = build_travel_notification_message(
        recipient_name="张三",
        peer_names=("李四", "王五"),
        destination="南京市",
        overlap_start=NOW,
        overlap_end=NOW + timedelta(days=1),
    )

    assert "李四、王五" in message["text"]
    assert "南京市" in message["text"]
    assert "2026-07-11" in message["text"]
    assert "2026-07-12" in message["text"]
    assert "是否需要协同" in message["text"]
    assert set(message) == {"text", "destination", "overlap_start", "overlap_end"}
    assert "案件" not in message["text"]
    assert "客户" not in message["text"]
    assert "金额" not in message["text"]


@pytest.mark.asyncio
async def test_mark_notification_sent_persists_transport_receipt_and_audit_history():
    event = _event()
    session = _FlushSession()

    await mark_notification_sent(
        session,  # type: ignore[arg-type]
        event,
        now=NOW,
        response={"processQueryKey": "delivery-123"},
    )

    assert event.status == "sent"
    assert event.sent_at == NOW
    assert event.external_message_id == "delivery-123"
    assert event.response_json == {"processQueryKey": "delivery-123"}
    assert event.locked_by == ""
    assert event.locked_at is None
    assert event.dispatch_history_json[-1]["outcome"] == "sent"
    assert event.dispatch_history_json[-1]["external_message_id"] == "delivery-123"
    assert len(session.statements) == 1
    receipt_params = session.statements[0].compile(dialect=postgresql.dialect()).params
    assert receipt_params["command_type"] == "dispatch_travel_notification"
    assert receipt_params["status"] == "executed"
    assert receipt_params["resource_id"] == str(event.notification_id)
    assert receipt_params["actual_write"] is True
    audits = [item for item in session.added if isinstance(item, BusinessAuditEvent)]
    assert len(audits) == 1
    assert audits[0].receipt_id == receipt_params["receipt_id"]
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_notification_without_external_transport_id_is_not_marked_sent():
    event = _event()
    session = _FlushSession()

    with pytest.raises(ValueError, match="transport receipt"):
        await mark_notification_sent(
            session,  # type: ignore[arg-type]
            event,
            now=NOW,
            response={"invalidStaffIdList": []},
        )

    assert event.status == "processing"
    assert event.sent_at is None
    assert not event.external_message_id
    assert session.statements == []


@pytest.mark.asyncio
async def test_notification_failure_retries_then_moves_to_dead_letter_with_history():
    event = _event()
    session = _FlushSession()

    await mark_notification_failed(
        session,  # type: ignore[arg-type]
        event,
        now=NOW,
        error_message="temporary network failure",
        max_attempts=2,
        retry_base_seconds=30,
    )

    assert event.status == "failed"
    assert event.retry_count == 1
    assert event.next_retry_at == NOW + timedelta(seconds=30)
    assert event.dispatch_history_json[-1]["outcome"] == "failed"

    event.status = "processing"
    event.locked_by = "worker-2"
    event.locked_at = NOW + timedelta(seconds=30)
    await mark_notification_failed(
        session,  # type: ignore[arg-type]
        event,
        now=NOW + timedelta(seconds=30),
        error_message="still unavailable",
        max_attempts=2,
        retry_base_seconds=30,
    )

    assert event.status == "dead_letter"
    assert event.retry_count == 2
    assert event.next_retry_at is None
    assert len(event.dispatch_history_json) == 2
    assert event.dispatch_history_json[-1]["outcome"] == "dead_letter"
    assert event.error_message == "still unavailable"
    assert len(session.statements) == 2
    receipts = [statement.compile(dialect=postgresql.dialect()).params for statement in session.statements]
    assert [item["status"] for item in receipts] == ["failed", "failed"]
    assert [item["after_json"]["status"] for item in receipts] == ["failed", "dead_letter"]
    assert all(item["error_code"] == "notification_dispatch_failed" for item in receipts)
    assert len([item for item in session.added if isinstance(item, BusinessAuditEvent)]) == 2


@pytest.mark.asyncio
async def test_dispatch_uses_real_dingtalk_identity_mapping_and_records_delivery(monkeypatch):
    persisted_outcomes = []

    async def _persist(session, outcomes, **scope):
        persisted_outcomes.append((outcomes, scope))
        return 1

    monkeypatch.setattr(
        "app.agent2.business.notifications.persist_operation_outcomes", _persist
    )
    event = _event()
    event.status = "pending"
    event.locked_by = ""
    event.locked_at = None
    binding = Agent2IdentityBinding(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        user_id="user-1",
        dingtalk_user_id="ding-user-1",
        display_name="张三",
        role_ids=["lawyer"],
        permission_scope_json={},
        active=True,
    )
    session = _DispatchSession(event, binding)
    transport = _Transport()

    summary = await dispatch_notification_batch(
        session,  # type: ignore[arg-type]
        transport,
        worker_id="worker-1",
        now=NOW,
        allowed_tenant_ids=("tenant-test",),
    )

    assert summary.claimed == summary.sent == 1
    assert summary.failed == summary.dead_letter == 0
    assert transport.calls == [{"user_ids": ["ding-user-1"], "text": "测试通知"}]
    assert event.status == "sent"
    assert event.external_message_id == "delivery-123"
    # Provider receipt is committed before the separately auditable Outcome.
    assert session.commit_count == 4
    assert len(persisted_outcomes) == 1
    outcome = persisted_outcomes[0][0][0]
    assert outcome.message_status == "accepted_by_provider"
    assert outcome.receipt_refs[0].external_message_id == "delivery-123"


@pytest.mark.asyncio
async def test_sent_notification_without_outcome_is_reconciled_without_resending(monkeypatch):
    persisted_outcomes = []

    async def _persist(session, outcomes, **scope):
        persisted_outcomes.extend(outcomes)
        return 1

    monkeypatch.setattr(
        "app.agent2.business.notifications.persist_operation_outcomes", _persist
    )
    event = _event()
    event.status = "sent"
    event.external_message_id = "delivery-reconcile-1"
    session = _DispatchSession(event, None)

    reconciled = await reconcile_sent_notification_outcomes(
        session,  # type: ignore[arg-type]
        now=NOW,
        allowed_tenant_ids=("tenant-test",),
        allowed_message_types=("travel_collaboration_question",),
    )

    assert reconciled == 1
    assert len(persisted_outcomes) == 1
    assert persisted_outcomes[0].message_status == "accepted_by_provider"
    assert event.status == "sent"


@pytest.mark.asyncio
async def test_missing_channel_identity_is_audited_and_dead_lettered_without_sending():
    event = _event()
    event.status = "pending"
    event.locked_by = ""
    event.locked_at = None
    session = _DispatchSession(event, None)
    transport = _Transport()

    summary = await dispatch_notification_batch(
        session,  # type: ignore[arg-type]
        transport,
        worker_id="worker-1",
        now=NOW,
        max_attempts=1,
        allowed_tenant_ids=("tenant-test",),
    )

    assert summary.claimed == summary.dead_letter == 1
    assert summary.sent == summary.failed == 0
    assert transport.calls == []
    assert event.status == "dead_letter"
    assert "active_dingtalk_identity_binding_missing" in event.error_message
    assert event.dispatch_history_json[-1]["outcome"] == "dead_letter"


@pytest.mark.asyncio
async def test_dispatch_is_fail_closed_when_no_test_tenant_allowlist_is_configured():
    event = _event()
    event.status = "pending"
    session = _DispatchSession(event, None)
    transport = _Transport()

    summary = await dispatch_notification_batch(
        session,  # type: ignore[arg-type]
        transport,
        worker_id="worker-1",
        now=NOW,
    )

    assert summary.claimed == summary.sent == summary.failed == summary.dead_letter == 0
    assert event.status == "pending"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_stale_processing_notification_is_dead_lettered_as_unknown_not_requeued():
    event = _event()
    event.status = "processing"
    event.locked_at = NOW - timedelta(minutes=20)
    session = _DispatchSession(event, None)

    recovered = await recover_stale_notification_claims(
        session,  # type: ignore[arg-type]
        now=NOW,
        stale_before=NOW - timedelta(minutes=10),
        allowed_tenant_ids=("tenant-test",),
    )

    assert recovered == (event,)
    assert event.status == "dead_letter"
    assert event.retry_count == 1
    assert event.error_message == "delivery_outcome_unknown_after_stale_processing"
    assert event.dispatch_history_json[-1]["outcome"] == "delivery_unknown"


@pytest.mark.asyncio
async def test_created_collaboration_candidate_has_system_receipt_and_audit():
    candidate = TravelCollaborationCandidate(
        candidate_id=uuid4(),
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        travel_intent_ids=[str(uuid4()), str(uuid4())],
        participant_ids=["user-1", "user-2"],
        destination="南京市",
        overlap_start=NOW,
        overlap_end=NOW + timedelta(days=1),
        match_reason="same_city_and_overlapping_date",
        match_score=0.99,
        status="notified",
        notification_ids=[],
        responses_json={},
        deduplication_key="candidate-dedup",
        version=2,
        expires_at=NOW + timedelta(days=2),
        created_at=NOW,
        updated_at=NOW,
    )
    notification = _event()
    notification.candidate_id = candidate.candidate_id
    session = _CandidateReceiptSession()

    await _record_candidate_creation(
        session,  # type: ignore[arg-type]
        candidate,
        notifications=(notification,),
        now=NOW,
    )

    assert len(session.statements) == 1
    params = session.statements[0].compile(dialect=postgresql.dialect()).params
    assert params["tenant_id"] == "tenant-test"
    assert params["command_type"] == "create_travel_collaboration_candidate"
    assert params["actual_write"] is True
    assert params["after_json"]["notification_ids"] == [str(notification.notification_id)]
    audits = [item for item in session.added if isinstance(item, BusinessAuditEvent)]
    assert len(audits) == 1
    assert audits[0].receipt_id == params["receipt_id"]
    assert audits[0].actor_user_id == "system:agent2_travel_matcher"
