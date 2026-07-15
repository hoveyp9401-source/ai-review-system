from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2 import admission_store_sql
from app.agent2.admission_hashes import compute_admission_claim_hashes
from app.agent2.admission_store_sql import (
    SqlAdmissionTicketLease,
    SqlAdmissionTicketStore,
)
from app.agent2.business.contracts import (
    BusinessCommandContext,
    BusinessCommandError,
    RespondTravelCollaboration,
    UpdateCaseProgress,
)
from app.agent2.case_followup_commands import (
    TriggerCaseFollowupNow,
    UpdateCaseFollowupPolicy,
)


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


class _MappingResult:
    def __init__(self, row=None):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class _LiveSession:
    def __init__(
        self,
        *,
        progress=None,
        case=None,
        travel=None,
        policy=None,
        active_task=None,
    ):
        self.progress = progress
        self.case = case
        self.travel = travel
        self.policy = policy
        self.active_task = active_task
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "agent2_case_progress" in sql:
            return _MappingResult(self.progress)
        if "agent2_travel_collaboration_candidates" in sql:
            return _MappingResult(self.travel)
        if "agent2_case_followup_policies" in sql:
            return _MappingResult(self.policy)
        if "agent2_case_followup_tasks" in sql:
            return _MappingResult(self.active_task)
        if "agent2_cases" in sql:
            return _MappingResult(self.case)
        raise AssertionError(sql)


class _IssuedStore(SqlAdmissionTicketStore):
    def __init__(self, session, ticket):
        super().__init__(session)
        self.ticket = ticket

    async def acquire(self, request):
        return SqlAdmissionTicketLease(
            ticket_id=UUID(self.ticket["ticket_id"]),
            tenant_id=request.tenant_id,
            receipt_kind=request.receipt_kind,
            command_type=request.command_type,
            authoritative_ticket=self.ticket,
        )


def _context(*, case_id: str) -> BusinessCommandContext:
    return BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="department-test",
        team_id="team-test",
        actor_user_id="user-1",
        actor_role_ids=(),
        allowed_case_ids=(case_id,),
        source_message_id="message-1",
        source_channel="test",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        admission_required=True,
        admission_action_id="action-1",
        conversation_state_version=4,
    )


def _ticket(
    *,
    domain: str,
    operation: str,
    object_type: str,
    stable_id: str,
    version: int,
    authority_scope: dict,
    allowed_changed_fields: list[str],
) -> dict:
    return {
        "ticket_id": str(uuid4()),
        "tenant_id": "tenant-test",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_message_id": "message-1",
        "action_id": "action-1",
        "domain": domain,
        "operation": operation,
        "object_ref": {
            "object_type": object_type,
            "stable_id": stable_id,
            "version": version,
        },
        "expected_conversation_state_version": 4,
        "authority_scope": authority_scope,
        "allowed_changed_fields": allowed_changed_fields,
        "ticket_status": "issued",
        "contract_version": "agent2.domain_admission.v1",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "executor_revalidation_required": True,
        "proves_business_write": False,
    }


def _integrity_ticket(
    *,
    domain: str,
    operation: str,
    allowed_changed_fields: list[str],
) -> dict:
    stable_id = str(uuid4())
    ticket = _ticket(
        domain=domain,
        operation=operation,
        object_type=(
            "case_followup_policy"
            if "followup" in operation
            else "case_progress"
        ),
        stable_id=stable_id,
        version=1,
        authority_scope={"stable_id": stable_id},
        allowed_changed_fields=allowed_changed_fields,
    )
    fact_hash, command_hash = compute_admission_claim_hashes(
        action_id=ticket["action_id"],
        operation=operation,
        segment_text_sha256="a" * 64,
        domain=domain,
        object_ref=ticket["object_ref"],
        authority_scope=ticket["authority_scope"],
        allowed_changed_fields=allowed_changed_fields,
    )
    ticket.update(
        segment_text_sha256="a" * 64,
        fact_claims_sha256=fact_hash,
        authorized_command_sha256=command_hash,
        policy_version="agent2.domain_admission.policy.v1",
        ttl_seconds=300,
    )
    return ticket


@pytest.mark.parametrize(
    ("domain", "operation", "allowed_changed_fields"),
    [
        ("case", "update_case_progress", ["summary", "details"]),
        ("case", "link_case_progress", ["related_party_ids"]),
        (
            "case",
            "update_case_followup_policy",
            ["cadence_type", "enabled"],
        ),
    ],
)
def test_dynamic_mutation_changed_field_contract_is_closed_and_hash_bound(
    domain,
    operation,
    allowed_changed_fields,
):
    admission_store_sql._require_ticket_integrity(
        _integrity_ticket(
            domain=domain,
            operation=operation,
            allowed_changed_fields=allowed_changed_fields,
        )
    )


def test_dynamic_mutation_changed_field_order_drift_is_rejected():
    ticket = _integrity_ticket(
        domain="case",
        operation="update_case_progress",
        allowed_changed_fields=["details", "summary"],
    )

    with pytest.raises(BusinessCommandError) as error:
        admission_store_sql._require_ticket_integrity(ticket)

    assert error.value.code == "admission_ticket_claims_mismatch"


@pytest.mark.asyncio
async def test_case_progress_mutation_revalidates_live_owner_case_and_version():
    case_id = str(uuid4())
    progress_id = str(uuid4())
    ticket = _ticket(
        domain="case",
        operation="update_case_progress",
        object_type="case_progress",
        stable_id=progress_id,
        version=3,
        authority_scope={
            "progress_id": progress_id,
            "case_id": case_id,
            "version": 3,
            "raw_fact": "改成已联系法院",
            "replacement_summary": "已联系法院",
        },
        allowed_changed_fields=["summary"],
    )
    context = _context(case_id=case_id)
    context = BusinessCommandContext(
        **{
            **context.as_dict(),
            "admission_ticket": ticket,
            "admission_operation": "update_case_progress",
        }
    )
    command = UpdateCaseProgress(
        command_id="command-1",
        progress_id=progress_id,
        expected_version=3,
        summary="已联系法院",
        details=None,
    )
    session = _LiveSession(
        progress={
            "progress_id": UUID(progress_id),
            "case_id": UUID(case_id),
            "reporter_id": "user-1",
            "version": 3,
            "deleted_at": None,
        },
        case={"case_id": UUID(case_id)},
    )

    lease = await _IssuedStore(session, ticket).lock_and_validate(command, context)

    assert lease.authoritative_ticket == ticket
    assert all(
        "FOR UPDATE" in str(item.compile(dialect=postgresql.dialect()))
        for item in session.statements
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("progress_patch", "expected_code"),
    [
        ({"reporter_id": "user-2"}, "admission_ticket_permission_revoked"),
        ({"version": 4}, "admission_ticket_object_version_conflict"),
        ({"deleted_at": NOW}, "admission_ticket_object_missing"),
    ],
)
async def test_case_progress_live_drift_blocks_before_domain_write(
    progress_patch,
    expected_code,
):
    case_id = str(uuid4())
    progress_id = str(uuid4())
    ticket = _ticket(
        domain="case",
        operation="update_case_progress",
        object_type="case_progress",
        stable_id=progress_id,
        version=3,
        authority_scope={
            "progress_id": progress_id,
            "case_id": case_id,
            "version": 3,
            "raw_fact": "改成已联系法院",
            "replacement_summary": "已联系法院",
        },
        allowed_changed_fields=["summary"],
    )
    context = _context(case_id=case_id)
    context = BusinessCommandContext(
        **{
            **context.as_dict(),
            "admission_ticket": ticket,
            "admission_operation": "update_case_progress",
        }
    )
    command = UpdateCaseProgress(
        "command-1", progress_id, 3, "已联系法院", None
    )
    progress = {
        "progress_id": UUID(progress_id),
        "case_id": UUID(case_id),
        "reporter_id": "user-1",
        "version": 3,
        "deleted_at": None,
        **progress_patch,
    }

    with pytest.raises(BusinessCommandError) as error:
        await _IssuedStore(
            _LiveSession(progress=progress, case={"case_id": UUID(case_id)}),
            ticket,
        ).lock_and_validate(command, context)

    assert error.value.code == expected_code


@pytest.mark.asyncio
async def test_case_progress_compiled_claim_drift_blocks_without_live_query():
    case_id = str(uuid4())
    progress_id = str(uuid4())
    ticket = _ticket(
        domain="case",
        operation="update_case_progress",
        object_type="case_progress",
        stable_id=progress_id,
        version=3,
        authority_scope={
            "progress_id": progress_id,
            "case_id": case_id,
            "version": 3,
            "raw_fact": "改成已联系法院",
            "replacement_summary": "已联系法院",
        },
        allowed_changed_fields=["summary"],
    )
    context = _context(case_id=case_id)
    context = BusinessCommandContext(
        **{
            **context.as_dict(),
            "admission_ticket": ticket,
            "admission_operation": "update_case_progress",
        }
    )
    session = _LiveSession()

    with pytest.raises(BusinessCommandError) as error:
        await _IssuedStore(session, ticket).lock_and_validate(
            UpdateCaseProgress(
                "command-1", progress_id, 3, "更强的未授权事实", None
            ),
            context,
        )

    assert error.value.code == "admission_ticket_claims_mismatch"
    assert session.statements == []


@pytest.mark.asyncio
async def test_travel_response_revalidates_participant_status_expiry_and_version():
    case_id = str(uuid4())
    candidate_id = str(uuid4())
    ticket = _ticket(
        domain="travel",
        operation="respond_travel_collaboration",
        object_type="travel_collaboration_candidate",
        stable_id=candidate_id,
        version=2,
        authority_scope={
            "candidate_id": candidate_id,
            "version": 2,
            "participant_user_id": "user-1",
            "response": "accept",
            "raw_fact": "需要",
        },
        allowed_changed_fields=["responses_json", "status", "version"],
    )
    context = _context(case_id=case_id)
    context = BusinessCommandContext(
        **{
            **context.as_dict(),
            "admission_ticket": ticket,
            "admission_operation": "respond_travel_collaboration",
        }
    )
    session = _LiveSession(
        travel={
            "candidate_id": UUID(candidate_id),
            "participant_ids": ["user-1", "user-2"],
            "responses_json": {},
            "status": "notified",
            "version": 2,
            "expires_at": NOW + timedelta(days=1),
        }
    )

    await _IssuedStore(session, ticket).lock_and_validate(
        RespondTravelCollaboration("command-1", candidate_id, "accept", 2),
        context,
    )

    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_travel_response_repeated_actor_reply_is_blocked():
    case_id = str(uuid4())
    candidate_id = str(uuid4())
    ticket = _ticket(
        domain="travel",
        operation="respond_travel_collaboration",
        object_type="travel_collaboration_candidate",
        stable_id=candidate_id,
        version=2,
        authority_scope={
            "candidate_id": candidate_id,
            "version": 2,
            "participant_user_id": "user-1",
            "response": "accept",
            "raw_fact": "需要",
        },
        allowed_changed_fields=["responses_json", "status", "version"],
    )
    context = _context(case_id=case_id)
    context = BusinessCommandContext(
        **{
            **context.as_dict(),
            "admission_ticket": ticket,
            "admission_operation": "respond_travel_collaboration",
        }
    )
    session = _LiveSession(
        travel={
            "candidate_id": UUID(candidate_id),
            "participant_ids": ["user-1", "user-2"],
            "responses_json": {"user-1": "accept"},
            "status": "accepted_by_one",
            "version": 2,
            "expires_at": NOW + timedelta(days=1),
        }
    )

    with pytest.raises(BusinessCommandError) as error:
        await _IssuedStore(session, ticket).lock_and_validate(
            RespondTravelCollaboration("command-1", candidate_id, "accept", 2),
            context,
        )

    assert error.value.code == "admission_ticket_object_conflict"


@pytest.mark.asyncio
async def test_followup_policy_version_zero_revalidates_missing_policy_and_assignment():
    case_id = str(uuid4())
    ticket = _ticket(
        domain="case",
        operation="update_case_followup_policy",
        object_type="case_followup_policy",
        stable_id=case_id,
        version=0,
        authority_scope={
            "case_id": case_id,
            "case_version": 7,
            "assigned_user_id": "user-1",
            "policy_version": 0,
            "raw_fact": "这个案子改成每周问一次",
            "cadence_type": "weekly",
            "enabled": True,
        },
        allowed_changed_fields=["cadence_type", "enabled"],
    )
    context = _context(case_id=case_id)
    context = BusinessCommandContext(
        **{
            **context.as_dict(),
            "admission_ticket": ticket,
            "admission_operation": "update_case_followup_policy",
        }
    )
    command = UpdateCaseFollowupPolicy(
        command_id="command-1",
        tenant_id="tenant-test",
        case_id=case_id,
        assigned_user_id="user-1",
        expected_version=0,
        policy_source="case_manual_override",
        cadence_type="weekly",
        enabled=True,
        force_manual_override=False,
        source_turn_id="message-1",
        idempotency_key="followup-policy-1",
    )
    session = _LiveSession(
        case={"case_id": UUID(case_id), "owner_user_id": "user-1"},
        policy=None,
    )

    await _IssuedStore(session, ticket).lock_and_validate(command, context)

    assert len(session.statements) == 2


@pytest.mark.asyncio
async def test_followup_now_blocks_when_active_task_exists():
    case_id = str(uuid4())
    ticket = _ticket(
        domain="case",
        operation="trigger_case_followup_now",
        object_type="case_followup_policy",
        stable_id=case_id,
        version=2,
        authority_scope={
            "case_id": case_id,
            "case_version": 7,
            "assigned_user_id": "user-1",
            "policy_version": 2,
            "raw_fact": "现在问我一次",
        },
        allowed_changed_fields=["followup_task", "notification_outbox"],
    )
    context = _context(case_id=case_id)
    context = BusinessCommandContext(
        **{
            **context.as_dict(),
            "admission_ticket": ticket,
            "admission_operation": "trigger_case_followup_now",
        }
    )
    command = TriggerCaseFollowupNow(
        command_id="command-1",
        tenant_id="tenant-test",
        case_id=case_id,
        assigned_user_id="user-1",
        source_turn_id="message-1",
        idempotency_key="followup-now-1",
        expected_policy_version=2,
    )
    session = _LiveSession(
        case={"case_id": UUID(case_id), "owner_user_id": "user-1"},
        policy={"policy_id": uuid4(), "version": 2},
        active_task={"followup_id": uuid4()},
    )

    with pytest.raises(BusinessCommandError) as error:
        await _IssuedStore(session, ticket).lock_and_validate(command, context)

    assert error.value.code == "admission_ticket_object_conflict"
