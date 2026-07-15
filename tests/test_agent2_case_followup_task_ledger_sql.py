from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.agent2.business.models import Agent2TaskLedgerEntry
from app.agent2.case_followup_task_ledger_sql import (
    complete_followup_and_restore_report,
    focus_followup_and_suspend_current_task,
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
    assert report.task_id == report_id
    assert report.domain == "report"
    assert report.focus_state == "focused"
    assert report.resume_policy_json["report_status"] == "collecting"


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
