from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.sql.dml import Update

from app.agent2.information_pending import (
    InformationPendingContext,
    InformationPendingResolution,
    InformationPendingSettlement,
)
from app.agent2.information_pending_sql import (
    InformationPendingPersistenceConflict,
    SqlInformationPendingRepository,
    SqlInformationPendingValidator,
)


NOW = datetime(2026, 7, 14, 1, 0, tzinfo=UTC)
PENDING_ID = UUID("21b742ce-fe40-5bf5-a838-c80f73d569a1")
TRACE_ID = UUID("c796b14d-1d76-58e1-a46f-f62fa9ee8fea")
DECISION_ID = UUID("c33f9bc1-bc2e-5733-b014-190f597a0a7f")
TRAVEL_ID = UUID("2bbefcc9-d7cf-5f2e-a310-e800a434ef29")


def _row(**overrides):
    payload = {
        "pending_id": PENDING_ID,
        "pending_type": "information",
        "trace_id": TRACE_ID,
        "decision_id": DECISION_ID,
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_turn_id": "message-original",
        "source_message_id": "message-original",
        "segment_id": "segment-1",
        "segment_text_sha256": "a" * 64,
        "segment_start_offset": 0,
        "segment_end_offset": 6,
        "domain": "travel",
        "operation": "record_travel_event",
        "object_type": "travel_intent",
        "object_stable_id": str(TRAVEL_ID),
        "object_version": None,
        "object_label": None,
        "expected_conversation_state_version": 4,
        "missing_fields_json": ["travel_date"],
        "question_snapshot_json": {"destination": "南京"},
        "acceptable_answer_forms_json": {"field": "travel_date"},
        "pending_status": "awaiting_input",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "ttl_seconds": 600,
        "consumed_at": None,
        "consumed_by_trace_id": None,
        "invalidation_reason": "",
        "business_write_allowed": False,
        "idempotency_key": "pending-key",
        "updated_at": NOW,
    }
    payload.update(overrides)
    return payload


def _context(**overrides):
    payload = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_message_id": "message-answer",
        "occurred_at": NOW + timedelta(minutes=1),
        "conversation_state_version": 4,
    }
    payload.update(overrides)
    return InformationPendingContext(**payload)


class _MappingsResult:
    def __init__(self, rows=(), *, scalar=None, rowcount=1):
        self._rows = tuple(rows)
        self._scalar = scalar
        self.rowcount = rowcount

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


class _Session:
    def __init__(self, *, rows=(), duplicate_trace=None, rowcount=1, scalar=None):
        self.rows = tuple(rows)
        self.duplicate_trace = duplicate_trace
        self.rowcount = rowcount
        self.scalar_value = scalar
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if isinstance(statement, Update):
            return _MappingsResult(rowcount=self.rowcount)
        table_names = {table.name for table in statement.get_final_froms()}
        if "agent2_information_pendings" in table_names:
            return _MappingsResult(self.rows)
        if "agent2_semantic_admission_traces" in table_names:
            return _MappingsResult(scalar=self.duplicate_trace)
        raise AssertionError(f"unexpected statement tables: {table_names}")

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.scalar_value


@pytest.mark.asyncio
async def test_sql_repository_loads_only_full_scoped_contract_and_round_trips_row():
    session = _Session(rows=(_row(),))
    repository = SqlInformationPendingRepository(session)

    pending, = await repository.load_scoped(_context())

    assert pending.pending_id == str(PENDING_ID)
    assert pending.tenant_id == "tenant-1"
    assert pending.user_id == "user-1"
    assert pending.conversation_id == "conversation-1"
    assert pending.object_ref == {
        "object_type": "travel_intent",
        "stable_id": str(TRAVEL_ID),
        "version": None,
    }
    sql = str(session.statements[0])
    assert "tenant_id" in sql
    assert "user_id" in sql
    assert "conversation_id" in sql
    assert "pending_status IN" in sql


@pytest.mark.asyncio
async def test_sql_repository_detects_duplicate_source_message_in_same_scope():
    session = _Session(duplicate_trace=TRACE_ID)
    duplicate = await SqlInformationPendingRepository(
        session
    ).source_message_processed(_context())

    assert duplicate is True
    sql = str(session.statements[0])
    assert "source_message_id" in sql
    assert "tenant_id" in sql and "user_id" in sql and "conversation_id" in sql


@pytest.mark.asyncio
async def test_terminal_resolution_uses_compare_and_set_and_detects_race():
    load_session = _Session(rows=(_row(),))
    pending, = await SqlInformationPendingRepository(load_session).load_scoped(
        _context()
    )
    expired = InformationPendingResolution(
        status="expired",
        pending_id=pending.pending_id,
        reason="pending_expired",
        pending_after=replace(pending, pending_status="expired"),
        continuation=None,
    )
    session = _Session(rowcount=1)
    await SqlInformationPendingRepository(session).persist_resolution(
        resolution=expired,
        context=_context(),
    )
    assert isinstance(session.statements[0], Update)
    sql = str(session.statements[0])
    assert "expected_conversation_state_version" in sql
    assert "pending_status IN" in sql

    with pytest.raises(InformationPendingPersistenceConflict):
        await SqlInformationPendingRepository(_Session(rowcount=0)).persist_resolution(
            resolution=expired,
            context=_context(),
        )


@pytest.mark.asyncio
async def test_committed_settlement_consumes_with_fresh_trace_atomically():
    load_session = _Session(rows=(_row(),))
    pending, = await SqlInformationPendingRepository(load_session).load_scoped(
        _context()
    )
    consumed = replace(
        pending,
        pending_status="consumed",
        consumed_by_trace_id="7ba94ed4-3bac-54b2-8c1b-a93b62e5859a",
    )
    settlement = InformationPendingSettlement(
        status="consumed",
        pending_after=consumed,
        reason="committed_receipt_succeeded",
        receipt_refs=("receipt-1",),
    )
    session = _Session(rowcount=1)
    await SqlInformationPendingRepository(session).persist_settlement(
        pending=pending,
        settlement=settlement,
        settled_at=NOW + timedelta(minutes=2),
    )

    statement = session.statements[0]
    assert isinstance(statement, Update)
    params = statement.compile().params
    assert "consumed" in params.values()
    assert UUID(consumed.consumed_by_trace_id) in params.values()


@pytest.mark.asyncio
async def test_sql_validator_blocks_preallocated_object_reuse_and_permission_revoke():
    load_session = _Session(rows=(_row(),))
    pending, = await SqlInformationPendingRepository(load_session).load_scoped(
        _context()
    )
    reused = await SqlInformationPendingValidator(
        _Session(scalar=TRAVEL_ID),
        tenant_id="tenant-1",
        user_id="user-1",
    ).validate(pending, _context())
    assert reused.status == "version_conflict"

    async def revoked(_pending):
        return False

    permission = await SqlInformationPendingValidator(
        _Session(),
        tenant_id="tenant-1",
        user_id="user-1",
        permission_checker=revoked,
    ).validate(pending, _context())
    assert permission.status == "permission_revoked"
