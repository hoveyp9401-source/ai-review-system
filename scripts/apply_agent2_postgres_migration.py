#!/usr/bin/env python3
"""Apply one hash-pinned Agent2 SQL migration to the configured public schema."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from dotenv import dotenv_values

from verify_agent2_semantic_admission_postgres import (
    PgEnvironment,
    sha256_bytes,
    utc_now,
)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    return parser.parse_args(argv)


def _psql(pg: PgEnvironment, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", *args],
        env=pg.values,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if not args.migration.is_file() or not args.env_file.is_file():
        raise SystemExit("migration or environment file not found")
    actual_sha256 = sha256_bytes(args.migration.read_bytes())
    if actual_sha256 != args.expected_sha256.lower():
        raise SystemExit("migration hash mismatch")
    values = dotenv_values(args.env_file)
    database_url = values.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not configured")
    pg = PgEnvironment.from_database_url(database_url)
    schema_result = _psql(pg, "-A", "-t", "-c", "SELECT current_schema()")
    if schema_result.returncode != 0 or schema_result.stdout.strip() != "public":
        raise SystemExit("configured database current schema is not public")

    started_at = utc_now()
    applied = _psql(pg, "-f", str(args.migration))
    receipt = {
        "artifact_version": "agent2.postgres_migration_receipt.v1",
        "status": "PASS" if applied.returncode == 0 else "FAIL",
        "migration_path": args.migration.as_posix(),
        "migration_sha256": actual_sha256,
        "database_target": "configured_postgresql_redacted",
        "schema": "public",
        "started_at": started_at,
        "finished_at": utc_now(),
        "returncode": applied.returncode,
    }
    args.receipt_output.parent.mkdir(parents=True, exist_ok=True)
    args.receipt_output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if applied.returncode != 0:
        sanitized = applied.stderr
        for secret in pg.secrets:
            sanitized = sanitized.replace(secret, "<redacted>")
        print(sanitized[-1000:], file=sys.stderr)
        return 1
    print(json.dumps({"status": "PASS", "migration_sha256": actual_sha256}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
