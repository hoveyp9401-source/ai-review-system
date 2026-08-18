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
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.agent2.tool_calling.production_store import (
    ToolCallCanaryReceipt,
    _text_content as production_text_content,
)
from app.db import AsyncSessionLocal, engine
from app.models import (
    Agent2DailyCommandReceipt,
    DailyReport,
    ReportInteractionEvent,
    User,
    WebhookEvent,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
START_LOCAL = datetime(2026, 8, 17, 0, 0, tzinfo=SHANGHAI)
END_LOCAL = datetime(2026, 8, 18, 0, 0, tzinfo=SHANGHAI)
TIMELINE_START_LOCAL = datetime(2026, 8, 17, 19, 30, tzinfo=SHANGHAI)
TIMELINE_END_LOCAL = datetime(2026, 8, 17, 23, 30, tzinfo=SHANGHAI)
MINIMUM_FAILURE_EVENTS = 59
MINIMUM_UNIQUE_INPUTS = 39


def _text_content(payload: Any) -> str:
    return production_text_content(payload, max_length=None)


def _observation(row: WebhookEvent) -> dict[str, Any]:
    payload = row.response_payload or {}
    value = payload.get("_agent2_turn_observation_v1")
    return value if isinstance(value, dict) else {}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


def _event_snapshot(row: WebhookEvent, user: User | None) -> dict[str, Any]:
    content = _text_content(row.payload)
    return {
        "id": str(row.id),
        "idempotency_key": row.idempotency_key,
        "external_message_id": row.external_message_id,
        "dingtalk_user_id": row.dingtalk_user_id,
        "user_id": str(user.id) if user is not None else None,
        "user_name": user.name if user is not None else None,
        "conversation_id": (row.payload or {}).get("conversationId"),
        "report_id": str(row.report_id) if row.report_id is not None else None,
        "text": content,
        "text_sha256": _sha256(content),
        "text_characters": len(content),
        "payload": row.payload,
        "response_payload": row.response_payload,
        "status": row.status,
        "error_message": row.error_message,
        "received_at": row.received_at,
        "processed_at": row.processed_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _report_snapshot(row: DailyReport, user: User | None) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "user_id": str(row.user_id),
        "user_name": user.name if user is not None else None,
        "team_id": str(row.team_id),
        "report_date": row.report_date,
        "today_work": row.today_work,
        "problems": row.problems,
        "tomorrow_plan": row.tomorrow_plan,
        "emotion": row.emotion,
        "raw_input": row.raw_input,
        "input_fragments": row.input_fragments,
        "section_status": row.section_status,
        "completeness_score": row.completeness_score,
        "status": row.status,
        "confirmation_type": row.confirmation_type,
        "confirmed_by_user": row.confirmed_by_user,
        "quality_warning": row.quality_warning,
        "last_modified_by_user": row.last_modified_by_user,
        "last_modified_at": row.last_modified_at,
        "pending_confirmation_at": row.pending_confirmation_at,
        "auto_submit_at": row.auto_submit_at,
        "source": row.source,
        "llm_model": row.llm_model,
        "llm_payload": row.llm_payload,
        "submitted_at": row.submitted_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _tool_receipt_snapshot(row: ToolCallCanaryReceipt) -> dict[str, Any]:
    return {
        "receipt_id": str(row.receipt_id),
        "tenant_id": row.tenant_id,
        "user_id": row.user_id,
        "conversation_id": row.conversation_id,
        "source_message_id": row.source_message_id,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "idempotency_key": row.idempotency_key,
        "canonical_arguments_hash": row.canonical_arguments_hash,
        "request_fingerprint": row.request_fingerprint,
        "operation_fingerprint": row.operation_fingerprint,
        "status": row.status,
        "changed": row.changed,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "before_version": row.before_version,
        "after_version": row.after_version,
        "affected_item_ids": row.affected_item_ids,
        "safe_user_facts": row.safe_user_facts,
        "before_state_hash": row.before_state_hash,
        "after_state_hash": row.after_state_hash,
        "typed_receipt_ids": row.typed_receipt_ids,
        "error_code": row.error_code,
        "execution_mode": row.execution_mode,
        "created_at": row.created_at,
    }


def _typed_receipt_snapshot(row: Agent2DailyCommandReceipt) -> dict[str, Any]:
    return {
        "receipt_id": str(row.receipt_id),
        "tenant_id": row.tenant_id,
        "user_id": str(row.user_id),
        "report_id": str(row.report_id) if row.report_id is not None else None,
        "report_date": row.report_date,
        "message_id": row.message_id,
        "command_id": str(row.command_id),
        "decision_id": str(row.decision_id),
        "sub_decision_id": str(row.sub_decision_id),
        "command_type": row.command_type,
        "idempotency_key": row.idempotency_key,
        "status": row.status,
        "validation_status": row.validation_status,
        "actual_write": row.actual_write,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "reason_code": row.reason_code,
        "before_json": row.before_json,
        "after_json": row.after_json,
        "audit_json": row.audit_json,
        "created_at": row.created_at,
    }


def _interaction_snapshot(row: ReportInteractionEvent) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "user_id": str(row.user_id),
        "report_id": str(row.report_id) if row.report_id is not None else None,
        "dingtalk_user_id": row.dingtalk_user_id,
        "report_date": row.report_date,
        "message_text": row.message_text,
        "llm_decision_json": row.llm_decision_json,
        "backend_action": row.backend_action,
        "before_snapshot_json": row.before_snapshot_json,
        "after_snapshot_json": row.after_snapshot_json,
        "correction_type": row.correction_type,
        "correction_from": row.correction_from,
        "correction_to": row.correction_to,
        "confidence": row.confidence,
        "is_undo": row.is_undo,
        "is_repeated_item_edit": row.is_repeated_item_edit,
        "asr_suspect_json": row.asr_suspect_json,
        "created_at": row.created_at,
    }


async def _collect() -> tuple[dict[str, Any], dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            day_events = list(
                (
                    await session.scalars(
                        select(WebhookEvent)
                        .where(
                            WebhookEvent.received_at >= START_LOCAL.astimezone(UTC),
                            WebhookEvent.received_at < END_LOCAL.astimezone(UTC),
                        )
                        .order_by(WebhookEvent.received_at, WebhookEvent.id)
                    )
                ).all()
            )
            failures = [
                row
                for row in day_events
                if _observation(row).get("business_result_status") == "failed"
            ]
            unique_inputs = {_sha256(_text_content(row.payload)) for row in failures}
            if len(failures) < MINIMUM_FAILURE_EVENTS:
                raise RuntimeError(
                    "failure count dropped below the previously audited floor: "
                    f"{len(failures)}"
                )
            if len(unique_inputs) < MINIMUM_UNIQUE_INPUTS:
                raise RuntimeError(
                    "unique input count dropped below the previously audited floor: "
                    f"{len(unique_inputs)}"
                )
            dingtalk_ids = sorted(
                {
                    str(row.dingtalk_user_id)
                    for row in failures
                    if row.dingtalk_user_id
                }
            )
            users = list(
                (
                    await session.scalars(
                        select(User).where(User.dingtalk_user_id.in_(dingtalk_ids))
                    )
                ).all()
            )
            users_by_dingtalk = {user.dingtalk_user_id: user for user in users}
            users_by_id = {user.id: user for user in users}
            if set(users_by_dingtalk) != set(dingtalk_ids):
                raise RuntimeError("one or more failed events have no User binding")
            user_ids = sorted(users_by_id, key=str)
            timeline_events = list(
                (
                    await session.scalars(
                        select(WebhookEvent)
                        .where(
                            WebhookEvent.dingtalk_user_id.in_(dingtalk_ids),
                            WebhookEvent.received_at
                            >= TIMELINE_START_LOCAL.astimezone(UTC),
                            WebhookEvent.received_at
                            < TIMELINE_END_LOCAL.astimezone(UTC),
                        )
                        .order_by(
                            WebhookEvent.dingtalk_user_id,
                            WebhookEvent.received_at,
                            WebhookEvent.id,
                        )
                    )
                ).all()
            )
            reports = list(
                (
                    await session.scalars(
                        select(DailyReport)
                        .where(
                            DailyReport.user_id.in_(user_ids),
                            DailyReport.report_date.in_(
                                (date(2026, 8, 16), date(2026, 8, 17), date(2026, 8, 18))
                            ),
                        )
                        .order_by(DailyReport.user_id, DailyReport.report_date)
                    )
                ).all()
            )
            failure_source_ids = {
                str(row.idempotency_key)
                for row in failures
                if row.idempotency_key
            }
            tool_receipts = list(
                (
                    await session.scalars(
                        select(ToolCallCanaryReceipt)
                        .where(
                            ToolCallCanaryReceipt.source_message_id.in_(
                                failure_source_ids
                            )
                        )
                        .order_by(ToolCallCanaryReceipt.created_at)
                    )
                ).all()
            )
            typed_receipts = list(
                (
                    await session.scalars(
                        select(Agent2DailyCommandReceipt)
                        .where(
                            Agent2DailyCommandReceipt.user_id.in_(user_ids),
                            Agent2DailyCommandReceipt.report_date
                            == date(2026, 8, 17),
                        )
                        .order_by(Agent2DailyCommandReceipt.created_at)
                    )
                ).all()
            )
            interactions = list(
                (
                    await session.scalars(
                        select(ReportInteractionEvent)
                        .where(
                            ReportInteractionEvent.user_id.in_(user_ids),
                            ReportInteractionEvent.report_date
                            == date(2026, 8, 17),
                        )
                        .order_by(ReportInteractionEvent.created_at)
                    )
                ).all()
            )

    raw = {
        "schema_version": "agent2.aug17.daily-repair-backup.v1",
        "generated_at": datetime.now(UTC),
        "range": {
            "failure_start": START_LOCAL,
            "failure_end": END_LOCAL,
            "timeline_start": TIMELINE_START_LOCAL,
            "timeline_end": TIMELINE_END_LOCAL,
        },
        "users": [
            {
                "id": str(user.id),
                "dingtalk_user_id": user.dingtalk_user_id,
                "name": user.name,
                "team_id": str(user.team_id),
                "role": user.role,
                "timezone": user.timezone,
                "active": user.active,
            }
            for user in sorted(users, key=lambda item: str(item.id))
        ],
        "failure_events": [
            _event_snapshot(row, users_by_dingtalk.get(row.dingtalk_user_id or ""))
            for row in failures
        ],
        "timeline_events": [
            _event_snapshot(row, users_by_dingtalk.get(row.dingtalk_user_id or ""))
            for row in timeline_events
        ],
        "reports": [
            _report_snapshot(row, users_by_id.get(row.user_id)) for row in reports
        ],
        "tool_receipts": [_tool_receipt_snapshot(row) for row in tool_receipts],
        "typed_daily_receipts": [
            _typed_receipt_snapshot(row) for row in typed_receipts
        ],
        "report_interactions": [
            _interaction_snapshot(row) for row in interactions
        ],
    }
    failure_counts: dict[str, int] = {}
    content_counts: dict[str, int] = {}
    report_by_user = {
        str(row.user_id): row
        for row in reports
        if row.report_date == date(2026, 8, 17)
    }
    for row in failures:
        user = users_by_dingtalk[row.dingtalk_user_id or ""]
        key = str(user.id)
        failure_counts[key] = failure_counts.get(key, 0) + 1
        if len(_text_content(row.payload)) >= 10:
            content_counts[key] = content_counts.get(key, 0) + 1
    summary_users = []
    for index, user in enumerate(sorted(users, key=lambda item: str(item.id)), start=1):
        key = str(user.id)
        report = report_by_user.get(key)
        summary_users.append(
            {
                "anonymous_user": f"affected-{index:02d}",
                "failure_events": failure_counts.get(key, 0),
                "content_shaped_failures": content_counts.get(key, 0),
                "aug17_report_exists": report is not None,
                "aug17_report_status": report.status if report is not None else None,
                "aug17_item_counts": {
                    "today_work": len(report.today_work or ()) if report else 0,
                    "problems": len(report.problems or ()) if report else 0,
                    "tomorrow_plan": len(report.tomorrow_plan or ()) if report else 0,
                },
            }
        )
    summary = {
        "failure_events": len(failures),
        "unique_inputs": len(unique_inputs),
        "affected_users": len(users),
        "timeline_events": len(timeline_events),
        "aug17_reports": len(report_by_user),
        "failed_event_tool_receipts": len(tool_receipts),
        "users": summary_users,
    }
    return _json_safe(raw), summary


def _write_backup(output_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    backup_root = Path("/home/ai_review_tunnel/backups").resolve()
    resolved = output_dir.resolve()
    if backup_root not in resolved.parents:
        raise RuntimeError("backup output must stay under the production backup root")
    resolved.mkdir(mode=0o700, parents=False, exist_ok=False)
    output_path = resolved / "pre-repair.json"
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    descriptor = os.open(
        output_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
    except BaseException:
        os.close(descriptor)
        raise
    return {
        "path": str(output_path),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    payload, summary = await _collect()
    backup = _write_backup(args.output_dir, payload)
    print(
        json.dumps(
            {"status": "pass", "summary": summary, "backup": backup},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
