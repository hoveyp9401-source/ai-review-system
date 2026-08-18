from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.db import AsyncSessionLocal, engine
from app.models import (
    Agent2DailyCommandReceipt,
    DailyReport,
    ReportInteractionEvent,
    User,
    WebhookEvent,
)
from scripts.audit_aug17_failed_daily_reports import (
    _event_snapshot,
    _interaction_snapshot,
    _json_safe,
    _observation,
    _report_snapshot,
    _sha256,
    _text_content,
    _tool_receipt_snapshot,
    _typed_receipt_snapshot,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
START_LOCAL = datetime(2026, 8, 18, 0, 0, tzinfo=SHANGHAI)
END_LOCAL = datetime(2026, 8, 18, 12, 0, tzinfo=SHANGHAI)
TIMELINE_START_LOCAL = datetime(2026, 8, 17, 23, 30, tzinfo=SHANGHAI)
TIMELINE_END_LOCAL = END_LOCAL
REPORT_DATES = (date(2026, 8, 17), date(2026, 8, 18))
EXPECTED_FAILURES = 21


async def _collect() -> tuple[dict, dict]:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            window_events = list(
                (
                    await session.scalars(
                        select(WebhookEvent)
                        .where(
                            WebhookEvent.received_at
                            >= START_LOCAL.astimezone(UTC),
                            WebhookEvent.received_at < END_LOCAL.astimezone(UTC),
                        )
                        .order_by(WebhookEvent.received_at, WebhookEvent.id)
                    )
                ).all()
            )
            failures = [
                row
                for row in window_events
                if _observation(row).get("business_result_status") == "failed"
            ]
            if len(failures) != EXPECTED_FAILURES:
                raise RuntimeError(
                    f"expected {EXPECTED_FAILURES} morning failures, got {len(failures)}"
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
                raise RuntimeError("one or more morning failures have no User binding")
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
                            DailyReport.report_date.in_(REPORT_DATES),
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
                            Agent2DailyCommandReceipt.report_date.in_(REPORT_DATES),
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
                            ReportInteractionEvent.report_date.in_(REPORT_DATES),
                        )
                        .order_by(ReportInteractionEvent.created_at)
                    )
                ).all()
            )
    raw = {
        "schema_version": "agent2.aug18-morning.daily-repair-backup.v1",
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
    counts_by_user: dict[str, int] = {}
    for row in failures:
        user = users_by_dingtalk[row.dingtalk_user_id or ""]
        key = str(user.id)
        counts_by_user[key] = counts_by_user.get(key, 0) + 1
    report_lookup = {
        (str(row.user_id), row.report_date.isoformat()): row for row in reports
    }
    summary_users = []
    for index, user in enumerate(sorted(users, key=lambda item: str(item.id)), start=1):
        user_id = str(user.id)
        date_rows = {}
        for report_date in REPORT_DATES:
            report = report_lookup.get((user_id, report_date.isoformat()))
            date_rows[report_date.isoformat()] = {
                "exists": report is not None,
                "status": report.status if report is not None else None,
                "item_counts": {
                    field: len(getattr(report, field) or ()) if report else 0
                    for field in ("today_work", "problems", "tomorrow_plan")
                },
            }
        summary_users.append(
            {
                "anonymous_user": f"morning-{index:02d}",
                "failure_events": counts_by_user.get(user_id, 0),
                "reports": date_rows,
            }
        )
    summary = {
        "failure_events": len(failures),
        "unique_inputs": len({_sha256(_text_content(row.payload)) for row in failures}),
        "affected_users": len(users),
        "timeline_events": len(timeline_events),
        "failed_event_tool_receipts": len(tool_receipts),
        "users": summary_users,
    }
    return _json_safe(raw), summary


def _write_backup(output_dir: Path, payload: dict) -> dict:
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
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
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
