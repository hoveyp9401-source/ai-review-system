from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
from app.agent2.typed_daily_executor import (
    TYPED_AUDIT_KEY,
    TYPED_REPORT_VERSION_KEY,
)
from app.db import AsyncSessionLocal, engine
from app.models import DailyReport, ReportInteractionEvent, User
from app.repositories import acquire_daily_report_advisory_lock
from app.scheduler.jobs import mark_report_auto_submitted
from app.services.report_service import _attach_draft_item_ids
from app.services.state_machine import assess_daily_report_completeness
from scripts.plan_aug17_failed_daily_repairs import (
    REPORT_DATE,
    REPORT_FIELDS,
    _existing_report,
    _read_verified_backup,
    _timeline,
    _validate_and_materialize,
)


EXPECTED_USERS = 14
EXPECTED_CHANGED = 8
EXPECTED_CREATED = 3
REPORT_DATE_VALUE = date.fromisoformat(REPORT_DATE)
REPAIR_AUDIT_KEY = "_agent2_aug17_failure_repair_v1"


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_verified_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    encoded = path.read_bytes()
    actual = _sha256_bytes(encoded)
    if actual != expected_sha256:
        raise RuntimeError(
            f"plan hash mismatch: expected {expected_sha256}, got {actual}"
        )
    value = json.loads(encoded.decode("utf-8"))
    if value.get("schema_version") != "agent2.aug17.daily-repair-plan.v1":
        raise RuntimeError("unsupported repair plan schema")
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value) if hasattr(value, "hex") else value


def _report_guard_snapshot(report: DailyReport) -> dict[str, Any]:
    return _json_safe(
        {
            "id": report.id,
            "user_id": report.user_id,
            "team_id": report.team_id,
            "report_date": report.report_date,
            "today_work": report.today_work,
            "problems": report.problems,
            "tomorrow_plan": report.tomorrow_plan,
            "emotion": report.emotion,
            "raw_input": report.raw_input,
            "input_fragments": report.input_fragments,
            "section_status": report.section_status,
            "completeness_score": report.completeness_score,
            "status": report.status,
            "confirmation_type": report.confirmation_type,
            "confirmed_by_user": report.confirmed_by_user,
            "quality_warning": report.quality_warning,
            "last_modified_by_user": report.last_modified_by_user,
            "last_modified_at": report.last_modified_at,
            "pending_confirmation_at": report.pending_confirmation_at,
            "auto_submit_at": report.auto_submit_at,
            "source": report.source,
            "llm_model": report.llm_model,
            "llm_payload": report.llm_payload,
            "submitted_at": report.submitted_at,
            "created_at": report.created_at,
            "updated_at": report.updated_at,
        }
    )


def _backup_guard_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: report.get(key)
        for key in (
            "id",
            "user_id",
            "team_id",
            "report_date",
            "today_work",
            "problems",
            "tomorrow_plan",
            "emotion",
            "raw_input",
            "input_fragments",
            "section_status",
            "completeness_score",
            "status",
            "confirmation_type",
            "confirmed_by_user",
            "quality_warning",
            "last_modified_by_user",
            "last_modified_at",
            "pending_confirmation_at",
            "auto_submit_at",
            "source",
            "llm_model",
            "llm_payload",
            "submitted_at",
            "created_at",
            "updated_at",
        )
    }


def _validate_plan(
    backup: dict[str, Any],
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    if not str(plan.get("backup_sha256") or ""):
        raise RuntimeError("repair plan has no backup binding")
    if plan.get("model") != CANARY_MODEL_NAME or plan.get("thinking_enabled") is not True:
        raise RuntimeError("repair plan did not use the required Flash reasoning model")
    if plan.get("failures") or plan.get("non_approved"):
        raise RuntimeError("repair plan contains failed or unapproved users")
    results = list(plan.get("results") or ())
    if len(results) != EXPECTED_USERS:
        raise RuntimeError(f"expected {EXPECTED_USERS} approved users")
    users = sorted(backup["users"], key=lambda row: row["id"])
    aliases = {
        f"affected-{index:02d}": user["id"]
        for index, user in enumerate(users, start=1)
    }
    if {row.get("anonymous_user") for row in results} != set(aliases):
        raise RuntimeError("repair plan user aliases do not match the backup")
    validated: list[dict[str, Any]] = []
    for row in results:
        alias = row["anonymous_user"]
        user_id = aliases[alias]
        if row.get("user_id") != user_id:
            raise RuntimeError(f"{alias} user binding changed")
        if row.get("review", {}).get("decision") != "approve":
            raise RuntimeError(f"{alias} lacks independent approval")
        if row.get("plan_sha256") != _sha256_json(row.get("plan")):
            raise RuntimeError(f"{alias} plan hash mismatch")
        if row.get("review_sha256") != _sha256_json(row.get("review")):
            raise RuntimeError(f"{alias} review hash mismatch")
        backup_report = _existing_report(backup, user_id)
        timeline = _timeline(backup, user_id)
        compact_plan = {
            key: row["plan"][key]
            for key in (
                "decision",
                "fields",
                "acknowledged_empty_fields",
                "reason",
            )
        }
        materialized = _validate_and_materialize(
            compact_plan,
            report=backup_report,
            timeline=timeline,
        )
        if materialized != row["plan"]:
            raise RuntimeError(f"{alias} materialized plan drifted")
        validated.append(
            {
                **row,
                "backup_report": backup_report,
                "timeline": timeline,
            }
        )
    changed = [row for row in validated if row["plan"]["decision"] == "change"]
    created = [row for row in changed if row["backup_report"] is None]
    if len(changed) != EXPECTED_CHANGED or len(created) != EXPECTED_CREATED:
        raise RuntimeError(
            f"unexpected repair scope: changed={len(changed)}, created={len(created)}"
        )
    return validated


def _source_indexes(row: dict[str, Any]) -> list[int]:
    return sorted(
        {
            int(evidence["source_message_index"])
            for field in REPORT_FIELDS
            for evidence in row["plan"]["fields"][field]
            if "source_message_index" in evidence
        }
    )


def _content_snapshot(report: DailyReport) -> dict[str, Any]:
    return {
        "report_id": str(report.id),
        "report_date": report.report_date.isoformat(),
        "today_work": list(report.today_work or ()),
        "problems": list(report.problems or ()),
        "tomorrow_plan": list(report.tomorrow_plan or ()),
        "status": report.status,
        "confirmation_type": report.confirmation_type,
        "confirmed_by_user": report.confirmed_by_user,
        "submitted_at": (
            report.submitted_at.isoformat() if report.submitted_at else None
        ),
        "section_status": dict(report.section_status or {}),
    }


def _updated_section_status(
    report: DailyReport | None,
    *,
    fields: dict[str, list[str]],
    acknowledged_empty_fields: list[str],
    audit: dict[str, Any],
) -> tuple[dict[str, Any], float]:
    previous = dict(report.section_status or {}) if report is not None else {}
    assessment = assess_daily_report_completeness(
        today_work=fields["today_work"],
        problems=fields["problems"],
        tomorrow_plan=fields["tomorrow_plan"],
        section_status={},
        acknowledged_empty_fields=set(acknowledged_empty_fields),
    )
    for key in (
        "today_work",
        "problems",
        "tomorrow_plan",
        "today_work_acknowledged_empty",
        "problems_acknowledged_empty",
        "tomorrow_plan_acknowledged_empty",
    ):
        previous.pop(key, None)
    previous.update(assessment.section_status)
    previous[TYPED_REPORT_VERSION_KEY] = max(
        0,
        int(previous.get(TYPED_REPORT_VERSION_KEY, 0) or 0),
    ) + 1
    existing_audits = previous.get(REPAIR_AUDIT_KEY)
    audits = (
        [dict(item) for item in existing_audits if isinstance(item, dict)]
        if isinstance(existing_audits, list)
        else []
    )
    previous[REPAIR_AUDIT_KEY] = [*audits, audit]
    typed_audits = previous.get(TYPED_AUDIT_KEY)
    if not isinstance(typed_audits, list):
        previous[TYPED_AUDIT_KEY] = []
    _attach_draft_item_ids(
        previous,
        existing=report,
        today_work=fields["today_work"],
        problems=fields["problems"],
        tomorrow_plan=fields["tomorrow_plan"],
    )
    return previous, assessment.completeness_score


def _new_report(
    user: User,
    *,
    row: dict[str, Any],
    now: datetime,
    backup_sha256: str,
    plan_sha256: str,
) -> DailyReport:
    fields = row["plan"]["materialized_fields"]
    source_indexes = _source_indexes(row)
    sources = [
        item
        for item in row["timeline"]
        if item["source_message_index"] in source_indexes
    ]
    audit = {
        "applied_at": now.isoformat(),
        "backup_sha256": backup_sha256,
        "repair_plan_sha256": plan_sha256,
        "user_plan_sha256": row["plan_sha256"],
        "user_review_sha256": row["review_sha256"],
        "source_message_indexes": source_indexes,
        "operation": "create_missing_report",
    }
    report = DailyReport(
        id=uuid4(),
        user_id=user.id,
        team_id=user.team_id,
        report_date=REPORT_DATE_VALUE,
        today_work=list(fields["today_work"]),
        problems=list(fields["problems"]),
        tomorrow_plan=list(fields["tomorrow_plan"]),
        emotion="",
        raw_input="\n".join(item["text"] for item in sources),
        input_fragments=[
            {
                "source": "agent2_aug17_failure_repair",
                "source_message_index": item["source_message_index"],
                "received_at": item["received_at"],
                "content": item["text"],
            }
            for item in sources
        ],
        section_status={},
        completeness_score=Decimal("0"),
        status="collecting",
        confirmation_type="none",
        confirmed_by_user=False,
        quality_warning=None,
        last_modified_by_user=False,
        last_modified_at=now,
        source="agent2_aug17_repair",
        llm_model=CANARY_MODEL_NAME,
        llm_payload={"agent2_aug17_failure_repair": audit},
    )
    section_status, completeness = _updated_section_status(
        None,
        fields=fields,
        acknowledged_empty_fields=row["plan"]["acknowledged_empty_fields"],
        audit=audit,
    )
    report.section_status = section_status
    report.completeness_score = Decimal(str(completeness))
    mark_report_auto_submitted(report, now)
    return report


def _apply_existing(
    report: DailyReport,
    *,
    row: dict[str, Any],
    now: datetime,
    backup_sha256: str,
    plan_sha256: str,
) -> None:
    fields = row["plan"]["materialized_fields"]
    audit = {
        "applied_at": now.isoformat(),
        "backup_sha256": backup_sha256,
        "repair_plan_sha256": plan_sha256,
        "user_plan_sha256": row["plan_sha256"],
        "user_review_sha256": row["review_sha256"],
        "source_message_indexes": _source_indexes(row),
        "operation": "replace_inaccurate_content",
    }
    section_status, completeness = _updated_section_status(
        report,
        fields=fields,
        acknowledged_empty_fields=row["plan"]["acknowledged_empty_fields"],
        audit=audit,
    )
    report.today_work = list(fields["today_work"])
    report.problems = list(fields["problems"])
    report.tomorrow_plan = list(fields["tomorrow_plan"])
    report.section_status = section_status
    report.completeness_score = Decimal(str(completeness))
    report.last_modified_by_user = False
    report.last_modified_at = now
    llm_payload = dict(report.llm_payload or {})
    llm_payload["agent2_aug17_failure_repair"] = audit
    report.llm_payload = llm_payload
    report.llm_model = CANARY_MODEL_NAME


def _interaction_event(
    *,
    user: User,
    report: DailyReport,
    before: dict[str, Any],
    after: dict[str, Any],
    row: dict[str, Any],
    backup_sha256: str,
    plan_sha256: str,
) -> ReportInteractionEvent:
    changed = before != after
    return ReportInteractionEvent(
        user_id=user.id,
        report_id=report.id,
        dingtalk_user_id=user.dingtalk_user_id,
        report_date=REPORT_DATE_VALUE,
        message_text="2026-08-17 Agent2 failure repair",
        llm_decision_json={
            "backup_sha256": backup_sha256,
            "repair_plan_sha256": plan_sha256,
            "user_plan_sha256": row["plan_sha256"],
            "user_review_sha256": row["review_sha256"],
            "independent_review": row["review"],
            "source_message_indexes": _source_indexes(row),
            "changed": changed,
        },
        backend_action=(
            "agent2_aug17_failure_repair"
            if changed
            else "agent2_aug17_failure_review_no_change"
        ),
        before_snapshot_json=before,
        after_snapshot_json=after,
        correction_type="historical_failure_reconstruction",
        correction_from=_sha256_json(before),
        correction_to=_sha256_json(after),
        confidence=Decimal("1"),
        is_undo=False,
        is_repeated_item_edit=False,
        asr_suspect_json={},
    )


async def _preview_or_apply(
    *,
    backup: dict[str, Any],
    rows: list[dict[str, Any]],
    backup_sha256: str,
    plan_sha256: str,
    apply: bool,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    manifest_rows: list[dict[str, Any]] = []
    async with AsyncSessionLocal() as session:
        async with session.begin():
            for row in rows:
                user_id = row["user_id"]
                user = await session.get(User, user_id)
                if user is None:
                    raise RuntimeError(f"{row['anonymous_user']} user is missing")
                backup_user = next(
                    item for item in backup["users"] if item["id"] == user_id
                )
                if str(user.team_id) != backup_user["team_id"]:
                    raise RuntimeError(f"{row['anonymous_user']} team changed")
                await acquire_daily_report_advisory_lock(
                    session,
                    user.id,
                    REPORT_DATE_VALUE,
                )
                report = await session.scalar(
                    select(DailyReport).where(
                        DailyReport.user_id == user.id,
                        DailyReport.report_date == REPORT_DATE_VALUE,
                    )
                )
                backup_report = row["backup_report"]
                if backup_report is None:
                    if report is not None:
                        raise RuntimeError(
                            f"{row['anonymous_user']} report was created after backup"
                        )
                    before_content = {
                        "report_id": None,
                        "report_date": REPORT_DATE,
                        "today_work": [],
                        "problems": [],
                        "tomorrow_plan": [],
                        "status": None,
                        "confirmation_type": None,
                        "confirmed_by_user": False,
                        "submitted_at": None,
                        "section_status": {},
                    }
                else:
                    if report is None:
                        raise RuntimeError(
                            f"{row['anonymous_user']} report disappeared after backup"
                        )
                    current_guard = _report_guard_snapshot(report)
                    backup_guard = _backup_guard_snapshot(backup_report)
                    if current_guard != backup_guard:
                        raise RuntimeError(
                            f"{row['anonymous_user']} report changed after backup"
                        )
                    before_content = _content_snapshot(report)
                decision = row["plan"]["decision"]
                if decision == "change":
                    if report is None:
                        report = _new_report(
                            user,
                            row=row,
                            now=now,
                            backup_sha256=backup_sha256,
                            plan_sha256=plan_sha256,
                        )
                        session.add(report)
                        await session.flush()
                    else:
                        original_metadata = (
                            report.status,
                            report.confirmation_type,
                            report.confirmed_by_user,
                            report.submitted_at,
                            report.pending_confirmation_at,
                            report.auto_submit_at,
                        )
                        _apply_existing(
                            report,
                            row=row,
                            now=now,
                            backup_sha256=backup_sha256,
                            plan_sha256=plan_sha256,
                        )
                        if (
                            report.status,
                            report.confirmation_type,
                            report.confirmed_by_user,
                            report.submitted_at,
                            report.pending_confirmation_at,
                            report.auto_submit_at,
                        ) != original_metadata:
                            raise RuntimeError(
                                f"{row['anonymous_user']} submission metadata changed"
                            )
                        await session.flush()
                if report is None:
                    raise RuntimeError("repair review has no report target")
                after_content = (
                    _content_snapshot(report)
                    if decision == "change"
                    else before_content
                )
                expected_fields = row["plan"]["materialized_fields"]
                if any(
                    after_content[field] != expected_fields[field]
                    for field in REPORT_FIELDS
                ):
                    raise RuntimeError(
                        f"{row['anonymous_user']} post-repair content mismatch"
                    )
                if apply:
                    session.add(
                        _interaction_event(
                            user=user,
                            report=report,
                            before=before_content,
                            after=after_content,
                            row=row,
                            backup_sha256=backup_sha256,
                            plan_sha256=plan_sha256,
                        )
                    )
                manifest_rows.append(
                    {
                        "anonymous_user": row["anonymous_user"],
                        "decision": decision,
                        "created": backup_report is None and decision == "change",
                        "before_sha256": _sha256_json(before_content),
                        "after_sha256": _sha256_json(after_content),
                        "status_before": before_content["status"],
                        "status_after": after_content["status"],
                        "item_counts_after": {
                            field: len(after_content[field]) for field in REPORT_FIELDS
                        },
                        "plan_sha256": row["plan_sha256"],
                        "review_sha256": row["review_sha256"],
                    }
                )
            if not apply:
                await session.rollback()
    return {
        "schema_version": "agent2.aug17.daily-repair-apply.v1",
        "mode": "apply" if apply else "preview",
        "applied_at": now.isoformat() if apply else None,
        "backup_sha256": backup_sha256,
        "repair_plan_sha256": plan_sha256,
        "dingtalk_send_calls": 0,
        "rows": manifest_rows,
    }


def _write_manifest(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
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
        "sha256": _sha256_bytes(encoded),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--backup-sha256", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    backup = _read_verified_backup(args.backup, args.backup_sha256)
    plan = _read_verified_plan(args.plan, args.plan_sha256)
    if plan.get("backup_sha256") != args.backup_sha256:
        raise RuntimeError("repair plan is not bound to this backup")
    rows = _validate_plan(backup, plan)
    manifest = await _preview_or_apply(
        backup=backup,
        rows=rows,
        backup_sha256=args.backup_sha256,
        plan_sha256=args.plan_sha256,
        apply=args.mode == "apply",
    )
    created = sum(row["created"] for row in manifest["rows"])
    changed = sum(row["decision"] == "change" for row in manifest["rows"])
    summary: dict[str, Any] = {
        "status": "pass",
        "mode": args.mode,
        "users_reviewed": len(manifest["rows"]),
        "reports_changed": changed,
        "reports_created": created,
        "reports_updated": changed - created,
        "reports_unchanged": len(manifest["rows"]) - changed,
        "audit_events": len(manifest["rows"]) if args.mode == "apply" else 0,
        "dingtalk_send_calls": 0,
        "rows": manifest["rows"],
    }
    if args.mode == "apply":
        if args.manifest is None:
            raise RuntimeError("apply mode requires --manifest")
        summary["manifest"] = _write_manifest(args.manifest, manifest)
    elif args.manifest is not None:
        raise RuntimeError("preview mode does not write a manifest")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
