from __future__ import annotations

from types import SimpleNamespace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.models import CaseProgress, TenantRouteControl
from app.agent2.business.repositories import (
    CaseProgressSqlRepository,
    PartyQueryScope,
    PartySqlRepository,
)
from app.agent2.business.route_control import decide_agent2_failure, decide_route


class _ScalarRows:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _RowResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _PartySessionStub:
    def __init__(self, *, identifier=None, scalar_batches=(), rows=()):
        self.identifier = identifier
        self.scalar_batches = list(scalar_batches)
        self.rows = rows
        self.calls = []

    async def scalar(self, statement):
        self.calls.append(("scalar", statement))
        return self.identifier

    async def scalars(self, statement):
        self.calls.append(("scalars", statement))
        return _ScalarRows(self.scalar_batches.pop(0))

    async def execute(self, statement):
        self.calls.append(("execute", statement))
        return _RowResult(self.rows)


class _ProgressSessionStub:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def scalars(self, statement):
        self.statements.append(statement)
        return _ScalarRows(self.rows)


def _progress_context(case_ids, *, roles=("lawyer",)):
    return BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=roles,
        allowed_case_ids=tuple(str(value) for value in case_ids),
        source_message_id="message-update",
        source_channel="dingtalk",
        occurred_at=datetime(2026, 7, 11, 9, 10, tzinfo=UTC),
    )


def _progress(case_id, *, progress_id=None, reporter="user-1", version=1):
    now = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)
    return CaseProgress(
        progress_id=progress_id or uuid4(),
        tenant_id="tenant-test",
        case_id=case_id,
        occurred_at=now,
        recorded_at=now,
        reporter_id=reporter,
        progress_type="general_update",
        summary="案件有新进展",
        details="",
        source_message_id="message-create",
        source_channel="dingtalk",
        content_origin="human_record",
        related_party_ids=[],
        related_document_ids=[],
        related_travel_intent_ids=[],
        confidence=Decimal("0.99"),
        confirmation_status="confirmed_by_reporter",
        version=version,
        idempotency_key="create-key",
        deleted_by="",
        delete_reason="",
        created_at=now,
        updated_at=now,
    )


def _control(*, mode: str, canary=(), rollback=False) -> TenantRouteControl:
    return TenantRouteControl(
        tenant_id="tenant-test",
        route_mode=mode,
        canary_user_ids=list(canary),
        agent1_rollback_enabled=rollback,
        version=1,
        changed_by="admin",
        change_reason="test",
    )


def test_missing_route_control_is_blocked():
    decision = decide_route(None, tenant_id="tenant-test", user_id="u1")

    assert decision.route == "blocked"
    assert decision.reason == "no_tenant_cutover_control"


def test_canary_routes_only_listed_users_to_agent2():
    control = _control(mode="agent2_canary", canary=("u1",))

    assert decide_route(control, tenant_id="tenant-test", user_id="u1").route == "agent2_primary"
    assert decide_route(control, tenant_id="tenant-test", user_id="u2").route == "blocked"


def test_agent2_primary_failure_never_automatically_falls_back_to_agent1():
    control = _control(mode="agent2_primary", rollback=True)
    primary = decide_route(control, tenant_id="tenant-test", user_id="u1")

    implicit = decide_agent2_failure(primary, explicit_rollback_requested=False)
    explicit = decide_agent2_failure(primary, explicit_rollback_requested=True)

    assert implicit.route == "blocked"
    assert implicit.reason == "agent2_failure_no_agent1_fallback"
    assert explicit.route == "blocked"
    assert explicit.reason == "agent2_failure_no_agent1_fallback"


def test_party_sql_statements_are_tenant_and_case_scoped_and_use_pg_trgm():
    case_id = uuid4()
    statement = PartySqlRepository._fuzzy_statement("tenant-test", (case_id,), "测试公司")
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "agent2_party_entities.tenant_id" in sql
    assert "agent2_party_case_roles.case_id IN" in sql
    assert "similarity(" in sql
    assert "confirmation_status" in sql


@pytest.mark.asyncio
async def test_sql_party_resolver_accepts_confirmed_exact_identifier():
    party_id = uuid4()
    session = _PartySessionStub(scalar_batches=((party_id,),))
    repository = PartySqlRepository(session)  # type: ignore[arg-type]

    result = await repository.resolve(
        "91320100TEST",
        scope=PartyQueryScope("tenant-test", (str(uuid4()),)),
    )

    assert result.status == "resolved"
    assert result.party_id == str(party_id)
    assert result.match_basis == "exact_identifier"


@pytest.mark.asyncio
async def test_sql_party_resolver_never_arbitrarily_selects_conflicting_exact_identifiers():
    party_ids = (uuid4(), uuid4())
    session = _PartySessionStub(scalar_batches=(party_ids,))
    repository = PartySqlRepository(session)  # type: ignore[arg-type]

    result = await repository.resolve(
        "91320100CONFLICT",
        scope=PartyQueryScope("tenant-test", (str(uuid4()),)),
    )

    assert result.status == "needs_clarification"
    assert result.party_id == ""
    assert {candidate.party_id for candidate in result.candidates} == {
        str(value) for value in party_ids
    }
    assert all(candidate.match_basis == "exact_identifier_conflict" for candidate in result.candidates)


@pytest.mark.asyncio
async def test_sql_party_resolver_stops_at_conflicting_exact_canonical_names():
    party_ids = (uuid4(), uuid4())
    session = _PartySessionStub(scalar_batches=((), party_ids))
    repository = PartySqlRepository(session)  # type: ignore[arg-type]

    result = await repository.resolve(
        "Same Name Company",
        scope=PartyQueryScope("tenant-test", (str(uuid4()),)),
    )

    assert result.status == "needs_clarification"
    assert {candidate.party_id for candidate in result.candidates} == {
        str(value) for value in party_ids
    }
    assert all(
        candidate.match_basis == "exact_canonical_name_conflict"
        for candidate in result.candidates
    )
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_sql_party_resolver_returns_trigram_matches_only_as_candidates():
    party_id = uuid4()
    session = _PartySessionStub(
        scalar_batches=((), (), ()),
        rows=(SimpleNamespace(party_id=party_id, canonical_name="南京测试公司", score=0.81),),
    )
    repository = PartySqlRepository(session)  # type: ignore[arg-type]

    result = await repository.resolve(
        "南京测试公可",
        scope=PartyQueryScope("tenant-test", (str(uuid4()),)),
    )

    assert result.status == "needs_clarification"
    assert result.party_id == ""
    assert result.candidates[0].party_id == str(party_id)
    assert result.candidates[0].confirmed is False


@pytest.mark.asyncio
async def test_party_resolver_fails_closed_without_valid_case_scope():
    session = _PartySessionStub()
    repository = PartySqlRepository(session)  # type: ignore[arg-type]

    result = await repository.resolve(
        "南京测试公司",
        scope=PartyQueryScope("tenant-test", ("not-a-uuid",)),
    )

    assert result.status == "not_found"
    assert session.calls == []


@pytest.mark.asyncio
async def test_recent_progress_target_resolves_only_when_unique_and_never_picks_latest_of_many():
    case_id = uuid4()
    first = _progress(case_id)
    second = _progress(case_id)
    unique_repository = CaseProgressSqlRepository(_ProgressSessionStub((first,)))  # type: ignore[arg-type]
    ambiguous_repository = CaseProgressSqlRepository(_ProgressSessionStub((first, second)))  # type: ignore[arg-type]

    unique = await unique_repository.resolve_write_target(_progress_context((case_id,)))
    ambiguous = await ambiguous_repository.resolve_write_target(_progress_context((case_id,)))

    assert unique.status == "resolved"
    assert unique.progress_id == str(first.progress_id)
    assert unique.version == 1
    assert ambiguous.status == "needs_clarification"
    assert set(ambiguous.candidate_progress_ids) == {str(first.progress_id), str(second.progress_id)}
    assert ambiguous.progress_id == ""


@pytest.mark.asyncio
async def test_explicit_progress_id_still_enforces_tenant_actor_and_allowed_case_in_query():
    case_id = uuid4()
    progress = _progress(case_id, version=3)
    session = _ProgressSessionStub((progress,))
    repository = CaseProgressSqlRepository(session)  # type: ignore[arg-type]

    result = await repository.resolve_write_target(
        _progress_context((case_id,)),
        progress_id=str(progress.progress_id),
    )

    assert result.status == "resolved"
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "agent2_case_progress.tenant_id" in sql
    assert "agent2_case_progress.reporter_id" in sql
    assert "agent2_case_progress.case_id IN" in sql
    assert "agent2_case_progress.deleted_at IS NULL" in sql
