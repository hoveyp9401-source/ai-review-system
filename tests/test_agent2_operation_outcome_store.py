from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.operation_outcome_store import (
    OperationOutcomeIdempotencyConflictError,
    persist_operation_outcomes,
)
from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)


class _Result:
    def __init__(self, value): self.value = value
    def scalar_one_or_none(self): return self.value


class _Session:
    def __init__(self):
        self.statements = []
        self.flush_count = 0
    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(uuid4())
    async def flush(self):
        self.flush_count += 1


class _ConflictSession(_Session):
    def __init__(self, existing):
        super().__init__()
        self.existing = existing

    async def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _Result(None)
        return _Result(self.existing)


def _outcome(**overrides) -> OperationOutcome:
    values = {
        "domain": "case_progress",
        "operation": "create",
        "object_ref": OutcomeObjectRef(
            "case_progress", str(uuid4()), "南京工程款案"
        ),
        "business_status": "succeeded",
        "message_status": "not_applicable",
        "changed_fields": ("summary",),
        "user_visible_snapshot": {"content": "今天联系法院"},
        "blocking_reason": "",
        "receipt_refs": (
            OutcomeReceiptRef(str(uuid4()), "database", "executed", True),
        ),
        "state_transition": OutcomeStateTransition("absent", "recorded"),
        "actual_write": True,
    }
    values.update(overrides)
    return OperationOutcome(**values)


def _persisted_row(outcome: OperationOutcome, *, now: datetime):
    return SimpleNamespace(
        outcome_id=UUID(outcome.outcome_id),
        tenant_id="tenant-a",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
        domain=outcome.domain,
        operation=outcome.operation,
        object_type=outcome.object_ref.object_type,
        object_id=outcome.object_ref.stable_id,
        object_label=outcome.object_ref.label,
        object_version=outcome.object_ref.version,
        business_status=outcome.business_status,
        message_status=outcome.message_status,
        actual_write=outcome.actual_write,
        would_write=outcome.would_write,
        changed_fields_json=list(outcome.changed_fields),
        user_visible_snapshot_json=dict(outcome.user_visible_snapshot),
        blocking_reason=outcome.blocking_reason,
        receipt_refs_json=[item.as_dict() for item in outcome.receipt_refs],
        audit_refs_json=list(outcome.audit_refs),
        state_transition_json=outcome.state_transition.as_dict(),
        idempotency_key=outcome.idempotency_key,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_reply_outcome_is_persisted_with_fallback_scope_and_receipt_facts():
    outcome = _outcome(
        object_ref=OutcomeObjectRef(
            "case_progress",
            str(uuid4()),
            "南京工程款案",
            7,
        )
    )
    session = _Session()

    inserted = await persist_operation_outcomes(
        session, (outcome,), tenant_id="tenant-a", user_id="user-1",
        conversation_id="conversation-1", source_turn_id="message-1",
        now=datetime(2026, 7, 13, 10, 0, tzinfo=UTC),
    )

    assert inserted == 1
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    assert compiled.params["tenant_id"] == "tenant-a"
    assert compiled.params["object_label"] == "南京工程款案"
    assert compiled.params["object_version"] == 7
    assert compiled.params["message_status"] == "not_applicable"
    assert compiled.params["receipt_refs_json"][0]["actual_write"] is True


@pytest.mark.asyncio
async def test_exact_outcome_scope_is_validated_but_caller_scope_remains_authoritative():
    outcome = _outcome(
        tenant_id="tenant-a",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
    )
    session = _Session()

    inserted = await persist_operation_outcomes(
        session,
        (outcome,),
        tenant_id="tenant-a",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
    )

    assert inserted == 1
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    assert compiled.params["tenant_id"] == "tenant-a"
    assert compiled.params["user_id"] == "user-1"
    assert compiled.params["conversation_id"] == "conversation-1"
    assert compiled.params["source_turn_id"] == "message-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field",
    ("tenant_id", "user_id", "conversation_id", "source_turn_id"),
)
async def test_trusted_caller_scope_must_be_nonempty_before_any_sql(missing_field):
    scope = {
        "tenant_id": "tenant-a",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_turn_id": "message-1",
    }
    scope[missing_field] = ""
    session = _Session()

    with pytest.raises(ValueError, match="operation_outcome_scope_required"):
        await persist_operation_outcomes(session, (_outcome(),), **scope)

    assert session.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome_field", "foreign_value"),
    (
        ("tenant_id", "tenant-b"),
        ("user_id", "user-2"),
        ("conversation_id", "conversation-2"),
        ("source_turn_id", "message-2"),
    ),
)
async def test_any_outcome_scope_mismatch_blocks_the_whole_batch_before_sql(
    outcome_field, foreign_value
):
    session = _Session()
    mismatched = _outcome(**{outcome_field: foreign_value})

    with pytest.raises(ValueError, match=f"operation_outcome_scope_mismatch:{outcome_field}"):
        await persist_operation_outcomes(
            session,
            (_outcome(), mismatched),
            tenant_id="tenant-a",
            user_id="user-1",
            conversation_id="conversation-1",
            source_turn_id="message-1",
        )

    assert session.statements == []


@pytest.mark.asyncio
async def test_exact_idempotent_replay_loads_and_compares_the_existing_row():
    now = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    outcome = _outcome(
        outcome_id=str(uuid4()),
        idempotency_key="operation-outcome-key",
        object_ref=OutcomeObjectRef(
            "case_progress",
            str(uuid4()),
            "南京工程款案",
            7,
        ),
    )
    session = _ConflictSession(_persisted_row(outcome, now=now))

    inserted = await persist_operation_outcomes(
        session,
        (outcome,),
        tenant_id="tenant-a",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
        now=now,
    )

    assert inserted == 0
    assert len(session.statements) == 2
    assert session.flush_count == 1
    replay_lookup = session.statements[1].compile(dialect=postgresql.dialect())
    assert "tenant-a" in replay_lookup.params.values()
    assert "operation-outcome-key" in replay_lookup.params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "different_value"),
    (
        ("outcome_id", uuid4()),
        ("tenant_id", "tenant-b"),
        ("user_id", "user-2"),
        ("conversation_id", "conversation-2"),
        ("source_turn_id", "message-2"),
        ("domain", "travel"),
        ("operation", "update"),
        ("object_type", "other_object"),
        ("object_id", "other-id"),
        ("object_label", "另一案件"),
        ("object_version", 8),
        ("business_status", "failed"),
        ("message_status", "queued"),
        ("actual_write", False),
        ("would_write", True),
        ("changed_fields_json", ["other_field"]),
        ("user_visible_snapshot_json", {"content": "不同事实"}),
        ("blocking_reason", "different_reason"),
        ("receipt_refs_json", []),
        ("audit_refs_json", ["audit-2"]),
        ("state_transition_json", {"from": "absent", "to": "failed"}),
        ("idempotency_key", "different-key"),
    ),
)
async def test_idempotency_collision_rejects_any_changed_persisted_fact(
    field_name, different_value
):
    now = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    outcome = _outcome(
        outcome_id=str(uuid4()),
        idempotency_key="operation-outcome-key",
    )
    existing = _persisted_row(outcome, now=now)
    setattr(existing, field_name, different_value)
    session = _ConflictSession(existing)

    with pytest.raises(
        OperationOutcomeIdempotencyConflictError,
        match="^operation_outcome_idempotency_conflict$",
    ):
        await persist_operation_outcomes(
            session,
            (outcome,),
            tenant_id="tenant-a",
            user_id="user-1",
            conversation_id="conversation-1",
            source_turn_id="message-1",
            now=now,
        )

    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_retry_time_is_not_treated_as_a_changed_business_fact():
    first_write_time = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    retry_time = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
    outcome = _outcome(
        outcome_id=str(uuid4()),
        idempotency_key="operation-outcome-key",
    )
    session = _ConflictSession(_persisted_row(outcome, now=first_write_time))

    inserted = await persist_operation_outcomes(
        session,
        (outcome,),
        tenant_id="tenant-a",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
        now=retry_time,
    )

    assert inserted == 0
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_explicit_outcome_created_at_is_compared_on_idempotent_replay():
    created_at = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    outcome = _outcome(
        outcome_id=str(uuid4()),
        idempotency_key="operation-outcome-key",
        created_at=created_at,
    )
    existing = _persisted_row(outcome, now=created_at)
    existing.created_at = datetime(2026, 7, 13, 10, 1, tzinfo=UTC)
    session = _ConflictSession(existing)

    with pytest.raises(OperationOutcomeIdempotencyConflictError):
        await persist_operation_outcomes(
            session,
            (outcome,),
            tenant_id="tenant-a",
            user_id="user-1",
            conversation_id="conversation-1",
            source_turn_id="message-1",
            now=created_at,
        )

    assert session.flush_count == 0
