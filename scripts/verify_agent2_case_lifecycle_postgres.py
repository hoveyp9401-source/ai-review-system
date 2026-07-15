#!/usr/bin/env python3
"""Verify the current Case Lifecycle migration in an isolated PostgreSQL schema.

The gate exercises the upgrade path from the pre-object-label Outcome table,
idempotent re-application, rollback, public-schema non-mutation, and exact
cleanup.  It never prints the configured database URL or protected row data.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Sequence

from dotenv import dotenv_values

from verify_agent2_semantic_admission_postgres import (
    GateFailure,
    PgEnvironment,
    PostgresGate,
    SCHEMA_RE,
    quote_ident,
    sha256_bytes,
    sql_literal,
    utc_now,
)


EXPECTED_TABLES = {
    "agent2_case_followup_policies",
    "agent2_case_lifecycle_states",
    "agent2_case_followup_tasks",
    "agent2_case_followup_pendings",
    "agent2_task_ledger",
    "agent2_report_projection_requests",
    "agent2_case_report_projections",
    "agent2_operation_outcomes",
}


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument(
        "--execution-transport",
        choices=("local", "ssh", "unspecified"),
        default="unspecified",
    )
    return parser.parse_args(argv)


def _legacy_outcome_table_sql() -> str:
    return """
        CREATE TABLE agent2_operation_outcomes (
            outcome_id uuid PRIMARY KEY,
            tenant_id varchar(128) NOT NULL,
            user_id varchar(128) NOT NULL,
            conversation_id varchar(256) NOT NULL,
            source_turn_id varchar(256) NOT NULL,
            domain varchar(64) NOT NULL,
            operation varchar(128) NOT NULL,
            object_type varchar(128) NOT NULL,
            object_id varchar(256) NOT NULL DEFAULT '',
            business_status varchar(64) NOT NULL,
            message_status varchar(64) NOT NULL,
            actual_write boolean NOT NULL DEFAULT false,
            would_write boolean NOT NULL DEFAULT false,
            changed_fields_json jsonb NOT NULL DEFAULT '[]'::jsonb,
            user_visible_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            blocking_reason text NOT NULL DEFAULT '',
            receipt_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
            audit_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
            state_transition_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(512) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """


def _legacy_row_sql(outcome_id: str) -> str:
    return f"""
        INSERT INTO agent2_operation_outcomes (
            outcome_id, tenant_id, user_id, conversation_id, source_turn_id,
            domain, operation, object_type, object_id, business_status,
            message_status, actual_write, would_write, changed_fields_json,
            user_visible_snapshot_json, blocking_reason, receipt_refs_json,
            audit_refs_json, state_transition_json, idempotency_key
        ) VALUES (
            {sql_literal(outcome_id)}::uuid,
            'synthetic-gate-tenant', 'synthetic-gate-user',
            'synthetic-gate-conversation', 'synthetic-gate-turn',
            'chat', 'query', 'chat', 'synthetic-object', 'unchanged',
            'not_applicable', false, false, '[]'::jsonb, '{{}}'::jsonb,
            '', '[]'::jsonb, '[]'::jsonb, '{{}}'::jsonb,
            'synthetic-gate-idempotency'
        )
    """


def _render_markdown(result: dict[str, object]) -> str:
    checks = result.get("checks") if isinstance(result.get("checks"), dict) else {}
    return "\n".join(
        (
            "# Agent2 Case Lifecycle PostgreSQL migration gate",
            "",
            f"- Status: `{result.get('status', 'FAIL')}`",
            f"- Migration SHA-256: `{result.get('migration_sha256', '')}`",
            f"- Execution transport: `{result.get('execution_transport', '')}`",
            f"- Temporary schema cleaned: `{bool((result.get('cleanup') or {}).get('confirmed'))}`",
            "",
            "| Check | Passed |",
            "|---|---:|",
            f"| legacy Outcome upgrade | {bool(checks.get('legacy_outcome_upgrade'))} |",
            f"| idempotent re-apply | {bool(checks.get('idempotent_reapply'))} |",
            f"| transaction rollback | {bool(checks.get('transaction_rollback'))} |",
            f"| public schema unchanged | {bool(checks.get('public_schema_unchanged'))} |",
            "",
            "This is a synthetic isolated-schema migration gate, not real-user E2E evidence.",
            "",
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not SCHEMA_RE.fullmatch(args.schema):
        raise SystemExit("temporary schema name does not match the safety allow-list")
    if not args.migration.is_file() or not args.env_file.is_file():
        raise SystemExit("migration or environment file not found")
    values = dotenv_values(args.env_file)
    database_url = values.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not configured")

    gate = PostgresGate(
        pg=PgEnvironment.from_database_url(database_url),
        schema=args.schema,
        migration=args.migration,
    )
    result: dict[str, object] = {
        "artifact_version": "agent2.case_lifecycle.postgres_gate.v1",
        "status": "FAIL",
        "started_at": utc_now(),
        "finished_at": None,
        "database_target": "remote_configured_postgresql_redacted",
        "execution_transport": args.execution_transport,
        "migration_sha256": sha256_bytes(args.migration.read_bytes()),
        "verifier_sha256": sha256_bytes(Path(__file__).read_bytes()),
        "temporary_schema": args.schema,
        "checks": {},
        "cleanup": {"passed": False, "confirmed": False},
    }
    checks = result["checks"]
    assert isinstance(checks, dict)
    created = False
    public_before: dict[str, object] | None = None
    row_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{args.schema}:legacy-outcome"))

    try:
        public_before = gate.catalog_snapshot("public")
        existing = int(
            gate.scalar(
                f"SELECT count(*)::text FROM pg_namespace WHERE nspname={sql_literal(args.schema)}",
                scoped=False,
            )
        )
        if existing:
            raise GateFailure("refusing to reuse or drop a pre-existing schema")
        gate.run(sql=f"CREATE SCHEMA {quote_ident(args.schema)}", scoped=False)
        created = True
        gate.run(sql=_legacy_outcome_table_sql())
        gate.run(sql=_legacy_row_sql(row_id))

        gate.run(file=args.migration)
        first = gate.catalog_snapshot(args.schema)
        table_names = {row[0] for row in first["relations"] if row[1] in {"r", "p"}}
        if table_names != EXPECTED_TABLES:
            raise GateFailure("case lifecycle migration table set mismatch")
        columns = gate.run(
            sql="""
                SELECT column_name, data_type, is_nullable, COALESCE(column_default, '')
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'agent2_operation_outcomes'
                  AND column_name IN ('object_label', 'object_version')
                ORDER BY column_name
            """
        ).stdout.strip().splitlines()
        if len(columns) != 2:
            raise GateFailure("Outcome object label/version columns missing")
        preserved = gate.scalar(
            """
                SELECT count(*)::text
                FROM agent2_operation_outcomes
                WHERE idempotency_key = 'synthetic-gate-idempotency'
                  AND object_label = ''
                  AND object_version IS NULL
            """
        )
        if preserved != "1":
            raise GateFailure("legacy Outcome row was not preserved safely")
        checks["legacy_outcome_upgrade"] = True

        gate.run(file=args.migration)
        second = gate.catalog_snapshot(args.schema)
        if first["sha256"] != second["sha256"]:
            raise GateFailure("second migration apply changed the canonical catalog")
        checks["idempotent_reapply"] = True

        gate.run(
            sql="""
                BEGIN;
                UPDATE agent2_operation_outcomes
                SET object_label = 'rollback-probe'
                WHERE idempotency_key = 'synthetic-gate-idempotency';
                ROLLBACK;
            """
        )
        if gate.scalar(
            """
                SELECT count(*)::text FROM agent2_operation_outcomes
                WHERE idempotency_key = 'synthetic-gate-idempotency'
                  AND object_label = ''
            """
        ) != "1":
            raise GateFailure("Outcome rollback probe changed persisted data")
        checks["transaction_rollback"] = True

        public_after = gate.catalog_snapshot("public")
        if public_before["sha256"] != public_after["sha256"]:
            raise GateFailure("isolated migration changed the public schema")
        checks["public_schema_unchanged"] = True
        result["status"] = "PASS"
    except Exception as exc:
        result["failure_type"] = type(exc).__name__
        result["failure_reason"] = gate.sanitize(str(exc), limit=800)
        raise
    finally:
        if created:
            gate.run(sql=f"DROP SCHEMA {quote_ident(args.schema)} CASCADE", scoped=False)
        remaining = int(
            gate.scalar(
                f"SELECT count(*)::text FROM pg_namespace WHERE nspname={sql_literal(args.schema)}",
                scoped=False,
            )
        )
        result["cleanup"] = {"passed": remaining == 0, "confirmed": remaining == 0}
        result["finished_at"] = utc_now()
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        args.markdown_output.write_text(_render_markdown(result), encoding="utf-8")

    print(json.dumps({"status": result["status"], "cleanup": result["cleanup"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
