from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import PerformanceSubmission, ReportInteractionEvent, User, WebhookEvent
from app.services.dingtalk import DingTalkPayloadError, parse_incoming_message
from app.utils.time import now_in_timezone
from app.workflows.daily_context import (
    ReplayDailyContext,
    daily_active_task_from_snapshot,
    replay_context_before_message,
    update_replay_daily_context_from_agent2_plan,
    update_replay_daily_context,
)
from app.workflows.gate import build_gate_decision, normalize_workflow_intake_mode
from app.workflows.intake import (
    ActiveWorkflowTask,
    IncomingMessageEnvelope,
    WORKFLOW_MONTHLY_REPORT,
    WorkflowRouter,
)
from app.workflows.replay_audit import classify_legacy_impact


@dataclass(frozen=True)
class ReplayMessage:
    source_kind: str
    source_id: str
    user_id: str
    user_name: str
    dingtalk_user_id: str
    raw_text: str
    created_at: str
    legacy_processed: bool = False
    legacy_report_id: str = ""
    legacy_status: str = ""
    legacy_action: str = ""
    legacy_response_preview: str = ""
    report_date: str = ""
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any] | None = None
    active_tasks: tuple[ActiveWorkflowTask, ...] = ()


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay historical messages through Agent 2.0 workflow gate.")
    parser.add_argument("--source", choices=["webhook", "interaction", "performance", "all"], default="all")
    parser.add_argument("--mode", choices=["observe_only", "protective_gate", "strict_gate"], default="protective_gate")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--start", default="", help="Inclusive ISO datetime, e.g. 2026-07-01T00:00:00+08:00")
    parser.add_argument("--end", default="", help="Exclusive ISO datetime. Defaults to now.")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--output", default="outputs/workflow_gate_replay_latest.json")
    parser.add_argument("--include-text", action="store_true", help="Include full raw text in output.")
    args = parser.parse_args()

    result = asyncio.run(run_replay(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = result["summary"]
    print(f"workflow gate replay written: {output}")
    print(
        "summary: "
        f"total={summary['total']} allow={summary['gate_allow_legacy_daily']} "
        f"block={summary['gate_block_legacy_daily']} "
        f"blocked_legacy={summary['blocked_legacy_processed_count']} "
        f"write_review={summary['review_needed_count']} "
        f"non_write_blocked={summary['blocked_legacy_non_write_count']} "
        f"unknown_blocked={summary['blocked_legacy_unknown_count']} "
        f"multi_effect={summary['multi_effect_count']}"
    )


async def run_replay(args: argparse.Namespace) -> dict[str, Any]:
    mode = normalize_workflow_intake_mode(args.mode)
    start_at, end_at = _resolve_window(args)
    messages: list[ReplayMessage] = []
    async with AsyncSessionLocal() as session:
        if args.source in {"webhook", "all"}:
            messages.extend(await _load_webhook_messages(session, start_at=start_at, end_at=end_at, limit=args.limit))
        if args.source in {"interaction", "all"}:
            messages.extend(await _load_interaction_messages(session, start_at=start_at, end_at=end_at, limit=args.limit))
        if args.source in {"performance", "all"}:
            messages.extend(await _load_performance_fragments(session, start_at=start_at, end_at=end_at, limit=args.limit))

    messages = sorted(messages, key=lambda item: (item.created_at, item.source_kind, item.source_id))[: args.limit]
    router = WorkflowRouter()
    rows: list[dict[str, Any]] = []
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    review_needed: list[dict[str, Any]] = []
    blocked_legacy_processed: list[dict[str, Any]] = []
    blocked_legacy_non_write: list[dict[str, Any]] = []
    blocked_legacy_unknown: list[dict[str, Any]] = []
    multi_effect_count = 0
    daily_context_by_user: dict[str, ReplayDailyContext | None] = {}

    for message in messages:
        user_key = message.user_id or message.dingtalk_user_id
        replay_daily_task = replay_context_before_message(daily_context_by_user.get(user_key)) if user_key else None
        snapshot_daily_task = daily_active_task_from_snapshot(
            message.before_snapshot,
            reason="historical before_snapshot daily context",
        )
        if snapshot_daily_task is None and message.source_kind == "interaction":
            snapshot_daily_task = daily_active_task_from_snapshot(
                message.after_snapshot,
                reason="historical after_snapshot daily context",
            )
        active_tasks = message.active_tasks
        for daily_task in (snapshot_daily_task, replay_daily_task):
            if daily_task is not None and not any(task.workflow == daily_task.workflow for task in active_tasks):
                active_tasks = (*active_tasks, daily_task)

        envelope = IncomingMessageEnvelope(
            sender_id=message.user_id,
            sender_name=message.user_name,
            dingtalk_user_id=message.dingtalk_user_id,
            source=f"replay_{message.source_kind}",
            raw_text=message.raw_text,
            message_id=message.source_id,
            conversation_id="",
            active_tasks=active_tasks,
        )
        plan = router.plan(envelope)
        gate = build_gate_decision(plan, mode=mode)
        legacy_impact = classify_legacy_impact(
            source_kind=message.source_kind,
            legacy_processed=message.legacy_processed,
            legacy_action=message.legacy_action,
            before_snapshot=message.before_snapshot,
            after_snapshot=message.after_snapshot,
            legacy_report_id=message.legacy_report_id,
            legacy_status=message.legacy_status,
        )
        effect_types = [effect.effect_type for effect in plan.effects]
        is_multi_effect = len(effect_types) > 1
        if is_multi_effect:
            multi_effect_count += 1
        row = {
            "source_kind": message.source_kind,
            "source_id": message.source_id,
            "created_at": message.created_at,
            "user_id": message.user_id,
            "user_name": message.user_name,
            "dingtalk_user_id": message.dingtalk_user_id,
            "raw_text_hash": _hash_text(message.raw_text),
            "raw_text_chars": len(message.raw_text or ""),
            "raw_text_preview": _preview(message.raw_text),
            "legacy_processed": message.legacy_processed,
            "legacy_report_id": message.legacy_report_id,
            "legacy_status": message.legacy_status,
            "legacy_action": message.legacy_action,
            "legacy_response_preview": message.legacy_response_preview,
            "legacy_impact": legacy_impact.as_observation(),
            "legacy_write_impact": legacy_impact.write_impact,
            "legacy_impact_kind": legacy_impact.kind,
            "report_date": message.report_date,
            "daily_context_active_before": replay_daily_task is not None or snapshot_daily_task is not None,
            "daily_context_source": (
                getattr(snapshot_daily_task, "reason", "")
                or getattr(replay_daily_task, "reason", "")
                if (snapshot_daily_task is not None or replay_daily_task is not None)
                else ""
            ),
            "active_task_workflows": [task.workflow for task in active_tasks],
            "primary_workflow": plan.primary_workflow,
            "matched_workflows": list(plan.matched_workflows),
            "effects": effect_types,
            "multi_effect": is_multi_effect,
            "gate": gate.as_observation(),
        }
        if args.include_text:
            row["raw_text"] = message.raw_text
        rows.append(row)

        counters["source_kind"][message.source_kind] += 1
        counters["primary_workflow"][plan.primary_workflow] += 1
        counters["reply_type"][gate.reply_type] += 1
        counters["gate_allow_legacy_daily"][str(gate.allow_legacy_daily)] += 1
        counters["gate_block_legacy_daily"][str(gate.block_legacy_daily)] += 1
        counters["legacy_impact_kind"][legacy_impact.kind] += 1
        counters["legacy_write_impact"][str(legacy_impact.write_impact)] += 1
        for tag in gate.audit_tags:
            counters["audit_tags"][tag] += 1
        for effect_type in effect_types:
            counters["effects"][effect_type] += 1

        if message.legacy_processed and gate.block_legacy_daily:
            blocked_legacy_processed.append(row)
            if legacy_impact.write_impact:
                review_needed.append(row)
            elif legacy_impact.kind == "unknown":
                blocked_legacy_unknown.append(row)
            else:
                blocked_legacy_non_write.append(row)

        if user_key:
            next_context = update_replay_daily_context_from_agent2_plan(
                daily_context_by_user.get(user_key),
                message,
                plan,
                gate,
            )
            daily_context_by_user[user_key] = update_replay_daily_context(next_context, message)

    return {
        "mode": mode,
        "window": {"start": start_at.isoformat(), "end": end_at.isoformat()},
        "summary": {
            "total": len(rows),
            "gate_allow_legacy_daily": counters["gate_allow_legacy_daily"].get("True", 0),
            "gate_block_legacy_daily": counters["gate_block_legacy_daily"].get("True", 0),
            "blocked_legacy_processed_count": len(blocked_legacy_processed),
            "review_needed_count": len(review_needed),
            "blocked_legacy_non_write_count": len(blocked_legacy_non_write),
            "blocked_legacy_unknown_count": len(blocked_legacy_unknown),
            "legacy_write_impact_count": counters["legacy_write_impact"].get("True", 0),
            "legacy_non_write_or_unknown_count": counters["legacy_write_impact"].get("False", 0),
            "multi_effect_count": multi_effect_count,
            "source_kind": dict(counters["source_kind"]),
            "primary_workflow": dict(counters["primary_workflow"]),
            "reply_type": dict(counters["reply_type"]),
            "audit_tags": dict(counters["audit_tags"]),
            "effects": dict(counters["effects"]),
            "legacy_impact_kind": dict(counters["legacy_impact_kind"]),
        },
        "review_needed": review_needed[:200],
        "blocked_legacy_processed": blocked_legacy_processed[:200],
        "blocked_legacy_non_write": blocked_legacy_non_write[:100],
        "blocked_legacy_unknown": blocked_legacy_unknown[:100],
        "rows": rows,
    }


async def _load_webhook_messages(session: Any, *, start_at: datetime, end_at: datetime, limit: int) -> list[ReplayMessage]:
    stmt = (
        select(WebhookEvent, User)
        .outerjoin(User, WebhookEvent.dingtalk_user_id == User.dingtalk_user_id)
        .where(WebhookEvent.created_at >= start_at, WebhookEvent.created_at < end_at)
        .order_by(WebhookEvent.created_at)
        .limit(limit)
    )
    result = await session.execute(stmt)
    messages: list[ReplayMessage] = []
    for event, user in result.all():
        text, dingtalk_user_id, message_id = _parse_webhook_payload(event.payload, event.dingtalk_user_id or "")
        if not text:
            continue
        messages.append(
            ReplayMessage(
                source_kind="webhook",
                source_id=str(event.id or message_id or ""),
                user_id=str(getattr(user, "id", "") or ""),
                user_name=str(getattr(user, "name", "") or ""),
                dingtalk_user_id=dingtalk_user_id or str(event.dingtalk_user_id or ""),
                raw_text=text,
                created_at=_iso(event.created_at),
                legacy_processed=bool(event.report_id),
                legacy_report_id=str(event.report_id or ""),
                legacy_status=str(event.status or ""),
                legacy_response_preview=_response_preview(event.response_payload),
            )
        )
    return messages


async def _load_interaction_messages(session: Any, *, start_at: datetime, end_at: datetime, limit: int) -> list[ReplayMessage]:
    stmt = (
        select(ReportInteractionEvent, User)
        .outerjoin(User, ReportInteractionEvent.user_id == User.id)
        .where(ReportInteractionEvent.created_at >= start_at, ReportInteractionEvent.created_at < end_at)
        .order_by(ReportInteractionEvent.created_at)
        .limit(limit)
    )
    result = await session.execute(stmt)
    messages: list[ReplayMessage] = []
    for event, user in result.all():
        if not event.message_text:
            continue
        before_snapshot = dict(event.before_snapshot_json or {})
        after_snapshot = dict(event.after_snapshot_json or {})
        messages.append(
            ReplayMessage(
                source_kind="interaction",
                source_id=str(event.id),
                user_id=str(event.user_id),
                user_name=str(getattr(user, "name", "") or ""),
                dingtalk_user_id=str(event.dingtalk_user_id or getattr(user, "dingtalk_user_id", "") or ""),
                raw_text=str(event.message_text),
                created_at=_iso(event.created_at),
                legacy_processed=True,
                legacy_report_id=str(event.report_id or ""),
                legacy_status=str(after_snapshot.get("status") or ""),
                legacy_action=str(event.backend_action or ""),
                report_date=_iso(event.report_date),
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
            )
        )
    return messages


async def _load_performance_fragments(session: Any, *, start_at: datetime, end_at: datetime, limit: int) -> list[ReplayMessage]:
    stmt = (
        select(PerformanceSubmission, User)
        .outerjoin(User, PerformanceSubmission.user_id == User.id)
        .where(PerformanceSubmission.updated_at >= start_at, PerformanceSubmission.created_at < end_at)
        .order_by(PerformanceSubmission.updated_at)
        .limit(limit)
    )
    result = await session.execute(stmt)
    messages: list[ReplayMessage] = []
    for submission, user in result.all():
        task = ActiveWorkflowTask(
            workflow=WORKFLOW_MONTHLY_REPORT,
            task_id=str(submission.task_id),
            status=str(submission.status or ""),
            reply_candidate=True,
            awaiting_confirmation=str(submission.status or "") == "pending_confirmation",
            reason="historical performance fragment replay",
            metadata={"submission_id": str(submission.id)},
        )
        for index, fragment in enumerate(list(submission.input_fragments_json or [])):
            raw_input = str(fragment.get("raw_input") or "").strip()
            if not raw_input:
                continue
            created_at = _fragment_created_at(fragment) or submission.updated_at or submission.created_at
            if created_at and (created_at < start_at or created_at >= end_at):
                continue
            messages.append(
                ReplayMessage(
                    source_kind="performance",
                    source_id=f"{submission.id}:{index}",
                    user_id=str(submission.user_id),
                    user_name=str(getattr(user, "name", "") or submission.recipient_name or ""),
                    dingtalk_user_id=str(getattr(user, "dingtalk_user_id", "") or ""),
                    raw_text=raw_input,
                    created_at=_iso(created_at),
                    legacy_processed=False,
                    legacy_status=str(submission.status or ""),
                    legacy_action="performance_fragment",
                    active_tasks=(task,),
                )
            )
    return messages


def _parse_webhook_payload(payload: dict[str, Any], fallback_user_id: str) -> tuple[str, str, str]:
    try:
        incoming = parse_incoming_message(payload or {})
        return incoming.text, incoming.dingtalk_user_id, str(incoming.message_id or "")
    except (DingTalkPayloadError, AttributeError):
        text = _extract_text_fallback(payload or {})
        user_id = str(
            (payload or {}).get("senderStaffId")
            or (payload or {}).get("senderId")
            or (payload or {}).get("userId")
            or fallback_user_id
            or ""
        )
        message_id = str((payload or {}).get("msgId") or (payload or {}).get("messageId") or "")
        return text, user_id, message_id


def _extract_text_fallback(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if isinstance(text, dict):
        return str(text.get("content") or "").strip()
    if isinstance(text, str):
        return text.strip()
    content = payload.get("content") or payload.get("Content")
    if isinstance(content, dict):
        return str(content.get("content") or content.get("text") or "").strip()
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            return content.strip()
        if isinstance(decoded, dict):
            return str(decoded.get("content") or decoded.get("text") or "").strip()
        return content.strip()
    return ""


def _resolve_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end_at = _parse_datetime(args.end) if args.end else now_in_timezone("Asia/Shanghai")
    start_at = _parse_datetime(args.start) if args.start else end_at - timedelta(days=max(args.days, 1))
    return start_at, end_at


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("datetime value is empty")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _fragment_created_at(fragment: dict[str, Any]) -> datetime | None:
    for key in ("created_at", "received_at", "submitted_at"):
        value = fragment.get(key)
        if not value:
            continue
        try:
            return _parse_datetime(str(value))
        except ValueError:
            continue
    return None


def _response_preview(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    if isinstance(text, dict):
        return _preview(str(text.get("content") or ""))
    return _preview(str(payload.get("message") or payload.get("content") or ""))


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _preview(value: str, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


if __name__ == "__main__":
    main()
