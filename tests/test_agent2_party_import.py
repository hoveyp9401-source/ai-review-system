from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.business.models import BusinessAuditEvent
from app.agent2.business.party_import import (
    CaseImportRow,
    PartyImportDataset,
    PartyImportRow,
    PartyCaseClueImportRow,
    PartyRelationImportRow,
    build_party_import_plan,
    dataset_from_dict,
    persist_party_import_plan,
)


def _case(external_id: str) -> CaseImportRow:
    return CaseImportRow(
        external_case_id=external_id,
        case_number=f"（2026）苏01民初{external_id[-1]}号",
        case_name=f"测试案件 {external_id}",
        case_type="litigation",
        status="open",
        owner_user_id="user-1",
        source_type="case_registry",
        source_id=f"case-source-{external_id}",
    )


def _party(
    source_party_id: str,
    name: str,
    case_id: str,
    role: str,
    *,
    source_id: str,
    identifiers=(),
    aliases=(),
) -> PartyImportRow:
    return PartyImportRow(
        party_source_id=source_party_id,
        canonical_name=name,
        party_type="company",
        aliases=tuple(aliases),
        identifiers=tuple(identifiers),
        external_case_id=case_id,
        role_type=role,
        source_type="case_registry",
        source_id=source_id,
    )


def _dataset(parties) -> PartyImportDataset:
    return PartyImportDataset(
        dataset_id="sandbox-party-v1",
        tenant_id="sandbox-alpha",
        company_id="alpha-company",
        department_id="alpha-legal",
        team_id="alpha-dispute",
        cases=(_case("case-1"), _case("case-2")),
        parties=tuple(parties),
    )


def test_exact_identifier_merges_source_rows_and_preserves_case_specific_roles_and_conflict():
    plan = build_party_import_plan(
        _dataset(
            (
                _party(
                    "party-a",
                    "南京华东建设有限公司",
                    "case-1",
                    "plaintiff",
                    source_id="row-1",
                    identifiers=(("uscc", "91320100 TEST 001"),),
                    aliases=("华东建设",),
                ),
                _party(
                    "party-b",
                    "南京华东建设股份有限公司",
                    "case-2",
                    "defendant",
                    source_id="row-2",
                    identifiers=(("uscc", "91320100TEST001"),),
                    aliases=("南京华建",),
                ),
            )
        )
    )

    assert len(plan.parties) == 1
    assert plan.parties[0].unified_social_credit_code == "91320100TEST001"
    assert len(plan.identifiers) == 1
    assert len(plan.case_roles) == 2
    assert {item.role_type for item in plan.case_roles} == {"plaintiff", "defendant"}
    assert len(plan.source_references) == 2
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].field_name == "canonical_name"
    assert {item.alias for item in plan.aliases} >= {
        "华东建设",
        "南京华建",
        "南京华东建设股份有限公司",
    }
    assert plan.merge_candidates == ()


def test_same_normalized_name_without_shared_identifier_stays_separate_and_enters_merge_queue():
    plan = build_party_import_plan(
        _dataset(
            (
                _party("party-a", "南京同名科技有限公司", "case-1", "plaintiff", source_id="row-1"),
                _party("party-b", "南京同名科技有限公司", "case-2", "defendant", source_id="row-2"),
            )
        )
    )

    assert len(plan.parties) == 2
    assert len(plan.merge_candidates) == 1
    assert plan.merge_candidates[0].match_basis[0] == "exact_normalized_name_without_shared_identifier"
    assert plan.merge_candidates[0].left_party_id != plan.merge_candidates[0].right_party_id


def test_import_plan_is_deterministic_under_input_reordering():
    rows = (
        _party("party-a", "甲公司", "case-1", "plaintiff", source_id="row-1"),
        _party("party-b", "乙公司", "case-2", "defendant", source_id="row-2"),
    )

    first = build_party_import_plan(_dataset(rows))
    second = build_party_import_plan(_dataset(tuple(reversed(rows))))

    assert first.counts() == second.counts()
    assert [item.party_id for item in first.parties] == [item.party_id for item in second.parties]
    assert [item.role_id for item in first.case_roles] == [item.role_id for item in second.case_roles]


def test_import_plan_includes_typed_relations_and_source_traceable_case_clues():
    dataset = _dataset(
        (
            _party("party-a", "Target Company", "case-1", "defendant", source_id="row-1"),
            PartyImportRow(
                party_source_id="person-a",
                canonical_name="Visible Person",
                party_type="person",
                aliases=(),
                identifiers=(),
                external_case_id="case-1",
                role_type="legal_representative",
                source_type="case_registry",
                source_id="row-2",
            ),
        )
    )
    dataset = PartyImportDataset(
        **{
            **dataset.__dict__,
            "relations": (
                PartyRelationImportRow(
                    from_party_source_id="party-a",
                    to_party_source_id="person-a",
                    external_case_id="case-1",
                    relation_type="legal_representative",
                    source_type="case_registry",
                    source_id="row-2",
                ),
            ),
            "clues": (
                PartyCaseClueImportRow(
                    party_source_id="party-a",
                    external_case_id="case-1",
                    clue_type="payment",
                    summary="Recovered payment",
                    amount=Decimal("120000.00"),
                    currency="CNY",
                    source_type="payment_ledger",
                    source_id="payment-1",
                    source_field="received_amount",
                    source_reference={"row": "42"},
                ),
            ),
        }
    )

    plan = build_party_import_plan(dataset)

    assert len(plan.relations) == 1
    assert plan.relations[0].from_party_id != plan.relations[0].to_party_id
    assert len(plan.clues) == 1
    assert plan.clues[0].row.clue_type == "payment"
    assert plan.clues[0].row.amount == Decimal("120000.00")
    assert plan.counts()["relations"] == 1
    assert plan.counts()["clues"] == 1


def test_dataset_parser_rejects_party_role_for_unknown_case():
    payload = {
        "dataset_id": "bad",
        "tenant_id": "sandbox-alpha",
        "company_id": "alpha-company",
        "department_id": "alpha-legal",
        "team_id": "alpha-dispute",
        "cases": [
            {
                "external_case_id": "case-1",
                "case_number": "1",
                "case_name": "案件一",
                "case_type": "litigation",
                "status": "open",
                "owner_user_id": "user-1",
                "source_type": "fixture",
                "source_id": "case-1",
            }
        ],
        "parties": [
            {
                "party_source_id": "party-1",
                "canonical_name": "甲公司",
                "party_type": "company",
                "aliases": [],
                "identifiers": [],
                "external_case_id": "missing-case",
                "role_type": "plaintiff",
                "source_type": "fixture",
                "source_id": "party-1",
            }
        ],
    }

    try:
        build_party_import_plan(dataset_from_dict(payload))
    except ValueError as exc:
        assert "unknown case" in str(exc)
    else:
        raise AssertionError("unknown case role must be rejected")


def test_dataset_parser_preserves_only_explicit_confirmed_case_aliases():
    payload = {
        "dataset_id": "aliases",
        "tenant_id": "sandbox-alpha",
        "company_id": "alpha-company",
        "department_id": "alpha-legal",
        "team_id": "alpha-dispute",
        "cases": [
            {
                "external_case_id": "case-1",
                "case_number": "1",
                "case_name": "案件一",
                "case_type": "litigation",
                "status": "open",
                "owner_user_id": "user-1",
                "source_type": "fixture",
                "source_id": "case-1",
                "confirmed_aliases": ["江心洲设计案", "启洲案"],
            }
        ],
        "parties": [],
    }

    dataset = dataset_from_dict(payload)

    assert dataset.cases[0].confirmed_aliases == ("江心洲设计案", "启洲案")


class _PersistenceSession:
    def __init__(self, *, insert_rowcount=1):
        self.statements = []
        self.added = []
        self.insert_rowcount = insert_rowcount

    async def scalar(self, statement):
        self.statements.append(statement)
        params = statement.compile(dialect=postgresql.dialect()).params
        return params["receipt_id"]

    async def execute(self, statement):
        self.statements.append(statement)
        rowcount = self.insert_rowcount if statement.is_insert else 1
        return type("Result", (), {"rowcount": rowcount})()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_persistence_adapter_writes_tenant_scoped_rows_with_import_receipt_and_audit():
    plan = build_party_import_plan(
        _dataset(
            (
                _party(
                    "party-a",
                    "南京华东建设有限公司",
                    "case-1",
                    "defendant",
                    source_id="row-1",
                    identifiers=(("uscc", "91320100TEST001"),),
                ),
            )
        )
    )
    session = _PersistenceSession()

    result = await persist_party_import_plan(
        session,  # type: ignore[arg-type]
        plan,
        actor_user_id="alpha-admin",
        source_message_id="manual-import-1",
        occurred_at=datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
    )

    assert result.status == "executed"
    assert result.actual_write is True
    assert result.counts["planned"] == plan.counts()
    assert result.counts["inserted"] == plan.counts()
    assert all(value == 0 for value in result.counts["skipped_existing_or_conflicting"].values())
    assert len(session.statements) >= 8
    tenant_scoped_inserts = 0
    for statement in session.statements:
        compiled = statement.compile(dialect=postgresql.dialect())
        params = compiled.params
        if "tenant_id" in params:
            tenant_scoped_inserts += 1
            assert params["tenant_id"] == "sandbox-alpha"
    assert tenant_scoped_inserts >= 7
    audits = [item for item in session.added if isinstance(item, BusinessAuditEvent)]
    assert len(audits) == 1
    assert audits[0].tenant_id == "sandbox-alpha"
    assert audits[0].actor_user_id == "alpha-admin"
    assert audits[0].after_json == result.counts


@pytest.mark.asyncio
async def test_persistence_adapter_reports_no_change_when_every_domain_insert_conflicts():
    plan = build_party_import_plan(
        _dataset(
            (
                _party(
                    "party-existing",
                    "南京既有公司",
                    "case-1",
                    "defendant",
                    source_id="row-existing",
                ),
            )
        )
    )
    session = _PersistenceSession(insert_rowcount=0)

    result = await persist_party_import_plan(
        session,  # type: ignore[arg-type]
        plan,
        actor_user_id="alpha-admin",
        source_message_id="manual-import-conflict",
        occurred_at=datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
    )

    assert result.status == "executed"
    assert result.actual_write is False
    assert all(value == 0 for value in result.counts["inserted"].values())
    assert result.counts["skipped_existing_or_conflicting"] == plan.counts()
    assert not any(isinstance(item, BusinessAuditEvent) for item in session.added)
