from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    Agent2TaskLedgerEntry,
    CaseFollowupTask,
    NotificationOutbox,
)
from app.agent2.case_followup_admin_sql import cancel_unsent_case_followup_task
from app.agent2.case_followup_commands import CancelCaseFollowupTask


NOW = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


class _Rows:
    def __init__(self, rows): self.rows = rows
    def all(self): return list(self.rows)


class _Session:
    def __init__(self, case, binding, task, events, ledger):
        self.case, self.binding, self.task = case, binding, task
        self.events, self.ledger, self.added = events, ledger, []

    async def scalar(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "agent2_business_command_receipts" in sql: return None
        if "agent2_identity_bindings" in sql: return self.binding
        if "agent2_case_followup_tasks" in sql: return self.task
        if "agent2_cases" in sql: return self.case
        raise AssertionError(sql)

    async def scalars(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "agent2_notification_outbox" in sql
        return _Rows(self.events)

    async def get(self, model, key):
        assert model is Agent2TaskLedgerEntry
        return self.ledger if key == self.task.followup_id else None

    def add_all(self, values): self.added.extend(values)
    def add(self, value): self.added.append(value)
    async def flush(self): return None


def _fixture(*, event_status="pending"):
    case_id, followup_id = uuid4(), uuid4()
    case = Agent2Case(
        case_id=case_id, tenant_id="tenant-a", company_id="company",
        department_id="legal", team_id="team", external_case_id="P-1",
        case_number="(2026)苏01民初1号", case_name="南京工程款案",
        case_type="plaintiff", status="open", owner_user_id="user-1",
        source_type="real_source_sandbox", source_id="source-1",
        source_json={}, version=1, created_at=NOW, updated_at=NOW,
    )
    binding = Agent2IdentityBinding(
        binding_id=uuid4(), tenant_id="tenant-a", company_id="company",
        department_id="legal", team_id="team", user_id="user-1",
        dingtalk_user_id="ding-1", display_name="庞浩", role_ids=["lawyer"],
        permission_scope_json={"allowed_case_ids": [str(case_id)]}, active=True,
        created_at=NOW, updated_at=NOW,
    )
    task = CaseFollowupTask(
        followup_id=followup_id, tenant_id="tenant-a", case_id=case_id,
        assigned_user_id="user-1", trigger_type="manual",
        trigger_event_id="manual-1", trigger_sources_json=[], case_type="plaintiff",
        stage="拟诉", node="", case_version=1, question_type="meaningful_progress",
        question_text="目前有什么新进展？", priority=300, task_status="queued",
        message_status="queued", response_status="not_requested", due_at=NOW,
        expires_at=NOW + timedelta(days=7), reminder_count=0, max_reminders=1,
        conversation_id="conversation-1", provider_message_id="",
        idempotency_key="task-key", version=2, created_at=NOW, updated_at=NOW,
    )
    event = NotificationOutbox(
        notification_id=uuid4(), tenant_id="tenant-a", candidate_id=followup_id,
        recipient_user_id="user-1", channel="dingtalk",
        message_type="case_lifecycle_followup", message_json={"case_id": str(case_id)},
        idempotency_key="event-key", status=event_status, retry_count=0,
        external_message_id=("provider-1" if event_status == "sent" else ""),
        response_json={}, dispatch_history_json=[], created_at=NOW, updated_at=NOW,
    )
    ledger = Agent2TaskLedgerEntry(
        task_id=followup_id, tenant_id="tenant-a", user_id="user-1",
        conversation_id="conversation-1", domain="case_followup", operation="answer",
        object_ref_json={}, status="active", focus_state="active", version=1,
        source_turn_id="turn-1", pending_requirements_json={}, resume_policy_json={},
        created_at=NOW, updated_at=NOW,
    )
    return case, binding, task, event, ledger


def _command(case, task):
    return CancelCaseFollowupTask(
        command_id="cancel-1", tenant_id="tenant-a", case_id=str(case.case_id),
        followup_id=str(task.followup_id), assigned_user_id="user-1",
        expected_version=task.version, source_turn_id="turn-cancel",
        idempotency_key="cancel-key-123",
    )


@pytest.mark.asyncio
async def test_admin_cancel_only_changes_an_unsent_task_and_persists_receipt_audit():
    case, binding, task, event, ledger = _fixture()
    session = _Session(case, binding, task, (event,), ledger)

    receipt = await cancel_unsent_case_followup_task(
        session, _command(case, task), actor_user_id="admin",
        actor_is_tenant_admin=True, now=NOW,
    )

    assert receipt.status == "executed" and receipt.actual_write is True
    assert task.task_status == "cancelled"
    assert event.status == "cancelled"
    assert ledger.status == "cancelled"
    assert len(session.added) == 2


@pytest.mark.asyncio
async def test_admin_cancel_fails_closed_once_provider_evidence_exists():
    case, binding, task, event, ledger = _fixture(event_status="sent")
    session = _Session(case, binding, task, (event,), ledger)

    receipt = await cancel_unsent_case_followup_task(
        session, _command(case, task), actor_user_id="admin",
        actor_is_tenant_admin=True, now=NOW,
    )

    assert receipt.actual_write is False
    assert receipt.error_code == "followup_already_sending_or_sent"
    assert task.task_status == "queued"
