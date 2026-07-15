from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.models import DailyReport, ProgressOutboxEvent, ReportInteractionEvent
from app.progress.outbox import build_progress_outbox_idempotency_key, raw_text_hash
from app.repositories import create_progress_outbox_event_once


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressReconciliationPlan:
    start_date: date
    end_date: date
    dry_run: bool = True
    include_daily_reports: bool = True
    include_interaction_events: bool = True


@dataclass
class ProgressReconciliationStats:
    scanned_count: int = 0
    missing_count: int = 0
    created_count: int = 0
    skipped_count: int = 0
    error_count: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned_count": self.scanned_count,
            "missing_count": self.missing_count,
            "created_count": self.created_count,
            "skipped_count": self.skipped_count,
            "error_count": self.error_count,
        }


@dataclass(frozen=True)
class ReconciliationOutboxRecord:
    event_type: str
    source_type: str
    source_id: str
    user_id: uuid.UUID | None
    team_id: uuid.UUID | None
    report_id: uuid.UUID | None
    report_date: date | None
    raw_text: str
    payload_json: dict[str, Any]

    @property
    def text_hash(self) -> str:
        return raw_text_hash(self.raw_text)

    @property
    def idempotency_key(self) -> str:
        return build_progress_outbox_idempotency_key(
            event_type=self.event_type,
            source_type=self.source_type,
            source_id=self.source_id,
            report_id=str(self.report_id) if self.report_id else "",
            text_hash=self.text_hash,
        )


async def reconcile_progress_outbox(*, session: Any, plan: ProgressReconciliationPlan) -> ProgressReconciliationStats:
    stats = ProgressReconciliationStats()
    records = await _collect_reconciliation_records(session=session, plan=plan)
    for record in records:
        stats.scanned_count += 1
        try:
            if await _outbox_exists(session, record.idempotency_key):
                stats.skipped_count += 1
                continue
            stats.missing_count += 1
            if plan.dry_run:
                continue
            _, inserted = await create_progress_outbox_event_once(
                session,
                event_type=record.event_type,
                source_type=record.source_type,
                source_id=record.source_id,
                user_id=record.user_id,
                team_id=record.team_id,
                report_id=record.report_id,
                report_date=record.report_date,
                payload_json=record.payload_json,
                raw_text_hash=record.text_hash,
                idempotency_key=record.idempotency_key,
            )
            if inserted:
                stats.created_count += 1
            else:
                stats.skipped_count += 1
        except Exception:
            stats.error_count += 1
            logger.exception(
                "progress reconciliation record failed source_type=%s source_id=%s",
                record.source_type,
                record.source_id,
            )
    if not plan.dry_run:
        await session.commit()
    return stats


async def _collect_reconciliation_records(*, session: Any, plan: ProgressReconciliationPlan) -> list[ReconciliationOutboxRecord]:
    records: list[ReconciliationOutboxRecord] = []
    if plan.include_daily_reports:
        records.extend(await _daily_report_records(session=session, plan=plan))
    if plan.include_interaction_events:
        records.extend(await _interaction_event_records(session=session, plan=plan))
    return records


async def _daily_report_records(*, session: Any, plan: ProgressReconciliationPlan) -> list[ReconciliationOutboxRecord]:
    result = await session.execute(
        select(DailyReport).where(DailyReport.report_date >= plan.start_date, DailyReport.report_date <= plan.end_date)
    )
    records = []
    for report in result.scalars().all():
        report_id = getattr(report, "id", None)
        raw_text = str(getattr(report, "raw_input", "") or "")
        records.append(
            ReconciliationOutboxRecord(
                event_type="daily_report_reconciled",
                source_type="daily_report",
                source_id=str(report_id or ""),
                user_id=getattr(report, "user_id", None),
                team_id=getattr(report, "team_id", None),
                report_id=report_id,
                report_date=getattr(report, "report_date", None),
                raw_text=raw_text,
                payload_json={
                    "source": "reconciliation",
                    "source_table": "daily_reports",
                    "report_id": str(report_id or ""),
                    "report_date": getattr(report, "report_date", None).isoformat() if getattr(report, "report_date", None) else "",
                    "raw_text_hash": raw_text_hash(raw_text),
                    "raw_text_len": len(raw_text),
                    "status": str(getattr(report, "status", "") or ""),
                    "today_work_count": len(getattr(report, "today_work", None) or []),
                    "problems_count": len(getattr(report, "problems", None) or []),
                    "tomorrow_plan_count": len(getattr(report, "tomorrow_plan", None) or []),
                },
            )
        )
    return records


async def _interaction_event_records(*, session: Any, plan: ProgressReconciliationPlan) -> list[ReconciliationOutboxRecord]:
    result = await session.execute(
        select(ReportInteractionEvent).where(
            ReportInteractionEvent.report_date >= plan.start_date,
            ReportInteractionEvent.report_date <= plan.end_date,
        )
    )
    records = []
    for event in result.scalars().all():
        event_id = getattr(event, "id", None)
        report_id = getattr(event, "report_id", None)
        message_text = str(getattr(event, "message_text", "") or "")
        records.append(
            ReconciliationOutboxRecord(
                event_type="report_interaction_reconciled",
                source_type="report_interaction_event",
                source_id=str(event_id or ""),
                user_id=getattr(event, "user_id", None),
                team_id=None,
                report_id=report_id,
                report_date=getattr(event, "report_date", None),
                raw_text=message_text,
                payload_json={
                    "source": "reconciliation",
                    "source_table": "report_interaction_events",
                    "interaction_event_id": str(event_id or ""),
                    "report_id": str(report_id or ""),
                    "report_date": getattr(event, "report_date", None).isoformat() if getattr(event, "report_date", None) else "",
                    "raw_text_hash": raw_text_hash(message_text),
                    "raw_text_len": len(message_text),
                    "backend_action": str(getattr(event, "backend_action", "") or ""),
                },
            )
        )
    return records


async def _outbox_exists(session: Any, idempotency_key: str) -> bool:
    result = await session.execute(select(ProgressOutboxEvent.id).where(ProgressOutboxEvent.idempotency_key == idempotency_key))
    return result.scalar_one_or_none() is not None


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


async def _run_cli(args: argparse.Namespace) -> None:
    from app.db import AsyncSessionLocal, engine

    plan = ProgressReconciliationPlan(
        start_date=_parse_date(args.start_date),
        end_date=_parse_date(args.end_date),
        dry_run=not args.apply,
        include_daily_reports=not args.only_interactions,
        include_interaction_events=not args.only_daily_reports,
    )
    try:
        async with AsyncSessionLocal() as session:
            stats = await reconcile_progress_outbox(session=session, plan=plan)
        print("PROGRESS_RECONCILIATION_STATS_START")
        for key, value in stats.as_dict().items():
            print(f"{key}={value}")
        print(f"dry_run={plan.dry_run}")
        print("PROGRESS_RECONCILIATION_STATS_END")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill missing Progress outbox events.")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--apply", action="store_true", help="Actually create missing outbox rows. Omit for dry-run.")
    parser.add_argument("--only-daily-reports", action="store_true")
    parser.add_argument("--only-interactions", action="store_true")
    args = parser.parse_args()
    get_settings()
    asyncio.run(_run_cli(args))


if __name__ == "__main__":
    main()
