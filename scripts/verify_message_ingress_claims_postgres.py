#!/usr/bin/env python3
"""Verify the message-ingress claim migration in an isolated PostgreSQL schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Sequence
from uuid import uuid4

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_agent2_semantic_admission_postgres import (
    GateFailure,
    PgEnvironment,
    PostgresGate,
    quote_ident,
    sha256_bytes,
    sql_literal,
    utc_now,
)


SCHEMA_RE = re.compile(r"^agent2_ingress_gate_[a-z0-9_]{8,64}$")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not SCHEMA_RE.fullmatch(args.schema):
        raise SystemExit("schema name is outside the isolated gate allowlist")
    if not args.migration.is_file() or not args.env_file.is_file():
        raise SystemExit("migration or environment file not found")
    database_url = dotenv_values(args.env_file).get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not configured")

    pg = PgEnvironment.from_database_url(database_url)
    gate = PostgresGate(pg=pg, schema=args.schema, migration=args.migration)
    schema_ident = quote_ident(args.schema)
    started_at = utc_now()
    checks: dict[str, object] = {}
    status = "FAIL"
    failure_type = ""
    cleaned = False
    try:
        gate.run(
            scoped=False,
            sql=f"CREATE SCHEMA {schema_ident}",
        )
        gate.run(
            sql="""
                CREATE TABLE webhook_events (
                    id uuid PRIMARY KEY,
                    idempotency_key varchar(256) NOT NULL UNIQUE,
                    platform varchar(32) NOT NULL DEFAULT 'dingtalk',
                    external_message_id varchar(256),
                    received_at timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now()
                )
            """,
        )
        first_id = str(uuid4())
        second_id = str(uuid4())
        gate.run(
            sql=f"""
                INSERT INTO webhook_events (
                    id, idempotency_key, platform, external_message_id
                ) VALUES
                    ({sql_literal(first_id)}::uuid, 'dingtalk-stream:legacy-1', 'dingtalk', 'legacy-1'),
                    ({sql_literal(second_id)}::uuid, 'dingtalk:current-2', 'dingtalk', 'current-2')
            """,
        )

        gate.run(file=args.migration)
        first_catalog = gate.catalog_snapshot(args.schema)
        if gate.scalar("SELECT count(*) FROM message_ingress_claims") != "2":
            raise GateFailure("historical webhook events were not fully backfilled")
        if gate.scalar(
            """
                SELECT count(*)
                FROM message_ingress_claims claim
                LEFT JOIN webhook_events event
                  ON event.id = claim.webhook_event_id
                 AND event.idempotency_key = claim.idempotency_key
                WHERE event.id IS NULL
            """
        ) != "0":
            raise GateFailure("ingress claim backfill contains an orphan")
        gate.run(
            sql=f"""
                INSERT INTO message_ingress_claims (
                    idempotency_key, platform, external_message_id, webhook_event_id
                ) VALUES ('dingtalk:replay-legacy-1', 'dingtalk', 'legacy-1', '{uuid4()}'::uuid)
            """,
            expect_success=False,
            expected_sqlstate="23505",
        )

        gate.run(file=args.migration)
        second_catalog = gate.catalog_snapshot(args.schema)
        if first_catalog["sha256"] != second_catalog["sha256"]:
            raise GateFailure("idempotent migration replay changed the catalog")
        if gate.scalar("SELECT count(*) FROM message_ingress_claims") != "2":
            raise GateFailure("idempotent migration replay changed the backfill")
        checks = {
            "first_apply": True,
            "historical_backfill_rows": 2,
            "orphaned_claims": 0,
            "provider_identity_conflict_sqlstate": "23505",
            "second_apply": True,
            "catalog_stable": True,
            "catalog_sha256": first_catalog["sha256"],
        }
        status = "PASS"
    except Exception as exc:
        failure_type = type(exc).__name__
    finally:
        # Keep cleanup unconditional and report only the failure type. DROP
        # SCHEMA IF EXISTS is deterministic and schema-scoped.
        try:
            gate.run(
                scoped=False,
                sql=f"DROP SCHEMA IF EXISTS {schema_ident} CASCADE",
            )
            cleaned = True
        except Exception:
            cleaned = False

    artifact = {
        "artifact_version": "agent2.message_ingress_postgres_gate.v1",
        "status": status if cleaned else "FAIL",
        "migration_sha256": sha256_bytes(args.migration.read_bytes()),
        "schema_ref": args.schema,
        "started_at": started_at,
        "finished_at": utc_now(),
        "checks": checks,
        "failure_type": failure_type,
        "temporary_schema_cleaned": cleaned,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": artifact["status"], "cleaned": cleaned}))
    return 0 if artifact["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
