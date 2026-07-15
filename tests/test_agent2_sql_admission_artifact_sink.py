from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.sql.dml import Insert, Update

from app.agent2.admission_hashes import compute_admission_claim_hashes
from app.agent2.admission_artifact_sink_sql import (
    AdmissionArtifactPersistenceError,
    SqlAdmissionArtifactSink,
)
from app.agent2.admission_contracts import (
    ADMISSION_CONTRACT_VERSION,
    AdmissionDecision,
    AdmissionTicket,
    AdmissionTrace,
    DeferredSemanticEvent,
    InformationPending,
    SemanticReviewItem,
)
from app.agent2.admission_store_sql import (
    AdmissionReceiptReference,
    AdmissionTicketExecutionRequest,
    SqlAdmissionTicketStore,
)
from app.agent2.business.contracts import BusinessCommandError
from app.agent2.turn_runtime import AdmissionArtifactPersistenceRequest


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


def _uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"sql-admission-sink:{label}"))


def _with_fields(value, **changes):
    dataclass_fields = getattr(value, "__dataclass_fields__", {})
    if dataclass_fields and set(changes).issubset(dataclass_fields):
        return replace(value, **changes)
    return SimpleNamespace(**{**vars(value), **changes})


def _ticket_artifacts(*, mode: str = "enforced"):
    trace_id = _uuid("trace")
    decision_id = _uuid("decision")
    ticket_id = _uuid("ticket")
    object_ref = {
        "object_type": "periodic_report",
        "stable_id": _uuid("report"),
        "version": 0,
    }
    decision = AdmissionDecision(
        decision_id=decision_id,
        trace_id=trace_id,
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
        source_message_id="message-1",
        action_id="action-1",
        segment_id="segment-1",
        segment_text_sha256="a" * 64,
        segment_start_offset=0,
        segment_end_offset=8,
        domain="report",
        operation="capture_report_event",
        object_ref=object_ref,
        expected_conversation_state_version=4,
        status="admitted",
        reason_code="report_mutation_authorized",
        evidence_refs=("segment_sha256:" + "a" * 64,),
        ticket_id=ticket_id,
        idempotency_key="decision-key",
        created_at=NOW,
    )
    authority_scope = {
        "report_type": "weekly",
        "report_id": object_ref["stable_id"],
        "report_version": 0,
    }
    fact_hash, command_hash = compute_admission_claim_hashes(
        action_id="action-1",
        operation="capture_report_event",
        segment_text_sha256="a" * 64,
        domain="report",
        object_ref=object_ref,
        authority_scope=authority_scope,
        allowed_changed_fields=("section", "items"),
    )
    ticket = AdmissionTicket(
        ticket_id=ticket_id,
        trace_id=trace_id,
        decision_id=decision_id,
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
        source_message_id="message-1",
        action_id="action-1",
        segment_id="segment-1",
        segment_text_sha256="a" * 64,
        segment_start_offset=0,
        segment_end_offset=8,
        domain="report",
        operation="capture_report_event",
        object_ref=object_ref,
        expected_conversation_state_version=4,
        authority_scope=authority_scope,
        allowed_changed_fields=("section", "items"),
        fact_claims_sha256=fact_hash,
        authorized_command_sha256=command_hash,
        contract_version=ADMISSION_CONTRACT_VERSION,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key="ticket-key",
    )
    trace = AdmissionTrace(
        trace_id=trace_id,
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
        source_message_id="message-1",
        expected_conversation_state_version=4,
        proposal_sha256="d" * 64,
        contract_version=ADMISSION_CONTRACT_VERSION,
        decisions=(decision,),
        admission_summary="admitted",
        idempotency_key="trace-key",
        created_at=NOW,
    )
    cognitive_decision = SimpleNamespace(
        admission_mode=mode,
        admission_trace=trace,
        admission_tickets=(ticket,),
        admission_information_pendings=(),
    )
    return trace, ticket, cognitive_decision


def _pending_artifacts(*, mode: str = "enforced"):
    trace_id = _uuid("pending-trace")
    decision_id = _uuid("pending-decision")
    pending_id = _uuid("pending")
    decision = AdmissionDecision(
        decision_id=decision_id,
        trace_id=trace_id,
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-2",
        source_message_id="message-2",
        action_id="action-2",
        segment_id="segment-2",
        segment_text_sha256="e" * 64,
        segment_start_offset=0,
        segment_end_offset=4,
        domain="case",
        operation="record_case_progress",
        object_ref=None,
        expected_conversation_state_version=5,
        status="information_required",
        reason_code="case_reference_required",
        evidence_refs=("segment_sha256:" + "e" * 64,),
        pending_id=pending_id,
        idempotency_key="pending-decision-key",
        created_at=NOW,
    )
    pending = InformationPending(
        pending_id=pending_id,
        trace_id=trace_id,
        decision_id=decision_id,
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-2",
        source_message_id="message-2",
        segment_id="segment-2",
        segment_text_sha256="e" * 64,
        segment_start_offset=0,
        segment_end_offset=4,
        domain="case",
        operation="record_case_progress",
        object_ref=None,
        expected_conversation_state_version=6,
        missing_fields=("case_id",),
        question_snapshot={"prompt": "Which case?"},
        acceptable_answer_forms={"ordinal": True},
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        idempotency_key="pending-key",
    )
    trace = AdmissionTrace(
        trace_id=trace_id,
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-2",
        source_message_id="message-2",
        expected_conversation_state_version=5,
        proposal_sha256="f" * 64,
        contract_version=ADMISSION_CONTRACT_VERSION,
        decisions=(decision,),
        admission_summary="information_required",
        idempotency_key="pending-trace-key",
        created_at=NOW,
    )
    cognitive_decision = SimpleNamespace(
        admission_mode=mode,
        admission_trace=trace,
        admission_tickets=(),
        admission_information_pendings=(pending,),
    )
    return trace, pending, cognitive_decision


def _request(*, mode="enforced", pending=False, session=None):
    if pending:
        trace, artifact, cognitive_decision = _pending_artifacts(mode=mode)
        tickets = ()
        pendings = (artifact,)
        source_message_id = "message-2"
    else:
        trace, artifact, cognitive_decision = _ticket_artifacts(mode=mode)
        tickets = (artifact,)
        pendings = ()
        source_message_id = "message-1"
    return AdmissionArtifactPersistenceRequest(
        session=session or _ArtifactSession(),
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        source_message_id=source_message_id,
        admission_mode=mode,
        decision=cognitive_decision,
        trace=trace,
        tickets=tickets,
        information_pendings=pendings,
    )


def _review_deferred_request(*, session=None):
    base = _request(mode="shadow", session=session or _ArtifactSession())
    admitted_decision = base.trace.decisions[0]
    deferred_decision = replace(
        admitted_decision,
        decision_id=_uuid("deferred-decision"),
        action_id="action-deferred",
        segment_id="segment-deferred",
        segment_text_sha256="b" * 64,
        segment_start_offset=9,
        segment_end_offset=17,
        status="deferred_audit_only",
        reason_code="future_semantic_signal_audit_only",
        ticket_id="",
        object_ref=None,
        evidence_refs=("segment_sha256:" + "b" * 64,),
        idempotency_key="deferred-decision-key",
    )
    trace = replace(
        base.trace,
        decisions=(admitted_decision, deferred_decision),
        admission_summary="partially_admitted",
    )
    review = SemanticReviewItem(
        review_id=_uuid("review"),
        trace_id=trace.trace_id,
        decision_id=admitted_decision.decision_id,
        tenant_id=admitted_decision.tenant_id,
        user_id=admitted_decision.user_id,
        conversation_id=admitted_decision.conversation_id,
        source_turn_id=admitted_decision.source_turn_id,
        source_message_id=admitted_decision.source_message_id,
        segment_id=admitted_decision.segment_id,
        segment_text_sha256=admitted_decision.segment_text_sha256,
        segment_start_offset=admitted_decision.segment_start_offset,
        segment_end_offset=admitted_decision.segment_end_offset,
        domain=admitted_decision.domain,
        operation=admitted_decision.operation,
        object_ref=admitted_decision.object_ref,
        reason_code="shadow_decision_human_review",
        review_status="pending_human_review",
        candidate_snapshot={
            "action_id": admitted_decision.action_id,
            "decision_verdict": admitted_decision.status,
            "evidence_refs": admitted_decision.evidence_refs,
        },
        resolution={},
        audit_only=True,
        business_write_allowed=False,
        idempotency_key="review-key",
        created_at=NOW,
        resolved_at=None,
    )
    deferred = DeferredSemanticEvent(
        deferred_event_id=_uuid("deferred"),
        trace_id=trace.trace_id,
        decision_id=deferred_decision.decision_id,
        tenant_id=deferred_decision.tenant_id,
        user_id=deferred_decision.user_id,
        conversation_id=deferred_decision.conversation_id,
        source_turn_id=deferred_decision.source_turn_id,
        source_message_id=deferred_decision.source_message_id,
        segment_id=deferred_decision.segment_id,
        segment_text_sha256=deferred_decision.segment_text_sha256,
        segment_start_offset=deferred_decision.segment_start_offset,
        segment_end_offset=deferred_decision.segment_end_offset,
        domain=deferred_decision.domain,
        operation=deferred_decision.operation,
        object_ref=deferred_decision.object_ref,
        reason_code=deferred_decision.reason_code,
        event_status="recorded",
        payload={
            "action_id": deferred_decision.action_id,
            "decision_verdict": deferred_decision.status,
            "evidence_refs": deferred_decision.evidence_refs,
        },
        not_before=NOW + timedelta(minutes=15),
        expires_at=NOW + timedelta(days=1),
        audit_only=True,
        business_write_allowed=False,
        requires_fresh_admission=True,
        idempotency_key="deferred-key",
        created_at=NOW,
    )
    envelope = SimpleNamespace(
        admission_mode=base.admission_mode,
        admission_trace=trace,
        admission_tickets=base.tickets,
        admission_information_pendings=base.information_pendings,
        admission_review_items=(review,),
        admission_deferred_events=(deferred,),
    )
    return _with_fields(
        base,
        decision=envelope,
        trace=trace,
        review_items=(review,),
        deferred_events=(deferred,),
        review_capture_enabled=True,
        deferred_capture_enabled=True,
    )


class _MappingResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class _ArtifactSession:
    def __init__(self, *, allow_updates=False):
        self.rows: dict[str, dict[str, dict]] = {}
        self.statements = []
        self.flush_count = 0
        self.savepoint_count = 0
        self.allow_updates = allow_updates

    @asynccontextmanager
    async def begin_nested(self):
        self.savepoint_count += 1
        yield

    async def execute(self, statement):
        self.statements.append(statement)
        if isinstance(statement, Update):
            if not self.allow_updates:
                raise AssertionError(
                    "artifact issuance must never consume or update a ticket"
                )
            if statement.table.name != "agent2_semantic_admission_tickets":
                raise AssertionError(statement.table.name)
            values = dict(statement.compile().params)
            ticket_id = next(
                (
                    str(value)
                    for name, value in values.items()
                    if name.startswith("ticket_id_")
                ),
                "",
            )
            row = self.rows[statement.table.name].get(ticket_id)
            if row is None or row["ticket_status"] != "issued":
                return SimpleNamespace(rowcount=0)
            row.update(
                ticket_status=values["ticket_status"],
                consumed_at=values["consumed_at"],
                consumed_receipt_ref=values["consumed_receipt_ref"],
                updated_at=values["updated_at"],
            )
            return SimpleNamespace(rowcount=1)
        if isinstance(statement, Insert):
            table_name = statement.table.name
            values = dict(statement.compile().params)
            primary_key = next(iter(statement.table.primary_key.columns)).name
            key = str(values[primary_key])
            rows = self.rows.setdefault(table_name, {})
            if key not in rows and not any(
                row.get("tenant_id") == values.get("tenant_id")
                and row.get("idempotency_key") == values.get("idempotency_key")
                for row in rows.values()
            ):
                rows[key] = values
            return _MappingResult(None)
        table = statement.get_final_froms()[0]
        if table.name == "agent2_identity_bindings":
            return _MappingResult(
                {
                    "company_id": "",
                    "department_id": "",
                    "team_id": "",
                    "role_ids": [],
                    "permission_scope_json": {"allowed_case_ids": []},
                    "active": True,
                }
            )
        rows = self.rows.setdefault(table.name, {})
        params = statement.compile().params
        identifier = next(
            (
                str(value)
                for name, value in params.items()
                if name.endswith("_1") and str(value) in rows
            ),
            None,
        )
        row = rows.get(identifier) if identifier is not None else None
        return _MappingResult(row)

    async def flush(self):
        self.flush_count += 1


class _PostgresTimestamptzSession(_ArtifactSession):
    """Simulate PostgreSQL returning timestamptz values normalized to UTC."""

    async def execute(self, statement):
        result = await super().execute(statement)
        if isinstance(statement, Insert):
            rows = self.rows.get(statement.table.name, {})
            for row in rows.values():
                for key, value in tuple(row.items()):
                    if isinstance(value, datetime) and value.tzinfo is not None:
                        row[key] = value.astimezone(UTC)
        return result


@pytest.mark.asyncio
async def test_enforced_sink_atomically_issues_trace_decision_and_ticket_without_consuming():
    session = _ArtifactSession()
    request = _request(session=session)

    await SqlAdmissionArtifactSink().persist(request)

    assert session.savepoint_count == 1
    assert session.flush_count == 1
    assert set(session.rows) == {
        "agent2_semantic_admission_traces",
        "agent2_semantic_admission_decisions",
        "agent2_semantic_admission_tickets",
    }
    ticket = next(iter(session.rows["agent2_semantic_admission_tickets"].values()))
    assert ticket["ticket_status"] == "issued"
    assert ticket["consumed_at"] is None
    assert ticket["consumed_receipt_ref"] is None
    assert not any(isinstance(statement, Update) for statement in session.statements)


@pytest.mark.asyncio
async def test_sink_accepts_postgresql_utc_roundtrip_for_beijing_timestamps():
    session = _PostgresTimestamptzSession()
    request = _request(session=session)
    local_now = datetime(2026, 7, 14, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    decision = replace(request.trace.decisions[0], created_at=local_now)
    trace = replace(request.trace, decisions=(decision,), created_at=local_now)
    ticket = replace(
        request.tickets[0],
        issued_at=local_now,
        expires_at=local_now + timedelta(minutes=5),
    )
    request = replace(
        request,
        trace=trace,
        tickets=(ticket,),
        decision=SimpleNamespace(
            admission_mode="enforced",
            admission_trace=trace,
            admission_tickets=(ticket,),
            admission_information_pendings=(),
        ),
    )

    await SqlAdmissionArtifactSink().persist(request)

    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_enforced_periodic_ticket_round_trips_issue_acquire_receipt_and_consume():
    session = _ArtifactSession(allow_updates=True)
    request = _request(session=session)
    await SqlAdmissionArtifactSink().persist(request)
    raw_ticket = request.tickets[0]
    store = SqlAdmissionTicketStore(session)

    lease = await store.acquire(
        AdmissionTicketExecutionRequest(
            admission_ticket=raw_ticket.as_dict(),
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            source_message_id=request.source_message_id,
            action_id=raw_ticket.action_id,
            domain=raw_ticket.domain,
            operation=raw_ticket.operation,
            object_ref=raw_ticket.object_ref,
            conversation_state_version=(
                raw_ticket.expected_conversation_state_version
            ),
            executed_at=NOW + timedelta(seconds=1),
            command_type="append_item",
            receipt_kind="periodic_report",
        )
    )
    assert lease.authoritative_ticket == raw_ticket.as_dict()

    receipt_id = _uuid("periodic-receipt")
    session.rows["agent2_periodic_report_command_receipts"] = {
        receipt_id: {
            "receipt_id": receipt_id,
            "tenant_id": request.tenant_id,
            "actor_user_id": request.user_id,
            "source_message_id": request.source_message_id,
            "command_type": "append_item",
            "status": "authorized",
            "actual_write": True,
        }
    }
    await store.consume(
        lease,
        receipt=AdmissionReceiptReference(
            receipt_kind="periodic_report",
            receipt_id=receipt_id,
            status="authorized",
            actual_write=True,
        ),
        consumed_at=NOW + timedelta(seconds=2),
    )

    persisted_ticket = next(
        iter(session.rows["agent2_semantic_admission_tickets"].values())
    )
    assert persisted_ticket["ticket_status"] == "consumed"
    assert persisted_ticket["consumed_receipt_ref"] == receipt_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domain", "operation"),
    (
        ("report", "query_daily_report"),
        ("report", "query_periodic_report"),
        ("case", "answer_case_query"),
        ("case", "query_case_progress"),
        ("runtime", "query_operation_status"),
        ("knowledge", "search_enterprise_knowledge"),
    ),
)
async def test_read_only_admitted_decisions_persist_without_fabricated_ticket(
    domain,
    operation,
):
    trace, _, _ = _ticket_artifacts()
    decision = replace(
        trace.decisions[0],
        domain=domain,
        operation=operation,
        ticket_id="",
    )
    trace = replace(trace, decisions=(decision,))
    envelope = SimpleNamespace(
        admission_mode="enforced",
        admission_trace=trace,
        admission_tickets=(),
        admission_information_pendings=(),
    )
    session = _ArtifactSession()
    request = AdmissionArtifactPersistenceRequest(
        session=session,
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conversation-1",
        source_message_id="message-1",
        admission_mode="enforced",
        decision=envelope,
        trace=trace,
        tickets=(),
        information_pendings=(),
    )

    await SqlAdmissionArtifactSink().persist(request)

    assert "agent2_semantic_admission_tickets" not in session.rows
    persisted = next(
        iter(session.rows["agent2_semantic_admission_decisions"].values())
    )
    assert persisted["verdict"] == "admitted"
    assert persisted["ticket_id"] is None


@pytest.mark.asyncio
async def test_enforced_information_pending_is_persisted_awaiting_input_and_non_writable():
    session = _ArtifactSession()

    await SqlAdmissionArtifactSink().persist(
        _request(session=session, pending=True)
    )

    pending = next(iter(session.rows["agent2_information_pendings"].values()))
    assert pending["pending_status"] == "awaiting_input"
    assert pending["business_write_allowed"] is False
    assert pending["consumed_at"] is None
    assert pending["consumed_by_trace_id"] is None


@pytest.mark.asyncio
async def test_shadow_artifacts_are_persisted_cancelled_and_cannot_be_execution_authority():
    session = _ArtifactSession()
    request = _request(session=session, mode="shadow")

    await SqlAdmissionArtifactSink().persist(request)

    ticket = next(iter(session.rows["agent2_semantic_admission_tickets"].values()))
    assert ticket["ticket_status"] == "cancelled"
    assert ticket["invalidation_reason"] == "shadow_artifact_non_executable"
    assert ticket["consumed_at"] is None
    assert ticket["consumed_receipt_ref"] is None

    raw_ticket = request.tickets[0]
    with pytest.raises(BusinessCommandError) as exc_info:
        await SqlAdmissionTicketStore(session).acquire(
            AdmissionTicketExecutionRequest(
                admission_ticket=raw_ticket.as_dict(),
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                conversation_id=request.conversation_id,
                source_message_id=request.source_message_id,
                action_id=raw_ticket.action_id,
                domain=raw_ticket.domain,
                operation=raw_ticket.operation,
                object_ref=raw_ticket.object_ref,
                conversation_state_version=(
                    raw_ticket.expected_conversation_state_version
                ),
                executed_at=NOW + timedelta(seconds=1),
                command_type="append_item",
                receipt_kind="periodic_report",
            )
        )
    assert exc_info.value.code == "admission_ticket_inactive"


@pytest.mark.asyncio
async def test_shadow_information_pending_is_cancelled_and_non_writable():
    session = _ArtifactSession()

    await SqlAdmissionArtifactSink().persist(
        _request(session=session, mode="shadow", pending=True)
    )

    pending = next(iter(session.rows["agent2_information_pendings"].values()))
    assert pending["pending_status"] == "cancelled"
    assert pending["invalidation_reason"] == "shadow_artifact_non_executable"
    assert pending["business_write_allowed"] is False


@pytest.mark.asyncio
async def test_sink_is_idempotent_only_when_existing_artifacts_are_exactly_equal():
    session = _ArtifactSession()
    request = _request(session=session)
    sink = SqlAdmissionArtifactSink()

    await sink.persist(request)
    await sink.persist(request)

    assert all(len(rows) == 1 for rows in session.rows.values())
    ticket = next(iter(session.rows["agent2_semantic_admission_tickets"].values()))
    ticket["operation"] = "different_operation"

    with pytest.raises(
        AdmissionArtifactPersistenceError,
        match="artifact_idempotency_collision",
    ):
        await sink.persist(request)


@pytest.mark.asyncio
async def test_scope_mismatch_and_preconsumed_ticket_fail_before_any_sql():
    session = _ArtifactSession()
    mismatched = replace(_request(session=session), tenant_id="tenant-other")

    with pytest.raises(
        AdmissionArtifactPersistenceError,
        match="artifact_scope_mismatch",
    ):
        await SqlAdmissionArtifactSink().persist(mismatched)
    assert session.statements == []

    request = _request(session=session)
    consumed_ticket = replace(
        request.tickets[0],
        ticket_status="consumed",
        consumed_receipt_ref="receipt-1",
    )
    request = replace(
        request,
        tickets=(consumed_ticket,),
        decision=SimpleNamespace(
            admission_mode="enforced",
            admission_trace=request.trace,
            admission_tickets=(consumed_ticket,),
            admission_information_pendings=(),
        ),
    )
    with pytest.raises(
        AdmissionArtifactPersistenceError,
        match="artifact_ticket_not_issuable",
    ):
        await SqlAdmissionArtifactSink().persist(request)
    assert session.statements == []


@pytest.mark.asyncio
async def test_review_and_deferred_artifacts_persist_in_the_same_atomic_set():
    session = _ArtifactSession()
    request = _review_deferred_request(session=session)

    await SqlAdmissionArtifactSink().persist(request)

    assert session.savepoint_count == 1
    assert session.flush_count == 1
    assert set(session.rows) == {
        "agent2_semantic_admission_traces",
        "agent2_semantic_admission_decisions",
        "agent2_semantic_admission_tickets",
        "agent2_semantic_review_items",
        "agent2_deferred_semantic_events",
    }
    review = next(iter(session.rows["agent2_semantic_review_items"].values()))
    assert review["review_status"] == "pending_human_review"
    assert review["audit_only"] is True
    assert review["business_write_allowed"] is False
    assert review["reviewed_by"] == ""
    assert review["resolved_at"] is None
    deferred = next(
        iter(session.rows["agent2_deferred_semantic_events"].values())
    )
    assert deferred["event_status"] == "recorded"
    assert deferred["audit_only"] is True
    assert deferred["business_write_allowed"] is False
    assert deferred["requires_fresh_admission"] is True
    assert deferred["updated_at"] == NOW


@pytest.mark.asyncio
async def test_review_and_deferred_exact_retry_is_idempotent_but_payload_drift_is_rejected():
    session = _ArtifactSession()
    request = _review_deferred_request(session=session)
    sink = SqlAdmissionArtifactSink()

    await sink.persist(request)
    await sink.persist(request)

    assert {
        table: len(rows) for table, rows in session.rows.items()
    } == {
        "agent2_semantic_admission_traces": 1,
        "agent2_semantic_admission_decisions": 2,
        "agent2_semantic_admission_tickets": 1,
        "agent2_semantic_review_items": 1,
        "agent2_deferred_semantic_events": 1,
    }
    changed_review = replace(
        request.review_items[0],
        candidate_snapshot={
            "action_id": "action-different",
            "decision_verdict": request.review_items[0].candidate_snapshot[
                "decision_verdict"
            ],
            "evidence_refs": request.review_items[0].candidate_snapshot[
                "evidence_refs"
            ],
        },
    )
    changed_request = _with_fields(
        request,
        review_items=(changed_review,),
        decision=_with_fields(
            request.decision,
            admission_review_items=(changed_review,),
        ),
    )

    statement_count = len(session.statements)
    savepoint_count = session.savepoint_count
    with pytest.raises(
        AdmissionArtifactPersistenceError,
        match="artifact_review_link_invalid",
    ):
        await sink.persist(changed_request)
    assert len(session.statements) == statement_count
    assert session.savepoint_count == savepoint_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_kind", "change", "expected_code"),
    (
        ("review", {"tenant_id": "other-tenant"}, "artifact_review_scope_mismatch"),
        ("review", {"operation": "different"}, "artifact_review_link_invalid"),
        (
            "deferred",
            {"segment_end_offset": 18},
            "artifact_deferred_link_invalid",
        ),
    ),
)
async def test_review_and_deferred_scope_or_decision_drift_fails_before_sql(
    artifact_kind,
    change,
    expected_code,
):
    session = _ArtifactSession()
    request = _review_deferred_request(session=session)
    reviews = request.review_items
    deferred_events = request.deferred_events
    if artifact_kind == "review":
        reviews = (replace(reviews[0], **change),)
    else:
        deferred_events = (replace(deferred_events[0], **change),)
    envelope = SimpleNamespace(
        **{
            **vars(request.decision),
            "admission_review_items": reviews,
            "admission_deferred_events": deferred_events,
        }
    )
    request = _with_fields(
        request,
        decision=envelope,
        review_items=reviews,
        deferred_events=deferred_events,
    )

    with pytest.raises(AdmissionArtifactPersistenceError, match=expected_code):
        await SqlAdmissionArtifactSink().persist(request)

    assert session.statements == []
    assert session.savepoint_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_kind", ("review", "deferred"))
@pytest.mark.parametrize(
    ("candidate_field", "drifted_value"),
    (
        ("action_id", "action-different"),
        ("decision_verdict", "blocked"),
        ("evidence_refs", ("segment_sha256:" + "c" * 64,)),
    ),
)
async def test_review_and_deferred_candidate_must_exactly_bind_its_decision(
    artifact_kind,
    candidate_field,
    drifted_value,
):
    session = _ArtifactSession()
    request = _review_deferred_request(session=session)
    reviews = request.review_items
    deferred_events = request.deferred_events
    if artifact_kind == "review":
        original = reviews[0]
        reviews = (
            replace(
                original,
                candidate_snapshot={
                    **dict(original.candidate_snapshot),
                    candidate_field: drifted_value,
                },
            ),
        )
        expected_code = "artifact_review_link_invalid"
    else:
        original = deferred_events[0]
        deferred_events = (
            replace(
                original,
                payload={
                    **dict(original.payload),
                    candidate_field: drifted_value,
                },
            ),
        )
        expected_code = "artifact_deferred_link_invalid"
    envelope = SimpleNamespace(
        **{
            **vars(request.decision),
            "admission_review_items": reviews,
            "admission_deferred_events": deferred_events,
        }
    )
    request = _with_fields(
        request,
        decision=envelope,
        review_items=reviews,
        deferred_events=deferred_events,
    )

    with pytest.raises(AdmissionArtifactPersistenceError, match=expected_code):
        await SqlAdmissionArtifactSink().persist(request)

    assert session.statements == []
    assert session.savepoint_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("review_enabled", "deferred_enabled", "expected_code"),
    (
        (False, True, "artifact_review_capture_disabled"),
        (True, False, "artifact_deferred_capture_disabled"),
    ),
)
async def test_capture_flags_reject_injected_artifacts_before_sql(
    review_enabled,
    deferred_enabled,
    expected_code,
):
    session = _ArtifactSession()
    request = _with_fields(
        _review_deferred_request(session=session),
        review_capture_enabled=review_enabled,
        deferred_capture_enabled=deferred_enabled,
    )

    with pytest.raises(AdmissionArtifactPersistenceError, match=expected_code):
        await SqlAdmissionArtifactSink().persist(request)

    assert session.statements == []
    assert session.savepoint_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_kind", "field", "unsafe_value", "expected_code"),
    (
        ("review", "audit_only", False, "artifact_review_not_audit_only"),
        (
            "review",
            "business_write_allowed",
            True,
            "artifact_review_can_authorize_write",
        ),
        (
            "deferred",
            "requires_fresh_admission",
            False,
            "artifact_deferred_requires_fresh_admission",
        ),
        (
            "deferred",
            "business_write_allowed",
            True,
            "artifact_deferred_can_authorize_write",
        ),
        (
            "review",
            "review_status",
            "unknown",
            "artifact_review_status_invalid",
        ),
        (
            "deferred",
            "event_status",
            "unknown",
            "artifact_deferred_status_invalid",
        ),
    ),
)
async def test_sink_revalidates_audit_only_safety_constants(
    artifact_kind,
    field,
    unsafe_value,
    expected_code,
):
    session = _ArtifactSession()
    request = _review_deferred_request(session=session)
    reviews = request.review_items
    deferred_events = request.deferred_events
    target = reviews[0] if artifact_kind == "review" else deferred_events[0]
    object.__setattr__(target, field, unsafe_value)

    with pytest.raises(AdmissionArtifactPersistenceError, match=expected_code):
        await SqlAdmissionArtifactSink().persist(request)

    assert session.statements == []
    assert session.savepoint_count == 0
