#!/usr/bin/env python3
"""Collect redacted, read-only evidence for the Agent2 Shadow deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Sequence

from dotenv import dotenv_values

from verify_agent2_semantic_admission_postgres import PgEnvironment, utc_now


SEMANTIC_TABLES = (
    "agent2_semantic_admission_traces",
    "agent2_semantic_admission_decisions",
    "agent2_semantic_admission_tickets",
    "agent2_semantic_review_items",
    "agent2_deferred_semantic_events",
    "agent2_information_pendings",
)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def _query(pg: PgEnvironment, sql: str) -> list[list[str]]:
    result = subprocess.run(
        [
            "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
            "-A", "-t", "-F", "\t", "-c", sql,
        ],
        env=pg.values,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("redacted PostgreSQL evidence query failed")
    return [line.split("\t") for line in result.stdout.splitlines() if line.strip()]


def _csv(values: dict[str, str], key: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in values.get(key, "").split(",") if part.strip())


def _false(values: dict[str, str], key: str) -> bool:
    return values.get(key, "").strip().lower() in {"0", "false", "no", "off"}


def _true(values: dict[str, str], key: str) -> bool:
    return values.get(key, "").strip().lower() in {"1", "true", "yes", "on"}


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    values = {
        key: str(value or "")
        for key, value in dotenv_values(args.env_file).items()
    }
    database_url = values.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not configured")
    pg = PgEnvironment.from_database_url(database_url)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    runtime_rows = []
    for item in manifest["runtime_files"]:
        path = str(item["path"])
        digest = hashlib.sha256((args.repo / path).read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise SystemExit("deployed runtime hash mismatch")
        runtime_rows.append(f"{path}\t{digest}")
    runtime_hash = hashlib.sha256(("\n".join(runtime_rows) + "\n").encode()).hexdigest()
    if runtime_hash != manifest["runtime_bundle_sha256"]:
        raise SystemExit("deployed runtime bundle hash mismatch")

    count_sql = " UNION ALL ".join(
        f"SELECT '{name}', count(*)::text FROM public.{name}"
        for name in SEMANTIC_TABLES
    )
    table_rows = _query(pg, count_sql)
    table_counts = {row[0]: int(row[1]) for row in table_rows}
    if set(table_counts) != set(SEMANTIC_TABLES):
        raise SystemExit("semantic admission table set mismatch")

    outcome_columns = _query(
        pg,
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema='public'
          AND table_name='agent2_operation_outcomes'
          AND column_name IN ('object_label','object_version')
        ORDER BY column_name
        """,
    )
    violations = {
        "review_not_audit_only": int(_query(pg, "SELECT count(*) FROM public.agent2_semantic_review_items WHERE audit_only IS NOT TRUE OR business_write_allowed IS NOT FALSE")[0][0]),
        "deferred_not_audit_only": int(_query(pg, "SELECT count(*) FROM public.agent2_deferred_semantic_events WHERE audit_only IS NOT TRUE OR business_write_allowed IS NOT FALSE OR requires_fresh_admission IS NOT TRUE")[0][0]),
        "invalid_ticket_consumption": int(_query(pg, "SELECT count(*) FROM public.agent2_semantic_admission_tickets WHERE (ticket_status='consumed' AND (consumed_at IS NULL OR consumed_receipt_ref IS NULL)) OR (ticket_status<>'consumed' AND (consumed_at IS NOT NULL OR consumed_receipt_ref IS NOT NULL))")[0][0]),
        "forbidden_trace_raw_columns": int(_query(pg, "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='agent2_semantic_admission_traces' AND column_name IN ('proposal_json','raw_text','segment_text','message_text')")[0][0]),
    }

    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=10) as response:
        health = json.loads(response.read().decode("utf-8"))
    process_text = subprocess.run(
        ["ps", "-eo", "args="], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    repo_text = str(args.repo)
    processes = {
        "api": sum(f"{repo_text}/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000" in line for line in process_text),
        "stream": sum(f"{repo_text}/venv/bin/python -m app.stream_runner" in line for line in process_text),
        "scheduler": sum(f"{repo_text}/venv/bin/python -m app.scheduler.runner" in line for line in process_text),
    }
    config = {
        "tenant_count": len(_csv(values, "AGENT2_SEMANTIC_ADMISSION_TENANT_ALLOWLIST")),
        "user_count": len(_csv(values, "AGENT2_SEMANTIC_ADMISSION_USER_ALLOWLIST")),
        "semantic_enabled": _true(values, "AGENT2_SEMANTIC_ADMISSION_ENABLED"),
        "semantic_enforce": _true(values, "AGENT2_SEMANTIC_ADMISSION_ENFORCE"),
        "review_capture": _true(values, "AGENT2_SEMANTIC_ADMISSION_REVIEW_CAPTURE"),
        "deferred_capture": _true(values, "AGENT2_SEMANTIC_ADMISSION_DEFERRED_CAPTURE"),
        "shadow_replay": _true(values, "AGENT2_SEMANTIC_ADMISSION_SHADOW_REPLAY"),
        "followup_send_disabled": _false(values, "CASE_FOLLOWUP_SEND_ENABLED") and _false(values, "AGENT2_CASE_FOLLOWUP_SEND_ENABLED"),
        "report_projection_disabled": _false(values, "CASE_FOLLOWUP_REPORT_PROJECTION_ENABLED"),
    }
    passed = (
        health.get("status") == "ok"
        and processes == {"api": 1, "stream": 1, "scheduler": 1}
        and config == {
            "tenant_count": 1,
            "user_count": 2,
            "semantic_enabled": True,
            "semantic_enforce": False,
            "review_capture": True,
            "deferred_capture": False,
            "shadow_replay": False,
            "followup_send_disabled": True,
            "report_projection_disabled": True,
        }
        and len(outcome_columns) == 2
        and all(value == 0 for value in violations.values())
    )
    payload = {
        "artifact_version": "agent2.semantic_admission.shadow_deployment_evidence.v1",
        "status": "PASS" if passed else "FAIL",
        "collected_at": utc_now(),
        "runtime_bundle_sha256": runtime_hash,
        "runtime_file_count": len(runtime_rows),
        "health": health.get("status"),
        "process_counts": processes,
        "config": config,
        "semantic_table_counts": table_counts,
        "outcome_object_columns": outcome_columns,
        "violation_counts": violations,
        "evidence_limits": {
            "real_user_message_exercised": False,
            "enforce_exercised": False,
            "business_e2e_claimed": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "runtime_bundle_sha256": runtime_hash, "violation_total": sum(violations.values())}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
