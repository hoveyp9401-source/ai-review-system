from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from app.db import AsyncSessionLocal, engine
from app.models import DailyReport, ReportInteractionEvent
from scripts.apply_aug17_failed_daily_repairs import (
    EXPECTED_CHANGED,
    EXPECTED_CREATED,
    EXPECTED_USERS,
    REPORT_DATE_VALUE,
    _content_snapshot,
    _read_verified_plan,
    _sha256_json,
    _validate_plan,
)
from scripts.plan_aug17_failed_daily_repairs import _read_verified_backup


def _read_verified_json(path: Path, expected_sha256: str) -> dict[str, Any]:
    encoded = path.read_bytes()
    actual = hashlib.sha256(encoded).hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(
            f"artifact hash mismatch: expected {expected_sha256}, got {actual}"
        )
    value = json.loads(encoded.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("artifact is not a JSON object")
    return value


def _write_verification(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
    return {
        "path": str(path),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


async def _verify(
    *,
    backup: dict[str, Any],
    plan: dict[str, Any],
    manifest: dict[str, Any],
    backup_sha256: str,
    plan_sha256: str,
) -> dict[str, Any]:
    rows = _validate_plan(backup, plan)
    if manifest.get("schema_version") != "agent2.aug17.daily-repair-apply.v1":
        raise RuntimeError("unsupported apply manifest")
    if manifest.get("mode") != "apply":
        raise RuntimeError("manifest is not an applied repair")
    if (
        manifest.get("backup_sha256") != backup_sha256
        or manifest.get("repair_plan_sha256") != plan_sha256
    ):
        raise RuntimeError("manifest artifact binding mismatch")
    manifest_rows = {
        row["anonymous_user"]: row for row in manifest.get("rows", [])
    }
    if len(manifest_rows) != EXPECTED_USERS:
        raise RuntimeError("manifest does not contain 14 unique users")
    user_ids = [row["user_id"] for row in rows]
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            reports = list(
                (
                    await session.scalars(
                        select(DailyReport).where(
                            DailyReport.user_id.in_(user_ids),
                            DailyReport.report_date == REPORT_DATE_VALUE,
                        )
                    )
                ).all()
            )
            reports_by_user = {str(report.user_id): report for report in reports}
            interactions = list(
                (
                    await session.scalars(
                        select(ReportInteractionEvent).where(
                            ReportInteractionEvent.user_id.in_(user_ids),
                            ReportInteractionEvent.report_date == REPORT_DATE_VALUE,
                            ReportInteractionEvent.backend_action.in_(
                                {
                                    "agent2_aug17_failure_repair",
                                    "agent2_aug17_failure_review_no_change",
                                }
                            ),
                        )
                    )
                ).all()
            )
    matching_interactions = [
        event
        for event in interactions
        if (event.llm_decision_json or {}).get("repair_plan_sha256")
        == plan_sha256
        and (event.llm_decision_json or {}).get("backup_sha256")
        == backup_sha256
    ]
    if len(matching_interactions) != EXPECTED_USERS:
        raise RuntimeError(
            f"expected 14 repair audit events, got {len(matching_interactions)}"
        )
    events_by_user = {str(event.user_id): event for event in matching_interactions}
    if len(events_by_user) != EXPECTED_USERS:
        raise RuntimeError("repair audit events are not one-per-user")
    verified_rows: list[dict[str, Any]] = []
    for row in rows:
        alias = row["anonymous_user"]
        report = reports_by_user.get(row["user_id"])
        if report is None:
            raise RuntimeError(f"{alias} has no repaired report")
        expected_fields = row["plan"]["materialized_fields"]
        if any(
            list(getattr(report, field) or ()) != expected_fields[field]
            for field in ("today_work", "problems", "tomorrow_plan")
        ):
            raise RuntimeError(f"{alias} report content differs from approved plan")
        if report.status != "completed":
            raise RuntimeError(f"{alias} repaired report is not completed")
        backup_report = row["backup_report"]
        if backup_report is not None:
            preserved_metadata = (
                report.status == backup_report["status"]
                and report.confirmation_type == backup_report["confirmation_type"]
                and report.confirmed_by_user
                is backup_report["confirmed_by_user"]
                and (
                    report.submitted_at.isoformat() if report.submitted_at else None
                )
                == backup_report["submitted_at"]
            )
            if not preserved_metadata:
                raise RuntimeError(f"{alias} submission metadata changed")
        elif (
            report.confirmation_type != "auto_submitted_timeout"
            or report.confirmed_by_user
            or report.submitted_at is None
        ):
            raise RuntimeError(f"{alias} new report has incorrect submission metadata")
        content = _content_snapshot(report)
        manifest_row = manifest_rows[alias]
        if _sha256_json(content) != manifest_row["after_sha256"]:
            raise RuntimeError(f"{alias} does not match the apply manifest")
        event = events_by_user[row["user_id"]]
        if (
            event.correction_from != _sha256_json(event.before_snapshot_json)
            or event.correction_to != _sha256_json(event.after_snapshot_json)
            or event.after_snapshot_json != content
        ):
            raise RuntimeError(f"{alias} additive audit snapshot is incomplete")
        changed = row["plan"]["decision"] == "change"
        if changed != (event.before_snapshot_json != event.after_snapshot_json):
            raise RuntimeError(f"{alias} audit change flag is inconsistent")
        verified_rows.append(
            {
                "anonymous_user": alias,
                "decision": row["plan"]["decision"],
                "created": backup_report is None,
                "report_sha256": _sha256_json(content),
                "status": report.status,
                "confirmation_type": report.confirmation_type,
                "confirmed_by_user": report.confirmed_by_user,
                "item_counts": {
                    field: len(getattr(report, field) or ())
                    for field in ("today_work", "problems", "tomorrow_plan")
                },
                "audit_event_id": str(event.id),
            }
        )
    changed_count = sum(row["decision"] == "change" for row in verified_rows)
    created_count = sum(row["created"] for row in verified_rows)
    if changed_count != EXPECTED_CHANGED or created_count != EXPECTED_CREATED:
        raise RuntimeError("verified repair scope differs from approved scope")
    return {
        "schema_version": "agent2.aug17.daily-repair-verification.v1",
        "status": "pass",
        "users_verified": len(verified_rows),
        "reports_changed": changed_count,
        "reports_created": created_count,
        "reports_updated": changed_count - created_count,
        "reports_unchanged": len(verified_rows) - changed_count,
        "audit_events_verified": len(matching_interactions),
        "dingtalk_send_calls": 0,
        "rows": verified_rows,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--backup-sha256", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backup = _read_verified_backup(args.backup, args.backup_sha256)
    plan = _read_verified_plan(args.plan, args.plan_sha256)
    manifest = _read_verified_json(args.manifest, args.manifest_sha256)
    result = await _verify(
        backup=backup,
        plan=plan,
        manifest=manifest,
        backup_sha256=args.backup_sha256,
        plan_sha256=args.plan_sha256,
    )
    written = _write_verification(args.output, result)
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key != "rows"
            }
            | {"output": written},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
