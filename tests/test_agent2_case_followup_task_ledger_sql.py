from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.agent2.business.models import Agent2TaskLedgerEntry
from app.agent2.case_followup_task_ledger_sql import (
    complete_followup_and_restore_report,
    focus_followup_and_suspend_current_task,
    report_task_ledger_id,
    sync_focused_report_task,
)


NOW = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


def _entry(*, domain, status, focus_state, resume=None, expires_at=None):
    return Agent2TaskLedgerEntry(
        task_id=uuid4(), tenant_id="tenant-a", user_id="user-a",
        conversation_id="conversation-a", domain=domain, operation="collect",
        object_ref_json={"period_key": "2026-W29"} if domain == "report" else {},
        status=status, focus_state=focus_state, version=1,
        source_turn_id="turn-1", pending_requirements_json={},
        resume_policy_json=resume or {}, expires_at=expires_at,
        created_at=NOW, updated_at=NOW,
    )


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, followup, rows):
        self.followup = followup
        self.rows = rows

    async def get(self, model, key):
        assert model is Agent2TaskLedgerEntry
        return self.followup if key == self.followup.task_id else None

    async def scalars(self, _statement):
        return _Rows(self.rows)

    async def flush(self):
        return None

    def add(self, value):
        self.rows.append(value)


class _UniqueTaskStore:
    def __init__(self):
        self.rows = []


class _ScopedUniqueSession:
    """Mimic PostgreSQL PK enforcement while returning one conversation scope."""

    def __init__(self, store, conversation_id):
        self.store = store
        self.conversation_id = conversation_id
        self.pending = []

    async def scalars(self, _statement):
        return _Rows(
            row for row in self.store.rows
            if row.conversation_id == self.conversation_id
        )

    def add(self, value):
        self.pending.append(value)

    async def flush(self):
        existing_ids = {row.task_id for row in self.store.rows}
        for row in self.pending:
            if row.task_id in existing_ids:
                raise RuntimeError("duplicate_task_id")
            existing_ids.add(row.task_id)
            self.store.rows.append(row)
        self.pending.clear()


@pytest.mark.asyncio
async def test_provider_acceptance_suspends_focused_report_before_focusing_followup():
    report = _entry(
        domain="report", status="active", focus_state="focused",
        resume={"mode": "restore_previous", "report_status": "collecting"},
    )
    followup = _entry(domain="case_followup", status="active", focus_state="active")
    session = _Session(followup, (report, followup))

    result = await focus_followup_and_suspend_current_task(
        session, followup_task_id=followup.task_id, now=NOW,
        provider_receipt_succeeded=True,
    )

    assert result.status == "transitioned"
    assert report.status == "suspended"
    assert report.focus_state == "suspended"
    assert followup.status == "awaiting_input"
    assert followup.focus_state == "focused"
    assert followup.resume_policy_json["previous_task_id"] == str(report.task_id)


@pytest.mark.asyncio
async def test_committed_answer_restores_only_one_valid_collecting_report():
    report = _entry(
        domain="report", status="suspended", focus_state="suspended",
        resume={"mode": "restore_previous", "report_status": "collecting"},
        expires_at=NOW + timedelta(days=2),
    )
    followup = _entry(domain="case_followup", status="awaiting_input", focus_state="focused")
    session = _Session(followup, (report, followup))

    result = await complete_followup_and_restore_report(
        session, followup_task_id=followup.task_id, now=NOW,
        case_receipt_succeeded=True,
    )

    assert result.status == "transitioned"
    assert result.restored_task_id == str(report.task_id)
    assert followup.status == "completed"
    assert report.status == "active"
    assert report.focus_state == "focused"


@pytest.mark.asyncio
async def test_multiple_resumable_reports_require_clarification_and_restore_none():
    reports = tuple(
        _entry(
            domain="report", status="suspended", focus_state="suspended",
            resume={"mode": "restore_previous", "report_status": "collecting"},
        )
        for _ in range(2)
    )
    followup = _entry(domain="case_followup", status="awaiting_input", focus_state="focused")
    session = _Session(followup, (*reports, followup))

    result = await complete_followup_and_restore_report(
        session, followup_task_id=followup.task_id, now=NOW,
        case_receipt_succeeded=True,
    )

    assert result.status == "clarification_required"
    assert result.restored_task_id == ""
    assert all(item.status == "suspended" for item in reports)


@pytest.mark.asyncio
async def test_exact_previous_task_link_restores_only_the_report_interrupted_by_followup():
    older, previous = tuple(
        _entry(
            domain="report", status="suspended", focus_state="suspended",
            resume={"mode": "restore_previous", "report_status": "collecting"},
        ) for _ in range(2)
    )
    followup = _entry(
        domain="case_followup", status="awaiting_input", focus_state="focused",
        resume={
            "previous_task_id": str(previous.task_id),
            "previous_task_version": previous.version,
        },
    )
    session = _Session(followup, (older, previous, followup))

    result = await complete_followup_and_restore_report(
        session, followup_task_id=followup.task_id, now=NOW,
        case_receipt_succeeded=True,
    )

    assert result.restored_task_id == str(previous.task_id)
    assert previous.status == "active" and previous.focus_state == "focused"
    assert older.status == "suspended"


@pytest.mark.asyncio
async def test_committed_report_task_is_persisted_and_focused_for_exact_conversation():
    session = _Session(None, [])
    report_id = uuid4()

    result = await sync_focused_report_task(
        session, task_id=report_id, tenant_id="tenant-a", user_id="user-a",
        conversation_id="conversation-a", source_turn_id="turn-report",
        report_type="weekly", period_key="2026-W29", report_status="collecting",
        now=NOW, expires_at=NOW + timedelta(days=7),
    )

    assert result.status == "transitioned"
    report = session.rows[0]
    assert report.task_id == report_task_ledger_id(
        report_id=report_id,
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-a",
        report_type="weekly",
        period_key="2026-W29",
    )
    assert report.object_ref_json["source_report_id"] == str(report_id)
    assert report.domain == "report"
    assert report.focus_state == "focused"
    assert report.resume_policy_json["report_status"] == "collecting"


@pytest.mark.asyncio
async def test_existing_legacy_report_task_id_is_reused_in_its_original_conversation():
    report_id = uuid4()
    legacy = Agent2TaskLedgerEntry(
        task_id=report_id,
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-a",
        domain="report",
        operation="collect",
        object_ref_json={"report_type": "daily", "period_key": "2026-07-22"},
        status="active",
        focus_state="focused",
        version=1,
        source_turn_id="legacy-turn",
        pending_requirements_json={},
        resume_policy_json={"mode": "restore_previous", "report_status": "collecting"},
        expires_at=NOW + timedelta(days=1),
        created_at=NOW,
        updated_at=NOW,
    )
    session = _Session(None, [legacy])

    result = await sync_focused_report_task(
        session,
        task_id=report_id,
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-a",
        source_turn_id="new-turn",
        report_type="daily",
        period_key="2026-07-22",
        report_status="collecting",
        now=NOW,
        expires_at=NOW + timedelta(days=1),
    )

    assert result.status == "transitioned"
    assert len(session.rows) == 1
    assert legacy.task_id == report_id
    assert legacy.object_ref_json["source_report_id"] == str(report_id)


def test_report_task_ledger_id_is_stable_and_conversation_scoped():
    report_id = uuid4()
    common = dict(
        report_id=report_id,
        tenant_id="tenant-a",
        user_id="user-a",
        report_type="daily",
        period_key="2026-07-22",
    )

    first = report_task_ledger_id(conversation_id="conversation-a", **common)
    repeated = report_task_ledger_id(conversation_id="conversation-a", **common)
    another_conversation = report_task_ledger_id(
        conversation_id="conversation-b", **common
    )

    assert first == repeated
    assert first != another_conversation


@pytest.mark.asyncio
async def test_report_task_sync_requires_conversation_and_closed_status_values():
    session = _Session(None, [])
    common = dict(
        session=session, task_id=uuid4(), tenant_id="tenant-a", user_id="user-a",
        source_turn_id="turn-report", report_type="weekly", period_key="2026-W29",
        now=NOW,
    )
    missing_scope = await sync_focused_report_task(
        **common, conversation_id="", report_status="collecting"
    )
    unknown_status = await sync_focused_report_task(
        **common, conversation_id="conversation-a", report_status="draft"
    )

    assert missing_scope.reason_code == "conversation_scope_required"
    assert unknown_status.reason_code == "unknown_report_status"
    assert session.rows == []


@pytest.mark.asyncio
async def test_same_report_can_be_focused_in_two_conversations_without_primary_key_collision():
    store = _UniqueTaskStore()
    report_id = uuid4()
    common = dict(
        task_id=report_id,
        tenant_id="tenant-a",
        user_id="user-a",
        source_turn_id="turn-report",
        report_type="daily",
        period_key="2026-07-22",
        report_status="collecting",
        now=NOW,
        expires_at=NOW + timedelta(days=1),
    )

    first = await sync_focused_report_task(
        _ScopedUniqueSession(store, "conversation-smoke"),
        conversation_id="conversation-smoke",
        **common,
    )
    second = await sync_focused_report_task(
        _ScopedUniqueSession(store, "conversation-real"),
        conversation_id="conversation-real",
        **common,
    )

    assert first.status == second.status == "transitioned"
    assert len(store.rows) == 2
    assert store.rows[0].task_id != store.rows[1].task_id


@pytest.mark.asyncio
async def test_scoped_report_task_survives_followup_suspend_and_exact_restore():
    rows = []
    session = _Session(None, rows)
    report_id = uuid4()
    focused = await sync_focused_report_task(
        session,
        task_id=report_id,
        tenant_id="tenant-a",
        user_id="user-a",
        conversation_id="conversation-a",
        source_turn_id="turn-report",
        report_type="daily",
        period_key="2026-07-22",
        report_status="collecting",
        now=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    report = rows[0]
    followup = _entry(
        domain="case_followup", status="active", focus_state="active"
    )
    rows.append(followup)
    session.followup = followup

    suspended = await focus_followup_and_suspend_current_task(
        session,
        followup_task_id=followup.task_id,
        now=NOW,
        provider_receipt_succeeded=True,
    )
    restored = await complete_followup_and_restore_report(
        session,
        followup_task_id=followup.task_id,
        now=NOW,
        case_receipt_succeeded=True,
    )

    assert focused.restored_task_id == str(report.task_id)
    assert report.task_id != report_id
    assert suspended.status == "transitioned"
    assert restored.restored_task_id == str(report.task_id)
    assert report.status == "active"
    assert report.focus_state == "focused"
