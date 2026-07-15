from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.admission_hashes import compute_admission_claim_hashes
from app.agent2.business.contracts import BusinessCommandContext, BusinessCommandError
from app.agent2.business.models import PeriodicReport, PeriodicReportCommandReceipt
from app.agent2.report_domain import TypedPeriodicReportCommand
from app.agent2.report_sql_executor import (
    execute_periodic_report_commands as _execute_periodic_report_commands,
    periodic_report_datetimes,
    periodic_report_id,
)


async def execute_periodic_report_commands(*args, **kwargs):
    kwargs.setdefault("execution_authority", "authenticated_admin_command")
    return await _execute_periodic_report_commands(*args, **kwargs)


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


def _periodic_command_and_context():
    owner = uuid5(NAMESPACE_URL, "periodic-admission-owner")
    period_key, _, _ = periodic_report_datetimes("weekly", NOW, "Asia/Shanghai")
    report_id = periodic_report_id(
        "tenant-test",
        str(owner),
        "weekly",
        period_key,
    )
    object_ref = {
        "object_type": "periodic_report",
        "stable_id": str(report_id),
        "version": 0,
    }
    authority_scope = {
        "report_type": "weekly",
        "period_key": period_key,
        "report_id": str(report_id),
        "report_version": 0,
        "command_type": "append_item",
        "target_item_ids": [],
        "patch": {"field": "accomplishments", "value": "完成案件清单核验"},
    }
    segment_hash = "a" * 64
    fact_hash, command_hash = compute_admission_claim_hashes(
        action_id="periodic-action-1",
        operation="capture_report_event",
        segment_text_sha256=segment_hash,
        domain="report",
        object_ref=object_ref,
        authority_scope=authority_scope,
        allowed_changed_fields=("section", "items"),
    )
    ticket = {
        "ticket_id": str(uuid5(NAMESPACE_URL, "periodic-ticket-1")),
        "trace_id": str(uuid5(NAMESPACE_URL, "periodic-trace-1")),
        "decision_id": str(uuid5(NAMESPACE_URL, "periodic-decision-1")),
        "tenant_id": "tenant-test",
        "user_id": str(owner),
        "conversation_id": "conversation-1",
        "source_turn_id": "turn-1",
        "source_message_id": "message-1",
        "action_id": "periodic-action-1",
        "segment_id": "segment-1",
        "segment_text_sha256": segment_hash,
        "segment_start_offset": 0,
        "segment_end_offset": 8,
        "domain": "report",
        "operation": "capture_report_event",
        "object_ref": object_ref,
        "expected_conversation_state_version": 4,
        "authority_scope": authority_scope,
        "allowed_changed_fields": ["section", "items"],
        "fact_claims_sha256": fact_hash,
        "authorized_command_sha256": command_hash,
        "policy_version": "agent2.domain_admission.policy.v1",
        "ticket_status": "issued",
        "contract_version": "agent2.domain_admission.v1",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "ttl_seconds": 300,
        "executor_revalidation_required": True,
        "proves_business_write": False,
        "consumed_receipt_ref": None,
        "idempotency_key": "periodic-ticket-key",
    }
    command = TypedPeriodicReportCommand(
        command_id=uuid5(NAMESPACE_URL, "periodic-command-1"),
        decision_id=UUID(ticket["decision_id"]),
        sub_decision_id=uuid5(NAMESPACE_URL, "periodic-subdecision-1"),
        command_type="append_item",
        report_type="weekly",
        period_key=period_key,
        report_id=report_id,
        report_version=0,
        target_item_ids=(),
        patch={"field": "accomplishments", "value": "完成案件清单核验"},
        idempotency_key="periodic-command-key",
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id="periodic-action-1",
        admission_operation="capture_report_event",
    )
    context = BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id=str(owner),
        actor_role_ids=(),
        allowed_case_ids=(),
        source_message_id="message-1",
        source_channel="test",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        conversation_state_version=4,
    )
    return command, context


class _Session:
    def __init__(self, *, existing_receipt=None):
        self.added = []
        self.statements = []
        self.existing_receipt = existing_receipt

    async def execute(self, statement):
        self.statements.append(statement)
        return None

    async def scalar(self, statement):
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "agent2_periodic_report_command_receipts" in sql:
            return self.existing_receipt
        if "agent2_periodic_reports" in sql:
            return None
        raise AssertionError(sql)

    async def scalars(self, statement):
        self.statements.append(statement)

        class _Rows:
            @staticmethod
            def all():
                return []

        return _Rows()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for item in self.added:
            if isinstance(item, PeriodicReportCommandReceipt) and item.receipt_id is None:
                item.receipt_id = uuid4()


class _Store:
    def __init__(self):
        self.requests = []
        self.consumptions = []
        self.replays = []

    async def acquire(self, request):
        self.requests.append(request)
        return object()

    async def consume(self, lease, *, receipt, consumed_at):
        self.consumptions.append((lease, receipt, consumed_at))

    async def validate_consumed_execution_replay(self, request, *, receipt_id):
        self.replays.append((request, receipt_id))


@pytest.mark.asyncio
async def test_semantic_periodic_authority_allows_read_only_without_ticket():
    command, context = _periodic_command_and_context()
    query = replace(
        command,
        command_type="query_report",
        patch={},
        admission_ticket={},
        admission_required=False,
        admission_action_id="",
        admission_operation="",
        idempotency_key="periodic-query-key",
    )
    session = _Session()

    result = await execute_periodic_report_commands(
        session,
        commands=(query,),
        context=context,
        execution_authority="semantic_ticket",
    )

    assert result[0].actual_write is False


@pytest.mark.asyncio
async def test_semantic_periodic_authority_blocks_mixed_unadmitted_mutation_before_db():
    command, context = _periodic_command_and_context()
    unadmitted = replace(
        command,
        admission_ticket={},
        admission_required=False,
        admission_action_id="",
        admission_operation="",
        idempotency_key="periodic-unadmitted-key",
    )
    query = replace(
        command,
        command_type="query_report",
        patch={},
        admission_ticket={},
        admission_required=False,
        admission_action_id="",
        admission_operation="",
        idempotency_key="periodic-query-mixed-key",
    )
    session = _Session()

    with pytest.raises(BusinessCommandError) as exc_info:
        await execute_periodic_report_commands(
            session,
            commands=(query, unadmitted),
            context=context,
            execution_authority="semantic_ticket",
        )

    assert exc_info.value.code == "admission_ticket_required"
    assert session.statements == []


@pytest.mark.asyncio
async def test_periodic_report_sql_executor_requires_and_consumes_authoritative_ticket():
    command, context = _periodic_command_and_context()
    session = _Session()
    store = _Store()

    result = (
        await execute_periodic_report_commands(
            session,
            commands=(command,),
            context=context,
            admission_ticket_store=store,
            execution_authority="semantic_ticket",
        )
    )[0]

    assert result.status == "authorized"
    assert result.actual_write is True
    assert len(store.requests) == 1
    assert store.requests[0].receipt_kind == "periodic_report"
    assert store.requests[0].object_ref == command.admission_ticket["object_ref"]
    assert len(store.consumptions) == 1
    assert store.consumptions[0][1].receipt_id == result.receipt_id
    assert store.consumptions[0][1].status == "authorized"
    assert any(isinstance(item, PeriodicReport) for item in session.added)


@pytest.mark.asyncio
async def test_duplicate_periodic_report_ingress_does_not_reacquire_or_consume_ticket():
    command, context = _periodic_command_and_context()

    existing_receipt = SimpleNamespace(
        receipt_id=uuid5(NAMESPACE_URL, "periodic-receipt-1"),
        tenant_id=context.tenant_id,
        actor_user_id=context.actor_user_id,
        source_message_id=context.source_message_id,
        command_type=command.command_type,
        report_id=command.report_id,
        status="authorized",
        error_code="",
    )
    session = _Session(existing_receipt=existing_receipt)
    store = _Store()

    result = (
        await execute_periodic_report_commands(
            session,
            commands=(command,),
            context=context,
            admission_ticket_store=store,
            execution_authority="semantic_ticket",
        )
    )[0]

    assert result.status == "duplicate"
    assert result.actual_write is False
    assert store.requests == []
    assert store.consumptions == []
    assert len(store.replays) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    (
        ({"actor_user_id": "other-user"}, "idempotency_scope_conflict"),
        ({"status": "processing"}, "idempotency_in_progress"),
        ({"status": "blocked", "error_code": "prior_block"}, "prior_block"),
    ),
)
async def test_periodic_replay_scope_and_status_drift_fail_closed(
    overrides,
    reason_code,
):
    command, context = _periodic_command_and_context()
    payload = {
        "receipt_id": uuid5(NAMESPACE_URL, "periodic-drift-receipt"),
        "tenant_id": context.tenant_id,
        "actor_user_id": context.actor_user_id,
        "source_message_id": context.source_message_id,
        "command_type": command.command_type,
        "report_id": command.report_id,
        "status": "authorized",
        "error_code": "",
    }
    payload.update(overrides)
    session = _Session(existing_receipt=SimpleNamespace(**payload))
    store = _Store()

    result = (
        await execute_periodic_report_commands(
            session,
            commands=(command,),
            context=context,
            admission_ticket_store=store,
            execution_authority="semantic_ticket",
        )
    )[0]

    assert result.status == "blocked"
    assert result.actual_write is False
    assert result.execution.reason_code == reason_code
    assert store.requests == []
    assert store.consumptions == []
    assert not any(isinstance(item, PeriodicReport) for item in session.added)
