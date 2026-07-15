from __future__ import annotations

import ast
from pathlib import Path

from app.models import MessageIngressClaim


ROOT = Path(__file__).resolve().parents[1]


def test_message_ingress_claim_has_provider_identity_unique_index() -> None:
    index = next(
        item
        for item in MessageIngressClaim.__table__.indexes
        if item.name == "message_ingress_claims_platform_external_message_id_key"
    )

    assert index.unique is True
    assert tuple(column.name for column in index.columns) == (
        "platform",
        "external_message_id",
    )
    predicate = str(index.dialect_options["postgresql"]["where"])
    assert "external_message_id IS NOT NULL" in predicate
    assert "external_message_id <> ''" in predicate


def test_provider_identity_migration_is_transactional_and_refuses_duplicates() -> None:
    sql = (ROOT / "scripts" / "create_webhook_external_message_identity.sql").read_text(
        encoding="utf-8"
    )

    assert sql.lstrip().startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")
    assert "RAISE EXCEPTION" in sql
    assert "GROUP BY platform, external_message_id" in sql
    assert "CREATE TABLE IF NOT EXISTS message_ingress_claims" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS message_ingress_claims_platform_external_message_id_key" in sql
    assert "WHERE external_message_id IS NOT NULL AND external_message_id <> ''" in sql
    assert "INSERT INTO message_ingress_claims" in sql
    assert "FROM webhook_events" in sql
    assert "ALTER TABLE webhook_events" not in sql


def test_repository_conflict_handling_covers_every_unique_constraint() -> None:
    tree = ast.parse((ROOT / "app" / "repositories.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "create_webhook_event_once"
    )
    source = ast.unparse(function)

    assert ".on_conflict_do_nothing()" in source
    assert "MessageIngressClaim.platform == platform" in source
    assert "MessageIngressClaim.external_message_id == external_message_id" in source
    assert "incomplete or ambiguous message ingress claim" in source
