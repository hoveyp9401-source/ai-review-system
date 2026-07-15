from __future__ import annotations

from pathlib import Path

from scripts.verify_message_ingress_claims_postgres import SCHEMA_RE


def test_ingress_postgres_gate_only_accepts_isolated_schema_names() -> None:
    assert SCHEMA_RE.fullmatch("agent2_ingress_gate_20260715_a1b2c3d4")
    assert not SCHEMA_RE.fullmatch("public")
    assert not SCHEMA_RE.fullmatch("agent2_ingress_gate_bad-name")


def test_ingress_postgres_gate_has_unconditional_scoped_cleanup() -> None:
    source = Path("scripts/verify_message_ingress_claims_postgres.py").read_text(
        encoding="utf-8"
    )

    assert "finally:" in source
    assert "DROP SCHEMA IF EXISTS" in source
    assert "CASCADE" in source
    assert "temporary_schema_cleaned" in source
