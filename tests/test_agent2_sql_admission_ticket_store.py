from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace
import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from app.agent2.admission_store_sql import (
    AdmissionReceiptReference,
    SqlAdmissionTicketStore,
)
from app.agent2.business.contracts import (
    BusinessCommandContext,
    BusinessCommandError,
    CreateCaseProgress,
    CreateTravelIntent,
)
from app.agent2.business.models import (
    BusinessAuditEvent,
    BusinessCommandReceipt,
    TravelIntent,
)
from app.agent2.business.sql_executor import SqlBusinessExecutor


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


def _sha256_json(value: object) -> str:
    material = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _issued_case_ticket(*, case_id: str) -> dict[str, object]:
    action_id = "case-action-1"
    operation = "record_case_progress"
    segment_hash = hashlib.sha256("今天联系了法院".encode("utf-8")).hexdigest()
    authority_scope = {
        "case_id": case_id,
        "version": 3,
        "raw_fact": "今天联系了法院",
        "case_reference": "测试案",
        "attributes": {"stage": "general_update"},
    }
    allowed_changed_fields = [
        "summary",
        "details",
        "progress_type",
        "current_status",
        "next_actions",
        "hearing_readiness",
        "blocking_issues",
    ]
    fact_claims_sha256 = _sha256_json(
        {
            "action_id": action_id,
            "operation": operation,
            "segment_text_sha256": segment_hash,
            "authority_scope": authority_scope,
            "allowed_changed_fields": allowed_changed_fields,
        }
    )
    object_ref = {"object_type": "case", "stable_id": case_id, "version": 3}
    authorized_command_sha256 = _sha256_json(
        {
            "domain": "case",
            "operation": operation,
            "object_ref": object_ref,
            "authority_scope": authority_scope,
            "allowed_changed_fields": allowed_changed_fields,
            "fact_claims_sha256": fact_claims_sha256,
        }
    )
    return {
        "ticket_id": str(uuid5(NAMESPACE_URL, "sql-ticket-store-test")),
        "trace_id": str(uuid5(NAMESPACE_URL, "sql-ticket-store-trace")),
        "decision_id": str(uuid5(NAMESPACE_URL, "sql-ticket-store-decision")),
        "tenant_id": "tenant-test",
        "user_id": "u1",
        "conversation_id": "conversation-1",
        "source_turn_id": "turn-1",
        "source_message_id": "message-1",
        "action_id": action_id,
        "segment_id": "segment-1",
        "segment_text_sha256": segment_hash,
        "segment_start_offset": 0,
        "segment_end_offset": 7,
        "domain": "case",
        "operation": operation,
        "object_ref": object_ref,
        "expected_conversation_state_version": 4,
        "authority_scope": authority_scope,
        "allowed_changed_fields": allowed_changed_fields,
        "fact_claims_sha256": fact_claims_sha256,
        "authorized_command_sha256": authorized_command_sha256,
        "policy_version": "agent2.domain_admission.policy.v1",
        "ticket_status": "issued",
        "contract_version": "agent2.domain_admission.v1",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "ttl_seconds": 300,
        "executor_revalidation_required": True,
        "proves_business_write": False,
        "consumed_receipt_ref": None,
        "idempotency_key": "admission-test-ticket",
    }


def _ticket_row(ticket: dict[str, object]) -> dict[str, object]:
    object_ref = dict(ticket["object_ref"])
    return {
        **{key: value for key, value in ticket.items() if key != "object_ref"},
        "ticket_id": UUID(str(ticket["ticket_id"])),
        "trace_id": UUID(str(ticket["trace_id"])),
        "decision_id": UUID(str(ticket["decision_id"])),
        "object_type": object_ref["object_type"],
        "object_stable_id": object_ref["stable_id"],
        "object_version": object_ref["version"],
        "object_label": object_ref.get("label"),
        "authority_scope_json": ticket["authority_scope"],
        "allowed_changed_fields_json": ticket["allowed_changed_fields"],
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
        "consumed_at": None,
        "invalidation_reason": "",
        "created_at": NOW,
        "updated_at": NOW,
    }


class _MappingResult:
    def __init__(self, row: dict[str, object] | None = None, *, rowcount: int = 0):
        self._row = row
        self.rowcount = rowcount

    def mappings(self):
        return self

    def one_or_none(self):
        return self._row


class _TicketSession:
    def __init__(
        self,
        row: dict[str, object] | None,
        *,
        case_version: int | None = None,
        receipt_id: str | None = None,
        receipt_status: str = "executed",
        receipt_actual_write: bool = True,
        identity_active: bool = True,
        identity_company_id: str = "company-1",
        identity_department_id: str = "department-1",
        identity_team_id: str = "team-1",
        identity_role_ids: tuple[str, ...] = (),
        identity_allowed_case_ids: tuple[str, ...] | None = None,
        conversation_state_payload: dict[str, object] | None = None,
    ):
        self.row = row
        self.case_version = case_version
        self.receipt_id = receipt_id
        self.receipt_status = receipt_status
        self.receipt_actual_write = receipt_actual_write
        self.identity_active = identity_active
        self.identity_company_id = identity_company_id
        self.identity_department_id = identity_department_id
        self.identity_team_id = identity_team_id
        self.identity_role_ids = identity_role_ids
        self.identity_allowed_case_ids = identity_allowed_case_ids
        self.conversation_state_payload = conversation_state_payload
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if isinstance(statement, Select):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            if "agent2_identity_bindings" in sql:
                if self.row is None:
                    return _MappingResult(None)
                allowed_case_ids = self.identity_allowed_case_ids
                if allowed_case_ids is None:
                    allowed_case_ids = (str(self.row["object_stable_id"]),)
                return _MappingResult(
                    {
                        "company_id": self.identity_company_id,
                        "department_id": self.identity_department_id,
                        "team_id": self.identity_team_id,
                        "role_ids": list(self.identity_role_ids),
                        "permission_scope_json": {
                            "allowed_case_ids": list(allowed_case_ids)
                        },
                        "active": self.identity_active,
                    }
                )
            if "agent2_conversation_states" in sql:
                if self.conversation_state_payload is None:
                    return _MappingResult(None)
                return _MappingResult(
                    {
                        "user_key": "tenant-test:u1",
                        "conversation_id": "conversation-1",
                        "version": self.conversation_state_payload["version"],
                        "state_json": self.conversation_state_payload,
                    }
                )
            if "agent2_business_command_receipts" in sql:
                if self.receipt_id is None:
                    return _MappingResult(None)
                return _MappingResult(
                    {
                        "receipt_id": self.receipt_id,
                        "tenant_id": self.row["tenant_id"],
                        "actor_user_id": self.row["user_id"],
                        "source_message_id": self.row["source_message_id"],
                        "status": self.receipt_status,
                        "actual_write": self.receipt_actual_write,
                    }
                )
            if "agent2_cases" in sql:
                if self.row is None:
                    return _MappingResult(None)
                return _MappingResult(
                    {
                        "case_id": self.row["object_stable_id"],
                        "version": (
                            self.row["object_version"]
                            if self.case_version is None
                            else self.case_version
                        ),
                        "owner_user_id": self.row["user_id"],
                    }
                )
            return _MappingResult(self.row)
        if isinstance(statement, Update):
            if self.row is None or self.row["ticket_status"] != "issued":
                return _MappingResult(rowcount=0)
            params = statement.compile(dialect=postgresql.dialect()).params
            self.row.update(
                ticket_status=params["ticket_status"],
                consumed_at=params["consumed_at"],
                consumed_receipt_ref=params["consumed_receipt_ref"],
                updated_at=params["updated_at"],
            )
            return _MappingResult(rowcount=1)
        raise AssertionError(type(statement))


def _command_and_context(ticket: dict[str, object]):
    case_id = str(dict(ticket["object_ref"])["stable_id"])
    command = CreateCaseProgress(
        command_id="command-1",
        case_id=case_id,
        occurred_at=NOW,
        progress_type="general_update",
        summary="今天联系了法院",
        details="",
        related_party_ids=(),
        related_document_ids=(),
        related_travel_intent_ids=(),
        confidence=1.0,
    )
    context = BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="u1",
        actor_role_ids=(),
        allowed_case_ids=(case_id,),
        source_message_id="message-1",
        source_channel="test",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id="case-action-1",
        admission_operation="record_case_progress",
        conversation_state_version=4,
    )
    return command, context


@pytest.mark.asyncio
async def test_sql_ticket_store_locks_validates_and_consumes_one_authoritative_ticket():
    case_id = str(uuid4())
    ticket = _issued_case_ticket(case_id=case_id)
    receipt_id = str(uuid4())
    session = _TicketSession(_ticket_row(ticket), receipt_id=receipt_id)
    command, context = _command_and_context(ticket)
    store = SqlAdmissionTicketStore(session)

    lease = await store.lock_and_validate(command, context)
    assert lease.authoritative_ticket == ticket
    await store.consume(
        lease,
        receipt=AdmissionReceiptReference(
            receipt_kind="business",
            receipt_id=receipt_id,
            status="executed",
            actual_write=True,
        ),
        consumed_at=NOW + timedelta(seconds=2),
    )

    select_sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    consume_statement = next(
        item for item in session.statements if isinstance(item, Update)
    )
    consume_sql = str(consume_statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in select_sql
    assert "tenant_id" in consume_sql
    assert "ticket_id" in consume_sql
    assert "ticket_status" in consume_sql
    assert session.row["ticket_status"] == "consumed"
    assert session.row["consumed_receipt_ref"] == receipt_id


@pytest.mark.asyncio
async def test_sql_ticket_store_does_not_consume_before_receipt_is_successful():
    ticket = _issued_case_ticket(case_id=str(uuid4()))
    receipt_id = str(uuid4())
    session = _TicketSession(
        _ticket_row(ticket),
        receipt_id=receipt_id,
        receipt_status="processing",
        receipt_actual_write=False,
    )
    command, context = _command_and_context(ticket)
    store = SqlAdmissionTicketStore(session)
    lease = await store.lock_and_validate(command, context)

    with pytest.raises(BusinessCommandError) as error:
        await store.consume(
            lease,
            receipt=AdmissionReceiptReference(
                receipt_kind="business",
                receipt_id=receipt_id,
                status="executed",
                actual_write=True,
            ),
            consumed_at=NOW + timedelta(seconds=2),
        )

    assert error.value.code == "admission_ticket_receipt_not_successful"
    assert session.row["ticket_status"] == "issued"


@pytest.mark.asyncio
async def test_sql_ticket_store_rejects_matching_but_non_success_receipt_status():
    ticket = _issued_case_ticket(case_id=str(uuid4()))
    receipt_id = str(uuid4())
    session = _TicketSession(
        _ticket_row(ticket),
        receipt_id=receipt_id,
        receipt_status="blocked",
        receipt_actual_write=False,
    )
    command, context = _command_and_context(ticket)
    store = SqlAdmissionTicketStore(session)
    lease = await store.lock_and_validate(command, context)

    with pytest.raises(BusinessCommandError) as error:
        await store.consume(
            lease,
            receipt=AdmissionReceiptReference(
                receipt_kind="business",
                receipt_id=receipt_id,
                status="blocked",
                actual_write=False,
            ),
            consumed_at=NOW + timedelta(seconds=2),
        )

    assert error.value.code == "admission_ticket_receipt_not_successful"
    assert session.row["ticket_status"] == "issued"


@pytest.mark.asyncio
async def test_sql_ticket_store_rejects_unpersisted_self_attested_ticket_without_write():
    ticket = _issued_case_ticket(case_id=str(uuid4()))
    session = _TicketSession(None)
    command, context = _command_and_context(ticket)

    with pytest.raises(BusinessCommandError) as error:
        await SqlAdmissionTicketStore(session).lock_and_validate(command, context)

    assert error.value.code == "admission_ticket_not_found"
    assert len(session.statements) == 1
    assert isinstance(session.statements[0], Select)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row_patch", "expected_code"),
    [
        ({"user_id": "u2"}, "admission_ticket_scope_mismatch"),
        (
            {"expected_conversation_state_version": 5},
            "admission_ticket_state_version_conflict",
        ),
        (
            {
                "issued_at": NOW - timedelta(minutes=10),
                "expires_at": NOW - timedelta(minutes=5),
            },
            "admission_ticket_expired",
        ),
    ],
)
async def test_sql_ticket_store_revalidates_authoritative_scope_state_and_ttl(
    row_patch,
    expected_code,
):
    ticket = _issued_case_ticket(case_id=str(uuid4()))
    row = _ticket_row(ticket)
    row.update(row_patch)
    command, context = _command_and_context(ticket)

    with pytest.raises(BusinessCommandError) as error:
        await SqlAdmissionTicketStore(_TicketSession(row)).lock_and_validate(
            command,
            context,
        )

    assert error.value.code == expected_code


@pytest.mark.asyncio
async def test_sql_ticket_store_rejects_corrupt_authoritative_claim_digest():
    ticket = _issued_case_ticket(case_id=str(uuid4()))
    row = _ticket_row(ticket)
    row["fact_claims_sha256"] = "0" * 64
    command, context = _command_and_context(ticket)

    with pytest.raises(BusinessCommandError) as error:
        await SqlAdmissionTicketStore(_TicketSession(row)).lock_and_validate(
            command,
            context,
        )

    assert error.value.code == "admission_ticket_claims_mismatch"


@pytest.mark.asyncio
async def test_sql_ticket_store_rejects_authoritative_object_different_from_command():
    ticket = _issued_case_ticket(case_id=str(uuid4()))
    row = _ticket_row(ticket)
    row["object_stable_id"] = str(uuid4())
    object_ref = {
        "object_type": row["object_type"],
        "stable_id": row["object_stable_id"],
        "version": row["object_version"],
    }
    row["authorized_command_sha256"] = _sha256_json(
        {
            "domain": row["domain"],
            "operation": row["operation"],
            "object_ref": object_ref,
            "authority_scope": row["authority_scope_json"],
            "allowed_changed_fields": row["allowed_changed_fields_json"],
            "fact_claims_sha256": row["fact_claims_sha256"],
        }
    )
    command, context = _command_and_context(ticket)

    with pytest.raises(BusinessCommandError) as error:
        await SqlAdmissionTicketStore(_TicketSession(row)).lock_and_validate(
            command,
            context,
        )

    assert error.value.code == "admission_ticket_object_mismatch"


@pytest.mark.asyncio
async def test_sql_ticket_store_rejects_live_case_version_changed_after_ticket_issue():
    ticket = _issued_case_ticket(case_id=str(uuid4()))
    command, context = _command_and_context(ticket)

    with pytest.raises(BusinessCommandError) as error:
        await SqlAdmissionTicketStore(
            _TicketSession(_ticket_row(ticket), case_version=4)
        ).lock_and_validate(command, context)

    assert error.value.code == "admission_ticket_object_version_conflict"


@pytest.mark.asyncio
async def test_sql_ticket_store_rejects_compiled_business_fact_changed_after_admission():
    ticket = _issued_case_ticket(case_id=str(uuid4()))
    command, context = _command_and_context(ticket)
    changed_command = replace(command, summary="法院已经同意下周付款")

    with pytest.raises(BusinessCommandError) as error:
        await SqlAdmissionTicketStore(
            _TicketSession(_ticket_row(ticket))
        ).lock_and_validate(changed_command, context)

    assert error.value.code == "admission_ticket_claims_mismatch"


@pytest.mark.asyncio
async def test_sql_ticket_store_rejects_unknown_authoritative_policy_version():
    ticket = _issued_case_ticket(case_id=str(uuid4()))
    ticket["policy_version"] = "future-unreviewed-policy"
    row = _ticket_row(ticket)
    command, context = _command_and_context(ticket)

    with pytest.raises(BusinessCommandError) as error:
        await SqlAdmissionTicketStore(_TicketSession(row)).lock_and_validate(
            command,
            context,
        )

    assert error.value.code == "unknown_admission_policy"


@pytest.mark.asyncio
async def test_sql_ticket_store_rejects_broadened_changed_field_authority():
    ticket = _issued_case_ticket(case_id=str(uuid4()))
    ticket["allowed_changed_fields"] = [
        *ticket["allowed_changed_fields"],
        "case_owner_id",
    ]
    ticket["fact_claims_sha256"] = _sha256_json(
        {
            "action_id": ticket["action_id"],
            "operation": ticket["operation"],
            "segment_text_sha256": ticket["segment_text_sha256"],
            "authority_scope": ticket["authority_scope"],
            "allowed_changed_fields": ticket["allowed_changed_fields"],
        }
    )
    ticket["authorized_command_sha256"] = _sha256_json(
        {
            "domain": ticket["domain"],
            "operation": ticket["operation"],
            "object_ref": ticket["object_ref"],
            "authority_scope": ticket["authority_scope"],
            "allowed_changed_fields": ticket["allowed_changed_fields"],
            "fact_claims_sha256": ticket["fact_claims_sha256"],
        }
    )
    command, context = _command_and_context(ticket)

    with pytest.raises(BusinessCommandError) as error:
        await SqlAdmissionTicketStore(
            _TicketSession(_ticket_row(ticket))
        ).lock_and_validate(command, context)

    assert error.value.code == "admission_ticket_claims_mismatch"


@pytest.mark.asyncio
async def test_sql_ticket_store_blocks_consumed_selection_pending_before_effect():
    case_id = str(uuid4())
    ticket = _issued_case_ticket(case_id=case_id)
    authority = dict(ticket["authority_scope"])
    authority.update(
        {
            "selection_pending_id": "selection-1",
            "selection_pending_source_turn_id": "message-original",
            "original_source_digest": "source-digest",
            "original_continuation_sha256": "continuation-digest",
            "candidate_stable_id": case_id,
            "candidate_version": 3,
            "final_command_claims": {
                "bound_payload_sha256": "bound-digest",
            },
        }
    )
    ticket["authority_scope"] = authority
    ticket["fact_claims_sha256"] = _sha256_json(
        {
            "action_id": ticket["action_id"],
            "operation": ticket["operation"],
            "segment_text_sha256": ticket["segment_text_sha256"],
            "authority_scope": authority,
            "allowed_changed_fields": ticket["allowed_changed_fields"],
        }
    )
    ticket["authorized_command_sha256"] = _sha256_json(
        {
            "domain": ticket["domain"],
            "operation": ticket["operation"],
            "object_ref": ticket["object_ref"],
            "authority_scope": authority,
            "allowed_changed_fields": ticket["allowed_changed_fields"],
            "fact_claims_sha256": ticket["fact_claims_sha256"],
        }
    )
    state_payload = {
        "user_id": "tenant-test:u1",
        "conversation_id": "conversation-1",
        "version": 4,
        "selection_pending": [
            {
                "pending_id": "selection-1",
                "tenant_id": "tenant-test",
                "user_id": "u1",
                "conversation_id": "conversation-1",
                "domain": "case_progress",
                "operation": "create",
                "source_turn_id": "message-original",
                "candidates": [
                    {
                        "stable_id": case_id,
                        "version": 3,
                        "label": "case one",
                    },
                ],
                "acceptable_answer_forms": {"first": case_id},
                "expected_conversation_state_version": 4,
                "created_at": (NOW - timedelta(minutes=1)).isoformat(),
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                "status": "consumed",
                "continuation_payload": {},
                "consumed_receipt_id": "receipt-1",
                "invalidation_reason": "",
            }
        ],
    }
    command, context = _command_and_context(ticket)
    store = SqlAdmissionTicketStore(
        _TicketSession(
            _ticket_row(ticket),
            conversation_state_payload=state_payload,
        )
    )

    with pytest.raises(BusinessCommandError) as error:
        await store.lock_and_validate(command, context)

    assert error.value.code == "selection_pending_conflict"


def _issued_travel_ticket() -> dict[str, object]:
    action_id = "travel-action-1"
    operation = "record_travel_event"
    segment_hash = hashlib.sha256("明天去南京出差".encode("utf-8")).hexdigest()
    authority_scope = {
        "destination": "南京",
        "travel_date": "2026-07-15",
        "raw_fact": "明天去南京出差",
        "purpose": "",
    }
    allowed_changed_fields = [
        "destination",
        "start_at",
        "end_at",
        "purpose_summary",
    ]
    fact_claims_sha256 = _sha256_json(
        {
            "action_id": action_id,
            "operation": operation,
            "segment_text_sha256": segment_hash,
            "authority_scope": authority_scope,
            "allowed_changed_fields": allowed_changed_fields,
        }
    )
    object_ref = {
        "object_type": "travel_intent",
        "stable_id": str(uuid5(NAMESPACE_URL, "travel-object-1")),
        "version": None,
    }
    authorized_command_sha256 = _sha256_json(
        {
            "domain": "travel",
            "operation": operation,
            "object_ref": object_ref,
            "authority_scope": authority_scope,
            "allowed_changed_fields": allowed_changed_fields,
            "fact_claims_sha256": fact_claims_sha256,
        }
    )
    return {
        "ticket_id": str(uuid5(NAMESPACE_URL, "travel-ticket-1")),
        "trace_id": str(uuid5(NAMESPACE_URL, "travel-trace-1")),
        "decision_id": str(uuid5(NAMESPACE_URL, "travel-decision-1")),
        "tenant_id": "tenant-test",
        "user_id": "u1",
        "conversation_id": "conversation-1",
        "source_turn_id": "turn-1",
        "source_message_id": "message-1",
        "action_id": action_id,
        "segment_id": "segment-1",
        "segment_text_sha256": segment_hash,
        "segment_start_offset": 0,
        "segment_end_offset": 7,
        "domain": "travel",
        "operation": operation,
        "object_ref": object_ref,
        "expected_conversation_state_version": 4,
        "authority_scope": authority_scope,
        "allowed_changed_fields": allowed_changed_fields,
        "fact_claims_sha256": fact_claims_sha256,
        "authorized_command_sha256": authorized_command_sha256,
        "policy_version": "agent2.domain_admission.policy.v1",
        "ticket_status": "issued",
        "contract_version": "agent2.domain_admission.v1",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "ttl_seconds": 300,
        "executor_revalidation_required": True,
        "proves_business_write": False,
        "consumed_receipt_ref": None,
        "idempotency_key": "admission-travel-ticket",
    }


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _BusinessWriteSession:
    def __init__(self):
        self.receipt = None
        self.added = []

    async def scalar(self, statement):
        from sqlalchemy.sql.dml import Insert

        if isinstance(statement, Insert):
            params = statement.compile(dialect=postgresql.dialect()).params
            self.receipt = BusinessCommandReceipt(
                receipt_id=params["receipt_id"],
                tenant_id=params["tenant_id"],
                command_id=params["command_id"],
                command_type=params["command_type"],
                actor_user_id=params["actor_user_id"],
                source_message_id=params["source_message_id"],
                idempotency_key=params["idempotency_key"],
                status=params["status"],
                resource_type="",
                resource_id="",
                before_json={},
                after_json={},
                error_code="",
                failed_stage="",
                actual_write=False,
                created_at=params["created_at"],
                updated_at=params["updated_at"],
            )
            return self.receipt.receipt_id
        raise AssertionError(type(statement))

    async def execute(self, statement):
        assert isinstance(statement, Update)
        params = statement.compile(dialect=postgresql.dialect()).params
        for name in (
            "status",
            "resource_type",
            "resource_id",
            "before_json",
            "after_json",
            "error_code",
            "failed_stage",
            "actual_write",
            "updated_at",
        ):
            setattr(self.receipt, name, params[name])

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None

    async def get(self, model, primary_key):
        assert model is BusinessCommandReceipt
        assert primary_key == self.receipt.receipt_id
        return self.receipt

    def begin_nested(self):
        return _Nested()


class _RecordingTicketStore:
    def __init__(self, session=None):
        self.session = session
        self.locked = 0
        self.consumed = []
        self.replays = []

    async def lock_and_validate(self, command, context):
        self.locked += 1
        return object()

    async def consume(self, lease, *, receipt, consumed_at):
        if self.session is not None:
            assert self.session.receipt.status == "executed"
            assert self.session.receipt.actual_write is True
        self.consumed.append((lease, receipt, consumed_at))

    async def validate_consumed_business_replay(
        self,
        command,
        context,
        *,
        receipt_id,
    ):
        self.replays.append((command, context, receipt_id))


class _AuthoritativeBusinessSession(_BusinessWriteSession):
    def __init__(self, ticket):
        super().__init__()
        self.ticket_row = _ticket_row(ticket)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if isinstance(statement, Select):
            if "agent2_semantic_admission_tickets" in sql:
                return _MappingResult(self.ticket_row)
            if "agent2_identity_bindings" in sql:
                return _MappingResult(
                    {
                        "company_id": "company-1",
                        "department_id": "department-1",
                        "team_id": "team-1",
                        "role_ids": [],
                        "permission_scope_json": {"allowed_case_ids": []},
                        "active": True,
                    }
                )
            if "agent2_business_command_receipts" in sql:
                return _MappingResult(
                    {
                        "receipt_id": self.receipt.receipt_id,
                        "command_type": self.receipt.command_type,
                        "status": self.receipt.status,
                        "actual_write": self.receipt.actual_write,
                    }
                )
            raise AssertionError(sql)
        if isinstance(statement, Update) and "agent2_semantic_admission_tickets" in sql:
            params = statement.compile(dialect=postgresql.dialect()).params
            if self.ticket_row["ticket_status"] != "issued":
                return _MappingResult(rowcount=0)
            self.ticket_row.update(
                ticket_status=params["ticket_status"],
                consumed_at=params["consumed_at"],
                consumed_receipt_ref=params["consumed_receipt_ref"],
                updated_at=params["updated_at"],
            )
            return _MappingResult(rowcount=1)
        return await super().execute(statement)


@pytest.mark.asyncio
async def test_sql_executor_consumes_ticket_with_successful_business_receipt():
    ticket = _issued_travel_ticket()
    session = _BusinessWriteSession()
    store = _RecordingTicketStore(session)
    command = CreateTravelIntent(
        command_id="travel-command-1",
        destination_raw="南京",
        destination_normalized="南京市",
        city_code="320100",
        province_code="320000",
        start_at=datetime(2026, 7, 15, tzinfo=UTC),
        end_at=datetime(2026, 7, 15, 23, 59, tzinfo=UTC),
        time_precision="day",
        purpose_summary="",
        related_case_ids=(),
        confidence=1.0,
    )
    context = BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="u1",
        actor_role_ids=(),
        allowed_case_ids=(),
        source_message_id="message-1",
        source_channel="test",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id="travel-action-1",
        admission_operation="record_travel_event",
        conversation_state_version=4,
    )

    receipt = await SqlBusinessExecutor(
        session,
        admission_ticket_store=store,
        execution_authority="semantic_ticket",
    ).execute(command, context)

    assert (receipt.status, receipt.error_code) == ("executed", None)
    assert store.locked == 1
    assert len(store.consumed) == 1
    assert store.consumed[0][1].receipt_id == str(receipt.receipt_id)
    intent = next(item for item in session.added if isinstance(item, TravelIntent))
    assert str(intent.travel_intent_id) == dict(ticket["object_ref"])["stable_id"]
    assert any(isinstance(item, BusinessAuditEvent) for item in session.added)


@pytest.mark.asyncio
async def test_sql_executor_default_repository_atomically_consumes_authoritative_ticket():
    ticket = _issued_travel_ticket()
    session = _AuthoritativeBusinessSession(ticket)
    command = CreateTravelIntent(
        command_id="travel-command-1",
        destination_raw="南京",
        destination_normalized="南京市",
        city_code="320100",
        province_code="320000",
        start_at=datetime(2026, 7, 15, tzinfo=UTC),
        end_at=datetime(2026, 7, 15, 23, 59, tzinfo=UTC),
        time_precision="day",
        purpose_summary="",
        related_case_ids=(),
        confidence=1.0,
    )
    context = BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="u1",
        actor_role_ids=(),
        allowed_case_ids=(),
        source_message_id="message-1",
        source_channel="test",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id="travel-action-1",
        admission_operation="record_travel_event",
        conversation_state_version=4,
    )

    receipt = await SqlBusinessExecutor(
        session,
        execution_authority="semantic_ticket",
    ).execute(command, context)

    assert (receipt.status, receipt.error_code) == ("executed", None)
    assert session.ticket_row["ticket_status"] == "consumed"
    assert session.ticket_row["consumed_receipt_ref"] == receipt.receipt_id
    assert any(
        "FOR UPDATE" in str(item.compile(dialect=postgresql.dialect()))
        for item in session.statements
        if isinstance(item, Select)
    )


class _DuplicateBusinessSession:
    def __init__(self):
        self.receipt = BusinessCommandReceipt(
            receipt_id=uuid4(),
            tenant_id="tenant-test",
            command_id="travel-command-1",
            command_type="create_travel_intent",
            actor_user_id="u1",
            source_message_id="message-1",
            idempotency_key="existing-key",
            status="executed",
            resource_type="travel_intent",
            resource_id=str(uuid4()),
            before_json={},
            after_json={"destination": "南京市"},
            error_code="",
            failed_stage="",
            actual_write=True,
            created_at=NOW,
            updated_at=NOW,
        )

    async def scalar(self, statement):
        from sqlalchemy.sql.dml import Insert

        if isinstance(statement, Insert):
            return None
        if isinstance(statement, Select):
            return self.receipt
        raise AssertionError(type(statement))

    def begin_nested(self):
        raise AssertionError("duplicate ingress must return before execution")


@pytest.mark.asyncio
async def test_sql_executor_duplicate_ingress_returns_receipt_without_second_ticket_consume():
    ticket = _issued_travel_ticket()
    store = _RecordingTicketStore()
    command = CreateTravelIntent(
        command_id="travel-command-1",
        destination_raw="南京",
        destination_normalized="南京市",
        city_code="320100",
        province_code="320000",
        start_at=datetime(2026, 7, 15, tzinfo=UTC),
        end_at=datetime(2026, 7, 15, 23, 59, tzinfo=UTC),
        time_precision="day",
        purpose_summary="",
        related_case_ids=(),
        confidence=1.0,
    )
    context = BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="u1",
        actor_role_ids=(),
        allowed_case_ids=(),
        source_message_id="message-1",
        source_channel="test",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id="travel-action-1",
        admission_operation="record_travel_event",
        conversation_state_version=4,
    )

    receipt = await SqlBusinessExecutor(
        _DuplicateBusinessSession(),
        admission_ticket_store=store,
        execution_authority="semantic_ticket",
    ).execute(command, context)

    assert receipt.status == "duplicate"
    assert receipt.actual_write is False
    assert store.locked == 0
    assert store.consumed == []


@pytest.mark.asyncio
async def test_sql_executor_does_not_consume_ticket_when_domain_command_is_blocked():
    ticket = _issued_travel_ticket()
    store = _RecordingTicketStore()
    session = _BusinessWriteSession()
    command = CreateTravelIntent(
        command_id="travel-command-1",
        destination_raw="南京",
        destination_normalized="",
        city_code="",
        province_code="",
        start_at=datetime(2026, 7, 15, tzinfo=UTC),
        end_at=datetime(2026, 7, 15, 23, 59, tzinfo=UTC),
        time_precision="day",
        purpose_summary="",
        related_case_ids=(),
        confidence=1.0,
    )
    context = BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="u1",
        actor_role_ids=(),
        allowed_case_ids=(),
        source_message_id="message-1",
        source_channel="test",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id="travel-action-1",
        admission_operation="record_travel_event",
        conversation_state_version=4,
    )

    receipt = await SqlBusinessExecutor(
        session,
        admission_ticket_store=store,
        execution_authority="semantic_ticket",
    ).execute(command, context)

    assert receipt.status == "blocked"
    assert receipt.error_code == "travel_location_ambiguous"
    assert store.locked == 1
    assert store.consumed == []
    assert not any(isinstance(item, TravelIntent) for item in session.added)


def _issued_information_continuation_ticket() -> dict[str, object]:
    ticket = _issued_travel_ticket()
    pending_id = str(uuid5(NAMESPACE_URL, "information-continuation-pending"))
    authority_scope = {
        **dict(ticket["authority_scope"]),
        "information_pending_id": pending_id,
        "information_pending_trace_id": str(
            uuid5(NAMESPACE_URL, "information-continuation-original-trace")
        ),
        "continuation_source_message_id": ticket["source_message_id"],
        "continuation_field_values": {"travel_date": "2026-07-15"},
        "continuation_raw_values": {"travel_date": "鏄庡ぉ"},
        "continuation_evidence_spans": {"travel_date": [0, 2]},
    }
    ticket["authority_scope"] = authority_scope
    ticket["fact_claims_sha256"] = _sha256_json(
        {
            "action_id": ticket["action_id"],
            "operation": ticket["operation"],
            "segment_text_sha256": ticket["segment_text_sha256"],
            "authority_scope": authority_scope,
            "allowed_changed_fields": ticket["allowed_changed_fields"],
        }
    )
    ticket["authorized_command_sha256"] = _sha256_json(
        {
            "domain": ticket["domain"],
            "operation": ticket["operation"],
            "object_ref": ticket["object_ref"],
            "authority_scope": authority_scope,
            "allowed_changed_fields": ticket["allowed_changed_fields"],
            "fact_claims_sha256": ticket["fact_claims_sha256"],
        }
    )
    return ticket


class _RollbackNested:
    def __init__(self, session):
        from copy import deepcopy

        self.session = session
        self.snapshot = {
            "ticket": deepcopy(session.ticket_row),
            "pending": deepcopy(session.pending_row),
            "receipt": deepcopy(session.receipt),
            "added": list(session.added),
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.session.ticket_row = self.snapshot["ticket"]
            self.session.pending_row = self.snapshot["pending"]
            self.session.receipt = self.snapshot["receipt"]
            self.session.added = self.snapshot["added"]
        return False


class _ContinuationBusinessSession(_AuthoritativeBusinessSession):
    def __init__(self, ticket, *, pending_cas_success=True):
        super().__init__(ticket)
        authority = dict(ticket["authority_scope"])
        self.pending_cas_success = pending_cas_success
        self.pending_row = {
            "pending_id": authority["information_pending_id"],
            "tenant_id": ticket["tenant_id"],
            "user_id": ticket["user_id"],
            "conversation_id": ticket["conversation_id"],
            "object_type": dict(ticket["object_ref"])["object_type"],
            "object_stable_id": dict(ticket["object_ref"])["stable_id"],
            "expected_conversation_state_version": ticket[
                "expected_conversation_state_version"
            ],
            "pending_status": (
                "awaiting_input" if pending_cas_success else "conflicted"
            ),
            "consumed_at": None,
            "consumed_by_trace_id": None,
            "invalidation_reason": (
                "" if pending_cas_success else "concurrent_state_change"
            ),
            "updated_at": NOW,
        }

    async def execute(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if isinstance(statement, Update) and "agent2_information_pendings" in sql:
            self.statements.append(statement)
            if not self.pending_cas_success:
                return _MappingResult(rowcount=0)
            params = statement.compile(dialect=postgresql.dialect()).params
            self.pending_row.update(
                pending_status=params["pending_status"],
                consumed_at=params["consumed_at"],
                consumed_by_trace_id=params["consumed_by_trace_id"],
                invalidation_reason=params["invalidation_reason"],
                updated_at=params["updated_at"],
            )
            return _MappingResult(rowcount=1)
        return await super().execute(statement)

    def begin_nested(self):
        return _RollbackNested(self)


def _continuation_travel_command_and_context(ticket):
    command = CreateTravelIntent(
        command_id="travel-command-continuation",
        destination_raw=str(dict(ticket["authority_scope"])["destination"]),
        destination_normalized="鍗椾含甯?",
        city_code="320100",
        province_code="320000",
        start_at=datetime(2026, 7, 15, tzinfo=UTC),
        end_at=datetime(2026, 7, 15, 23, 59, tzinfo=UTC),
        time_precision="day",
        purpose_summary="",
        related_case_ids=(),
        confidence=1.0,
    )
    context = BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-1",
        department_id="department-1",
        team_id="team-1",
        actor_user_id="u1",
        actor_role_ids=(),
        allowed_case_ids=(),
        source_message_id="message-1",
        source_channel="test",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id="travel-action-1",
        admission_operation="record_travel_event",
        conversation_state_version=4,
    )
    return command, context


@pytest.mark.asyncio
async def test_continuation_business_receipt_ticket_and_pending_settle_in_one_savepoint():
    ticket = _issued_information_continuation_ticket()
    session = _ContinuationBusinessSession(ticket)
    command, context = _continuation_travel_command_and_context(ticket)

    receipt = await SqlBusinessExecutor(
        session,
        execution_authority="semantic_ticket",
    ).execute(command, context)

    assert (receipt.status, receipt.actual_write) == ("executed", True)
    assert session.ticket_row["ticket_status"] == "consumed"
    assert session.pending_row["pending_status"] == "consumed"
    assert str(session.pending_row["consumed_by_trace_id"]) == ticket["trace_id"]
    assert session.ticket_row["consumed_receipt_ref"] == receipt.receipt_id
    assert any(isinstance(item, TravelIntent) for item in session.added)
    pending_update = next(
        item
        for item in session.statements
        if isinstance(item, Update)
        and "agent2_information_pendings"
        in str(item.compile(dialect=postgresql.dialect()))
    )
    pending_sql = str(pending_update.compile(dialect=postgresql.dialect()))
    assert "expected_conversation_state_version" in pending_sql
    assert "pending_status IN" in pending_sql
    assert "object_stable_id" in pending_sql


@pytest.mark.asyncio
async def test_pending_status_or_version_drift_rolls_back_domain_receipt_and_ticket():
    ticket = _issued_information_continuation_ticket()
    session = _ContinuationBusinessSession(ticket, pending_cas_success=False)
    command, context = _continuation_travel_command_and_context(ticket)

    receipt = await SqlBusinessExecutor(
        session,
        execution_authority="semantic_ticket",
    ).execute(command, context)

    assert receipt.status == "blocked"
    assert receipt.actual_write is False
    assert receipt.error_code == "information_pending_settlement_conflict"
    assert session.ticket_row["ticket_status"] == "issued"
    assert session.ticket_row["consumed_receipt_ref"] is None
    assert session.pending_row["pending_status"] == "conflicted"
    assert not any(isinstance(item, TravelIntent) for item in session.added)


@pytest.mark.asyncio
async def test_duplicate_continuation_receipt_does_not_consume_pending_a_second_time():
    ticket = _issued_information_continuation_ticket()
    store = _RecordingTicketStore()
    command, context = _continuation_travel_command_and_context(ticket)

    receipt = await SqlBusinessExecutor(
        _DuplicateBusinessSession(),
        admission_ticket_store=store,
        execution_authority="semantic_ticket",
    ).execute(command, context)

    assert receipt.status == "duplicate"
    assert receipt.actual_write is False
    assert store.locked == 0
    assert store.consumed == []
