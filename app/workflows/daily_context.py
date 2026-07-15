from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.services.state_machine import STATUS_COLLECTING, STATUS_COMPLETED, STATUS_PENDING_CONFIRMATION
from app.utils.time import now_in_timezone
from app.agent2.daily_state import has_confirmation_pending, pending_keys
from app.workflows.intake import ActiveWorkflowTask, WORKFLOW_DAILY_REPORT


ACTIVE_DAILY_STATUSES = {STATUS_COLLECTING, STATUS_PENDING_CONFIRMATION}
CONTEXTUAL_DAILY_ACTIONS = {
    "report_update",
    "agent_edit_draft",
    "agent_confirm_incomplete_report",
    "current_report_query",
    "agent_fill_report_prompt",
    "report_agent_error",
    "agent_no_change",
}
COMPLETING_DAILY_ACTIONS = {"confirm_submit", "complete_report", "clear_report", "revoke_report"}


@dataclass(frozen=True)
class ReplayDailyContext:
    """Lightweight per-user context for historical gate replay."""

    task_id: str = ""
    status: str = ""
    report_date: str = ""
    awaiting_confirmation: bool = False
    reply_candidate: bool = False
    reason: str = ""

    def as_task(self) -> ActiveWorkflowTask | None:
        if not (self.awaiting_confirmation or self.reply_candidate):
            return None
        return ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id=self.task_id,
            status=self.status,
            reply_candidate=self.reply_candidate,
            awaiting_confirmation=self.awaiting_confirmation,
            reason=self.reason or "historical daily context",
            metadata={"report_date": self.report_date},
        )


@dataclass(frozen=True)
class LiveDailyContext:
    report: Any | None
    report_date: date
    active_task: ActiveWorkflowTask | None
    source: str


async def load_live_daily_report(session: Any, user: Any, settings: Any) -> Any | None:
    """Load today's active daily report that can own an unqualified follow-up.

    Historical collecting reports remain queryable through Daily history, but
    they must never become the implicit write target for a later calendar day.
    """
    from sqlalchemy import select

    from app.models import DailyReport

    now = now_in_timezone(getattr(user, "timezone", "") or getattr(settings, "timezone", "Asia/Shanghai"))
    stmt = (
        select(DailyReport)
        .where(DailyReport.user_id == user.id, DailyReport.report_date == now.date())
        .order_by(DailyReport.updated_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    for report in result.scalars().all():
        if (
            getattr(report, "report_date", None) == now.date()
            and daily_active_task_from_report(report) is not None
        ):
            return report
    return None


async def load_live_daily_context(session: Any, user: Any, settings: Any) -> LiveDailyContext:
    """Resolve the report date from persisted user/report interaction evidence.

    A reminder is a report-scoped interaction even when no Daily row exists yet.
    Persisting and reading that interaction keeps an early-morning reply attached
    to the report date the robot actually asked about instead of silently using
    the wall-clock date.
    """

    from sqlalchemy import select

    from app.models import DailyReport, ReportInteractionEvent

    timezone = getattr(user, "timezone", "") or getattr(settings, "timezone", "Asia/Shanghai")
    now = now_in_timezone(timezone)
    current_report = await _load_daily_report_for_date(
        session,
        user_id=user.id,
        report_date=now.date(),
    )
    current_task = daily_active_task_from_report(current_report)
    if current_task is not None:
        return LiveDailyContext(
            report=current_report,
            report_date=now.date(),
            active_task=current_task,
            source="active_report",
        )

    reminder_statement = (
        select(ReportInteractionEvent)
        .where(
            ReportInteractionEvent.user_id == user.id,
            ReportInteractionEvent.backend_action == "daily_report_reminder_sent",
            ReportInteractionEvent.report_date <= now.date(),
            ReportInteractionEvent.created_at >= now - timedelta(hours=16),
        )
        .order_by(ReportInteractionEvent.created_at.desc())
        .limit(1)
    )
    reminder_result = await session.execute(reminder_statement)
    reminder = next(iter(reminder_result.scalars().all()), None)
    if reminder is None:
        return LiveDailyContext(
            report=None,
            report_date=now.date(),
            active_task=None,
            source="current_date_default",
        )

    target_date = reminder.report_date
    target_report = await _load_daily_report_for_date(
        session,
        user_id=user.id,
        report_date=target_date,
    )
    if target_report is not None and str(getattr(target_report, "status", "") or "") == STATUS_COMPLETED:
        return LiveDailyContext(
            report=None,
            report_date=now.date(),
            active_task=None,
            source="completed_reminder_target_ignored",
        )
    active_task = daily_active_task_from_report(target_report)
    if active_task is None:
        active_task = ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id=f"reminder:{getattr(reminder, 'id', '')}",
            status=STATUS_COLLECTING,
            reply_candidate=True,
            awaiting_confirmation=False,
            reason="recent persisted daily reminder awaits a reply",
            metadata={
                "report_date": target_date.isoformat(),
                "reminder_event_id": str(getattr(reminder, "id", "") or ""),
            },
        )
    return LiveDailyContext(
        report=target_report,
        report_date=target_date,
        active_task=active_task,
        source="recent_reminder",
    )


async def _load_daily_report_for_date(
    session: Any,
    *,
    user_id: Any,
    report_date: date,
) -> Any | None:
    from sqlalchemy import select

    from app.models import DailyReport

    statement = (
        select(DailyReport)
        .where(
            DailyReport.user_id == user_id,
            DailyReport.report_date == report_date,
        )
        .order_by(DailyReport.updated_at.desc())
        .limit(1)
    )
    result = await session.execute(statement)
    return next(iter(result.scalars().all()), None)


async def build_live_daily_active_task(session: Any, user: Any, settings: Any) -> ActiveWorkflowTask | None:
    """Build a daily active task from recent persisted report state."""

    report = await load_live_daily_report(session, user, settings)
    return daily_active_task_from_report(report) if report is not None else None


def daily_active_task_from_report(report: Any) -> ActiveWorkflowTask | None:
    if report is None:
        return None
    status = str(getattr(report, "status", "") or "")
    section_status = getattr(report, "section_status", None) or {}
    active_pending_keys = pending_keys(section_status)
    awaiting_confirmation = status == STATUS_PENDING_CONFIRMATION or has_confirmation_pending(section_status)
    reply_candidate = status in ACTIVE_DAILY_STATUSES or bool(active_pending_keys)
    if not (reply_candidate or awaiting_confirmation):
        return None
    report_date = getattr(report, "report_date", "")
    return ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id=str(getattr(report, "id", "") or ""),
        status=status,
        reply_candidate=reply_candidate,
        awaiting_confirmation=awaiting_confirmation,
        reason="recent daily report has active state",
        metadata={
            "report_date": report_date.isoformat() if hasattr(report_date, "isoformat") else str(report_date or ""),
            "pending_keys": active_pending_keys,
        },
    )


def daily_active_task_from_snapshot(snapshot: dict[str, Any] | None, *, reason: str = "historical daily snapshot") -> ActiveWorkflowTask | None:
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    status = str(snapshot.get("status") or "")
    report_id = str(snapshot.get("report_id") or "")
    report_date = str(snapshot.get("report_date") or snapshot.get("date") or "")
    section_status = snapshot.get("section_status")
    if not isinstance(section_status, dict):
        section_status = snapshot
    active_pending_keys = pending_keys(section_status)
    has_report_content = any(
        bool(snapshot.get(key))
        for key in ("today_work", "problems", "tomorrow_plan")
    ) or any(
        _positive_count(snapshot.get(key))
        for key in ("today_work_count", "problems_count", "tomorrow_plan_count")
    )
    awaiting_confirmation = status == STATUS_PENDING_CONFIRMATION or has_confirmation_pending(section_status)
    reply_candidate = status in ACTIVE_DAILY_STATUSES or bool(active_pending_keys) or bool(report_id and has_report_content)
    if not (reply_candidate or awaiting_confirmation):
        return None
    if not report_id and not has_report_content and not active_pending_keys and not awaiting_confirmation:
        return None
    return ActiveWorkflowTask(
        workflow=WORKFLOW_DAILY_REPORT,
        task_id=report_id,
        status=status,
        reply_candidate=reply_candidate,
        awaiting_confirmation=awaiting_confirmation,
        reason=reason,
        metadata={
            "report_date": report_date,
            "pending_keys": active_pending_keys,
            "snapshot_source": reason,
        },
    )


def replay_context_before_message(context: ReplayDailyContext | None) -> ActiveWorkflowTask | None:
    return context.as_task() if context is not None else None


def update_replay_daily_context(context: ReplayDailyContext | None, message: Any) -> ReplayDailyContext | None:
    """Update lightweight context after one historical message.

    This is intentionally conservative. It does not emulate the full daily
    state machine; it only keeps enough context to avoid treating known daily
    follow-up messages as orphan standalone text during replay.
    """

    action = str(getattr(message, "legacy_action", "") or "")
    status = str(getattr(message, "legacy_status", "") or "")
    snapshot = dict(getattr(message, "after_snapshot", {}) or {})
    report_id = str(snapshot.get("report_id") or getattr(message, "legacy_report_id", "") or "")
    report_date = str(snapshot.get("report_date") or getattr(message, "report_date", "") or "")
    snapshot_status = str(snapshot.get("status") or status or "")

    if action in COMPLETING_DAILY_ACTIONS or snapshot_status == STATUS_COMPLETED:
        return None

    if action in CONTEXTUAL_DAILY_ACTIONS or snapshot_status in ACTIVE_DAILY_STATUSES:
        awaiting = snapshot_status == STATUS_PENDING_CONFIRMATION or action == "agent_confirm_incomplete_report"
        return ReplayDailyContext(
            task_id=report_id or (context.task_id if context else ""),
            status=snapshot_status or STATUS_COLLECTING,
            report_date=report_date or (context.report_date if context else ""),
            awaiting_confirmation=awaiting,
            reply_candidate=True,
            reason=f"replay action {action or snapshot_status} keeps daily context active",
        )

    if action.startswith("agent_") and context is not None:
        return ReplayDailyContext(
            task_id=context.task_id,
            status=context.status or STATUS_COLLECTING,
            report_date=context.report_date,
            awaiting_confirmation=context.awaiting_confirmation,
            reply_candidate=True,
            reason=f"replay action {action} kept prior daily context",
        )
    return context


def update_replay_daily_context_from_agent2_plan(
    context: ReplayDailyContext | None,
    message: Any,
    plan: Any,
    gate: Any,
) -> ReplayDailyContext | None:
    """Carry lightweight daily context according to Agent2's own replay decision.

    Historical webhook rows often lack before/after daily snapshots, so legacy-only
    replay cannot know that a previous Agent2 turn would have opened or preserved
    daily context. This mirrors the runtime contract at a coarse level: if Agent2
    accepted a daily effect, the next turn may use active daily context; if Agent2
    confirmed submission, the context closes.
    """

    effects = list(getattr(plan, "effects", []) or [])
    daily_effects = [
        effect
        for effect in effects
        if str(getattr(effect, "target_system", "") or "") == WORKFLOW_DAILY_REPORT
    ]
    if not daily_effects:
        return context

    allow_daily = bool(getattr(gate, "allow_legacy_daily", False)) if gate is not None else True
    if not allow_daily:
        return context

    if any(str(getattr(effect, "effect_type", "") or "") in {"confirm_daily_report"} for effect in daily_effects):
        return None

    first = daily_effects[0]
    target = getattr(first, "target", {}) if isinstance(getattr(first, "target", {}), dict) else {}
    task_id = str(target.get("task_id") or (context.task_id if context else "") or getattr(message, "legacy_report_id", "") or "")
    status = str(target.get("status") or (context.status if context else "") or STATUS_COLLECTING)
    report_date = str(target.get("report_date") or getattr(message, "report_date", "") or (context.report_date if context else "") or "")
    return ReplayDailyContext(
        task_id=task_id,
        status=status or STATUS_COLLECTING,
        report_date=report_date,
        awaiting_confirmation=status == STATUS_PENDING_CONFIRMATION,
        reply_candidate=True,
        reason="agent2 replay daily effect keeps daily context active",
    )

def _positive_count(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False
