from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from app.agent2.business.case_followups import (
    active_case_progress_followup_resources,
    build_case_progress_followup_message,
    enqueue_case_progress_followup,
)
from app.agent2.business.models import Agent2Case, Agent2IdentityBinding, NotificationOutbox
from app.agent2.business.notifications import mark_notification_sent
from app.agent2.business.notifications import dispatch_notification_batch


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


def test_case_progress_followup_names_one_case_and_explains_evidence_boundary():
    message = build_case_progress_followup_message(
        case_name="Agent2 Sandbox 测试案件 1",
        case_number="（2026）苏01测1号",
    )

    assert "Agent2 Sandbox 测试案件 1" in message["text"]
    assert "（2026）苏01测1号" in message["text"]
    assert "（（2026）" not in message["text"]
    assert "请直接回复" in message["text"]
    assert "不会被视为法院正式事实" in message["text"]
    assert set(message) == {"text", "case_name", "case_number"}


class _FollowupSession:
    def __init__(self, binding, case):
        self.binding = binding
        self.case = case
        self.event = None
        self.insert_params = None
        self.flush_count = 0

    async def scalar(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "agent2_identity_bindings" in sql:
            return self.binding
        if "agent2_cases" in sql:
            return self.case
        if isinstance(statement, Insert):
            self.insert_params = statement.compile(dialect=postgresql.dialect()).params
            self.event = NotificationOutbox(**self.insert_params)
            return self.event.notification_id
        return None

    async def get(self, model, key):
        assert model is NotificationOutbox
        return self.event if self.event is not None and self.event.notification_id == key else None

    async def flush(self):
        self.flush_count += 1


@pytest.mark.asyncio
async def test_manual_case_followup_trigger_creates_one_permission_bound_outbox_task():
    case_id = uuid4()
    binding = Agent2IdentityBinding(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        user_id="user-1",
        dingtalk_user_id="ding-user-1",
        display_name="张三",
        role_ids=["lawyer"],
        permission_scope_json={"allowed_case_ids": [str(case_id)]},
        active=True,
    )
    case = Agent2Case(
        case_id=case_id,
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        external_case_id="case-1",
        case_number="（2026）苏01测1号",
        case_name="Agent2 Sandbox 测试案件 1",
        case_type="litigation",
        status="open",
        owner_user_id="user-1",
        source_type="sandbox_fixture",
        source_id="case-source-1",
        source_json={},
    )
    session = _FollowupSession(binding, case)

    event = await enqueue_case_progress_followup(
        session,  # type: ignore[arg-type]
        tenant_id="tenant-test",
        case_id=str(case_id),
        recipient_user_id="user-1",
        trigger_id="manual-20260712-1",
        now=NOW,
        expires_at=NOW + timedelta(days=2),
        allowed_tenant_ids=("tenant-test",),
        allowed_user_ids=("user-1",),
    )

    assert event.message_type == "case_progress_followup"
    assert event.recipient_user_id == "user-1"
    assert event.candidate_id is None
    assert event.status == "pending"
    assert event.message_json["case_id"] == str(case_id)
    assert event.message_json["trigger_id"] == "manual-20260712-1"
    assert event.message_json["expires_at"] == (NOW + timedelta(days=2)).isoformat()
    assert "Agent2 Sandbox 测试案件 1" in event.message_json["text"]
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_case_followup_trigger_fails_closed_outside_specific_tenant_user_cohort():
    session = _FollowupSession(None, None)

    with pytest.raises(PermissionError, match="follow-up cohort"):
        await enqueue_case_progress_followup(
            session,  # type: ignore[arg-type]
            tenant_id="tenant-test",
            case_id=str(uuid4()),
            recipient_user_id="user-2",
            trigger_id="manual-outside-cohort",
            now=NOW,
            expires_at=NOW + timedelta(days=1),
            allowed_tenant_ids=("tenant-test",),
            allowed_user_ids=("user-1",),
        )


class _AttemptSession:
    def __init__(self):
        self.statements = []
        self.added = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return statement.compile(dialect=postgresql.dialect()).params["receipt_id"]

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_case_followup_delivery_writes_case_specific_transport_receipt():
    event = NotificationOutbox(
        notification_id=uuid4(),
        tenant_id="tenant-test",
        candidate_id=None,
        recipient_user_id="user-1",
        channel="dingtalk",
        message_type="case_progress_followup",
        message_json={"text": "请回复案件进展", "case_id": str(uuid4())},
        idempotency_key="case-followup-1",
        status="processing",
        retry_count=0,
        locked_by="worker-1",
        locked_at=NOW,
        response_json={},
        dispatch_history_json=[],
        created_at=NOW,
        updated_at=NOW,
    )
    session = _AttemptSession()

    await mark_notification_sent(
        session,  # type: ignore[arg-type]
        event,
        now=NOW,
        response={"processQueryKey": "delivery-followup-1"},
    )

    params = session.statements[0].compile(dialect=postgresql.dialect()).params
    assert params["command_type"] == "dispatch_case_progress_followup"
    assert params["resource_type"] == "case_progress_followup_notification"
    assert params["resource_id"] == str(event.notification_id)
    assert params["actual_write"] is True


def test_only_delivered_unexpired_unanswered_followup_becomes_reply_context():
    case_id = str(uuid4())
    event = NotificationOutbox(
        notification_id=uuid4(),
        tenant_id="tenant-test",
        candidate_id=None,
        recipient_user_id="user-1",
        channel="dingtalk",
        message_type="case_progress_followup",
        message_json={
            "case_id": case_id,
            "case_name": "Agent2 Sandbox 测试案件 1",
            "case_number": "（2026）苏01测1号",
            "expires_at": (NOW + timedelta(days=1)).isoformat(),
        },
        idempotency_key="case-followup-resource-1",
        status="sent",
        retry_count=0,
        external_message_id="delivery-1",
        response_json={},
        dispatch_history_json=[],
        created_at=NOW,
        updated_at=NOW,
    )

    resources = active_case_progress_followup_resources(
        (event,),
        allowed_case_ids=(case_id,),
        now=NOW,
    )

    assert resources == (
        {
            "notification_id": str(event.notification_id),
            "case_id": case_id,
            "case_name": "Agent2 Sandbox 测试案件 1",
            "case_number": "（2026）苏01测1号",
            "expires_at": (NOW + timedelta(days=1)).isoformat(),
        },
    )

    event.response_json = {"followup_status": "completed"}
    assert (
        active_case_progress_followup_resources(
            (event,), allowed_case_ids=(case_id,), now=NOW
        )
        == ()
    )


class _Rows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)


class _DispatchFollowupSession(_AttemptSession):
    def __init__(self, event, binding, case):
        super().__init__()
        self.event = event
        self.binding = binding
        self.case = case
        self.commit_count = 0

    async def scalars(self, _statement):
        return _Rows((self.event,))

    async def scalar(self, statement):
        if isinstance(statement, Insert):
            return await super().scalar(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "agent2_identity_bindings" in sql:
            return self.binding
        if "agent2_cases" in sql:
            return self.case
        return None

    async def commit(self):
        self.commit_count += 1


class _NoSendTransport:
    def __init__(self):
        self.calls = []

    async def send_robot_direct_text(self, *, user_ids, text):
        self.calls.append({"user_ids": user_ids, "text": text})
        return {"processQueryKey": "must-not-send"}


@pytest.mark.asyncio
async def test_expired_case_followup_is_cancelled_before_transport():
    case_id = uuid4()
    binding = Agent2IdentityBinding(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        user_id="user-1",
        dingtalk_user_id="ding-user-1",
        display_name="张三",
        role_ids=["lawyer"],
        permission_scope_json={"allowed_case_ids": [str(case_id)]},
        active=True,
    )
    case = Agent2Case(
        case_id=case_id,
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        external_case_id="case-1",
        case_number="1",
        case_name="案件 1",
        case_type="litigation",
        status="open",
        owner_user_id="user-1",
        source_type="sandbox_fixture",
        source_id="case-1",
        source_json={},
    )
    event = NotificationOutbox(
        notification_id=uuid4(),
        tenant_id="tenant-test",
        candidate_id=None,
        recipient_user_id="user-1",
        channel="dingtalk",
        message_type="case_progress_followup",
        message_json={
            "text": "追问",
            "case_id": str(case_id),
            "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
        },
        idempotency_key="expired-followup",
        status="pending",
        retry_count=0,
        response_json={},
        dispatch_history_json=[],
        created_at=NOW,
        updated_at=NOW,
    )
    session = _DispatchFollowupSession(event, binding, case)
    transport = _NoSendTransport()

    summary = await dispatch_notification_batch(
        session,  # type: ignore[arg-type]
        transport,
        worker_id="worker-1",
        now=NOW,
        allowed_tenant_ids=("tenant-test",),
        allowed_message_types=("case_progress_followup",),
        case_followup_tenant_ids=("tenant-test",),
        case_followup_user_ids=("user-1",),
    )

    assert summary.cancelled == 1
    assert event.status == "cancelled"
    assert "expired" in event.error_message
    assert transport.calls == []
