from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    BusinessCommandReceipt,
    CaseFollowupPolicy,
    CaseLifecycleState,
    CaseProgress,
    PartyCaseRole,
    PartyEntity,
    PeriodicReport,
    TravelIntent,
)
from app.legal_ops.phase2_read import (
    _case_is_visible,
    _data_origin,
    load_case_followup_configuration,
    load_phase2_case_detail,
    load_phase2_party_detail,
    load_phase2_read_model,
    search_phase2_parties,
)
from app.legal_ops.access import load_live_principal_scope
from app.legal_ops.auth import SandboxPrincipal
from app.legal_ops.api import phase2_evidence_center
from app.config import Settings


def test_robot_followup_origin_takes_precedence_over_dingtalk_transport_channel():
    assert (
        _data_origin(
            {
                "source_channel": "dingtalk_stream",
                "source_message_id": "real-message-1",
                "content_origin": "robot_followup",
            }
        )
        == "robot_followup"
    )


def test_replaced_fixture_case_is_hidden_without_deleting_its_audit_history():
    assert _case_is_visible(SimpleNamespace(source_json={})) is True
    assert _case_is_visible(SimpleNamespace(source_json={"display_hidden": False})) is True
    assert _case_is_visible(SimpleNamespace(source_json={"display_hidden": True})) is False
    assert (
        _data_origin(
            {
                "source_channel": "dingtalk_stream",
                "after_json": {"content_origin": "robot_followup"},
            }
        )
        == "robot_followup"
    )
    assert (
        _data_origin(
            {
                "source_channel": "dingtalk",
                "resource_type": "case_progress_followup_notification",
            }
        )
        == "robot_followup"
    )
    assert (
        _data_origin(
            {
                "message_type": "case_progress_followup",
                "message_json": {"content_origin": "robot_followup"},
            }
        )
        == "robot_followup"
    )


class _Scalars:
    def all(self):
        return []


class _Session:
    def __init__(self):
        self.statements = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return _Scalars()

    async def scalar(self, statement):
        self.statements.append(statement)
        return None


class _AccessSession(_Session):
    def __init__(self, binding):
        super().__init__()
        self.binding = binding
        self.scalar_count = 0

    async def scalar(self, statement):
        self.statements.append(statement)
        self.scalar_count += 1
        return self.binding if self.scalar_count == 1 else None


class _Values:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)


class _CaseSession:
    def __init__(self, case, progress, party_rows, audits=(), projections=(), clues=()):
        self.case = case
        self.progress = progress
        self.party_rows = party_rows
        self.audits = audits
        self.projections = projections
        self.clues = clues
        self.statements = []
        self.scalar_batches = 0

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.case

    async def scalars(self, statement):
        self.statements.append(statement)
        self.scalar_batches += 1
        if self.scalar_batches == 1:
            return _Values(self.progress)
        if self.scalar_batches == 2:
            return _Values(self.clues)
        if self.scalar_batches == 3:
            return _Values(self.audits)
        return _Values(self.projections)

    async def execute(self, statement):
        self.statements.append(statement)
        return _Values(self.party_rows)


@pytest.mark.asyncio
async def test_phase2_read_model_is_empty_safe_and_every_query_is_tenant_scoped():
    session = _Session()

    result = await load_phase2_read_model(
        session,  # type: ignore[arg-type]
        tenant_id="tenant-test",
    )

    assert result["tenant_id"] == "tenant-test"
    assert result["route_control"] is None
    assert all(value == 0 for value in result["summary"].values())
    assert len(session.statements) == 23
    for statement in session.statements:
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert "tenant_id" in sql
        assert "tenant-test" in compiled.params.values()


@pytest.mark.asyncio
async def test_phase2_case_queries_are_fenced_to_principals_database_case_scope():
    session = _Session()
    allowed_case_id = uuid4()

    await load_phase2_read_model(
        session,  # type: ignore[arg-type]
        tenant_id="tenant-test",
        allowed_case_ids=(str(allowed_case_id),),
        principal_user_id="pang-user",
    )

    case_statement = next(
        statement
        for statement in session.statements
        if statement.column_descriptions[0].get("entity") is Agent2Case
    )
    compiled = case_statement.compile(dialect=postgresql.dialect())
    assert "agent2_cases.case_id IN" in str(compiled)
    assert allowed_case_id in tuple(compiled.params.values())[1]

    statements_by_entity = {
        statement.column_descriptions[0].get("entity"): statement
        for statement in session.statements
        if statement.column_descriptions
    }
    identity_sql = str(
        statements_by_entity[Agent2IdentityBinding].compile(dialect=postgresql.dialect())
    )
    party_sql = str(
        statements_by_entity[PartyEntity].compile(dialect=postgresql.dialect())
    )
    progress_sql = str(
        statements_by_entity[CaseProgress].compile(dialect=postgresql.dialect())
    )
    lifecycle_sql = str(
        statements_by_entity[CaseLifecycleState].compile(dialect=postgresql.dialect())
    )
    policy_sql = str(
        statements_by_entity[CaseFollowupPolicy].compile(dialect=postgresql.dialect())
    )
    travel_sql = str(
        statements_by_entity[TravelIntent].compile(dialect=postgresql.dialect())
    )
    report_sql = str(
        statements_by_entity[PeriodicReport].compile(dialect=postgresql.dialect())
    )
    receipt_sql = str(
        statements_by_entity[BusinessCommandReceipt].compile(dialect=postgresql.dialect())
    )
    assert "agent2_identity_bindings.user_id" in identity_sql
    assert "agent2_party_case_roles.case_id IN" in party_sql
    assert "agent2_case_progress.case_id IN" in progress_sql
    assert "agent2_case_lifecycle_states.case_id IN" in lifecycle_sql
    assert "agent2_case_followup_policies.case_id IN" in policy_sql
    assert "agent2_travel_intents.user_id" in travel_sql
    assert "agent2_periodic_reports.owner_user_id" in report_sql
    assert "agent2_business_command_receipts.actor_user_id" in receipt_sql


@pytest.mark.asyncio
async def test_phase2_case_detail_returns_not_found_outside_principals_database_case_scope():
    case_id = uuid4()
    session = _CaseSession(SimpleNamespace(case_id=case_id), (), ())

    with pytest.raises(LookupError, match="case not found"):
        await load_phase2_case_detail(
            session,  # type: ignore[arg-type]
            tenant_id="tenant-test",
            case_id=str(case_id),
            allowed_case_ids=(str(uuid4()),),
        )

    assert session.statements == []


@pytest.mark.asyncio
async def test_live_principal_scope_is_loaded_from_active_identity_binding_not_credential_payload():
    first_case_id = uuid4()
    second_case_id = uuid4()
    binding = SimpleNamespace(
        tenant_id="tenant-test",
        user_id="pang-user",
        active=True,
        permission_scope_json={
            "allowed_case_ids": [str(first_case_id), str(second_case_id), "not-a-uuid"]
        },
    )
    session = _AccessSession(binding)
    principal = SandboxPrincipal(
        tenant_id="tenant-test",
        user_id="pang-user",
        role_ids=("case_owner",),
        team_ids=("untrusted-team-from-token",),
    )

    scope = await load_live_principal_scope(session, principal)  # type: ignore[arg-type]

    assert scope.allowed_case_ids == (str(first_case_id), str(second_case_id))
    assert scope.user_id == "pang-user"
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    assert "agent2_identity_bindings.tenant_id" in str(compiled)
    assert "agent2_identity_bindings.user_id" in str(compiled)
    assert "agent2_identity_bindings.active" in str(compiled)


@pytest.mark.asyncio
async def test_live_principal_without_active_identity_binding_is_fail_closed():
    principal = SandboxPrincipal(
        tenant_id="tenant-test", user_id="unknown-user", role_ids=("case_owner",)
    )

    with pytest.raises(PermissionError, match="active Legal Ops identity binding required"):
        await load_live_principal_scope(_AccessSession(None), principal)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_party_search_and_detail_are_fenced_to_authorized_cases():
    allowed_case_id = uuid4()
    search_session = _Session()

    await search_phase2_parties(
        search_session,  # type: ignore[arg-type]
        tenant_id="tenant-test",
        allowed_case_ids=(str(allowed_case_id),),
    )

    search_sql = str(
        search_session.statements[0].compile(dialect=postgresql.dialect())
    )
    assert "agent2_party_case_roles.case_id IN" in search_sql

    party_id = uuid4()
    detail_session = _CaseSession(None, (), ())
    with pytest.raises(LookupError, match="party not found"):
        await load_phase2_party_detail(
            detail_session,  # type: ignore[arg-type]
            tenant_id="tenant-test",
            party_id=str(party_id),
            allowed_case_ids=(str(allowed_case_id),),
        )
    detail_sql = str(
        detail_session.statements[0].compile(dialect=postgresql.dialect())
    )
    assert "agent2_party_case_roles.case_id IN" in detail_sql


@pytest.mark.asyncio
async def test_followup_configuration_is_not_discoverable_outside_authorized_cases():
    session = _Session()

    with pytest.raises(LookupError, match="case not found"):
        await load_case_followup_configuration(
            session,  # type: ignore[arg-type]
            tenant_id="tenant-test",
            case_id=str(uuid4()),
            allowed_case_ids=(str(uuid4()),),
        )

    assert session.statements == []


@pytest.mark.asyncio
async def test_live_phase2_endpoint_reads_when_write_switch_is_closed():
    result = await phase2_evidence_center(
        runtime=SimpleNamespace(mode="sandbox_live"),
        principal=SandboxPrincipal(
            tenant_id="tenant-test", user_id="admin", role_ids=("tenant_admin",)
        ),
        settings=Settings(
            legal_ops_live_enabled=True,
            legal_ops_live_tenant_id="tenant-test",
            legal_ops_live_token="test-token",
            agent2_business_phase2_enabled=False,
        ),
        session=_Session(),  # type: ignore[arg-type]
        limit=100,
    )

    assert result["tenant_id"] == "tenant-test"
    assert result["summary"]["parties"] == 0


@pytest.mark.asyncio
async def test_live_phase2_endpoint_applies_database_case_scope_for_case_owner():
    allowed_case_id = uuid4()
    binding = SimpleNamespace(
        permission_scope_json={"allowed_case_ids": [str(allowed_case_id)]}
    )
    session = _AccessSession(binding)

    await phase2_evidence_center(
        runtime=SimpleNamespace(mode="sandbox_live"),
        principal=SandboxPrincipal(
            tenant_id="tenant-test", user_id="pang-user", role_ids=("case_owner",)
        ),
        settings=Settings(
            legal_ops_live_enabled=True,
            legal_ops_live_tenant_id="tenant-test",
            legal_ops_live_token="test-token",
        ),
        session=session,  # type: ignore[arg-type]
        limit=100,
    )

    case_statement = next(
        statement
        for statement in session.statements
        if statement.column_descriptions[0].get("entity") is Agent2Case
    )
    assert "agent2_cases.case_id IN" in str(
        case_statement.compile(dialect=postgresql.dialect())
    )


@pytest.mark.asyncio
async def test_phase2_case_detail_projects_internal_progress_into_lifecycle_with_source_and_audit_fields():
    now = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)
    case_id = uuid4()
    party_id = uuid4()
    case = Agent2Case(
        case_id=case_id,
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        external_case_id="case-ext-1",
        case_number="（2026）苏01民初101号",
        case_name="华东建设合同纠纷案",
        case_type="litigation",
        status="open",
        owner_user_id="user-1",
        source_type="case_registry",
        source_id="case-ext-1",
        source_json={},
        version=1,
        created_at=now,
        updated_at=now,
    )
    progress = CaseProgress(
        progress_id=uuid4(),
        tenant_id="tenant-test",
        case_id=case_id,
        occurred_at=now,
        recorded_at=now,
        reporter_id="user-1",
        progress_type="hearing",
        summary="今天开庭，对方提出调解",
        details="",
        source_message_id="message-1",
        source_channel="dingtalk",
        content_origin="human_record",
        related_party_ids=[],
        related_document_ids=[],
        related_travel_intent_ids=[],
        confidence=Decimal("0.99"),
        confirmation_status="confirmed_by_reporter",
        version=1,
        idempotency_key="progress-key",
        deleted_by="",
        delete_reason="",
        created_at=now,
        updated_at=now,
    )
    party = PartyEntity(
        party_id=party_id,
        tenant_id="tenant-test",
        party_type="company",
        canonical_name="南京华东建设有限公司",
        normalized_name="南京华东建设有限公司",
        short_name="华东建设",
        former_names=[],
        unified_social_credit_code="91320100TEST001",
        registration_number="",
        legal_representative="",
        status="active",
        registered_address="",
        source_type="case_registry",
        source_id="party-1",
        data_quality="confirmed_identifier",
        version=1,
        created_at=now,
        updated_at=now,
    )
    role = PartyCaseRole(
        role_id=uuid4(),
        tenant_id="tenant-test",
        party_id=party_id,
        case_id=case_id,
        role_type="defendant",
        source_reference={"source_id": "party-1"},
        confirmation_status="confirmed",
        created_at=now,
        updated_at=now,
    )
    session = _CaseSession(case, (progress,), ((role, party),))

    result = await load_phase2_case_detail(
        session,  # type: ignore[arg-type]
        tenant_id="tenant-test",
        case_id=str(case_id),
    )

    assert result["case"]["case_id"] == str(case_id)
    assert result["parties"][0]["role"]["role_type"] == "defendant"
    node = result["lifecycle"]["internal_progress_nodes"][0]
    assert node["kind"] == "internal_progress"
    assert node["content_origin"] == "human_record"
    assert node["source_message_id"] == "message-1"
    assert node["deleted"] is False
    assert node["version"] == 1
    for statement in session.statements:
        compiled = statement.compile(dialect=postgresql.dialect())
        assert "tenant_id" in str(compiled)
        assert "tenant-test" in compiled.params.values()
