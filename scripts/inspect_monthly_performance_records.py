from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import PerformanceSubmission, PerformanceTask, User
from app.services.performance_service import missing_by_metric, submission_metrics


DEFAULT_CREATED_BY = "codex_formal_send_20260701"


def _iso(value: Any) -> str:
    return value.isoformat() if value else ""


def _short(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


async def collect_records(*, period: str, created_by: str, show_raw: bool) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        query = (
            select(User, PerformanceSubmission, PerformanceTask)
            .join(PerformanceSubmission, PerformanceSubmission.user_id == User.id)
            .join(PerformanceTask, PerformanceSubmission.task_id == PerformanceTask.id)
            .where(PerformanceTask.period_label == period)
            .order_by(PerformanceTask.created_at, User.name)
        )
        if created_by:
            query = query.where(PerformanceTask.created_by == created_by)

        rows = (await session.execute(query)).all()
        result: list[dict[str, Any]] = []
        for user, submission, task in rows:
            metrics = submission_metrics(submission)
            responses = list(submission.responses_json or [])
            missing = missing_by_metric(metrics, responses)
            snapshot = submission.sent_snapshot_json if isinstance(submission.sent_snapshot_json, dict) else {}
            messages = snapshot.get("messages") if isinstance(snapshot.get("messages"), dict) else {}
            fragments = list(submission.input_fragments_json or [])
            fragment_payload: list[dict[str, Any]] = []
            for fragment in fragments:
                item = {
                    "received_at": fragment.get("received_at", ""),
                    "source": fragment.get("source", ""),
                    "touched_metrics": fragment.get("touched_metrics", []),
                    "status_after": fragment.get("status_after", ""),
                    "missing": fragment.get("missing", {}),
                }
                if show_raw:
                    item["raw_input"] = fragment.get("raw_input", "")
                else:
                    item["raw_preview"] = _short(fragment.get("raw_input", ""))
                fragment_payload.append(item)

            result.append(
                {
                    "unit_name": snapshot.get("unit_name", ""),
                    "leader": submission.recipient_name or user.name,
                    "dingtalk_user_id": user.dingtalk_user_id,
                    "task_title": task.title,
                    "period_label": task.period_label,
                    "task_id": str(task.id),
                    "submission_id": str(submission.id),
                    "task_status": task.status,
                    "submission_status": submission.status,
                    "confirmed_by_user": submission.confirmed_by_user,
                    "submitted_at": _iso(submission.submitted_at),
                    "last_prompted_at": _iso(submission.last_prompted_at),
                    "metric_count": len(metrics),
                    "completed_metric_count": len(metrics) - len(missing),
                    "missing": missing,
                    "send_record": {
                        "formal_send": bool(snapshot.get("formal_send")),
                        "overview_skipped": bool(messages.get("overview_skipped")),
                        "has_overview_markdown": bool(messages.get("overview_markdown")),
                        "has_reply_prompt": bool(messages.get("reply_prompt")),
                        "resend_count": len(snapshot.get("resends") or []),
                        "resends": snapshot.get("resends") or [],
                    },
                    "reply_record": {
                        "fragment_count": len(fragments),
                        "fragments": fragment_payload,
                    },
                    "responses": responses,
                }
            )
        return result


def print_text(records: list[dict[str, Any]]) -> None:
    print(f"monthly performance records: {len(records)}")
    for index, record in enumerate(records, start=1):
        print()
        print(f"{index}. {record['unit_name'] or record['task_title']} / {record['leader']} / {record['dingtalk_user_id']}")
        print(f"   task: {record['task_title']} ({record['period_label']})")
        print(f"   ids: task={record['task_id']} submission={record['submission_id']}")
        print(
            "   status: "
            f"task={record['task_status']} submission={record['submission_status']} "
            f"confirmed={record['confirmed_by_user']} submitted_at={record['submitted_at'] or '-'}"
        )
        send = record["send_record"]
        print(
            "   send: "
            f"overview={'skipped' if send['overview_skipped'] else ('yes' if send['has_overview_markdown'] else 'no')} "
            f"reply_prompt={'yes' if send['has_reply_prompt'] else 'no'} "
            f"resends={send['resend_count']} last_prompted_at={record['last_prompted_at'] or '-'}"
        )
        print(
            "   progress: "
            f"{record['completed_metric_count']}/{record['metric_count']} metrics complete; "
            f"reply_fragments={record['reply_record']['fragment_count']}"
        )
        if record["missing"]:
            missing_text = ", ".join(
                f"{metric_no}:{'/'.join(fields)}" for metric_no, fields in sorted(record["missing"].items(), key=lambda item: int(item[0]))
            )
            print(f"   missing: {missing_text}")
        for fragment in record["reply_record"]["fragments"]:
            print(
                "   fragment: "
                f"received_at={fragment.get('received_at') or '-'} "
                f"source={fragment.get('source') or '-'} "
                f"touched={fragment.get('touched_metrics')} "
                f"status_after={fragment.get('status_after') or '-'}"
            )
            preview = fragment.get("raw_input") if "raw_input" in fragment else fragment.get("raw_preview")
            if preview:
                print(f"     raw: {_short(preview, 220)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect formal monthly performance send/reply records.")
    parser.add_argument("--period", default="2026-06", help="Performance task period_label. Default: 2026-06")
    parser.add_argument("--created-by", default=DEFAULT_CREATED_BY, help="Filter PerformanceTask.created_by. Use empty string to disable.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    parser.add_argument("--show-raw", action="store_true", help="Include full raw user replies.")
    args = parser.parse_args()

    records = asyncio.run(collect_records(period=args.period, created_by=args.created_by, show_raw=args.show_raw))
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        print_text(records)


if __name__ == "__main__":
    main()
