from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    Agent2OperationOutcome,
    BusinessAuditEvent,
    BusinessCommandReceipt,
    CaseProgress,
    CaseReportProjection,
    PeriodicReport,
    TravelIntent,
)
from app.db import AsyncSessionLocal, engine
from app.models import DailyReport, WebhookEvent


DEFAULT_TENANT = "sandbox-agent2-phase2-20260711"
DEFAULT_USERS = ("庞浩", "刘聪")


def _text_content(payload: dict[str, Any] | None) -> str:
    value = (payload or {}).get("text")
    return str(value.get("content") or "").strip() if isinstance(value, dict) else ""


def _ref(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _content_evidence(value: str, *, include_text: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "characters": len(value),
    }
    if include_text:
        payload["text"] = value
    return payload


async def _collect(args: argparse.Namespace) -> dict[str, Any]:
    since = datetime.fromisoformat(args.since)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            bindings = (
                await session.execute(
                    select(Agent2IdentityBinding).where(
                        Agent2IdentityBinding.tenant_id == args.tenant,
                        Agent2IdentityBinding.display_name.in_(args.users),
                        Agent2IdentityBinding.active.is_(True),
                    )
                )
            ).scalars().all()
            if {item.display_name for item in bindings} != set(args.users):
                raise RuntimeError("active identity binding missing for requested user")

            users: dict[str, Any] = {}
            for binding in bindings:
                events = (
                    await session.execute(
                        select(WebhookEvent)
                        .where(
                            WebhookEvent.dingtalk_user_id == binding.dingtalk_user_id,
                            WebhookEvent.received_at >= since,
                        )
                        .order_by(WebhookEvent.received_at.desc())
                        .limit(args.limit)
                    )
                ).scalars().all()
                events = list(reversed(events))
                message_ids = [
                    str(item.external_message_id)
                    for item in events
                    if item.external_message_id
                ]

                receipts = (
                    await session.execute(
                        select(BusinessCommandReceipt).where(
                            BusinessCommandReceipt.tenant_id == args.tenant,
                            BusinessCommandReceipt.source_message_id.in_(message_ids),
                        )
                    )
                ).scalars().all()
                audits = (
                    await session.execute(
                        select(BusinessAuditEvent).where(
                            BusinessAuditEvent.tenant_id == args.tenant,
                            BusinessAuditEvent.source_message_id.in_(message_ids),
                        )
                    )
                ).scalars().all()
                outcomes = (
                    await session.execute(
                        select(Agent2OperationOutcome).where(
                            Agent2OperationOutcome.tenant_id == args.tenant,
                            Agent2OperationOutcome.source_turn_id.in_(message_ids),
                        )
                    )
                ).scalars().all()
                progresses = (
                    await session.execute(
                        select(CaseProgress).where(
                            CaseProgress.tenant_id == args.tenant,
                            CaseProgress.source_message_id.in_(message_ids),
                        )
                    )
                ).scalars().all()
                travels = (
                    await session.execute(
                        select(TravelIntent).where(
                            TravelIntent.tenant_id == args.tenant,
                            TravelIntent.source_message_id.in_(message_ids),
                        )
                    )
                ).scalars().all()
                projections = (
                    await session.execute(
                        select(CaseReportProjection).where(
                            CaseReportProjection.tenant_id == args.tenant,
                            CaseReportProjection.source_message_id.in_(message_ids),
                        )
                    )
                ).scalars().all()

                case_ids = {item.case_id for item in progresses}
                cases = (
                    await session.execute(
                        select(Agent2Case).where(
                            Agent2Case.tenant_id == args.tenant,
                            Agent2Case.case_id.in_(case_ids),
                        )
                    )
                ).scalars().all() if case_ids else []
                case_names = {str(item.case_id): item.case_name for item in cases}

                ingress_rows = (
                    await session.execute(
                        text(
                            """
                            SELECT external_message_id, count(*) AS claim_count
                            FROM message_ingress_claims
                            WHERE external_message_id = ANY(:message_ids)
                            GROUP BY external_message_id
                            """
                        ),
                        {"message_ids": message_ids},
                    )
                ).mappings().all() if message_ids else []
                ingress_counts = {
                    str(row["external_message_id"]): int(row["claim_count"])
                    for row in ingress_rows
                }

                by_message: dict[str, dict[str, Any]] = {
                    message_id: {
                        "receipts": [],
                        "audits": [],
                        "outcomes": [],
                        "case_progress": [],
                        "travel_intents": [],
                        "report_projections": [],
                    }
                    for message_id in message_ids
                }
                for item in receipts:
                    by_message[item.source_message_id]["receipts"].append(
                        {
                            "command_type": item.command_type,
                            "status": item.status,
                            "actual_write": item.actual_write,
                            "resource_type": item.resource_type,
                            "resource_ref": _ref(item.resource_id),
                            "error_code": item.error_code,
                            "failed_stage": item.failed_stage,
                        }
                    )
                for item in audits:
                    by_message[item.source_message_id]["audits"].append(
                        {
                            "command_type": item.command_type,
                            "resource_type": item.resource_type,
                            "resource_ref": _ref(item.resource_id),
                        }
                    )
                for item in outcomes:
                    by_message[item.source_turn_id]["outcomes"].append(
                        {
                            "domain": item.domain,
                            "operation": item.operation,
                            "business_status": item.business_status,
                            "message_status": item.message_status,
                            "actual_write": item.actual_write,
                            "would_write": item.would_write,
                        }
                    )
                for item in progresses:
                    by_message[item.source_message_id]["case_progress"].append(
                        {
                            "case_ref": _ref(item.case_id),
                            "case_name": _content_evidence(
                                case_names.get(str(item.case_id), ""),
                                include_text=args.include_text,
                            ),
                            "summary": _content_evidence(
                                item.summary,
                                include_text=args.include_text,
                            ),
                            "content_origin": item.content_origin,
                            "source_channel": item.source_channel,
                            "version": item.version,
                            "deleted": item.deleted_at is not None,
                        }
                    )
                for item in travels:
                    by_message[item.source_message_id]["travel_intents"].append(
                        {
                            "destination": _content_evidence(
                                item.destination_normalized,
                                include_text=args.include_text,
                            ),
                            "start_at": item.start_at,
                            "end_at": item.end_at,
                            "purpose": _content_evidence(
                                item.purpose_summary,
                                include_text=args.include_text,
                            ),
                            "related_case_refs": [
                                _ref(case_id) for case_id in item.related_case_ids or []
                            ],
                            "source_channel": item.source_channel,
                            "status": item.status,
                        }
                    )
                for item in projections:
                    by_message[item.source_message_id]["report_projections"].append(
                        {
                            "report_type": item.report_type,
                            "projection_type": item.projection_type,
                            "status": item.status,
                        }
                    )

                daily_reports = []
                try:
                    legacy_user_id = UUID(binding.user_id)
                except ValueError:
                    legacy_user_id = None
                if legacy_user_id is not None:
                    daily_reports = (
                        await session.execute(
                            select(DailyReport)
                            .where(
                                DailyReport.user_id == legacy_user_id,
                                DailyReport.updated_at >= since,
                            )
                            .order_by(DailyReport.updated_at)
                        )
                    ).scalars().all()
                periodic_reports = (
                    await session.execute(
                        select(PeriodicReport)
                        .where(
                            PeriodicReport.tenant_id == args.tenant,
                            PeriodicReport.owner_user_id == binding.user_id,
                            PeriodicReport.updated_at >= since,
                        )
                        .order_by(PeriodicReport.updated_at)
                    )
                ).scalars().all()

                users[binding.display_name] = {
                    "user_ref": _ref(binding.user_id),
                    "event_count": len(events),
                    "provider_shaped_event_count": sum(
                        bool(item.external_message_id)
                        and str(item.external_message_id).startswith("msg")
                        and str(item.external_message_id).endswith("==")
                        for item in events
                    ),
                    "events": [
                        {
                            "message_ref": _ref(item.external_message_id),
                            "provider_shaped": bool(item.external_message_id)
                            and str(item.external_message_id).startswith("msg")
                            and str(item.external_message_id).endswith("=="),
                            "ingress_claim_count": ingress_counts.get(
                                str(item.external_message_id), 0
                            ),
                            "received_at": item.received_at,
                            "processed_at": item.processed_at,
                            "status": item.status,
                            "error": item.error_message or "",
                            "message": _content_evidence(
                                _text_content(item.payload),
                                include_text=args.include_text,
                            ),
                            "reply": _content_evidence(
                                _text_content(item.response_payload),
                                include_text=args.include_text,
                            ),
                            **by_message.get(str(item.external_message_id), {}),
                        }
                        for item in events
                    ],
                    "daily_reports": [
                        {
                            "report_date": str(item.report_date),
                            "status": item.status,
                            "today_work_count": len(item.today_work or []),
                            "problems_count": len(item.problems or []),
                            "tomorrow_plan_count": len(item.tomorrow_plan or []),
                            "source": item.source,
                            "updated_at": item.updated_at,
                        }
                        for item in daily_reports
                    ],
                    "periodic_reports": [
                        {
                            "report_type": item.report_type,
                            "period_key": item.period_key,
                            "status": item.status,
                            "version": item.version,
                            "source_channel": item.source_channel,
                            "updated_at": item.updated_at,
                        }
                        for item in periodic_reports
                    ],
                }

            return {
                "artifact_version": "agent2.two_user_real_dialogue_evidence.v1",
                "tenant_ref": _ref(args.tenant),
                "since": args.since,
                "read_only_transaction": True,
                "include_text": args.include_text,
                "users": users,
            }


async def _run(args: argparse.Namespace) -> None:
    try:
        payload = await _collect(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(_json_safe(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary = {
            name: {
                "events": data["event_count"],
                "provider_shaped": data["provider_shaped_event_count"],
                "daily_reports": len(data["daily_reports"]),
                "periodic_reports": len(data["periodic_reports"]),
            }
            for name, data in payload["users"].items()
        }
        print(json.dumps(summary, ensure_ascii=False))
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect read-only, two-user Agent2 dialogue evidence."
    )
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    parser.add_argument("--users", nargs="+", default=list(DEFAULT_USERS))
    parser.add_argument("--since", default="2026-07-13T00:00:00+08:00")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--include-text", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
