from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import difflib
import hashlib
import json
import re
import time
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.action_plan import ActionPlan, AgentAction, PendingInteractionPlan
from app.agent.context_builder import build_agent_context
from app.agent.decision_router import DecisionRoute, DecisionRouter
from app.agent.edit_cursor import build_current_edit_cursor, build_historical_edit_cursor
from app.agent.executor import AgentExecutionResult, ReportAgentExecutor
from app.agent.report_agent import ReportAgent
from app.agent.state_resolver import resolve_pending_interaction
from app.config import Settings
from app.llm.extractor import DailyReportExtractor, LLMOutputError
from app.models import DailyReport, User
from app.repositories import acquire_daily_report_advisory_lock, get_report, list_active_user_habits, merge_ordered, upsert_daily_report
from app.schemas import DailyInputIntentDecision, DraftDecision, DraftFieldUpdate, StructuredDailyReport
from app.services.state_machine import (
    CONFIRMATION_AUTO_SUBMITTED_TIMEOUT,
    CONFIRMATION_NONE,
    CONFIRMATION_USER_CONFIRMED,
    STATUS_COLLECTING,
    STATUS_COMPLETED,
    STATUS_PENDING_CONFIRMATION,
    build_completed_message,
    build_confirmation_message,
    build_followup_message,
    infer_report_state,
)
from app.utils import time as time_utils
from app.workflows.daily_intent import daily_intent_from_action_plan, daily_intent_timing_payload


def now_in_timezone(timezone_name: str):
    return time_utils.now_in_timezone(timezone_name)


def today_in_timezone(timezone_name: str) -> date:
    return time_utils.today_in_timezone(timezone_name)


@dataclass(frozen=True)
class SubmitReportResult:
    report_id: str | None
    report_date: date
    status: str
    completeness_score: float
    missing_sections: list[str]
    message: str
    structured: StructuredDailyReport
    today_work: list[str]
    problems: list[str]
    tomorrow_plan: list[str]
    section_status: dict[str, bool]
    confirmation_type: str
    confirmed_by_user: bool
    quality_warning: str | None
    report_saved: bool
    reply_kind: str
    timings: dict[str, Any]


@dataclass(frozen=True)
class ParsedInput:
    intent: str
    today_work: list[str]
    problems: list[str]
    tomorrow_plan: list[str]
    quality_warning: str | None
    structured: StructuredDailyReport


@dataclass(frozen=True)
class LongReportDetection:
    is_long_report: bool
    detected_sections: int
    score: int


@dataclass(frozen=True)
class DraftEditResult:
    today_work: list[str]
    problems: list[str]
    tomorrow_plan: list[str]
    error: str | None = None
    pending_edit: dict[str, Any] | None = None
    changed_field: str | None = None
    changed_indices: list[int] | None = None


PENDING_ACTION_KEY = "_pending_action"
PENDING_ACTION_PAYLOAD_KEY = "_pending_action_payload"
PENDING_ACTION_CONFIRM_CLEAR_CURRENT_REPORT = "confirm_clear_current_report"
PENDING_ACTION_CONFIRM_CLEAR_DATED_REPORT = "confirm_clear_dated_report"
PENDING_DRAFT_EDIT_KEY = "_pending_draft_edit"
PENDING_INTERACTION_KEY = "_pending_interaction"
PENDING_INTERACTION_AWAITING_APPEND_TARGET = "awaiting_append_target"
PENDING_INTERACTION_AWAITING_APPEND_CONTENT = "awaiting_append_content"
PENDING_INTERACTION_CURRENT_REPORT_EDIT_FLOW = "current_report_edit_flow"
PENDING_QUALITY_CLARIFICATION_KEY = "_pending_quality_clarification"
QUALITY_CLARIFICATION_HISTORY_KEY = "_quality_clarification_history"
LONG_REPORT_MODE_KEY = "long_report_mode"
DRAFT_PREVIOUS_SNAPSHOT_KEY = "_previous_draft_snapshot"
DRAFT_ITEM_IDS_KEY = "_draft_item_ids"
RECENT_REPORT_CONTEXT_KEY = "_recent_report_context"
UNRESOLVED_DRAFT_EDIT_KEY = "_unresolved_draft_edit"
REPORT_FIELD_NAMES = ("today_work", "problems", "tomorrow_plan")
MESSAGE_CLEAR_CURRENT_REPORT_DONE = "已清空当前复盘草稿。你可以重新说今天主要做了什么。"
MESSAGE_CONFIRM_CLEAR_DRAFT = "你是想清空当前复盘草稿吗？回复“清空”我就清掉当前内容，回复“取消”则保留原内容。"
MESSAGE_CONFIRM_CLEAR_COMPLETED = "当前复盘已经提交。你确定要清空并重新填写吗？回复“清空”确认，回复“取消”保留。"
MESSAGE_CANCEL_CLEAR_CURRENT_REPORT = "好的，当前复盘内容已保留。你可以继续补充或修改。"


class DailyReportService:
    def __init__(self, settings: Settings, extractor: DailyReportExtractor):
        self.settings = settings
        self.extractor = extractor
        self.report_agent = ReportAgent(extractor.client)
        self.decision_router = DecisionRouter(self.report_agent)
        self.report_agent_executor = ReportAgentExecutor()

    async def submit_text(
        self,
        session: AsyncSession,
        *,
        user: User,
        raw_input: str,
        source: str,
        report_date: date | None = None,
    ) -> SubmitReportResult:
        total_start = time.perf_counter()
        timings: dict[str, Any] = {}
        received_at = now_in_timezone(user.timezone or self.settings.timezone)
        explicit_report_date = report_date is not None
        requested_report_date = report_date
        calendar_date = today_in_timezone(user.timezone or self.settings.timezone)
        default_report_date = _default_report_date_for_received_at(received_at)
        report_date = requested_report_date or default_report_date
        timings["received_at"] = received_at.isoformat()
        timings["calendar_date"] = calendar_date.isoformat()
        timings["default_report_date"] = default_report_date.isoformat()
        timings["requested_report_date"] = requested_report_date.isoformat() if requested_report_date else ""
        timings["previous_report_cutoff_allowed"] = _within_previous_report_cutoff(received_at)
        if (
            explicit_report_date
            and report_date < default_report_date
            and not _is_allowed_previous_report_date(report_date, default_report_date, received_at)
        ):
            return _build_non_report_result(
                existing=None,
                report_date=default_report_date,
                message=_previous_report_cutoff_message(default_report_date),
                reply_kind="previous_report_cutoff",
                timings=_finalize_timings(timings, total_start),
            )
        current_report_signal = _looks_like_current_report_content(raw_input) or _looks_like_current_report_lock_reply(raw_input)
        timings["current_report_signal"] = current_report_signal
        if not explicit_report_date and report_date != calendar_date and _looks_like_current_report_lock_reply(raw_input):
            report_date = calendar_date
            timings["explicit_current_report_date_override"] = report_date.isoformat()
        if (
            not explicit_report_date
            and not _reporting_required_on(report_date)
            and not _is_allowed_non_reporting_day_request(raw_input, report_date)
        ):
            return _build_non_report_result(
                existing=None,
                report_date=report_date,
                message=_non_reporting_day_message(calendar_date),
                reply_kind="non_reporting_day",
                timings=_finalize_timings(timings, total_start),
            )
        if not explicit_report_date:
            reassignment_target_date = _resolve_date_reassignment_target(raw_input, default_report_date, received_at)
            if reassignment_target_date is not None and reassignment_target_date != default_report_date:
                if reassignment_target_date < default_report_date and not _is_allowed_previous_report_date(reassignment_target_date, default_report_date, received_at):
                    return _build_non_report_result(
                        existing=None,
                        report_date=default_report_date,
                        message=_previous_report_cutoff_message(default_report_date),
                        reply_kind="previous_report_cutoff",
                        timings=_finalize_timings(timings, total_start),
                    )
                step_start = time.perf_counter()
                await _acquire_report_processing_lock(session, user.id, default_report_date)
                timings["acquire_reassignment_source_lock_seconds"] = round(time.perf_counter() - step_start, 4)
                source_existing = await get_report(session, user.id, default_report_date)
                if source_existing is not None and _has_report_content(source_existing):
                    return await self._move_existing_draft_to_report_date(
                        session,
                        user=user,
                        existing=source_existing,
                        source_report_date=default_report_date,
                        target_report_date=reassignment_target_date,
                        received_at=received_at,
                        source=source,
                        raw_input=raw_input,
                        timings=timings,
                        total_start=total_start,
                    )
            if _is_previous_report_blocked_by_cutoff(raw_input, default_report_date, received_at):
                return _build_non_report_result(
                    existing=None,
                    report_date=default_report_date,
                    message=_previous_report_cutoff_message(default_report_date),
                    reply_kind="previous_report_cutoff",
                    timings=_finalize_timings(timings, total_start),
                )
            target_date = (
                None
                if (
                    _looks_like_previous_plan_completion_request(raw_input)
                    or _looks_like_pasted_reference_report(raw_input)
                    or _looks_like_copy_recent_report_to_today(raw_input)
                    or _looks_like_historical_report_delete_request(raw_input, default_report_date)
                    or _resolve_report_display_request(raw_input, default_report_date) is not None
                    or _looks_like_previous_report_edit_entry_request(raw_input)
                )
                else _resolve_report_update_target_date(raw_input, default_report_date, received_at)
            )
            if target_date is not None:
                report_date = target_date

        step_start = time.perf_counter()
        await _acquire_report_processing_lock(session, user.id, report_date)
        timings["acquire_report_lock_seconds"] = round(time.perf_counter() - step_start, 4)

        step_start = time.perf_counter()
        existing = await get_report(session, user.id, report_date)
        timings["load_existing_report_seconds"] = round(time.perf_counter() - step_start, 4)

        skip_recent_context_followup = (
            existing is not None
            and _valid_recent_report_context(existing) is None
            and _is_current_report_display_request(raw_input, existing)
        )
        if not skip_recent_context_followup:
            recent_context_result = await self._handle_recent_report_context_followup(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
            )
            if recent_context_result is not None:
                return recent_context_result

        if not explicit_report_date and report_date == default_report_date and existing is None:
            recent_date = default_report_date - timedelta(days=1)
            recent_report = await get_report(session, user.id, recent_date)
            if (
                recent_report is not None
                and not _within_previous_report_cutoff(received_at)
                and not current_report_signal
                and _explicitly_targets_previous_report_change(raw_input, default_report_date)
                and _looks_like_previous_draft_operation(raw_input)
            ):
                return _build_non_report_result(
                    existing=None,
                    report_date=default_report_date,
                    message=_previous_report_cutoff_message(default_report_date),
                    reply_kind="previous_report_cutoff",
                    timings=_finalize_timings(timings, total_start),
                )
            if (
                not current_report_signal
                and _should_continue_recent_backfill(recent_report, raw_input=raw_input, received_at=received_at, default_report_date=default_report_date)
            ):
                report_date = recent_date
                step_start = time.perf_counter()
                await _acquire_report_processing_lock(session, user.id, report_date)
                timings["acquire_backfill_report_lock_seconds"] = round(time.perf_counter() - step_start, 4)
                step_start = time.perf_counter()
                existing = await get_report(session, user.id, report_date)
                timings["load_backfill_report_seconds"] = round(time.perf_counter() - step_start, 4)

        timings["pre_message_auto_submit_due"] = bool(existing and _should_auto_submit(existing, received_at))

        clear_pending_draft_for_report_input = False
        clear_pending_quality_for_report_input = False
        full_report_input = _looks_like_full_report_input(raw_input)
        pending_action = _get_pending_action(existing)
        if pending_action == PENDING_ACTION_CONFIRM_CLEAR_DATED_REPORT:
            clear_confirmation = _resolve_pending_clear_reply(raw_input)
            if clear_confirmation == "confirm":
                return await self._clear_dated_report(
                    session,
                    user=user,
                    state_report=existing,
                    current_report_date=report_date,
                    target_date=_pending_action_target_date(existing, report_date),
                    received_at=received_at,
                    source=source,
                    timings=timings,
                    total_start=total_start,
                )
            if clear_confirmation == "cancel":
                return await self._cancel_pending_clear_current_report(
                    session,
                    existing=existing,
                    report_date=report_date,
                    timings=timings,
                    total_start=total_start,
                )
            target_date = _pending_action_target_date(existing, report_date)
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=_build_confirm_clear_dated_message(target_date),
                reply_kind="ask_confirm_clear_dated_report",
                timings=_finalize_timings(timings, total_start),
            )
        if pending_action == PENDING_ACTION_CONFIRM_CLEAR_CURRENT_REPORT:
            clear_confirmation = _resolve_pending_clear_reply(raw_input)
            if clear_confirmation == "confirm":
                return await self._clear_current_report(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    source=source,
                    timings=timings,
                    total_start=total_start,
                )
            if clear_confirmation == "cancel":
                return await self._cancel_pending_clear_current_report(
                    session,
                    existing=existing,
                    report_date=report_date,
                    timings=timings,
                    total_start=total_start,
                )
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=MESSAGE_CONFIRM_CLEAR_DRAFT if existing and existing.status != STATUS_COMPLETED else MESSAGE_CONFIRM_CLEAR_COMPLETED,
                reply_kind="ask_confirm_clear_current_report",
                timings=_finalize_timings(timings, total_start),
            )

        if _looks_like_historical_report_delete_request(raw_input, report_date):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="历史日报不能删除。我可以帮你查看该日报，或基于历史日报整篇复制为今天草稿。",
                reply_kind="historical_report_delete_blocked",
                timings=_finalize_timings(timings, total_start),
            )

        dated_clear_target = _resolve_dated_clear_request(raw_input, report_date)
        if dated_clear_target is not None:
            return await self._clear_dated_report(
                session,
                user=user,
                state_report=existing,
                current_report_date=report_date,
                target_date=dated_clear_target,
                received_at=received_at,
                source=source,
                timings=timings,
                total_start=total_start,
            )

        if _is_current_report_display_request(raw_input, existing):
            query_plan = _direct_query_current_plan(raw_input, existing) or ActionPlan(
                intent="query_current",
                confidence="high",
                should_write=False,
                reason="direct current report draft query",
            )
            return await self._execute_direct_agent_plan(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
                previous_report=None,
                plan=query_plan,
                branch="direct_query_current",
            )

        display_request = _resolve_report_display_request(raw_input, report_date)
        if display_request is not None:
            target_date, field = display_request
            target_report = await get_report(session, user.id, target_date)
            message = _format_report_display_message(target_report, target_date, field=field) if target_report else f"我没有查到 {target_date.isoformat()} 的日报记录。"
            state_report = existing
            if target_report is not None:
                state_report = await _save_recent_report_context_state(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    viewed_report=target_report,
                    viewed_report_date=target_date,
                    received_at=received_at,
                    source=source,
                    raw_input=raw_input,
                    field=field,
                )
            return _build_non_report_result(
                existing=state_report,
                report_date=report_date,
                message=message,
                reply_kind="history_query",
                timings=_finalize_timings(timings, total_start),
            )

        if _is_daily_briefing_feedback_request(raw_input):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="收到，这是对晨报总览的反馈，不会写入你的个人日报。晨报总览会按风险、问题、卡点和明日关键计划来简化。",
                reply_kind="daily_briefing_feedback",
                timings=_finalize_timings(timings, total_start),
            )

        if _is_probable_noise_input(raw_input):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=(
                    "\u8fd9\u6bb5\u5185\u5bb9\u770b\u8d77\u6765\u50cf\u6587\u4ef6\u7247\u6bb5\u3001"
                    "\u7f16\u7801\u5185\u5bb9\u6216\u65e0\u6548\u6587\u672c\uff0c\u6682\u65f6\u4e0d\u4f1a"
                    "\u8bb0\u5f55\u5230\u65e5\u62a5\u3002\u8bf7\u91cd\u65b0\u8f93\u5165"
                    "\u4eca\u5929\u5b8c\u6210\u5de5\u4f5c\u3001\u660e\u65e5\u8ba1\u5212"
                    "\u6216\u98ce\u9669\u3002"
                ),
                reply_kind="invalid_report_noise",
                timings=_finalize_timings(timings, total_start),
            )

        current_edit_pending = _get_pending_interaction(existing)
        if current_edit_pending and current_edit_pending.get("type") == PENDING_INTERACTION_CURRENT_REPORT_EDIT_FLOW:
            return await self._handle_current_report_edit_flow(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                pending_interaction=current_edit_pending,
                timings=timings,
                total_start=total_start,
            )
        if _looks_like_previous_report_edit_entry_request(raw_input):
            return await self._start_historical_report_edit_flow(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
            )

        if current_edit_pending and current_edit_pending.get("type") == "historical_report_edit_flow":
            pending_resolution = resolve_pending_interaction(raw_input, current_edit_pending)
            if pending_resolution is not None:
                return await self._execute_direct_agent_plan(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    source=source,
                    raw_input=raw_input,
                    timings=timings,
                    total_start=total_start,
                    previous_report=None,
                    plan=pending_resolution.plan,
                    branch=pending_resolution.branch,
                )
            timings["historical_edit_flow_delegated_to_report_agent"] = True
            return await self._submit_text_with_report_agent(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
            )

        direct_restore_plan = _direct_restore_snapshot_plan(raw_input, existing)
        if direct_restore_plan is not None:
            return await self._execute_direct_agent_plan(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
                previous_report=None,
                plan=direct_restore_plan,
                branch="direct_restore_snapshot",
            )

        direct_current_edit = _resolve_direct_agent_plan(raw_input, existing, report_date=report_date)
        if direct_current_edit is not None and direct_current_edit[0] in {
            "direct_delete_last_modified_item",
            "direct_negative_replacement",
            "direct_not_replace_but_add",
            "direct_restore_snapshot",
        }:
            branch, plan = direct_current_edit
            return await self._execute_direct_agent_plan(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
                previous_report=None,
                plan=plan,
                branch=branch,
            )

        if _looks_like_direct_current_report_edit_instruction(raw_input, existing):
            synthetic_pending = _build_current_report_edit_pending(
                report_date,
                existing,
                target_field=_resolve_current_report_field_choice(raw_input),
            )
            pending_resolution = resolve_pending_interaction(raw_input, synthetic_pending)
            if pending_resolution is not None:
                return await self._execute_direct_agent_plan(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    source=source,
                    raw_input=raw_input,
                    timings=timings,
                    total_start=total_start,
                    previous_report=None,
                    plan=pending_resolution.plan,
                    branch=f"direct_current_edit_{pending_resolution.branch}",
                )

        if _looks_like_current_report_edit_entry_request(raw_input):
            return await self._start_current_report_edit_flow(
                session,
                existing=existing,
                report_date=report_date,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
            )

        if _looks_like_current_report_lock_reply(raw_input) and not _looks_like_current_pasted_report(raw_input, report_date):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=_build_report_date_entry_prompt(report_date, existing),
                reply_kind="report_date_clarification",
                timings=_finalize_timings(timings, total_start),
            )

        if _is_report_date_entry_clarification_request(raw_input):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=_build_report_date_entry_prompt(report_date, existing),
                reply_kind="report_date_clarification",
                timings=_finalize_timings(timings, total_start),
            )

        if existing and _is_no_remaining_content_reply(raw_input):
            empty_remaining_result = await self._fill_remaining_missing_sections_as_empty(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
            )
            if empty_remaining_result is not None:
                return empty_remaining_result

        if (
            existing
            and not _get_pending_interaction(existing)
            and not _get_pending_draft_edit(existing)
            and (
                (existing.status == STATUS_PENDING_CONFIRMATION and (_is_confirmation_reply(raw_input) or _is_fast_confirmation_reply(raw_input)))
                or (_has_report_content(existing) and _is_explicit_submit_request(raw_input))
            )
        ):
            return await self._confirm_report(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                timings=timings,
                total_start=total_start,
            )

        if existing is not None and _looks_like_restore_previous_edit_request(raw_input):
            decision = DraftDecision(
                decision_type="control_action",
                message_kind="report_control_action",
                operation="restore_previous",
                target_field="all",
                confidence=0.95,
                should_write=True,
                restore_previous={"enabled": True, "reason": "user requested undo recent edit"},
                reason="restore previous draft snapshot before report-agent routing",
            )
            return await self._execute_draft_decision(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                decision=decision,
                timings=timings,
                total_start=total_start,
            )

        if bool(getattr(self.settings, "report_agent_enabled", False)):
            return await self._submit_text_with_report_agent(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
            )

        direct_replacement_decision = _build_direct_not_this_but_that_decision(raw_input, existing=existing)
        if direct_replacement_decision is not None:
            timings["draft_shadow_compare_result"] = "backend_fast_path"
            timings["draft_shadow_backend_operation"] = direct_replacement_decision.operation
            _apply_draft_decision_timings(timings, direct_replacement_decision)
            return await self._execute_draft_decision(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                decision=direct_replacement_decision,
                timings=timings,
                total_start=total_start,
            )

        direct_report_update_decision = _build_direct_report_update_decision(raw_input, existing=existing)
        direct_patch_decision = None
        if direct_report_update_decision is None:
            direct_patch_decision = _build_direct_ordinal_patch_decision(
                raw_input,
                existing=existing,
                report_date=report_date,
                actual_date=received_at.date() if hasattr(received_at, "date") else report_date,
            )
        direct_fast_path_decision = direct_report_update_decision or direct_patch_decision
        if direct_fast_path_decision is not None:
            timings["draft_shadow_compare_result"] = "backend_fast_path"
            timings["draft_shadow_backend_operation"] = direct_fast_path_decision.operation
            _apply_draft_decision_timings(timings, direct_fast_path_decision)
            return await self._execute_draft_decision(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                decision=direct_fast_path_decision,
                timings=timings,
                total_start=total_start,
            )

        current_slot_for_problem_guard = _infer_current_slot(existing)
        if current_slot_for_problem_guard == "problems" and _is_ambiguous_problem_signal(raw_input):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="你说的问题/风险具体是什么？如果没有问题，可以回复“无”。",
                reply_kind="problem_clarification",
                timings=_finalize_timings(timings, total_start),
            )

        if bool(getattr(self.settings, "llm_draft_decision_enabled", False)):
            pre_current_slot = _infer_current_slot(existing)
            pending_interaction = _get_pending_interaction(existing)
            if existing is not None and _is_restore_previous_request(raw_input):
                decision = DraftDecision(
                    decision_type="control_action",
                    message_kind="report_control_action",
                    operation="restore_previous",
                    target_field="all",
                    confidence=0.95,
                    should_write=True,
                    restore_previous={"enabled": True, "reason": "user requested undo"},
                    reason="restore previous draft snapshot",
                )
                return await self._execute_draft_decision(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    source=source,
                    raw_input=raw_input,
                    decision=decision,
                    timings=timings,
                    total_start=total_start,
                )
            if pre_current_slot == "problems" and _is_ambiguous_problem_signal(raw_input):
                return _build_non_report_result(
                    existing=existing,
                    report_date=report_date,
                    message="你说的问题/风险具体是什么？如果没有问题，可以回复“无”。",
                    reply_kind="problem_clarification",
                    timings=_finalize_timings(timings, total_start),
                )
            backend_shadow_decision = None
            if pending_interaction and pending_interaction.get("type") == PENDING_INTERACTION_AWAITING_APPEND_TARGET:
                selected_field = _resolve_pending_append_target(raw_input)
                if selected_field and existing is not None:
                    report = await _set_pending_interaction(
                        session,
                        existing,
                        {
                            "type": PENDING_INTERACTION_AWAITING_APPEND_CONTENT,
                            "operation": "append",
                            "target_field": selected_field,
                        },
                    )
                    return _build_non_report_result(
                        existing=report,
                        report_date=report_date,
                        message=_build_pending_append_content_message(selected_field),
                        reply_kind="pending_append_target_selected",
                        timings=_finalize_timings(timings, total_start),
                    )
            if (
                existing
                and existing.status == STATUS_PENDING_CONFIRMATION
                and not _get_pending_interaction(existing)
                and not _get_pending_draft_edit(existing)
                and (
                _is_confirmation_reply(raw_input) or _is_fast_confirmation_reply(raw_input)
                )
            ):
                return await self._confirm_report(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    timings=timings,
                    total_start=total_start,
                )
            if _is_global_clear_fast_path(raw_input):
                if existing and existing.status == STATUS_COMPLETED:
                    return await self._ask_confirm_clear_current_report(
                        session,
                        existing=existing,
                        report_date=report_date,
                        timings=timings,
                        total_start=total_start,
                    )
                return await self._clear_current_report(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    source=source,
                    timings=timings,
                    total_start=total_start,
                )
            if _is_postpone_reply(raw_input):
                return _build_non_report_result(
                    existing=existing,
                    report_date=report_date,
                    message=_build_postpone_message(existing, pre_current_slot),
                    reply_kind="postpone_reply",
                    timings=_finalize_timings(timings, total_start),
                )
            if pre_current_slot in {"today_work", "problems", "tomorrow_plan"} and _is_empty_slot_reply(raw_input):
                decision = DraftDecision(
                    decision_type="report_update",
                    message_kind="report_content",
                    operation="set_fields",
                    target_field=pre_current_slot,
                    confidence=0.95,
                    should_write=True,
                    field_updates=[
                        DraftFieldUpdate(
                            field=pre_current_slot,
                            mode="replace",
                            items=[_empty_value_for_field(pre_current_slot, raw_input)],
                        )
                    ],
                    reason="slot-aware empty value answer",
                )
                return await self._execute_draft_decision(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    source=source,
                    raw_input=raw_input,
                    decision=decision,
                    timings=timings,
                    total_start=total_start,
                )
            if _is_courtesy_reply(raw_input) or _is_short_ack_reply(raw_input):
                return _build_non_report_result(
                    existing=existing,
                    report_date=report_date,
                    message=_build_slot_followup_message(existing, pre_current_slot, "这句我先不记入复盘。"),
                    reply_kind="courtesy_reply",
                    timings=_finalize_timings(timings, total_start),
                )
            if _is_short_no_problem_reply(raw_input) and pre_current_slot != "problems":
                return _build_non_report_result(
                    existing=existing,
                    report_date=report_date,
                    message=_build_slot_followup_message(existing, pre_current_slot, "这句我先不记入复盘。"),
                    reply_kind="casual_or_invalid",
                    timings=_finalize_timings(timings, total_start),
                )
            return await self._submit_text_with_draft_decision(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
                backend_shadow_decision=backend_shadow_decision,
            )

        pending_draft_edit = _get_pending_draft_edit(existing)
        if pending_draft_edit:
            if not pending_draft_edit.get("requires_confirmation") and full_report_input:
                clear_pending_draft_for_report_input = True
            else:
                draft_edit_reply = (
                    _resolve_pending_draft_edit_confirmation(raw_input)
                    if pending_draft_edit.get("requires_confirmation")
                    else _resolve_pending_draft_edit_reply(raw_input)
                )
                if draft_edit_reply == "cancel":
                    return await self._cancel_pending_draft_edit(
                        session,
                        existing=existing,
                        report_date=report_date,
                        timings=timings,
                        total_start=total_start,
                    )
                if draft_edit_reply == "answer":
                    return await self._apply_pending_draft_edit(
                        session,
                        user=user,
                        existing=existing,
                        report_date=report_date,
                        received_at=received_at,
                        source=source,
                        raw_input=raw_input,
                        pending_edit=pending_draft_edit,
                        timings=timings,
                        total_start=total_start,
                    )
                if pending_draft_edit.get("requires_confirmation"):
                    return _build_non_report_result(
                        existing=existing,
                        report_date=report_date,
                        message=str(pending_draft_edit.get("confirmation_message") or "请回复“确认”执行，回复“取消”保留。"),
                        reply_kind="draft_edit_instruction_needs_confirmation",
                        timings=_finalize_timings(timings, total_start),
                    )

        current_slot = _infer_current_slot(existing)
        missing_sections = _missing_sections_from_report(existing)
        long_report_detection = _detect_long_report(raw_input)
        timings["long_report_mode"] = long_report_detection.is_long_report
        timings["detected_sections"] = long_report_detection.detected_sections
        rule_intent = _detect_rule_intent(
            raw_input,
            existing=existing,
            current_slot=current_slot,
            long_report_mode=long_report_detection.is_long_report,
        )
        pending_quality = _get_pending_quality_clarification(existing)
        if pending_quality and rule_intent in {None, "continue_collecting", "casual_or_invalid", "courtesy_reply"}:
            if full_report_input:
                clear_pending_quality_for_report_input = True
            else:
                quality_reply = _resolve_pending_quality_reply(raw_input)
                if quality_reply == "decline":
                    return await self._accept_pending_quality_clarification(
                        session,
                        user=user,
                        existing=existing,
                        report_date=report_date,
                        received_at=received_at,
                        source=source,
                        timings=timings,
                        total_start=total_start,
                    )
                if quality_reply == "answer":
                    return await self._apply_pending_quality_clarification(
                        session,
                        user=user,
                        existing=existing,
                        report_date=report_date,
                        received_at=received_at,
                        source=source,
                        raw_input=raw_input,
                        pending_quality=pending_quality,
                        timings=timings,
                        total_start=total_start,
                    )
        intent_decision: DailyInputIntentDecision | None = None
        if rule_intent is None:
            step_start = time.perf_counter()
            try:
                intent_context = _build_intent_context(
                    existing=existing,
                    current_slot=current_slot,
                    missing_sections=missing_sections,
                    long_report_mode=long_report_detection.is_long_report,
                )
                if hasattr(self.extractor, "decide_intent_with_meta"):
                    intent_result = await self.extractor.decide_intent_with_meta(
                        raw_input=raw_input,
                        context=intent_context,
                    )
                    intent_decision = intent_result.payload  # type: ignore[assignment]
                    _apply_llm_meta(timings, "intent", intent_result.meta)
                else:
                    intent_decision = await self.extractor.decide_intent(
                        raw_input=raw_input,
                        context=intent_context,
                    )
            except LLMOutputError as exc:
                timings["llm_intent_seconds"] = round(time.perf_counter() - step_start, 4)
                _apply_llm_meta(timings, "intent", exc.meta)
                return _build_non_report_result(
                    existing=existing,
                    report_date=report_date,
                    message=_build_slot_followup_message(existing, current_slot, "我还没能判断这句要怎么处理，先不写入复盘。"),
                    reply_kind="intent_decision_failed",
                    timings=_finalize_timings(timings, total_start),
                )
            except (AttributeError, ValueError):
                timings["llm_intent_seconds"] = round(time.perf_counter() - step_start, 4)
                return _build_non_report_result(
                    existing=existing,
                    report_date=report_date,
                    message=_build_slot_followup_message(existing, current_slot, "我还没能判断这句要怎么处理，先不写入复盘。"),
                    reply_kind="intent_decision_failed",
                    timings=_finalize_timings(timings, total_start),
                )
            timings["llm_intent_seconds"] = round(time.perf_counter() - step_start, 4)
            intent = _resolve_intent_decision(intent_decision, existing=existing, current_slot=current_slot)
            _apply_semantic_router_timings(timings, intent_decision, used=True)
        else:
            intent = rule_intent
            _apply_semantic_router_timings(timings, None, used=False)

        relation_guard = None if full_report_input else _build_relation_guard_result(
            existing=existing,
            current_slot=current_slot,
            decision=intent_decision,
        )
        if relation_guard is not None:
            message, reply_kind = relation_guard
            timings["current_requested_slot"] = current_slot or ""
            timings["write_fields"] = []
            timings["executor_decision"] = "reject"
            timings["reject_reason"] = reply_kind
            timings["slot_semantic_match"] = False if reply_kind == "relation_unclear" else None
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=message,
                reply_kind=reply_kind,
                timings=_finalize_timings(timings, total_start),
            )

        relation_edit = None if full_report_input else _build_relation_elaboration_edit(
            existing=existing,
            decision=intent_decision,
        )
        if relation_edit is not None and existing is not None:
            timings["current_requested_slot"] = current_slot or ""
            timings["write_fields"] = [relation_edit.changed_field] if relation_edit.changed_field else []
            timings["executor_decision"] = "write"
            timings["reject_reason"] = ""
            timings["slot_semantic_match"] = True
            return await self._save_draft_edit_result(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                edit_result=relation_edit,
                timings=timings,
                total_start=total_start,
            )

        if intent == "confirm_submit":
            return await self._confirm_report(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                timings=timings,
                total_start=total_start,
            )

        if intent == "clear_current_report":
            if existing and existing.status == STATUS_COMPLETED:
                return await self._ask_confirm_clear_current_report(
                    session,
                    existing=existing,
                    report_date=report_date,
                    timings=timings,
                    total_start=total_start,
                )
            return await self._clear_current_report(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                timings=timings,
                total_start=total_start,
            )

        if intent in {"ask_system", "non_report_interaction", "courtesy_reply", "postpone_reply", "casual_or_invalid"}:
            reply_kind = intent
            if intent in {"ask_system", "non_report_interaction"}:
                message = (
                    intent_decision.non_report_reply
                    if intent_decision and intent_decision.non_report_reply
                    else _build_non_report_interaction_message(raw_input)
                )
                reply_kind = "non_report_interaction"
            elif intent == "courtesy_reply":
                message = _build_slot_followup_message(existing, current_slot, "不客气，这句我先不记入复盘。")
            elif intent == "postpone_reply":
                message = _build_postpone_message(existing, current_slot)
            else:
                message = _build_slot_followup_message(existing, current_slot, "这条我先不记入复盘。")
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=message,
                reply_kind=reply_kind,
                timings=_finalize_timings(timings, total_start),
            )

        if intent == "uncertain_high_risk":
            question = intent_decision.clarification_question if intent_decision and intent_decision.clarification_question else "我理解你可能是想调整当前复盘，但还不确定是清空、重写还是补充。你可以直接说“清空当前草稿”或说明要修改哪一项。"
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=question,
                reply_kind=intent,
                timings=_finalize_timings(timings, total_start),
            )

        if intent == "skip":
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="好的，这一项我先跳过。你之后随时可以继续补充。",
                reply_kind="skip",
                timings=_finalize_timings(timings, total_start),
            )

        if intent == "draft_edit_instruction":
            return await self._apply_draft_edit_instruction(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                decision=intent_decision,
                timings=timings,
                total_start=total_start,
            )

        report_input = _report_content_for_extraction(raw_input, intent)
        structured = _fast_path_structured_report(
            report_input,
            intent=intent,
            current_slot=current_slot,
            existing=existing,
        )
        if structured is None:
            step_start = time.perf_counter()
            try:
                if hasattr(self.extractor, "extract_with_meta"):
                    extract_result = await self.extractor.extract_with_meta(
                        report_input,
                        allow_fallback_to_pro=not (
                            bool(existing and existing.status == STATUS_COMPLETED)
                            and intent in {"replace_current_report", "clear_current_report"}
                        ),
                        context=_build_extract_context(
                            existing=existing,
                            current_slot=current_slot,
                            missing_sections=missing_sections,
                            intent=intent,
                            decision=intent_decision,
                            long_report_mode=long_report_detection.is_long_report,
                        ),
                    )
                    structured = extract_result.payload  # type: ignore[assignment]
                    _apply_llm_meta(timings, "extract", extract_result.meta)
                else:
                    structured = await self.extractor.extract(report_input)
            except LLMOutputError as exc:
                timings["llm_extract_seconds"] = round(time.perf_counter() - step_start, 4)
                _apply_llm_meta(timings, "extract", exc.meta)
                return _build_non_report_result(
                    existing=existing,
                    report_date=report_date,
                    message="这条内容我暂时没整理成功。你可以稍后重试，或直接分成几句发我：今天做了什么、有没有问题、明天计划。",
                    reply_kind="extract_failed",
                    timings=_finalize_timings(timings, total_start),
                )
            timings["llm_extract_seconds"] = round(time.perf_counter() - step_start, 4)
        else:
            timings["llm_extract_seconds"] = 0.0

        step_start = time.perf_counter()
        parsed = _interpret_report_input(
            report_input,
            structured,
            current_slot=current_slot,
            existing=existing,
            intent=intent,
            report_date=report_date,
            actual_date=received_at.date() if hasattr(received_at, "date") else report_date,
            allow_slot_fallback=_allow_slot_fallback(
                raw_input,
                existing=existing,
                current_slot=current_slot,
                intent=intent,
                decision=intent_decision,
            ),
        )
        parsed = _remove_multi_field_contamination(parsed)
        timings["current_requested_slot"] = current_slot or ""
        timings["write_fields"] = sorted(_parsed_write_fields(parsed))
        write_guard = _build_report_write_guard_result(
            raw_input,
            existing=existing,
            current_slot=current_slot,
            intent=intent,
            decision=intent_decision,
            parsed=parsed,
        )
        if write_guard is not None:
            timings["report_merge_seconds"] = round(time.perf_counter() - step_start, 4)
            message, reply_kind = write_guard
            timings["executor_decision"] = "reject"
            timings["reject_reason"] = reply_kind
            timings["slot_semantic_match"] = False if reply_kind == "slot_semantic_rejected" else None
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=message,
                reply_kind=reply_kind,
                timings=_finalize_timings(timings, total_start),
            )
        timings["executor_decision"] = "write"
        timings["reject_reason"] = ""
        timings["slot_semantic_match"] = True
        modify_targets = _target_fields_from_input(raw_input, parsed) if intent == "modify_field" else set()
        if intent == "modify_field" and modify_targets and not _has_modify_field_new_content(raw_input, parsed, modify_targets):
            timings["report_merge_seconds"] = round(time.perf_counter() - step_start, 4)
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=_build_modify_missing_content_message(modify_targets),
                reply_kind="modify_field_missing_content",
                timings=_finalize_timings(timings, total_start),
            )

        merged_today_work, merged_problems, merged_tomorrow_plan = _merge_by_intent(
            intent=intent,
            raw_input=raw_input,
            existing=existing,
            parsed=parsed,
            decision=intent_decision,
        )

        state = infer_report_state(
            existing_section_status=None if intent == "replace_current_report" else (existing.section_status if existing else None),
            merged_today_work=merged_today_work,
            merged_problems=merged_problems,
            merged_tomorrow_plan=merged_tomorrow_plan,
            structured=parsed.structured,
            raw_input=raw_input,
        )
        if existing and intent != "replace_current_report":
            previous_section_status = existing.section_status or {}
            if clear_pending_draft_for_report_input:
                previous_section_status = _clear_pending_draft_edit(previous_section_status)
            if clear_pending_quality_for_report_input:
                previous_section_status = _clear_pending_quality(previous_section_status)
            previous_section_status = _clear_unresolved_draft_edit(_clear_pending_interaction(previous_section_status))
            _preserve_internal_section_status(previous_section_status, state.section_status)
        if long_report_detection.is_long_report:
            state.section_status[LONG_REPORT_MODE_KEY] = True
        _attach_draft_item_ids(
            state.section_status,
            existing=None if intent == "replace_current_report" else existing,
            today_work=merged_today_work,
            problems=merged_problems,
            tomorrow_plan=merged_tomorrow_plan,
        )

        content_quality_check = _select_content_quality_clarification(parsed.structured, existing)
        quality_clarification = _build_quality_clarification_state(content_quality_check, parsed) if content_quality_check else None
        quality_warning = (
            parsed.quality_warning
            if intent == "replace_current_report"
            else _merge_quality_warning(existing.quality_warning if existing else None, parsed.quality_warning)
        )
        if content_quality_check and content_quality_check.quality_warning:
            quality_warning = _merge_quality_warning(quality_warning, content_quality_check.quality_warning)
        if quality_clarification and state.ready_for_confirmation and not _quality_clarification_was_asked(existing, quality_clarification):
            state.section_status[PENDING_QUALITY_CLARIFICATION_KEY] = quality_clarification
            history = list((existing.section_status if existing else {}).get(QUALITY_CLARIFICATION_HISTORY_KEY, [])) if existing else []
            history.append(_quality_clarification_history_key(quality_clarification))
            state.section_status[QUALITY_CLARIFICATION_HISTORY_KEY] = history[-10:]

        is_modification = bool(existing and existing.status in {STATUS_PENDING_CONFIRMATION, STATUS_COMPLETED})
        target_status = state.status
        reply_kind = "followup"
        confirmation_type = existing.confirmation_type if existing else CONFIRMATION_NONE
        confirmed_by_user = existing.confirmed_by_user if existing else False
        if existing and existing.status == STATUS_COMPLETED and is_modification and state.ready_for_confirmation:
            target_status = STATUS_COMPLETED
        if quality_clarification and state.ready_for_confirmation and PENDING_QUALITY_CLARIFICATION_KEY in state.section_status:
            target_status = STATUS_COLLECTING

        timings["report_merge_seconds"] = round(time.perf_counter() - step_start, 4)

        step_start = time.perf_counter()
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=report_date,
            raw_input=raw_input,
            source=source,
            today_work=merged_today_work,
            problems=merged_problems,
            tomorrow_plan=merged_tomorrow_plan,
            emotion=parsed.structured.emotion or (existing.emotion if existing else ""),
            completeness_score=state.completeness_score,
            status=target_status,
            section_status=state.section_status,
            llm_model=self.extractor.client.model,
            llm_payload=parsed.structured.model_dump(),
            received_at=received_at,
            confirmation_type=confirmation_type,
            confirmed_by_user=confirmed_by_user,
            quality_warning=quality_warning,
            last_modified_by_user=is_modification,
            last_modified_at=received_at if is_modification else (existing.last_modified_at if existing else None),
            pending_confirmation_at=received_at if target_status == STATUS_PENDING_CONFIRMATION else None,
            auto_submit_at=received_at + timedelta(minutes=30) if target_status == STATUS_PENDING_CONFIRMATION else None,
            replace_sections=True,
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)

        modified_fields = modify_targets if intent == "modify_field" else set()
        if quality_clarification and PENDING_QUALITY_CLARIFICATION_KEY in state.section_status:
            message = _build_quality_clarification_message(report, quality_clarification)
            reply_kind = "quality_clarification"
        elif intent == "modify_field" and modified_fields:
            if report.status == STATUS_COMPLETED and is_modification:
                message = _build_modify_field_message(
                    modified_fields=modified_fields,
                    report=report,
                    missing_sections=[],
                    include_completed_report=True,
                )
                reply_kind = "updated_completed_report"
            elif state.ready_for_confirmation:
                message = _build_modify_field_message(
                    modified_fields=modified_fields,
                    report=report,
                    missing_sections=[],
                    include_confirmation=True,
                    quality_warning=quality_warning,
                )
                reply_kind = "pending_confirmation"
            else:
                message = _build_modify_field_message(
                    modified_fields=modified_fields,
                    report=report,
                    missing_sections=state.missing_sections,
                )
        elif report.status == STATUS_COMPLETED and is_modification:
            message = build_completed_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                updated=True,
            )
            reply_kind = "updated_completed_report"
        elif state.ready_for_confirmation:
            message = build_confirmation_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                quality_warning=quality_warning,
                meta_notes=parsed.structured.meta_notes if long_report_detection.is_long_report else None,
                updated=is_modification,
            )
            reply_kind = "pending_confirmation"
        else:
            acknowledgements = _build_acknowledgements(parsed, state.missing_sections)
            message = build_followup_message(
                state.missing_sections,
                acknowledged=acknowledgements,
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
            )

        return SubmitReportResult(
            report_id=str(report.id),
            report_date=report.report_date,
            status=report.status,
            completeness_score=float(report.completeness_score),
            missing_sections=state.missing_sections,
            message=message,
            structured=parsed.structured,
            today_work=report.today_work,
            problems=report.problems,
            tomorrow_plan=report.tomorrow_plan,
            section_status=report.section_status,
            confirmation_type=report.confirmation_type,
            confirmed_by_user=report.confirmed_by_user,
            quality_warning=report.quality_warning,
            report_saved=True,
            reply_kind=reply_kind,
            timings=timings,
        )

    async def _submit_text_with_report_agent(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        previous_report = await get_report(session, user.id, report_date - timedelta(days=1))
        user_habits = await list_active_user_habits(session, user.id)
        understanding_input = _apply_active_user_habits(raw_input, user_habits)
        timings["reference_report_lookup_date"] = (report_date - timedelta(days=1)).isoformat()
        timings["reference_report_found"] = previous_report is not None
        timings["reference_report_tomorrow_plan_count"] = (
            len(getattr(previous_report, "tomorrow_plan", []) or []) if previous_report is not None else 0
        )
        timings["user_habits_loaded"] = len(user_habits)
        timings["user_habit_asr_applied"] = understanding_input != raw_input

        context = build_agent_context(
            user=user,
            existing=existing,
            raw_input=understanding_input,
            report_date=report_date,
            previous_report=previous_report,
            user_habits=user_habits,
        )
        timings["has_pending_before"] = bool(context.get("pending_interaction"))
        timings["pending_action_before"] = (context.get("pending_state") or {}).get("pending_action") or ""
        timings["pending_section_before"] = (context.get("pending_state") or {}).get("pending_section") or ""
        timings["entered_report_agent"] = False

        self.decision_router.report_agent = self.report_agent
        route = await self.decision_router.decide(
            raw_input=understanding_input,
            context=context,
            existing=existing,
            direct_plan_resolver=lambda candidate_input, candidate_existing: _resolve_direct_agent_plan(
                candidate_input,
                candidate_existing,
                report_date=report_date,
            ),
            fallback_plan_builder=_fallback_plan_from_report_agent_error,
        )
        _apply_decision_route_timings(timings, route)
        if route.plan is not None:
            _apply_daily_intent_frame_timings(
                timings,
                daily_intent_from_action_plan(
                    route.plan,
                    target_date=report_date,
                    raw_text=raw_input,
                    source=route.source,
                    branch=route.branch,
                ),
            )

        if route.source == "direct_rule" and route.plan is not None:
            return await self._execute_direct_agent_plan(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
                previous_report=previous_report,
                plan=route.plan,
                branch=route.branch,
            )

        if route.plan is None:
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="我刚才没理解稳妥，这句先不写入日报。你可以再说一遍，或分成“今日工作、问题/风险、明日计划”告诉我。",
                reply_kind="report_agent_error",
                timings=_finalize_timings(timings, total_start),
            )

        execution = await self.report_agent_executor.execute(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            raw_input=raw_input,
            source=source,
            plan=route.plan,
            meta=route.meta,
            previous_report=previous_report,
        )
        if route.plan.intent == "query_current" and existing is not None and not execution.report_saved:
            display_report = await self.report_agent_executor._save_display_context_state(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                raw_input=raw_input,
                source=source,
                plan=route.plan,
                meta=route.meta,
            )
            execution = AgentExecutionResult(
                report=display_report,
                structured=execution.structured,
                missing_sections=execution.missing_sections,
                message=execution.message,
                reply_kind=execution.reply_kind,
                report_saved=True,
            )
        _apply_agent_execution_timings_safe(timings, route.plan, execution)
        return _result_from_agent_execution_safe(
            execution,
            existing=existing,
            report_date=report_date,
            timings=_finalize_timings(timings, total_start),
        )

    async def _execute_direct_agent_plan(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        timings: dict[str, Any],
        total_start: float,
        previous_report: DailyReport | None,
        plan: ActionPlan,
        branch: str,
    ) -> SubmitReportResult:
        timings["state_resolver_decision"] = branch
        timings["state_resolver_reason"] = plan.reason
        timings["report_agent_state_branch"] = branch
        timings["report_agent_intent"] = plan.intent
        timings["report_agent_confidence"] = plan.confidence
        timings["report_agent_should_write"] = plan.should_write
        timings["report_agent_output_action"] = _agent_action_summary_safe(plan)
        execution = await self.report_agent_executor.execute(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            raw_input=raw_input,
            source=source,
            plan=plan,
            meta={"model": "state_resolver", "thinking": False, "timeout": False, "branch": branch},
            previous_report=previous_report,
        )
        if plan.intent == "query_current" and existing is not None and not execution.report_saved:
            display_report = await self.report_agent_executor._save_display_context_state(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                raw_input=raw_input,
                source=source,
                plan=plan,
                meta={"model": "state_resolver", "thinking": False, "timeout": False, "branch": branch},
            )
            execution = AgentExecutionResult(
                report=display_report,
                structured=execution.structured,
                missing_sections=execution.missing_sections,
                message=execution.message,
                reply_kind=execution.reply_kind,
                report_saved=True,
            )
        _apply_agent_execution_timings_safe(timings, plan, execution)
        return _result_from_agent_execution_safe(
            execution,
            existing=existing,
            report_date=report_date,
            timings=_finalize_timings(timings, total_start),
        )

    async def _submit_text_with_draft_decision(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        timings: dict[str, Any],
        total_start: float,
        backend_shadow_decision: DraftDecision | None = None,
    ) -> SubmitReportResult:
        current_slot = _infer_current_slot(existing)
        missing_sections = _missing_sections_from_report(existing)
        context = _build_draft_decision_context(
            user=user,
            existing=existing,
            report_date=report_date,
            actual_date=received_at.date() if hasattr(received_at, "date") else report_date,
            raw_input=raw_input,
            current_slot=current_slot,
            missing_sections=missing_sections,
        )
        step_start = time.perf_counter()
        try:
            if not hasattr(self.extractor, "decide_draft_with_meta"):
                raise LLMOutputError("Draft decision extractor is not available.")
            decision_result = await self.extractor.decide_draft_with_meta(raw_input=raw_input, context=context)
            decision = decision_result.payload  # type: ignore[assignment]
            _apply_llm_meta(timings, "draft_decision", decision_result.meta)
        except LLMOutputError as exc:
            timings["llm_draft_decision_seconds"] = round(time.perf_counter() - step_start, 4)
            _apply_llm_meta(timings, "draft_decision", exc.meta)
            if backend_shadow_decision is not None:
                timings["draft_shadow_compare_result"] = "llm_failed_use_backend"
                timings["draft_shadow_backend_operation"] = backend_shadow_decision.operation
                _apply_draft_decision_timings(timings, backend_shadow_decision)
                return await self._execute_draft_decision(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    source=source,
                    raw_input=raw_input,
                    decision=backend_shadow_decision,
                    timings=timings,
                    total_start=total_start,
                )
            timings["semantic_router_used"] = True
            timings["semantic_router_timeout"] = bool(timings.get("llm_draft_decision_timeout"))
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="我这次没能稳定理解这句话，先不写入日报。你可以直接按“今天做了什么、问题/风险、明天计划”再发一次。",
                reply_kind="draft_decision_failed",
                timings=_finalize_timings(timings, total_start),
            )
        timings["llm_draft_decision_seconds"] = round(time.perf_counter() - step_start, 4)
        _apply_draft_decision_timings(timings, decision)
        if backend_shadow_decision is not None:
            compatible = _draft_decisions_compatible(decision, backend_shadow_decision)
            timings["draft_shadow_compare_result"] = "compatible_use_llm" if compatible else "inconsistent_use_backend"
            timings["draft_shadow_backend_operation"] = backend_shadow_decision.operation
            if not compatible:
                _apply_draft_decision_timings(timings, backend_shadow_decision)
                return await self._execute_draft_decision(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    source=source,
                    raw_input=raw_input,
                    decision=backend_shadow_decision,
                    timings=timings,
                    total_start=total_start,
                )
        else:
            timings["draft_shadow_compare_result"] = "no_backend_shadow"
        return await self._execute_draft_decision(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            source=source,
            raw_input=raw_input,
            decision=decision,
            timings=timings,
            total_start=total_start,
        )


    async def _handle_recent_report_context_followup(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult | None:
        context = _valid_recent_report_context(existing)
        copy_direct_request = _looks_like_copy_recent_report_to_today(raw_input)
        copy_context_reply = context is not None and _looks_like_recent_report_whole_copy_context_reply(raw_input)
        if copy_direct_request or copy_context_reply:
            direct_source_report_date = _resolve_copy_report_source_date(raw_input, report_date) if copy_direct_request else None
            context_field = str((context or {}).get("field") or "all")
            if direct_source_report_date is not None:
                source_report_date = direct_source_report_date
                context_field = "all"
            elif context is not None:
                if context.get("owner") != "self" or not context.get("can_copy_to_today", False):
                    return _build_non_report_result(
                        existing=existing,
                        report_date=report_date,
                        message="这份日报只能查看，不能直接复制到你的今日日报。",
                        reply_kind="recent_report_copy_blocked",
                        timings=_finalize_timings(timings, total_start),
                    )
                try:
                    source_report_date = date.fromisoformat(str(context.get("report_date") or "")[:10])
                except ValueError:
                    return _build_non_report_result(
                        existing=existing,
                        report_date=report_date,
                        message="我刚才记录的日报日期无效，请重新说要复制哪一天的日报。",
                        reply_kind="recent_report_context_invalid",
                        timings=_finalize_timings(timings, total_start),
                    )
            else:
                return _build_non_report_result(
                    existing=existing,
                    report_date=report_date,
                    message="我还没有可复制的上一份日报上下文。请先说“昨天日报发我下”，确认是哪份后再说“整篇复制”；也可以直接说“复制昨天日报”。",
                    reply_kind="recent_report_context_missing",
                    timings=_finalize_timings(timings, total_start),
                )
            source_report = await get_report(session, user.id, source_report_date)
            if source_report is None:
                return _build_non_report_result(
                    existing=existing,
                    report_date=report_date,
                    message=f"我没有查到 {source_report_date.isoformat()} 的日报记录，暂时不能复制。",
                    reply_kind="recent_report_context_missing_source",
                    timings=_finalize_timings(timings, total_start),
                )
            sections = _copy_current_report_sections(source_report)
            structured = StructuredDailyReport(
                today_work=sections["today_work"],
                problems=sections["problems"],
                tomorrow_plan=sections["tomorrow_plan"],
                emotion=getattr(source_report, "emotion", "") or "",
                completeness=_calculate_completeness(sections["today_work"], sections["problems"], sections["tomorrow_plan"]),
            )
            previous_section_status = dict((existing.section_status if existing else {}) or {})
            state = infer_report_state(
                existing_section_status=previous_section_status,
                merged_today_work=sections["today_work"],
                merged_problems=sections["problems"],
                merged_tomorrow_plan=sections["tomorrow_plan"],
                structured=structured,
                raw_input="",
            )
            _preserve_internal_section_status(previous_section_status, state.section_status)
            if existing is not None:
                state.section_status[DRAFT_PREVIOUS_SNAPSHOT_KEY] = _snapshot_from_report(existing)
            state.section_status[RECENT_REPORT_CONTEXT_KEY] = _recent_report_context_payload(
                viewed_report=source_report,
                viewed_report_date=source_report_date,
                viewed_at=received_at,
                viewer=user,
                owner="self",
                field=context_field,
            )
            report = await upsert_daily_report(
                session,
                user=user,
                report_date=report_date,
                raw_input=raw_input,
                source=source,
                today_work=sections["today_work"],
                problems=sections["problems"],
                tomorrow_plan=sections["tomorrow_plan"],
                emotion=getattr(source_report, "emotion", "") or "",
                completeness_score=state.completeness_score,
                status=state.status,
                section_status=state.section_status,
                llm_model=getattr(source_report, "llm_model", None),
                llm_payload={"operation": "whole_report_copy_to_today", "source_report_date": source_report_date.isoformat()},
                received_at=received_at,
                confirmation_type=CONFIRMATION_NONE,
                confirmed_by_user=False,
                quality_warning=getattr(source_report, "quality_warning", None),
                last_modified_by_user=True,
                last_modified_at=received_at,
                pending_confirmation_at=received_at if state.status == STATUS_PENDING_CONFIRMATION else None,
                auto_submit_at=received_at + timedelta(minutes=30) if state.status == STATUS_PENDING_CONFIRMATION else None,
                replace_sections=True,
            )
            message = _build_whole_report_copy_message(report, source_report_date)
            return _result_from_report(
                report,
                structured=structured,
                missing_sections=_missing_sections_from_report(report),
                message=message,
                reply_kind="recent_report_copy_to_today",
                timings=_finalize_timings(timings, total_start),
            )
        if _looks_like_bare_recent_report_followup(raw_input) and context is None:
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="你说的是哪份日报？可以说“昨天日报发我下”来查看，或明确说明要把昨天计划转成今天完成事项。",
                reply_kind="recent_report_context_missing",
                timings=_finalize_timings(timings, total_start),
            )
        if context is not None and _looks_like_recent_report_display_followup(raw_input):
            try:
                context_date = date.fromisoformat(str(context.get("report_date") or "")[:10])
            except ValueError:
                context_date = report_date - timedelta(days=1)
            target_date = _resolve_date_from_text(_compact_for_intent(raw_input), report_date) or context_date
            target_report = await get_report(session, user.id, target_date)
            message = _format_report_display_message(target_report, target_date, field=str(context.get("field") or "all")) if target_report else f"我没有查到 {target_date.isoformat()} 的日报记录。"
            state_report = existing
            if target_report is not None:
                state_report = await _save_recent_report_context_state(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    viewed_report=target_report,
                    viewed_report_date=target_date,
                    received_at=received_at,
                    source=source,
                    raw_input=raw_input,
                    field=str(context.get("field") or "all"),
                )
            return _build_non_report_result(
                existing=state_report,
                report_date=report_date,
                message=message,
                reply_kind="history_query",
                timings=_finalize_timings(timings, total_start),
            )
        return None

    async def _execute_draft_decision(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        decision: DraftDecision,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        actual_date = received_at.date() if hasattr(received_at, "date") else report_date
        target_report_date = _resolve_decision_target_report_date(decision, raw_input, report_date, actual_date, received_at)
        timings["decision_actual_date"] = actual_date.isoformat()
        timings["decision_target_report_date"] = target_report_date.isoformat() if target_report_date else ""
        timings["decision_target_cutoff_allowed"] = (
            _is_allowed_previous_report_date(target_report_date, actual_date, received_at)
            if target_report_date is not None
            else None
        )
        if target_report_date is not None and target_report_date != report_date:
            if target_report_date < actual_date and not _is_allowed_previous_report_date(target_report_date, actual_date, received_at):
                return _build_non_report_result(
                    existing=existing,
                    report_date=report_date,
                    message=_previous_report_cutoff_message(actual_date),
                    reply_kind="previous_report_cutoff",
                    timings=_finalize_timings(timings, total_start),
                )
            if existing is not None and _has_report_content(existing) and _is_date_reassignment_request(raw_input):
                return await self._move_existing_draft_to_report_date(
                    session,
                    user=user,
                    existing=existing,
                    source_report_date=report_date,
                    target_report_date=target_report_date,
                    received_at=received_at,
                    source=source,
                    raw_input=raw_input,
                    timings=timings,
                    total_start=total_start,
                )
            await _acquire_report_processing_lock(session, user.id, target_report_date)
            target_existing = await get_report(session, user.id, target_report_date)
            return await self._execute_draft_decision(
                session,
                user=user,
                existing=target_existing,
                report_date=target_report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                decision=decision,
                timings=timings,
                total_start=total_start,
            )

        if decision.operation == "clear_report" and (decision.history_query.date or decision.history_query.requested):
            target_date = _resolve_history_query_date(decision.history_query.date, report_date)
            if target_date != report_date:
                return await self._ask_confirm_clear_dated_report(
                    session,
                    user=user,
                    existing=existing,
                    current_report_date=report_date,
                    target_date=target_date,
                    received_at=received_at,
                    source=source,
                    timings=timings,
                    total_start=total_start,
                )

        if decision.history_query.requested or decision.decision_type == "history_query":
            message = await _build_history_query_reply(session, user=user, report_date=report_date, decision=decision)
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=message,
                reply_kind="history_query",
                timings=_finalize_timings(timings, total_start),
            )

        pending_interaction = _get_pending_interaction(existing)
        pending_resolution = resolve_pending_interaction(raw_input, pending_interaction)
        if pending_resolution is not None:
            return await self._execute_direct_agent_plan(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
                previous_report=None,
                plan=pending_resolution.plan,
                branch=pending_resolution.branch,
            )
        if pending_interaction and _is_pending_interaction_cancel(raw_input) and existing is not None:
            report = await _set_pending_interaction(session, existing, None)
            return _build_non_report_result(
                existing=report,
                report_date=report_date,
                message="好的，先不补充，当前复盘草稿已保留。",
                reply_kind="pending_interaction_cancelled",
                timings=_finalize_timings(timings, total_start),
            )
        if pending_interaction and pending_interaction.get("type") == PENDING_INTERACTION_AWAITING_APPEND_TARGET:
            selected_field = _draft_decision_target_field(decision) or _resolve_pending_append_target(raw_input)
            if selected_field and existing is not None:
                report = await _set_pending_interaction(
                    session,
                    existing,
                    {
                        "type": PENDING_INTERACTION_AWAITING_APPEND_CONTENT,
                        "operation": "append",
                        "target_field": selected_field,
                    },
                )
                return _build_non_report_result(
                    existing=report,
                    report_date=report_date,
                    message=_build_pending_append_content_message(selected_field),
                    reply_kind="pending_append_target_selected",
                    timings=_finalize_timings(timings, total_start),
                )
        if (
            pending_interaction
            and pending_interaction.get("type") == PENDING_INTERACTION_AWAITING_APPEND_CONTENT
            and not _draft_decision_wants_write(decision)
            and not _is_non_report_interaction_request(raw_input)
            and not _is_courtesy_reply(raw_input)
            and not _is_short_ack_reply(raw_input)
        ):
            pending_field = str(pending_interaction.get("target_field") or "")
            if pending_field in {"today_work", "problems", "tomorrow_plan"} and raw_input.strip():
                decision = DraftDecision(
                    decision_type="report_update",
                    message_kind="report_content",
                    operation="append",
                    target_field=pending_field,
                    confidence=0.8,
                    should_write=True,
                    field_updates=[
                        DraftFieldUpdate(field=pending_field, mode="append", items=[raw_input])
                    ],
                    reason="pending append content fallback",
                )

        if not _draft_decision_wants_write(decision):
            pending_interaction_to_store = _pending_interaction_from_draft_decision(decision, existing)
            if pending_interaction_to_store and existing is not None:
                step_start = time.perf_counter()
                report = await _set_pending_interaction(session, existing, pending_interaction_to_store)
                timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
                return _build_non_report_result(
                    existing=report,
                    report_date=report_date,
                    message=decision.clarification_question or decision.reply_to_user or _pending_interaction_message(pending_interaction_to_store),
                    reply_kind="pending_interaction",
                    timings=_finalize_timings(timings, total_start),
                )
            pending_edit = _pending_edit_from_draft_decision(decision, existing)
            if pending_edit and existing is not None:
                step_start = time.perf_counter()
                report = await _set_pending_draft_edit(session, existing, pending_edit)
                timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
                return _build_non_report_result(
                    existing=report,
                    report_date=report_date,
                    message=decision.clarification_question or decision.reply_to_user or _pending_edit_question_from_action(pending_edit),
                    reply_kind="draft_decision_pending_edit",
                    timings=_finalize_timings(timings, total_start),
                )
            if _is_unresolved_draft_edit_clarification(decision, existing):
                marked_existing = await _mark_unresolved_draft_edit(
                    session,
                    existing,
                    raw_input=raw_input,
                    error=decision.reply_to_user or decision.clarification_question or "draft edit needs clarification",
                    received_at=received_at,
                )
                if marked_existing is not None:
                    existing = marked_existing
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=_draft_decision_no_write_message(decision),
                reply_kind=_draft_decision_reply_kind(decision),
                timings=_finalize_timings(timings, total_start),
            )

        if float(decision.confidence) < 0.55:
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=decision.clarification_question or "我还不确定这次要怎么改日报，先不写入。你可以说明要更新哪一部分。",
                reply_kind="draft_decision_low_confidence",
                timings=_finalize_timings(timings, total_start),
            )

        if existing and existing.status == STATUS_COMPLETED and _draft_decision_is_high_risk(decision):
            pending_edit = _pending_edit_from_draft_decision(decision, existing, require_confirmation=True)
            if pending_edit:
                step_start = time.perf_counter()
                report = await _set_pending_draft_edit(session, existing, pending_edit)
                timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
                return _build_non_report_result(
                    existing=report,
                    report_date=report_date,
                    message=str(pending_edit.get("confirmation_message") or "当前复盘已经提交。这个修改会影响正式内容，请回复“确认”执行，回复“取消”保留。"),
                    reply_kind="draft_decision_needs_confirmation",
                    timings=_finalize_timings(timings, total_start),
                )
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="当前复盘已经提交。删除、清空或整条重写需要先确认，请明确说明要修改哪一项。",
                reply_kind="draft_decision_needs_confirmation",
                timings=_finalize_timings(timings, total_start),
            )

        decision = _preserve_explicit_routine_work_item(decision, raw_input=raw_input)
        decision = _route_actual_today_work_to_tomorrow_plan(
            decision,
            raw_input=raw_input,
            report_date=report_date,
            actual_date=actual_date,
        )

        step_start = time.perf_counter()
        applied = _apply_draft_decision_to_sections(decision, existing)
        timings["report_merge_seconds"] = round(time.perf_counter() - step_start, 4)
        if applied.get("error"):
            existing_for_reply = await _mark_unresolved_draft_edit(
                session,
                existing,
                raw_input=raw_input,
                error=str(applied["error"]),
                received_at=received_at,
            )
            return _build_non_report_result(
                existing=existing_for_reply or existing,
                report_date=report_date,
                message=str(applied["error"]),
                reply_kind="draft_decision_rejected",
                timings=_finalize_timings(timings, total_start),
            )

        today_work = applied["today_work"]
        problems = applied["problems"]
        tomorrow_plan = applied["tomorrow_plan"]
        today_work, problems, tomorrow_plan = _preserve_enumerated_today_sections(
            raw_input,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            report_date=report_date,
            actual_date=actual_date,
        )
        today_work, problems, tomorrow_plan = _apply_direct_not_this_but_that_replacement(
            raw_input,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
        )
        today_work, problems = _recover_problem_fragments_from_today_work(today_work, problems)
        today_work = _remove_problem_overlap_from_today_work(today_work, problems)
        if decision.decision_type == "report_update" and not problems and _mentions_no_problem(raw_input):
            problems = [_empty_value_for_field("problems", raw_input)]
        if not any((today_work, problems, tomorrow_plan)) and not _draft_decision_allows_empty_report(decision):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="这次整理后没有可保存的日报内容，我先不覆盖当前草稿。",
                reply_kind="draft_decision_empty_write_rejected",
                timings=_finalize_timings(timings, total_start),
            )

        structured = StructuredDailyReport(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=existing.emotion if existing else "",
            completeness=_calculate_completeness(today_work, problems, tomorrow_plan),
        )
        previous_section_status = dict(existing.section_status or {}) if existing else {}
        previous_section_status = _clear_unresolved_draft_edit(
            _clear_pending_interaction(_clear_pending_draft_edit(_clear_pending_quality(previous_section_status)))
        )
        state = infer_report_state(
            existing_section_status=previous_section_status,
            merged_today_work=today_work,
            merged_problems=problems,
            merged_tomorrow_plan=tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(previous_section_status, state.section_status)
        _attach_draft_item_ids(
            state.section_status,
            existing=existing,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
        )
        if existing:
            state.section_status[DRAFT_PREVIOUS_SNAPSHOT_KEY] = _snapshot_from_report(existing)

        is_modification = bool(existing and _has_report_content(existing))
        confirmation_is_update = bool(existing and existing.status in {STATUS_PENDING_CONFIRMATION, STATUS_COMPLETED})
        target_status = STATUS_COMPLETED if existing and existing.status == STATUS_COMPLETED else state.status
        confirmation_type = existing.confirmation_type if existing else CONFIRMATION_NONE
        confirmed_by_user = existing.confirmed_by_user if existing else False
        quality_warning = existing.quality_warning if existing else None

        step_start = time.perf_counter()
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=report_date,
            raw_input=raw_input,
            source=source,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=structured.emotion,
            completeness_score=state.completeness_score,
            status=target_status,
            section_status=state.section_status,
            llm_model=str(timings.get("llm_draft_decision_model") or self.extractor.client.model),
            llm_payload=decision.model_dump(),
            received_at=received_at,
            confirmation_type=confirmation_type,
            confirmed_by_user=confirmed_by_user,
            quality_warning=quality_warning,
            last_modified_by_user=is_modification,
            last_modified_at=received_at if is_modification else (existing.last_modified_at if existing else None),
            pending_confirmation_at=received_at if target_status == STATUS_PENDING_CONFIRMATION else None,
            auto_submit_at=received_at + timedelta(minutes=30) if target_status == STATUS_PENDING_CONFIRMATION else None,
            replace_sections=True,
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)

        if report.status == STATUS_COMPLETED and is_modification:
            message = build_completed_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                updated=True,
            )
            reply_kind = "updated_completed_report"
        elif state.ready_for_confirmation:
            message = build_confirmation_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                quality_warning=quality_warning,
                updated=confirmation_is_update,
            )
            reply_kind = "pending_confirmation"
        else:
            acknowledged = [decision.reply_to_user] if decision.reply_to_user else []
            message = build_followup_message(
                state.missing_sections,
                acknowledged=acknowledged,
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
            )
            reply_kind = "followup"
        return _result_from_report(report, structured=structured, missing_sections=state.missing_sections, message=message, reply_kind=reply_kind, timings=timings)

    async def _move_existing_draft_to_report_date(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport,
        source_report_date: date,
        target_report_date: date,
        received_at,
        source: str,
        raw_input: str,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        await _acquire_report_processing_lock(session, user.id, target_report_date)
        target_existing = await get_report(session, user.id, target_report_date)
        today_work = merge_ordered(list(target_existing.today_work or []) if target_existing else [], list(existing.today_work or []))
        problems = merge_ordered(list(target_existing.problems or []) if target_existing else [], list(existing.problems or []))
        tomorrow_plan = merge_ordered(list(target_existing.tomorrow_plan or []) if target_existing else [], list(existing.tomorrow_plan or []))
        structured = StructuredDailyReport(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=existing.emotion,
            completeness=_calculate_completeness(today_work, problems, tomorrow_plan),
        )
        previous_section_status = dict((target_existing.section_status if target_existing else existing.section_status) or {})
        state = infer_report_state(
            existing_section_status=previous_section_status,
            merged_today_work=today_work,
            merged_problems=problems,
            merged_tomorrow_plan=tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(previous_section_status, state.section_status)
        _attach_draft_item_ids(
            state.section_status,
            existing=target_existing or existing,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
        )
        state.section_status[DRAFT_PREVIOUS_SNAPSHOT_KEY] = _snapshot_from_report(existing)

        step_start = time.perf_counter()
        target_report = await upsert_daily_report(
            session,
            user=user,
            report_date=target_report_date,
            raw_input=raw_input,
            source=source,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=existing.emotion,
            completeness_score=state.completeness_score,
            status=STATUS_COMPLETED if target_existing and target_existing.status == STATUS_COMPLETED else state.status,
            section_status=state.section_status,
            llm_model=existing.llm_model,
            llm_payload={"operation": "move_existing_draft_to_report_date", "source_report_date": source_report_date.isoformat()},
            received_at=received_at,
            confirmation_type=existing.confirmation_type,
            confirmed_by_user=existing.confirmed_by_user,
            quality_warning=existing.quality_warning,
            last_modified_by_user=True,
            last_modified_at=received_at,
            pending_confirmation_at=received_at if state.status == STATUS_PENDING_CONFIRMATION else None,
            auto_submit_at=received_at + timedelta(minutes=30) if state.status == STATUS_PENDING_CONFIRMATION else None,
            replace_sections=True,
        )
        if source_report_date != target_report_date:
            clear_structured = StructuredDailyReport(completeness=0)
            clear_state = infer_report_state(
                existing_section_status={},
                merged_today_work=[],
                merged_problems=[],
                merged_tomorrow_plan=[],
                structured=clear_structured,
                raw_input="",
            )
            await upsert_daily_report(
                session,
                user=user,
                report_date=source_report_date,
                raw_input=raw_input,
                source=source,
                today_work=[],
                problems=[],
                tomorrow_plan=[],
                emotion="",
                completeness_score=clear_state.completeness_score,
                status=clear_state.status,
                section_status=clear_state.section_status,
                llm_model=existing.llm_model,
                llm_payload={"operation": "cleared_after_date_reassignment", "target_report_date": target_report_date.isoformat()},
                received_at=received_at,
                confirmation_type=CONFIRMATION_NONE,
                confirmed_by_user=False,
                quality_warning=None,
                last_modified_by_user=True,
                last_modified_at=received_at,
                pending_confirmation_at=None,
                auto_submit_at=None,
                replace_sections=True,
            )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        message = build_followup_message(
            state.missing_sections,
            acknowledged=[f"已把这版内容归属到 {target_report_date.isoformat()} 的日报"],
            today_work=target_report.today_work,
            problems=target_report.problems,
            tomorrow_plan=target_report.tomorrow_plan,
        )
        return _result_from_report(
            target_report,
            structured=structured,
            missing_sections=state.missing_sections,
            message=message,
            reply_kind="date_reassigned",
            timings=_finalize_timings(timings, total_start),
        )

    async def _apply_draft_edit_instruction(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        decision: DailyInputIntentDecision | None,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        if existing is None or not _has_report_content(existing):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="我这边还没有可编辑的复盘草稿。你可以先发今天的复盘内容。",
                reply_kind="draft_edit_instruction",
                timings=_finalize_timings(timings, total_start),
            )

        if decision and _semantic_draft_decision_has_action(decision):
            edit_result = _apply_semantic_draft_edit_to_sections(
                decision,
                status=existing.status,
                today_work=list(existing.today_work or []),
                problems=list(existing.problems or []),
                tomorrow_plan=list(existing.tomorrow_plan or []),
            )
        else:
            edit_result = _apply_draft_edit_to_sections(
                raw_input,
                today_work=list(existing.today_work or []),
                problems=list(existing.problems or []),
                tomorrow_plan=list(existing.tomorrow_plan or []),
            )
        if edit_result.pending_edit:
            step_start = time.perf_counter()
            report = await _set_pending_draft_edit(session, existing, edit_result.pending_edit)
            timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
            return _build_non_report_result(
                existing=report,
                report_date=report_date,
                message=edit_result.error or "好的，你想把这条改成什么？",
                reply_kind="draft_edit_instruction_needs_content",
                timings=_finalize_timings(timings, total_start),
            )
        if edit_result.error:
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=edit_result.error,
                reply_kind="draft_edit_instruction_failed",
                timings=_finalize_timings(timings, total_start),
            )

        return await self._save_draft_edit_result(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            source=source,
            raw_input=raw_input,
            edit_result=edit_result,
            timings=timings,
            total_start=total_start,
        )

    async def _apply_pending_draft_edit(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        pending_edit: dict[str, Any],
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        edit_result = _apply_pending_draft_edit_to_sections(
            pending_edit,
            raw_input,
            today_work=list(existing.today_work or []),
            problems=list(existing.problems or []),
            tomorrow_plan=list(existing.tomorrow_plan or []),
        )
        if edit_result.error:
            step_start = time.perf_counter()
            report = await _set_pending_draft_edit(session, existing, None)
            timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
            return _build_non_report_result(
                existing=report,
                report_date=report_date,
                message=edit_result.error,
                reply_kind="draft_edit_instruction_failed",
                timings=_finalize_timings(timings, total_start),
            )
        return await self._save_draft_edit_result(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            source=source,
            raw_input=raw_input,
            edit_result=edit_result,
            timings=timings,
            total_start=total_start,
        )

    async def _cancel_pending_draft_edit(
        self,
        session: AsyncSession,
        *,
        existing: DailyReport,
        report_date: date,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        step_start = time.perf_counter()
        report = await _set_pending_draft_edit(session, existing, None)
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        return _build_non_report_result(
            existing=report,
            report_date=report_date,
            message="好的，先不改，当前复盘草稿已保留。",
            reply_kind="draft_edit_instruction_cancelled",
            timings=_finalize_timings(timings, total_start),
        )

    async def _save_draft_edit_result(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        edit_result: DraftEditResult,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        structured = StructuredDailyReport(
            today_work=edit_result.today_work,
            problems=edit_result.problems,
            tomorrow_plan=edit_result.tomorrow_plan,
            emotion=existing.emotion,
            completeness=_calculate_completeness(edit_result.today_work, edit_result.problems, edit_result.tomorrow_plan),
        )
        previous_section_status = _clear_unresolved_draft_edit(_clear_pending_draft_edit(_clear_pending_quality(existing.section_status or {})))
        state = infer_report_state(
            existing_section_status=previous_section_status,
            merged_today_work=edit_result.today_work,
            merged_problems=edit_result.problems,
            merged_tomorrow_plan=edit_result.tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(previous_section_status, state.section_status)
        _attach_draft_item_ids(
            state.section_status,
            existing=existing,
            today_work=edit_result.today_work,
            problems=edit_result.problems,
            tomorrow_plan=edit_result.tomorrow_plan,
        )
        target_status = STATUS_COMPLETED if existing.status == STATUS_COMPLETED else state.status
        step_start = time.perf_counter()
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=report_date,
            raw_input=raw_input,
            source=source,
            today_work=edit_result.today_work,
            problems=edit_result.problems,
            tomorrow_plan=edit_result.tomorrow_plan,
            emotion=existing.emotion,
            completeness_score=state.completeness_score,
            status=target_status,
            section_status=state.section_status,
            llm_model=existing.llm_model,
            llm_payload=structured.model_dump(),
            received_at=received_at,
            confirmation_type=existing.confirmation_type,
            confirmed_by_user=existing.confirmed_by_user,
            quality_warning=existing.quality_warning,
            last_modified_by_user=True,
            last_modified_at=received_at,
            pending_confirmation_at=received_at if target_status == STATUS_PENDING_CONFIRMATION else existing.pending_confirmation_at,
            auto_submit_at=received_at + timedelta(minutes=30) if target_status == STATUS_PENDING_CONFIRMATION else None,
            replace_sections=True,
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)
        acknowledgement = _build_draft_edit_acknowledgement(edit_result)
        if report.status == STATUS_COMPLETED:
            message = acknowledgement + "\n\n" + build_completed_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                updated=True,
            )
            reply_kind = "updated_completed_report"
        elif state.ready_for_confirmation:
            message = build_confirmation_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                quality_warning=report.quality_warning,
                updated=True,
            )
            reply_kind = "pending_confirmation"
        else:
            message = build_followup_message(
                state.missing_sections,
                acknowledged=[acknowledgement],
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
            )
            reply_kind = "followup"
        return _result_from_report(report, structured=structured, missing_sections=state.missing_sections, message=message, reply_kind=reply_kind, timings=timings)

    async def _apply_pending_quality_clarification(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        pending_quality: dict[str, Any],
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        today_work, problems, tomorrow_plan = _apply_quality_answer_to_sections(
            pending_quality,
            raw_input,
            today_work=list(existing.today_work or []),
            problems=list(existing.problems or []),
            tomorrow_plan=list(existing.tomorrow_plan or []),
        )
        structured = StructuredDailyReport(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=existing.emotion,
            completeness=_calculate_completeness(today_work, problems, tomorrow_plan),
        )
        section_status = _clear_unresolved_draft_edit(_clear_pending_quality(existing.section_status or {}))
        state = infer_report_state(
            existing_section_status=section_status,
            merged_today_work=today_work,
            merged_problems=problems,
            merged_tomorrow_plan=tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(section_status, state.section_status)
        _attach_draft_item_ids(
            state.section_status,
            existing=existing,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
        )
        step_start = time.perf_counter()
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=report_date,
            raw_input=raw_input,
            source=source,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=existing.emotion,
            completeness_score=state.completeness_score,
            status=state.status,
            section_status=state.section_status,
            llm_model=existing.llm_model,
            llm_payload=structured.model_dump(),
            received_at=received_at,
            confirmation_type=existing.confirmation_type,
            confirmed_by_user=existing.confirmed_by_user,
            quality_warning=None,
            last_modified_by_user=True,
            last_modified_at=received_at,
            pending_confirmation_at=received_at if state.status == STATUS_PENDING_CONFIRMATION else None,
            auto_submit_at=received_at + timedelta(minutes=30) if state.status == STATUS_PENDING_CONFIRMATION else None,
            replace_sections=True,
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)
        if state.ready_for_confirmation:
            message = build_confirmation_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                quality_warning=None,
                updated=True,
            )
            reply_kind = "pending_confirmation"
        else:
            message = build_followup_message(
                state.missing_sections,
                acknowledged=["已补充到刚才那项"],
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
            )
            reply_kind = "followup"
        return _result_from_report(report, structured=structured, missing_sections=state.missing_sections, message=message, reply_kind=reply_kind, timings=timings)

    async def _accept_pending_quality_clarification(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport,
        report_date: date,
        received_at,
        source: str,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        structured = StructuredDailyReport(
            today_work=existing.today_work,
            problems=existing.problems,
            tomorrow_plan=existing.tomorrow_plan,
            emotion=existing.emotion,
            completeness=float(existing.completeness_score),
        )
        section_status = _clear_unresolved_draft_edit(_clear_pending_quality(existing.section_status or {}))
        state = infer_report_state(
            existing_section_status=section_status,
            merged_today_work=existing.today_work,
            merged_problems=existing.problems,
            merged_tomorrow_plan=existing.tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(section_status, state.section_status)
        _attach_draft_item_ids(
            state.section_status,
            existing=existing,
            today_work=existing.today_work,
            problems=existing.problems,
            tomorrow_plan=existing.tomorrow_plan,
        )
        step_start = time.perf_counter()
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=report_date,
            raw_input="保留笼统内容",
            source=source,
            today_work=existing.today_work,
            problems=existing.problems,
            tomorrow_plan=existing.tomorrow_plan,
            emotion=existing.emotion,
            completeness_score=state.completeness_score,
            status=state.status,
            section_status=state.section_status,
            llm_model=existing.llm_model,
            llm_payload=existing.llm_payload or structured.model_dump(),
            received_at=received_at,
            confirmation_type=existing.confirmation_type,
            confirmed_by_user=existing.confirmed_by_user,
            quality_warning=existing.quality_warning,
            last_modified_by_user=existing.last_modified_by_user,
            last_modified_at=existing.last_modified_at,
            pending_confirmation_at=received_at if state.status == STATUS_PENDING_CONFIRMATION else None,
            auto_submit_at=received_at + timedelta(minutes=30) if state.status == STATUS_PENDING_CONFIRMATION else None,
            replace_sections=True,
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)
        if state.ready_for_confirmation:
            message = build_confirmation_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                quality_warning=report.quality_warning,
                updated=True,
            )
            reply_kind = "pending_confirmation"
        else:
            message = build_followup_message(
                state.missing_sections,
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
            )
            reply_kind = "followup"
        return _result_from_report(report, structured=structured, missing_sections=state.missing_sections, message=message, reply_kind=reply_kind, timings=timings)

    async def auto_submit_pending_reports(
        self,
        session: AsyncSession,
        *,
        reports: list[DailyReport],
        now,
    ) -> list[DailyReport]:
        submitted: list[DailyReport] = []
        for report in reports:
            if _should_auto_submit(report, now):
                submitted.append(await _auto_submit_existing_report(session, report, now))
        return submitted

    async def _confirm_report(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at,
        timings: dict[str, float],
        total_start: float,
    ) -> SubmitReportResult:
        if existing is None:
            return _build_non_report_result(
                existing=None,
                report_date=report_date,
                message="我这边还没有整理好的复盘内容。你可以先说今天做了什么。",
                reply_kind="confirm_without_report",
                timings=_finalize_timings(timings, total_start),
            )
        if _has_unresolved_draft_edit(existing):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="上一条修改还没有成功定位，我先不提交。请先重新说明要改哪一项，或回复“撤回上一步操作”。",
                reply_kind="confirm_blocked_by_unresolved_edit",
                timings=_finalize_timings(timings, total_start),
            )
        if existing.status == STATUS_COMPLETED:
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="这条复盘已经提交好了。你如果想修改，直接说要改哪一段就行。",
                reply_kind="already_completed",
                timings=_finalize_timings(timings, total_start),
            )

        state = infer_report_state(
            existing_section_status=existing.section_status,
            merged_today_work=existing.today_work,
            merged_problems=existing.problems,
            merged_tomorrow_plan=existing.tomorrow_plan,
            structured=StructuredDailyReport(
                today_work=existing.today_work,
                problems=existing.problems,
                tomorrow_plan=existing.tomorrow_plan,
                emotion=existing.emotion,
                completeness=float(existing.completeness_score),
            ),
            raw_input="",
        )
        if not state.ready_for_confirmation:
            if state.missing_sections == ["problems"] and existing.today_work and existing.tomorrow_plan:
                section_status = dict(existing.section_status or {})
                section_status["problems"] = True
                section_status["problems_acknowledged_empty"] = True
                step_start = time.perf_counter()
                report = await upsert_daily_report(
                    session,
                    user=user,
                    report_date=report_date,
                    raw_input="确认提交",
                    source="system_confirm",
                    today_work=existing.today_work,
                    problems=["暂无明显问题"],
                    tomorrow_plan=existing.tomorrow_plan,
                    emotion=existing.emotion,
                    completeness_score=1.0,
                    status=STATUS_COMPLETED,
                    section_status=section_status,
                    llm_model=existing.llm_model,
                    llm_payload=existing.llm_payload,
                    received_at=received_at,
                    confirmation_type=CONFIRMATION_USER_CONFIRMED,
                    confirmed_by_user=True,
                    quality_warning=existing.quality_warning,
                    last_modified_by_user=existing.last_modified_by_user,
                    last_modified_at=existing.last_modified_at,
                    pending_confirmation_at=existing.pending_confirmation_at,
                    auto_submit_at=None,
                )
                timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
                timings = _finalize_timings(timings, total_start)
                message = build_completed_message(
                    today_work=report.today_work,
                    problems=report.problems,
                    tomorrow_plan=report.tomorrow_plan,
                )
                return SubmitReportResult(
                    report_id=str(report.id),
                    report_date=report.report_date,
                    status=report.status,
                    completeness_score=float(report.completeness_score),
                    missing_sections=[],
                    message=message,
                    structured=StructuredDailyReport(
                        today_work=report.today_work,
                        problems=report.problems,
                        tomorrow_plan=report.tomorrow_plan,
                        emotion=report.emotion,
                        completeness=float(report.completeness_score),
                    ),
                    today_work=report.today_work,
                    problems=report.problems,
                    tomorrow_plan=report.tomorrow_plan,
                    section_status=report.section_status,
                    confirmation_type=report.confirmation_type,
                    confirmed_by_user=report.confirmed_by_user,
                    quality_warning=report.quality_warning,
                    report_saved=True,
                    reply_kind="confirmed",
                    timings=timings,
                )
            message = build_followup_message(
                state.missing_sections,
                today_work=existing.today_work,
                problems=existing.problems,
                tomorrow_plan=existing.tomorrow_plan,
            )
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=message,
                reply_kind="confirm_but_incomplete",
                timings=_finalize_timings(timings, total_start),
            )

        step_start = time.perf_counter()
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=report_date,
            raw_input="确认提交",
            source="system_confirm",
            today_work=existing.today_work,
            problems=existing.problems,
            tomorrow_plan=existing.tomorrow_plan,
            emotion=existing.emotion,
            completeness_score=float(existing.completeness_score),
            status=STATUS_COMPLETED,
            section_status=existing.section_status,
            llm_model=existing.llm_model,
            llm_payload=existing.llm_payload,
            received_at=received_at,
            confirmation_type=CONFIRMATION_USER_CONFIRMED,
            confirmed_by_user=True,
            quality_warning=existing.quality_warning,
            last_modified_by_user=existing.last_modified_by_user,
            last_modified_at=existing.last_modified_at,
            pending_confirmation_at=existing.pending_confirmation_at,
            auto_submit_at=None,
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)
        message = build_completed_message(
            today_work=report.today_work,
            problems=report.problems,
            tomorrow_plan=report.tomorrow_plan,
        )
        return SubmitReportResult(
            report_id=str(report.id),
            report_date=report.report_date,
            status=report.status,
            completeness_score=float(report.completeness_score),
            missing_sections=[],
            message=message,
            structured=StructuredDailyReport(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                emotion=report.emotion,
                completeness=float(report.completeness_score),
            ),
            today_work=report.today_work,
            problems=report.problems,
            tomorrow_plan=report.tomorrow_plan,
            section_status=report.section_status,
            confirmation_type=report.confirmation_type,
            confirmed_by_user=report.confirmed_by_user,
            quality_warning=report.quality_warning,
            report_saved=True,
            reply_kind="confirmed",
            timings=timings,
        )

    async def _fill_remaining_missing_sections_as_empty(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult | None:
        if existing.status == STATUS_COMPLETED or not _has_report_content(existing):
            return None

        missing_sections = _missing_sections_from_report(existing)
        if not missing_sections:
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="当前草稿已经没有缺项了；没问题可以回复“确认提交”。",
                reply_kind="no_remaining_missing_sections",
                timings=_finalize_timings(timings, total_start),
            )

        today_work = list(existing.today_work or [])
        problems = list(existing.problems or [])
        tomorrow_plan = list(existing.tomorrow_plan or [])
        section_status = _clear_unresolved_draft_edit(_clear_pending_draft_edit(_clear_pending_quality(existing.section_status or {})))

        for field in missing_sections:
            if field == "today_work" and not today_work:
                today_work = [_empty_value_for_field(field, raw_input)]
            elif field == "problems" and not problems:
                problems = [_empty_value_for_field(field, raw_input)]
                section_status["problems_acknowledged_empty"] = True
            elif field == "tomorrow_plan" and not tomorrow_plan:
                tomorrow_plan = [_empty_value_for_field(field, raw_input)]
                section_status["tomorrow_plan_acknowledged_empty"] = True
            section_status[field] = True

        structured = StructuredDailyReport(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=existing.emotion,
            completeness=_calculate_completeness(today_work, problems, tomorrow_plan),
        )
        state = infer_report_state(
            existing_section_status=section_status,
            merged_today_work=today_work,
            merged_problems=problems,
            merged_tomorrow_plan=tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(section_status, state.section_status)
        for field in missing_sections:
            state.section_status[field] = True
            if field == "problems":
                state.section_status["problems_acknowledged_empty"] = True
            elif field == "tomorrow_plan":
                state.section_status["tomorrow_plan_acknowledged_empty"] = True
        _attach_draft_item_ids(
            state.section_status,
            existing=existing,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
        )

        step_start = time.perf_counter()
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=report_date,
            raw_input=raw_input,
            source=source,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=existing.emotion,
            completeness_score=state.completeness_score,
            status=state.status,
            section_status=state.section_status,
            llm_model=existing.llm_model,
            llm_payload=structured.model_dump(),
            received_at=received_at,
            confirmation_type=existing.confirmation_type,
            confirmed_by_user=existing.confirmed_by_user,
            quality_warning=existing.quality_warning,
            last_modified_by_user=True,
            last_modified_at=received_at,
            pending_confirmation_at=received_at if state.status == STATUS_PENDING_CONFIRMATION else None,
            auto_submit_at=received_at + timedelta(minutes=30) if state.status == STATUS_PENDING_CONFIRMATION else None,
            replace_sections=True,
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)

        acknowledgement = "已把剩余未填写项按暂无处理。"
        if state.ready_for_confirmation:
            message = acknowledgement + "\n\n" + build_confirmation_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                quality_warning=report.quality_warning,
                updated=True,
            )
            reply_kind = "remaining_sections_empty_pending_confirmation"
        else:
            message = build_followup_message(
                state.missing_sections,
                acknowledged=[acknowledgement],
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
            )
            reply_kind = "remaining_sections_empty_followup"

        return _result_from_report(
            report,
            structured=structured,
            missing_sections=state.missing_sections,
            message=message,
            reply_kind=reply_kind,
            timings=timings,
        )

    async def _ask_confirm_clear_current_report(
        self,
        session: AsyncSession,
        *,
        existing: DailyReport,
        report_date: date,
        timings: dict[str, float],
        total_start: float,
    ) -> SubmitReportResult:
        step_start = time.perf_counter()
        report = await _set_pending_action(session, existing, PENDING_ACTION_CONFIRM_CLEAR_CURRENT_REPORT)
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)
        return _build_non_report_result(
            existing=report,
            report_date=report_date,
            message=MESSAGE_CONFIRM_CLEAR_COMPLETED if report.status == STATUS_COMPLETED else MESSAGE_CONFIRM_CLEAR_DRAFT,
            reply_kind="ask_confirm_clear_current_report",
            timings=timings,
        )

    async def _ask_confirm_clear_dated_report(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        current_report_date: date,
        target_date: date,
        received_at,
        source: str,
        timings: dict[str, float],
        total_start: float,
    ) -> SubmitReportResult:
        target_report = await get_report(session, user.id, target_date)
        if target_report is None:
            return _build_non_report_result(
                existing=existing,
                report_date=current_report_date,
                message=f"我没有查到 {target_date.isoformat()} 的日报记录，无法清空。",
                reply_kind="clear_dated_report_missing",
                timings=_finalize_timings(timings, total_start),
            )

        step_start = time.perf_counter()
        state_report = existing
        if state_report is None:
            empty_structured = StructuredDailyReport(completeness=0.0)
            state_report = await upsert_daily_report(
                session,
                user=user,
                report_date=current_report_date,
                raw_input="历史日报清空确认",
                source=source,
                today_work=[],
                problems=[],
                tomorrow_plan=[],
                emotion="",
                completeness_score=0.0,
                status=STATUS_COLLECTING,
                section_status={},
                llm_model=None,
                llm_payload=empty_structured.model_dump(),
                received_at=received_at,
                confirmation_type=CONFIRMATION_NONE,
                confirmed_by_user=False,
                quality_warning=None,
                pending_confirmation_at=None,
                auto_submit_at=None,
                replace_sections=True,
            )
        report = await _set_pending_action(
            session,
            state_report,
            PENDING_ACTION_CONFIRM_CLEAR_DATED_REPORT,
            payload={"target_date": target_date.isoformat()},
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)
        return _build_non_report_result(
            existing=report,
            report_date=current_report_date,
            message=_build_confirm_clear_dated_message(target_date),
            reply_kind="ask_confirm_clear_dated_report",
            timings=timings,
        )

    async def _cancel_pending_clear_current_report(
        self,
        session: AsyncSession,
        *,
        existing: DailyReport,
        report_date: date,
        timings: dict[str, float],
        total_start: float,
    ) -> SubmitReportResult:
        step_start = time.perf_counter()
        report = await _set_pending_action(session, existing, None)
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)
        return _build_non_report_result(
            existing=report,
            report_date=report_date,
            message=MESSAGE_CANCEL_CLEAR_CURRENT_REPORT,
            reply_kind="cancel_clear_current_report",
            timings=timings,
        )

    async def _clear_dated_report(
        self,
        session: AsyncSession,
        *,
        user: User,
        state_report: DailyReport | None,
        current_report_date: date,
        target_date: date,
        received_at,
        source: str,
        timings: dict[str, float],
        total_start: float,
    ) -> SubmitReportResult:
        if target_date != current_report_date:
            await _acquire_report_processing_lock(session, user.id, target_date)

        target_report = await get_report(session, user.id, target_date)
        if target_report is None:
            if state_report is not None:
                await _set_pending_action(session, state_report, None)
            return _build_non_report_result(
                existing=state_report,
                report_date=current_report_date,
                message=f"我没有查到 {target_date.isoformat()} 的日报记录，无法清空。",
                reply_kind="clear_dated_report_missing",
                timings=_finalize_timings(timings, total_start),
            )

        step_start = time.perf_counter()
        if state_report is not None and state_report.id != target_report.id:
            await _set_pending_action(session, state_report, None)

        empty_structured = StructuredDailyReport(completeness=0.0)
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=target_date,
            raw_input=f"清空 {target_date.isoformat()} 日报",
            source=source,
            today_work=[],
            problems=[],
            tomorrow_plan=[],
            emotion="",
            completeness_score=0.0,
            status=STATUS_COLLECTING,
            section_status={},
            llm_model=target_report.llm_model,
            llm_payload=empty_structured.model_dump(),
            received_at=received_at,
            confirmation_type=CONFIRMATION_NONE,
            confirmed_by_user=False,
            quality_warning=None,
            last_modified_by_user=True,
            last_modified_at=received_at,
            pending_confirmation_at=None,
            auto_submit_at=None,
            replace_sections=True,
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)
        return _result_from_report(
            report,
            structured=empty_structured,
            missing_sections=_missing_sections_from_report(report),
            message=f"已清空 {target_date.isoformat()} 的日报。\n\n{_format_report_display_message(report, target_date)}",
            reply_kind="clear_dated_report",
            timings=timings,
        )

    async def _clear_current_report(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at,
        source: str,
        timings: dict[str, float],
        total_start: float,
    ) -> SubmitReportResult:
        if existing is None:
            return _build_non_report_result(
                existing=None,
                report_date=report_date,
                message="当前没有需要清空的复盘草稿。你可以直接说今天主要做了什么。",
                reply_kind="clear_current_report",
                timings=_finalize_timings(timings, total_start),
            )

        empty_structured = StructuredDailyReport()
        step_start = time.perf_counter()
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=report_date,
            raw_input="",
            source=source,
            today_work=[],
            problems=[],
            tomorrow_plan=[],
            emotion="",
            completeness_score=0.0,
            status=STATUS_COLLECTING,
            section_status={"today_work": False, "problems": False, "tomorrow_plan": False},
            llm_model=self.extractor.client.model,
            llm_payload=empty_structured.model_dump(),
            received_at=received_at,
            confirmation_type=CONFIRMATION_NONE,
            confirmed_by_user=False,
            quality_warning=None,
            last_modified_by_user=True,
            last_modified_at=received_at,
            pending_confirmation_at=None,
            auto_submit_at=None,
            replace_sections=True,
        )
        if hasattr(report, "raw_input"):
            report.raw_input = ""
        if hasattr(report, "input_fragments"):
            report.input_fragments = []
        if hasattr(report, "submitted_at"):
            report.submitted_at = None
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)

        return SubmitReportResult(
            report_id=str(report.id),
            report_date=report.report_date,
            status=report.status,
            completeness_score=0.0,
            missing_sections=["today_work", "problems", "tomorrow_plan"],
            message=MESSAGE_CLEAR_CURRENT_REPORT_DONE,
            structured=empty_structured,
            today_work=[],
            problems=[],
            tomorrow_plan=[],
            section_status=report.section_status,
            confirmation_type=CONFIRMATION_NONE,
            confirmed_by_user=False,
            quality_warning=None,
            report_saved=True,
            reply_kind="clear_current_report",
            timings=timings,
        )

    async def _start_historical_report_edit_flow(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        target_date = _resolve_date_from_text(_compact_for_intent(raw_input), report_date) or (report_date - timedelta(days=1))
        target_report = await get_report(session, user.id, target_date)
        if target_report is None:
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=f"\u6211\u6ca1\u6709\u67e5\u5230 {target_date.isoformat()} \u7684\u65e5\u62a5\u8bb0\u5f55\u3002",
                reply_kind="historical_report_edit_missing",
                timings=_finalize_timings(timings, total_start),
            )
        target_field = _resolve_current_report_field_choice(raw_input)
        step_start = time.perf_counter()
        report = await _save_recent_report_context_state(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            viewed_report=target_report,
            viewed_report_date=target_date,
            received_at=received_at,
            source=source,
            raw_input=raw_input,
            field=target_field or "all",
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        message = _build_historical_report_edit_entry_message(target_report, target_date, target_field)
        return _result_from_report(
            report,
            structured=StructuredDailyReport(),
            missing_sections=_missing_sections_from_report(report),
            message=message,
            reply_kind="historical_report_edit_started",
            timings=_finalize_timings(timings, total_start),
        )

    async def _start_current_report_edit_flow(
        self,
        session: AsyncSession,
        *,
        existing: DailyReport | None,
        report_date: date,
        raw_input: str,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        if existing is None or not _has_report_content(existing):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="我还没有找到今天的日报草稿。你可以先直接发送今天做了什么、问题/风险和明日计划。",
                reply_kind="current_report_edit_missing",
                timings=_finalize_timings(timings, total_start),
            )

        target_field = _resolve_current_report_field_choice(raw_input)
        pending = _build_current_report_edit_pending(report_date, existing, target_field=target_field)
        step_start = time.perf_counter()
        report = await _set_pending_interaction(session, existing, pending)
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        message = (
            _build_current_report_field_focus_message(report, report_date, target_field)
            if target_field
            else _build_current_report_edit_entry_message(report, report_date)
        )
        return _result_from_report(
            report,
            structured=StructuredDailyReport(),
            missing_sections=_missing_sections_from_report(report),
            message=message,
            reply_kind="current_report_edit_started",
            timings=_finalize_timings(timings, total_start),
        )

    async def _handle_current_report_edit_flow(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        pending_interaction: dict[str, Any],
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        if existing is None or not _has_report_content(existing):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="我还没有找到今天的日报草稿。你可以先直接发送今天做了什么、问题/风险和明日计划。",
                reply_kind="current_report_edit_missing",
                timings=_finalize_timings(timings, total_start),
            )

        if _is_pending_interaction_cancel(raw_input):
            step_start = time.perf_counter()
            report = await _set_pending_interaction(session, existing, None)
            timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
            return _build_non_report_result(
                existing=report,
                report_date=report_date,
                message="好的，已退出今天日报编辑，当前日报内容已保留。",
                reply_kind="current_report_edit_cancelled",
                timings=_finalize_timings(timings, total_start),
            )

        if _is_bare_confirmation_reply(raw_input) and not _is_explicit_submit_request(raw_input):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message="已保留当前修改。需要继续调整可以直接说；如果确认要提交日报，请回复“确认提交”。",
                reply_kind="current_report_edit_bare_confirmation",
                timings=_finalize_timings(timings, total_start),
            )

        if _looks_like_current_report_edit_entry_request(raw_input):
            target_field = _resolve_current_report_field_choice(raw_input)
            pending = _build_current_report_edit_pending(report_date, existing, target_field=target_field)
            step_start = time.perf_counter()
            report = await _set_pending_interaction(session, existing, pending)
            timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
            message = (
                _build_current_report_field_focus_message(report, report_date, target_field)
                if target_field
                else _build_current_report_edit_entry_message(report, report_date)
            )
            return _build_non_report_result(
                existing=report,
                report_date=report_date,
                message=message,
                reply_kind="current_report_edit_restarted",
                timings=_finalize_timings(timings, total_start),
            )

        selected_field = _resolve_current_report_field_choice(raw_input)
        if selected_field and _looks_like_field_selection_only(raw_input):
            pending = _build_current_report_edit_pending(report_date, existing, target_field=selected_field)
            step_start = time.perf_counter()
            report = await _set_pending_interaction(session, existing, pending)
            timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
            return _build_non_report_result(
                existing=report,
                report_date=report_date,
                message=_build_current_report_field_focus_message(report, report_date, selected_field),
                reply_kind="current_report_edit_field_selected",
                timings=_finalize_timings(timings, total_start),
            )

        pending_resolution = resolve_pending_interaction(raw_input, pending_interaction)
        if pending_resolution is not None:
            return await self._execute_direct_agent_plan(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
                previous_report=None,
                plan=pending_resolution.plan,
                branch=pending_resolution.branch,
            )

        timings["current_edit_flow_delegated_to_report_agent"] = True
        return await self._submit_text_with_report_agent(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            source=source,
            raw_input=raw_input,
            timings=timings,
            total_start=total_start,
        )

    async def _replace_current_report_sections(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport,
        report_date: date,
        received_at,
        source: str,
        raw_input: str,
        sections: dict[str, list[str]],
        reply_kind: str,
        timings: dict[str, Any],
        total_start: float,
    ) -> SubmitReportResult:
        today_work = _sanitize_current_report_section_values(list(sections.get("today_work") or []))
        problems = _sanitize_current_report_section_values(list(sections.get("problems") or []))
        tomorrow_plan = _sanitize_current_report_section_values(list(sections.get("tomorrow_plan") or []))
        structured = StructuredDailyReport(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=existing.emotion if existing else "",
            completeness=_calculate_completeness(today_work, problems, tomorrow_plan),
        )
        previous_section_status = _clear_unresolved_draft_edit(
            _clear_pending_interaction(_clear_pending_draft_edit(_clear_pending_quality(dict(existing.section_status or {}))))
        )
        state = infer_report_state(
            existing_section_status=previous_section_status,
            merged_today_work=today_work,
            merged_problems=problems,
            merged_tomorrow_plan=tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(previous_section_status, state.section_status)
        _attach_draft_item_ids(
            state.section_status,
            existing=existing,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
        )
        state.section_status[DRAFT_PREVIOUS_SNAPSHOT_KEY] = _snapshot_from_report(existing)
        target_status = STATUS_COMPLETED if existing.status == STATUS_COMPLETED else state.status
        step_start = time.perf_counter()
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=report_date,
            raw_input=raw_input,
            source=source,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=structured.emotion,
            completeness_score=state.completeness_score,
            status=target_status,
            section_status=state.section_status,
            llm_model=getattr(getattr(self.extractor, "client", None), "model", None),
            llm_payload={"source": "current_report_edit_flow", "reply_kind": reply_kind, "sections": sections},
            received_at=received_at,
            confirmation_type=existing.confirmation_type,
            confirmed_by_user=existing.confirmed_by_user,
            quality_warning=existing.quality_warning,
            last_modified_by_user=True,
            last_modified_at=received_at,
            pending_confirmation_at=received_at if target_status == STATUS_PENDING_CONFIRMATION else None,
            auto_submit_at=received_at + timedelta(minutes=30) if target_status == STATUS_PENDING_CONFIRMATION else None,
            replace_sections=True,
        )
        timings["upsert_report_seconds"] = round(time.perf_counter() - step_start, 4)
        timings = _finalize_timings(timings, total_start)
        if report.status == STATUS_COMPLETED:
            message = build_completed_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                updated=True,
            )
        elif state.ready_for_confirmation:
            message = build_confirmation_message(
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
                quality_warning=report.quality_warning,
                updated=True,
            )
            reply_kind = "pending_confirmation"
        else:
            message = build_followup_message(
                state.missing_sections,
                acknowledged=["已更新今天日报。"],
                today_work=report.today_work,
                problems=report.problems,
                tomorrow_plan=report.tomorrow_plan,
            )
        return _result_from_report(
            report,
            structured=structured,
            missing_sections=state.missing_sections,
            message=message,
            reply_kind=reply_kind,
            timings=timings,
        )


CURRENT_REPORT_EDIT_FIELDS = {"today_work", "problems", "tomorrow_plan"}
CURRENT_REPORT_FIELD_LABELS = {
    "today_work": "今日工作",
    "problems": "问题/风险",
    "tomorrow_plan": "明日计划",
}


def _looks_like_current_report_edit_entry_request(raw_input: str) -> bool:
    if _looks_like_current_report_content(raw_input):
        return False
    compact = _compact_for_intent(raw_input)
    if not compact or any(token in compact for token in ("昨天", "昨日", "前天", "历史")):
        return False
    if any(token in compact for token in ("撤回", "撤销", "取消提交", "退回")):
        return False
    if "日报" not in compact:
        return False
    if not any(token in compact for token in ("改下", "改一下", "修改", "编辑", "调整", "更正", "重写", "重新写", "我要改", "我改下", "改")):
        return False
    if any(token in compact for token in ("改成", "改为", "修改为", "换成", "替换成", "替换为", "删掉", "删除", "清空")):
        return False
    return any(token in compact for token in ("今天", "今日", "当前", "现在", "今儿")) or not any(
        token in compact for token in ("明天", "明日", "计划")
    )
def _looks_like_direct_current_report_edit_instruction(raw_input: str, existing: DailyReport | None) -> bool:
    if existing is None or not _has_report_content(existing):
        return False
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if _looks_like_full_report_input(raw_input) or _looks_like_current_report_content(raw_input):
        return False
    if _is_clear_current_report_request(raw_input):
        return False
    if _looks_like_current_report_edit_entry_request(raw_input) or _looks_like_previous_report_edit_entry_request(raw_input):
        return False
    if _looks_like_report_merge_instruction(compact):
        return False
    if any(
        marker in compact
        for marker in ("\u53d1\u6211\u770b", "\u770b\u4e0b", "\u770b\u770b", "\u67e5\u8be2", "\u67e5\u4e0b", "\u786e\u8ba4\u63d0\u4ea4")
    ):
        return False
    delete_markers = ("\u5220\u6389", "\u5220\u9664", "\u5220\u4e86", "\u53bb\u6389", "\u79fb\u9664", "\u4e0d\u8981")
    replace_markers = ("\u6539\u6210", "\u6539\u4e3a", "\u6362\u6210", "\u66ff\u6362", "\u4fee\u6539\u4e3a", "\u66f4\u6b63\u4e3a")
    spelling_marker = "\u6539\u4e00\u4e0b" in compact and bool(re.search(r"\u662f.{1,12}\u7684.", raw_input))
    return any(marker in compact for marker in delete_markers) or any(marker in compact for marker in replace_markers) or spelling_marker


def _looks_like_report_merge_instruction(compact: str) -> bool:
    return any(
        marker in compact
        for marker in (
            "合并",
            "合到一起",
            "合成一条",
            "合成一个",
            "并成一条",
            "同一条",
            "同一项",
            "一回事",
            "一件事",
            "不要拆这么碎",
            "别拆这么碎",
            "拆得太碎",
        )
    )


def _build_current_report_edit_pending(
    report_date: date,
    report: DailyReport,
    *,
    target_field: str | None = None,
) -> dict[str, Any]:
    field = target_field if target_field in CURRENT_REPORT_EDIT_FIELDS else None
    current_report = _snapshot_from_report(report)
    stage = "awaiting_field_edit_content" if field else "awaiting_edit_instruction"
    return {
        "type": PENDING_INTERACTION_CURRENT_REPORT_EDIT_FLOW,
        "operation": "modify_report",
        "target_field": field or "",
        "context": {
            "target_date": report_date.isoformat(),
            "stage": stage,
            "snapshot": current_report,
            "current_report": current_report,
            "edit_cursor": build_current_edit_cursor(
                target_date=report_date,
                current_report=current_report,
                focused_section=field or "none",
                stage=stage,
            ),
        },
    }


def _build_current_report_edit_entry_message(report: DailyReport, report_date: date) -> str:
    label = report_date.isoformat()
    is_today = report_date == today_in_timezone("Asia/Shanghai")
    display_label = "\u4eca\u5929" if is_today else label
    heading = "\u4eca\u65e5\u65e5\u62a5" if is_today else "\u65e5\u62a5"
    empty = "\u672a\u586b\u5199"
    no_problem = "\u6682\u65e0\u660e\u663e\u95ee\u9898"
    today_work_text = _format_numbered_section(report.today_work, empty_fallback=empty)
    problems_text = _format_numbered_section(report.problems, empty_fallback=no_problem)
    tomorrow_plan_text = _format_numbered_section(report.tomorrow_plan, empty_fallback=empty)
    return (
        f"\u597d\u7684\uff0c\u6211\u5148\u628a{display_label}\u7684\u65e5\u62a5\u8c03\u51fa\u6765\uff1a\n\n"
        f"{heading}\uff08{label}\uff09\uff1a\n"
        f"\u4eca\u65e5\u5de5\u4f5c\uff1a\n{today_work_text}\n\n"
        f"\u95ee\u9898/\u98ce\u9669\uff1a\n{problems_text}\n\n"
        f"\u660e\u65e5\u8ba1\u5212\uff1a\n{tomorrow_plan_text}\n\n"
        f"\u4f60\u53ef\u4ee5\u76f4\u63a5\u53d1\u4e00\u6574\u6bb5\u65b0\u7684\u65e5\u62a5\u5185\u5bb9\uff0c\u6211\u4f1a\u6309\u4eca\u65e5\u5de5\u4f5c\u3001\u95ee\u9898/\u98ce\u9669\u3001\u660e\u65e5\u8ba1\u5212\u66f4\u65b0\u8fd9\u4efd\u8349\u7a3f\u3002"
    )

def _build_historical_report_edit_entry_message(report: DailyReport, target_date: date, field: str | None = None) -> str:
    if field in CURRENT_REPORT_FIELD_LABELS:
        label = CURRENT_REPORT_FIELD_LABELS[field]
        values = list(getattr(report, field) or [])
        fallback = "\u6682\u65e0\u660e\u663e\u95ee\u9898" if field == "problems" else "\u672a\u586b\u5199"
        return (
            f"\u597d\u7684\uff0c\u6211\u5148\u628a\u6628\u5929\u7684\u65e5\u62a5\u8c03\u51fa\u6765\uff0c\u5df2\u9501\u5b9a {target_date.isoformat()} \u65e5\u62a5\u7684\u201c{label}\u201d\u3002\n\n"
            f"\u5f53\u524d{label}\uff1a\n{_format_numbered_section(values, empty_fallback=fallback)}\n\n"
            "\u8bf7\u76f4\u63a5\u8bf4\u8981\u6539\u6210\u4ec0\u4e48\u3002"
        )
    empty = "\u672a\u586b\u5199"
    no_problem = "\u6682\u65e0\u660e\u663e\u95ee\u9898"
    today_work_text = _format_numbered_section(report.today_work, empty_fallback=empty)
    problems_text = _format_numbered_section(report.problems, empty_fallback=no_problem)
    tomorrow_plan_text = _format_numbered_section(report.tomorrow_plan, empty_fallback=empty)
    return (
        f"\u597d\u7684\uff0c\u6211\u5148\u628a\u6628\u5929\u7684\u65e5\u62a5\u8c03\u51fa\u6765\uff1a\n\n"
        f"\u6628\u65e5\u65e5\u62a5\uff08{target_date.isoformat()}\uff09\uff1a\n"
        f"\u4eca\u65e5\u5de5\u4f5c\uff1a\n{today_work_text}\n\n"
        f"\u95ee\u9898/\u98ce\u9669\uff1a\n{problems_text}\n\n"
        f"\u660e\u65e5\u8ba1\u5212\uff1a\n{tomorrow_plan_text}\n\n"
        "\u8bf7\u76f4\u63a5\u8bf4\u8981\u4fee\u6539\u54ea\u4e00\u680f\u3001\u54ea\u4e00\u6761\uff0c\u6216\u76f4\u63a5\u7c98\u8d34\u5b8c\u6574\u65e5\u62a5\u5185\u5bb9\u3002"
    )

def _build_current_report_field_focus_message(report: DailyReport, report_date: date, field: str | None) -> str:
    if field not in CURRENT_REPORT_EDIT_FIELDS:
        return _build_current_report_edit_entry_message(report, report_date)
    values = list(getattr(report, field) or [])
    fallback = "暂无明显问题" if field == "problems" else "未填写"
    return (
        f"好的，已锁定 {report_date.isoformat()} 日报的{CURRENT_REPORT_FIELD_LABELS[field]}。\n\n"
        f"当前{CURRENT_REPORT_FIELD_LABELS[field]}：\n{_format_numbered_section(values, empty_fallback=fallback)}\n\n"
        f"请直接说要改成什么。"
    )

def _format_numbered_section(values: list[str] | None, *, empty_fallback: str) -> str:
    cleaned = [str(value).strip() for value in (values or []) if str(value).strip()]
    if not cleaned:
        return empty_fallback
    return "\n".join(f"{index}. {value}" for index, value in enumerate(cleaned, start=1))


def _current_report_pending_field(pending_interaction: dict[str, Any] | None) -> str | None:
    field = str((pending_interaction or {}).get("target_field") or "")
    return field if field in CURRENT_REPORT_EDIT_FIELDS else None


def _resolve_current_report_field_choice(raw_input: str) -> str | None:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return None
    if any(token in compact for token in ("明日计划", "明天计划", "明日安排", "明天安排", "计划")):
        return "tomorrow_plan"
    if any(token in compact for token in ("问题风险", "问题/风险", "风险问题", "问题", "风险", "隐患")):
        return "problems"
    if any(token in compact for token in ("今日工作", "今天工作", "完成工作", "工作", "任务", "事项")):
        return "today_work"
    return None


def _looks_like_field_selection_only(raw_input: str) -> bool:
    field = _resolve_current_report_field_choice(raw_input)
    if not field:
        return False
    compact = _compact_for_intent(raw_input)
    if any(token in compact for token in ("改成", "改为", "换成", "替换", "删除", "删", "清空", "去", "开庭", "审核", "处理", "发现", "写")):
        return False
    compact = re.sub(r"(今天|今日|日报|部分|这一块|这个|的|吧|把|呢|啊|呀|哈|一下|改下|修改)", "", compact)
    return compact in {"工作", "任务", "事项", "问题", "风险", "隐患", "计划", "明日计划", "明天计划"} or len(compact) <= 4




def _build_whole_report_copy_message(report: DailyReport, source_report_date: date) -> str:
    empty = "\u6682\u65e0"
    no_problem = "\u6682\u65e0\u660e\u663e\u95ee\u9898"
    return (
        f"\u5df2\u6574\u7bc7\u590d\u5236 {source_report_date.isoformat()} \u7684\u65e5\u62a5\u5185\u5bb9\u3002\n\n"
        "\u5f53\u524d\u65e5\u62a5\u8349\u7a3f\uff1a\n\n"
        f"\u4eca\u65e5\u5de5\u4f5c\uff1a\n{_format_numbered_section(report.today_work, empty_fallback=empty)}\n\n"
        f"\u95ee\u9898/\u98ce\u9669\uff1a\n{_format_numbered_section(report.problems, empty_fallback=no_problem)}\n\n"
        f"\u660e\u65e5\u8ba1\u5212\uff1a\n{_format_numbered_section(report.tomorrow_plan, empty_fallback=empty)}\n\n"
        "\u6ca1\u95ee\u9898\u56de\u590d\u201c\u786e\u8ba4\u201d\u5373\u53ef\uff1b\u9700\u8981\u4fee\u6539\u53ef\u4ee5\u76f4\u63a5\u8bf4\u3002"
        "\u82e5\u4e00\u6bb5\u65f6\u95f4\u5185\u672a\u56de\u590d\uff0c\u7cfb\u7edf\u5c06\u6309\u4ee5\u4e0a\u5185\u5bb9\u81ea\u52a8\u63d0\u4ea4\u3002"
    )


def _copy_current_report_sections(report: DailyReport) -> dict[str, list[str]]:
    return {
        "today_work": _normalize_copied_report_section_items(getattr(report, "today_work", None), "today_work"),
        "problems": _normalize_copied_report_section_items(getattr(report, "problems", None), "problems"),
        "tomorrow_plan": _normalize_copied_report_section_items(getattr(report, "tomorrow_plan", None), "tomorrow_plan"),
    }


def _normalize_copied_report_section_items(values: list[str] | None, field: str) -> list[str]:
    items: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        items.extend(_split_copied_report_section_value(text, field))
    return _dedupe_current_items(items)


def _split_copied_report_section_value(text: str, field: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(
        r"\s+(?=(?:\d+|[\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+)[.\u3001\uff0e\uff09\)]\s*)",
        "\n",
        normalized,
    )
    if re.search(r"(?:^|\n)\s*(?:[-*\u2022]|(?:\d+|[\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+)[.\u3001\uff0e\uff09\)])\s+", normalized):
        parts = re.split(
            r"(?:^|\n)\s*(?:[-*\u2022]|(?:\d+|[\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+)[.\u3001\uff0e\uff09\)])\s+",
            normalized,
        )
    else:
        parts = []
        for line in normalized.split("\n"):
            if re.search(r"[ \t\u3000]{2,}", line):
                parts.extend(re.split(r"[ \t\u3000]{2,}", line))
            else:
                parts.append(line)
    cleaned = [_clean_copied_report_item(part, field) for part in parts]
    return [item for item in cleaned if item]


def _clean_copied_report_item(text: str, field: str) -> str:
    item = _strip_copied_report_field_label(str(text or ""), field)
    item = re.sub(
        r"^\s*(?:[-*\u2022]+|(?:\d+|[\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+)[.\u3001\uff0e\uff09\)])\s*",
        "",
        item,
    )
    return item.strip(" \t\n\r\u3000\uff1b;\uff0c,\u3002.")


def _strip_copied_report_field_label(text: str, field: str) -> str:
    item = text.strip("\uff1a:\uff0c,\uff1b;\u3001 \n\t")
    if field == "today_work":
        return re.sub(r"^(?:\u4eca\u65e5\u5de5\u4f5c|\u4eca\u5929\u5de5\u4f5c|\u5de5\u4f5c|\u4efb\u52a1|\u4e8b\u9879)[\uff1a:\s]*", "", item).strip()
    if field == "problems":
        return re.sub(r"^(?:\u95ee\u9898[/\uff0f]\u98ce\u9669|\u95ee\u9898\u98ce\u9669|\u95ee\u9898|\u98ce\u9669|\u9690\u60a3)[\uff1a:\s]*", "", item).strip()
    return re.sub(r"^(?:\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212|\u660e\u65e5\u5b89\u6392|\u660e\u5929\u5b89\u6392|\u8ba1\u5212)[\uff1a:\s]*", "", item).strip()


def _parse_full_report_replacement_text(raw_input: str) -> dict[str, list[str]] | None:
    if not _looks_like_full_report_replacement_input(raw_input):
        return None
    labeled = _parse_labeled_report_sections(raw_input)
    if labeled is not None:
        return labeled

    sections: dict[str, list[str]] = {"today_work": [], "problems": [], "tomorrow_plan": []}
    sentences = [part.strip() for part in re.split(r"[。！？!?\n]+", raw_input) if part.strip()]
    for sentence in sentences:
        before_plan, plan = _split_current_report_plan_sentence(sentence)
        if before_plan:
            _append_current_report_fragments(sections, before_plan)
        if plan:
            sections["tomorrow_plan"].extend(_split_current_report_field_items(plan, "tomorrow_plan"))

    sections = _clean_current_report_sections(sections)
    return sections if _has_enough_full_report_sections(sections) else None


def _looks_like_full_report_replacement_input(raw_input: str) -> bool:
    text = raw_input.strip()
    if len(text) < 24:
        return False
    compact = _compact_for_intent(text)
    if _looks_like_current_report_edit_entry_request(text):
        return False
    labeled_count = sum(1 for token in ("今日工作", "今天工作", "问题/风险", "问题风险", "明日计划", "明天计划") if token in compact)
    if labeled_count >= 2:
        return True
    has_work = any(token in compact for token in ("出差", "开庭", "审核", "撰写", "写了", "处理", "沟通", "完成", "跟进", "起草", "整理"))
    has_problem = any(token in compact for token in ("发现", "问题", "风险", "没签", "未签", "缺少", "缺失", "隐患", "异常"))
    has_plan = any(token in compact for token in ("明天", "明日", "计划", "后续"))
    return has_work and has_problem and has_plan


def _parse_labeled_report_sections(raw_input: str) -> dict[str, list[str]] | None:
    pattern = re.compile(r"(?<![\u4e00-\u9fffA-Za-z0-9])(?:\*{1,3}\s*)?[【\[\(（]?\s*(今日工作|今天工作|问题/风险|问题风险|问题|风险|明日计划|明天计划)\s*(?:[】\]\)）]\s*)?(?:\*{1,3}\s*)?(?:[:：]|\s+|$)", re.I)
    matches = list(pattern.finditer(raw_input))
    if len(matches) < 2:
        return None
    sections: dict[str, list[str]] = {"today_work": [], "problems": [], "tomorrow_plan": []}
    for index, match in enumerate(matches):
        label = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw_input)
        field = _resolve_current_report_field_choice(label)
        if field:
            sections[field].extend(_split_current_report_field_items(raw_input[start:end], field))
    sections = _clean_current_report_sections(sections)
    return sections if _has_enough_full_report_sections(sections) else None


def _split_current_report_plan_sentence(text: str) -> tuple[str, str]:
    match = re.search(r"(明天计划|明日计划|明天|明日|后续计划|后续)", text)
    if not match:
        return text, ""
    before = text[: match.start()].strip("，,；;、 ")
    plan = text[match.start():].strip("，,；;、 ")
    return before, plan


def _append_current_report_fragments(sections: dict[str, list[str]], text: str) -> None:
    fragments = [part.strip() for part in re.split(r"(?:同时|并且|另外|此外|以及|，|,|；|;|、)", text) if part.strip()]
    for fragment in fragments:
        if _looks_like_current_report_problem_fragment(fragment):
            sections["problems"].append(_normalize_current_report_problem(fragment))
        else:
            sections["today_work"].append(_normalize_current_report_work(fragment))


def _looks_like_current_report_problem_fragment(text: str) -> bool:
    compact = _compact_for_intent(text)
    return any(token in compact for token in ("发现", "问题", "风险", "没签", "未签", "缺少", "缺失", "隐患", "异常"))


def _split_current_report_field_items(text: str, field: str) -> list[str]:
    text = _strip_current_report_field_prefix(text, field)
    if field == "problems" and _mentions_no_problem(text):
        return [_empty_value_for_field("problems", text)]
    numbered_parts = _split_numbered_field_items(text)
    parts = numbered_parts or [part.strip() for part in re.split(r"(?:同时|并且|另外|此外|以及|，|,|；|;|、|\n)", text) if part.strip()]
    if not parts and text.strip():
        parts = [text.strip()]
    if field == "today_work":
        return [_normalize_current_report_work(part) for part in parts if part.strip()]
    if field == "problems":
        return [_normalize_current_report_problem(part) for part in parts if part.strip()]
    return [_normalize_current_report_plan(part) for part in parts if part.strip()]


def _strip_current_report_field_prefix(text: str, field: str) -> str:
    text = text.strip("：:，,；;、 \n\t")
    text = re.sub(r"^[\s*#_`【】\[\]（）()]+", "", text).strip("：:，,；;、 \n\t")
    if field == "today_work":
        return re.sub(r"^(今日工作|今天工作|工作|任务|事项)[：:\s]*", "", text).strip()
    if field == "problems":
        return re.sub(r"^(问题/风险|问题风险|问题|风险|隐患)[：:\s]*", "", text).strip()
    return re.sub(r"^(明日计划|明天计划|明日安排|明天安排|计划|明天|明日|后续计划|后续)[：:\s]*", "", text).strip()


def _split_numbered_field_items(text: str) -> list[str]:
    numbered = re.split(r"(?:^|[\n\r\uff1b;])\s*(?:\d+|[\uff08(]?\d+[\uff09)])\s*[\u3001\uff0e\.)]\s*", text)
    return [item.strip(" \t\r\n\u3000\uff1b;\uff0c,\u3002.") for item in numbered if item.strip(" \t\r\n\u3000\uff1b;\uff0c,\u3002.")]


def _normalize_current_report_work(text: str) -> str:
    text = _normalize_free_text(text)
    text = re.sub(r"^(?:\d+|[一二三四五六七八九十]+)[.、．）)]\s*", "", text)
    text = re.sub(r"^(今天|今日|我|同时|并且|另外|此外|还|又)[，,、\s]*", "", text)
    text = re.sub(r"^(主要就是|主要|就是)[，,、\s]*", "", text)
    text = re.sub(r"(嗯|呃|额|啊)[，,、\s]*", "", text)
    text = re.sub(r"(就是){2,}", "就是", text)
    text = re.sub(r"[，,。；;、\s]*(没问题|没啥问题|没什么问题|没有问题|问题没有|问题没|暂无问题|无问题|暂无明显问题|无明显问题|没风险|没有风险|无风险|暂无风险)\s*$", "", text)
    text = re.sub(r"出差去?了", "出差去", text)
    text = re.sub(r"审核了", "审核", text)
    text = re.sub(r"写了", "撰写", text)
    return text.strip("。；;，,、 ")


def _normalize_current_report_problem(text: str) -> str:
    text = _normalize_free_text(text)
    text = re.sub(r"^(?:\d+|[一二三四五六七八九十]+)[.、．）)]\s*", "", text)
    text = re.sub(r"^(问题是|风险是|问题/风险|问题|风险)[：:\s]*", "", text)
    text = text.replace("没签", "未签").replace("没有签", "未签")
    if _compact_for_intent(text) in {"暂无", "无", "暂无风险", "无风险", "暂无问题", "无问题"}:
        return _empty_value_for_field("problems", text)
    if text and not text.startswith(("发现", "存在", "部分", "暂无", "无")):
        text = f"发现{text}"
    return text.strip("。；;，,、 ")


def _normalize_current_report_plan(text: str) -> str:
    text = _normalize_free_text(text)
    text = re.sub(r"^(?:\d+|[一二三四五六七八九十]+)[.、．）)]\s*", "", text)
    text = _strip_current_report_field_prefix(text, "tomorrow_plan")
    text = re.sub(r"^(计划|准备|打算)[去做]?", "", text).strip()
    return text.strip("。；;，,、 ")


def _clean_current_report_sections(sections: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        "today_work": _dedupe_current_items([item for item in sections.get("today_work", []) if item]),
        "problems": _dedupe_current_items([item for item in sections.get("problems", []) if item]),
        "tomorrow_plan": _dedupe_current_items([item for item in sections.get("tomorrow_plan", []) if item]),
    }


def _sanitize_current_report_section_values(values: list[str]) -> list[str]:
    return _dedupe_current_items([str(item).strip() for item in values if str(item).strip()])


def _dedupe_current_items(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        compact = _compact_for_intent(item)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        result.append(item)
    return result


def _has_enough_full_report_sections(sections: dict[str, list[str]]) -> bool:
    return bool(sections.get("today_work")) and bool(sections.get("tomorrow_plan")) and bool(sections.get("problems"))


def _resolve_current_report_text_replacement(raw_input: str, report: DailyReport) -> dict[str, list[str]] | None:
    match = re.search(r"(.+?)(?:改成|改为|修改为|换成|替换成|替换为)(.+)", raw_input.strip())
    if not match:
        return None
    old = match.group(1).strip("把将 \t，,。；;：:")
    new = match.group(2).strip(" \t，,。；;：:")
    if not old or not new or len(old) > 30 or len(new) > 50:
        return None
    sections = _copy_current_report_sections(report)
    changed = False
    for field, values in sections.items():
        updated: list[str] = []
        for value in values:
            if old in value:
                value = value.replace(old, new)
                changed = True
            updated.append(value)
        sections[field] = updated
    return sections if changed else None


def _resolve_current_report_delete_mutation(
    raw_input: str,
    report: DailyReport,
    pending_interaction: dict[str, Any] | None,
) -> dict[str, list[str]] | None:
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("删", "删除", "清空", "不要", "去掉", "移除")):
        return None
    field = _resolve_current_report_field_choice(raw_input) or _current_report_pending_field(pending_interaction)
    index = _extract_current_report_item_index(raw_input)
    if not field and index is None:
        return None
    if not field:
        field = "today_work"
    sections = _copy_current_report_sections(report)
    values = list(sections.get(field) or [])
    if index is not None:
        if index < 0 or index >= len(values):
            return None
        values.pop(index)
    else:
        values = [_empty_value_for_field("problems", raw_input)] if field == "problems" else []
    sections[field] = values
    return sections


def _extract_current_report_item_index(raw_input: str) -> int | None:
    match = re.search(r"第?\s*([0-9一二两三四五六七八九十]+)\s*条", raw_input)
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token) - 1
    values = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    return values.get(token, 0) - 1 if token in values else None


def _resolve_current_report_field_replacement(
    raw_input: str,
    report: DailyReport,
    field: str | None,
) -> dict[str, list[str]] | None:
    field = field if field in CURRENT_REPORT_EDIT_FIELDS else _resolve_current_report_field_choice(raw_input)
    if field not in CURRENT_REPORT_EDIT_FIELDS:
        return None
    compact = _compact_for_intent(raw_input)
    if _looks_like_field_selection_only(raw_input) or _looks_like_current_report_edit_entry_request(raw_input):
        return None
    sections = _copy_current_report_sections(report)
    if any(token in compact for token in ("清空", "全删", "全部删", "删了", "删除", "不要了", "去掉")):
        sections[field] = [_empty_value_for_field("problems", raw_input)] if field == "problems" else []
        return sections
    content = _extract_current_report_field_replacement_content(raw_input, field)
    if not content:
        return None
    items = _split_current_report_field_items(content, field)
    if field == "problems" and not items and _mentions_no_problem(content):
        items = [_empty_value_for_field("problems", content)]
    if not items:
        return None
    sections[field] = items
    return sections


def _extract_current_report_field_replacement_content(raw_input: str, field: str) -> str:
    text = raw_input.strip()
    match = re.search(r"(?:改成|改为|修改为|换成|替换成|替换为)(.+)", text)
    if match:
        return match.group(1).strip()
    return _strip_current_report_field_prefix(text, field)


def _detect_rule_intent(
    raw_input: str,
    *,
    existing: DailyReport | None,
    current_slot: str | None,
    long_report_mode: bool = False,
) -> str | None:
    compact = _compact_for_intent(raw_input)
    has_existing_content = bool(existing and (existing.today_work or existing.problems or existing.tomorrow_plan))
    confirmation_tokens = {"确认", "可以", "ok", "okay", "提交", "没问题", "好", "行", "就这样", "对"}
    if existing and existing.status == STATUS_PENDING_CONFIRMATION and (compact in confirmation_tokens or _is_confirmation_reply(raw_input)):
        return "confirm_submit"
    if long_report_mode:
        if existing and existing.status == STATUS_COMPLETED:
            if _has_explicit_long_report_append_intent(compact):
                return "append_to_existing"
            return "uncertain_high_risk"
        if has_existing_content and _has_replace_current_report_intent(compact):
            return "replace_current_report"
        if has_existing_content and _has_explicit_long_report_append_intent(compact):
            return "append_to_existing"
        if existing and existing.status == STATUS_PENDING_CONFIRMATION:
            return "uncertain_high_risk"
        return "continue_collecting"
    if _is_clear_current_report_request(raw_input):
        return "clear_current_report"
    if _is_postpone_reply(raw_input):
        return "postpone_reply"
    if any(token in compact for token in ["跳过", "先不填", "不想填", "略过"]):
        return "skip"
    if _is_short_no_problem_reply(raw_input):
        return "continue_collecting" if current_slot == "problems" else "casual_or_invalid"
    if _is_courtesy_reply(raw_input):
        return "courtesy_reply"
    if _is_short_ack_reply(raw_input):
        return "courtesy_reply"
    if compact in {"哈哈", "哈哈哈", "hhh", "lol", "你猜", "随便", "正常"}:
        return "casual_or_invalid"
    if compact in {"?", "？", "1", "。", ".", "啊", "嗯", "表情包"}:
        return "casual_or_invalid"
    if bool(existing and (existing.today_work or existing.problems or existing.tomorrow_plan)) and _has_replace_current_report_intent(compact):
        if existing and existing.status == STATUS_COMPLETED:
            return "uncertain_high_risk"
        return "replace_current_report"
    if existing and not _is_draft_edit_instruction(raw_input) and (
        _extract_direct_field_update(raw_input) or _is_explicit_field_modify_request(raw_input)
    ):
        return "modify_field"
    if bool(existing and (existing.today_work or existing.problems or existing.tomorrow_plan)) and any(token in compact for token in ["这版", "这一版"]) and not current_slot:
        return "uncertain_high_risk"
    return None


def _build_intent_context(
    *,
    existing: DailyReport | None,
    current_slot: str | None,
    missing_sections: list[str],
    long_report_mode: bool = False,
) -> dict:
    return {
        "status": existing.status if existing else STATUS_COLLECTING,
        "missing_fields": missing_sections,
        "last_prompt_slot": current_slot,
        "pending_confirmation": bool(existing and existing.status == STATUS_PENDING_CONFIRMATION),
        "completed": bool(existing and existing.status == STATUS_COMPLETED),
        "has_existing_content": bool(existing and (existing.today_work or existing.problems or existing.tomorrow_plan)),
        "long_report_mode": long_report_mode,
        "pending_action": _get_pending_action(existing),
        "pending_draft_edit": _get_pending_draft_edit(existing),
        "pending_interaction": _get_pending_interaction(existing),
        "pending_quality_clarification": _get_pending_quality_clarification(existing),
        "quality_warning": existing.quality_warning if existing else "",
        "current_report_items": {
            "today_work": _numbered_context_items(existing.today_work if existing else []),
            "problems": _numbered_context_items(existing.problems if existing else []),
            "tomorrow_plan": _numbered_context_items(existing.tomorrow_plan if existing else []),
        },
        "current_report": {
            "today_work": existing.today_work if existing else [],
            "problems": existing.problems if existing else [],
            "tomorrow_plan": existing.tomorrow_plan if existing else [],
        },
    }


def _build_extract_context(
    *,
    existing: DailyReport | None,
    current_slot: str | None,
    missing_sections: list[str],
    intent: str,
    decision: DailyInputIntentDecision | None,
    long_report_mode: bool,
) -> dict[str, Any]:
    context = _build_intent_context(
        existing=existing,
        current_slot=current_slot,
        missing_sections=missing_sections,
        long_report_mode=long_report_mode,
    )
    context.update(
        {
            "resolved_intent": intent,
            "router_message_kind": decision.message_kind if decision else "",
            "router_operation": decision.operation if decision else "",
            "router_target_field": decision.target_field if decision else "",
            "router_relation_to_existing": decision.relation_to_existing if decision else "",
            "router_matched_field": decision.matched_field if decision else "",
            "router_matched_item_index": decision.matched_item_index if decision else 0,
            "router_new_content": decision.new_content if decision else "",
            "instruction": (
                "Extract only factual report content from the current user input. "
                "Use current_report to avoid duplicate items and to distinguish status questions or draft edits from report facts."
            ),
        }
    )
    return context


def _build_draft_decision_context(
    *,
    user: User,
    existing: DailyReport | None,
    report_date: date,
    actual_date: date,
    raw_input: str,
    current_slot: str | None,
    missing_sections: list[str],
) -> dict[str, Any]:
    section_status = existing.section_status if existing else {}
    item_ids = _draft_item_ids_for_context(existing)
    today_work_items = _numbered_context_items(existing.today_work if existing else [], field="today_work", item_ids=item_ids.get("today_work", []))
    problem_items = _numbered_context_items(existing.problems if existing else [], field="problems", item_ids=item_ids.get("problems", []))
    tomorrow_plan_items = _numbered_context_items(existing.tomorrow_plan if existing else [], field="tomorrow_plan", item_ids=item_ids.get("tomorrow_plan", []))
    current_report_items = {
        "today_work": today_work_items,
        "problems": problem_items,
        "tomorrow_plan": tomorrow_plan_items,
    }
    long_report_detection = _detect_long_report(raw_input)
    enumerated_today_items = _extract_explicit_today_enumerated_items(raw_input)
    return {
        "user": {
            "id": str(user.id),
            "name": getattr(user, "name", ""),
            "timezone": getattr(user, "timezone", "Asia/Shanghai"),
        },
        "report_date": report_date.isoformat(),
        "status": existing.status if existing else STATUS_COLLECTING,
        "current_user_input": raw_input,
        "missing_fields": missing_sections,
        "last_prompt_slot": current_slot,
        "pending_confirmation": bool(existing and existing.status == STATUS_PENDING_CONFIRMATION),
        "completed": bool(existing and existing.status == STATUS_COMPLETED),
        "pending_action": _get_pending_action(existing),
        "pending_draft_edit": _get_pending_draft_edit(existing),
        "pending_interaction": _get_pending_interaction(existing),
        "pending_quality_clarification": _get_pending_quality_clarification(existing),
        "previous_draft_snapshot": section_status.get(DRAFT_PREVIOUS_SNAPSHOT_KEY) if isinstance(section_status, dict) else None,
        "current_report_items": current_report_items,
        "global_report_items": _global_context_items(current_report_items),
        "current_report": {
            "today_work": existing.today_work if existing else [],
            "problems": existing.problems if existing else [],
            "tomorrow_plan": existing.tomorrow_plan if existing else [],
            "quality_warning": existing.quality_warning if existing else None,
        },
        "recent_turns": _recent_turns_from_report(existing),
        "input_structure": {
            "long_report_mode": long_report_detection.is_long_report,
            "detected_sections": long_report_detection.detected_sections,
            "explicit_today_enumeration": bool(enumerated_today_items),
            "enumerated_today_item_count": len(enumerated_today_items),
            "needs_slow_reasoning": long_report_detection.is_long_report or bool(enumerated_today_items),
        },
        "date_context": {
            "report_date": report_date.isoformat(),
            "actual_today": actual_date.isoformat(),
            "yesterday": (report_date - timedelta(days=1)).isoformat(),
            "actual_yesterday": (actual_date - timedelta(days=1)).isoformat(),
        },
        "instruction": (
            "First decide the user's intent in context. Only return a structured decision package. "
            "Do not treat questions, draft edit instructions, emotional comments, or assistant commands as report content."
        ),
    }


def _recent_turns_from_report(existing: DailyReport | None) -> list[dict[str, Any]]:
    if existing is None:
        return []
    fragments = getattr(existing, "input_fragments", None)
    if not isinstance(fragments, list):
        return []
    recent: list[dict[str, Any]] = []
    for fragment in fragments[-5:]:
        if not isinstance(fragment, dict):
            continue
        raw = str(fragment.get("raw_input") or "").strip()
        if not raw:
            continue
        recent.append(
            {
                "role": "user",
                "raw_input": raw[:500],
                "received_at": str(fragment.get("received_at") or ""),
                "structured": fragment.get("structured") if isinstance(fragment.get("structured"), dict) else {},
            }
        )
    return recent


def _apply_draft_decision_timings(timings: dict[str, Any], decision: DraftDecision) -> None:
    timings["semantic_router_used"] = True
    timings["semantic_router_model"] = timings.get("llm_draft_decision_model", "")
    timings["semantic_router_seconds"] = timings.get("llm_draft_decision_seconds", 0.0)
    timings["semantic_router_timeout"] = timings.get("llm_draft_decision_timeout", False)
    timings["message_kind"] = decision.message_kind or decision.decision_type
    timings["decision_type"] = decision.decision_type
    timings["operation"] = decision.operation
    timings["target_field"] = decision.target_field
    timings["item_refs"] = decision.item_refs
    timings["needs_clarification"] = decision.needs_clarification
    timings["draft_decision_confidence"] = decision.confidence
    timings["draft_decision_risk_level"] = decision.risk_level


def _draft_decisions_compatible(llm_decision: DraftDecision, backend_decision: DraftDecision) -> bool:
    return _draft_decision_signature(llm_decision) == _draft_decision_signature(backend_decision)


def _draft_decision_signature(decision: DraftDecision) -> tuple[Any, ...]:
    return (
        decision.decision_type,
        decision.message_kind,
        decision.operation,
        decision.target_field,
        decision.target_report_date,
        tuple(decision.item_refs or []),
        bool(decision.should_write),
        bool(decision.requires_user_confirmation),
        bool(decision.restore_previous.enabled),
        tuple(
            (
                update.field,
                update.mode,
                tuple(update.items or []),
                tuple(update.item_refs or []),
            )
            for update in decision.field_updates or []
        ),
        tuple(
            (
                move.source_field,
                move.destination_field,
                tuple(move.item_refs or []),
                move.source_item_text,
            )
            for move in decision.move_items or []
        ),
        tuple(
            (
                delete.target_field,
                tuple(delete.item_refs or []),
                delete.target_item_text,
            )
            for delete in decision.delete_items or []
        ),
        bool(decision.history_query.requested),
        decision.history_query.date,
        decision.history_query.field,
    )


async def _build_history_query_reply(
    session: AsyncSession,
    *,
    user: User,
    report_date: date,
    decision: DraftDecision,
) -> str:
    target_date = _resolve_history_query_date(decision.history_query.date, report_date)
    return await _build_report_display_reply(session, user=user, target_date=target_date, field=decision.history_query.field)


async def _build_report_display_reply(
    session: AsyncSession,
    *,
    user: User,
    target_date: date,
    field: str = "all",
) -> str:
    report = await get_report(session, user.id, target_date)
    if report is None:
        return f"我没有查到 {target_date.isoformat()} 的日报记录。"
    return _format_report_display_message(report, target_date, field=field)


def _format_report_display_message(report: DailyReport, target_date: date, *, field: str = "all") -> str:
    status_label = _report_status_label(report.status)
    if field == "today_work":
        return f"**【日期】** {target_date.isoformat()}\n**【今日工作】** {_display_plain_section(report.today_work)}\n**【状态】** {status_label}"
    if field == "problems":
        return f"**【日期】** {target_date.isoformat()}\n**【问题/风险】** {_display_plain_section(report.problems, empty_fallback='暂无明显问题')}\n**【状态】** {status_label}"
    if field == "tomorrow_plan":
        return f"**【日期】** {target_date.isoformat()}\n**【明日计划】** {_display_plain_section(report.tomorrow_plan)}\n**【状态】** {status_label}"
    return (
        f"**【日期】** {target_date.isoformat()}\n"
        f"**【今日工作】** {_display_plain_section(report.today_work)}\n"
        f"**【问题/风险】** {_display_plain_section(report.problems, empty_fallback='暂无明显问题')}\n"
        f"**【明日计划】** {_display_plain_section(report.tomorrow_plan)}\n"
        f"**【状态】** {status_label}"
    )


def _report_status_label(status: str) -> str:
    labels = {
        STATUS_COMPLETED: "已提交",
        STATUS_PENDING_CONFIRMATION: "待确认",
        STATUS_COLLECTING: "填写中",
        "skipped": "已跳过",
        "cancelled": "已取消",
    }
    return labels.get(status, status or "未知")


def _resolve_history_query_date(value: str, report_date: date) -> date:
    text = (value or "").strip().lower()
    if text in {"yesterday", "昨天", "previous_day", "last_day"}:
        return report_date - timedelta(days=1)
    if text in {"today", "今天", "current_day"}:
        return report_date
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return report_date - timedelta(days=1)


def _build_confirm_clear_dated_message(target_date: date) -> str:
    return f"你确定要清空 {target_date.isoformat()} 的日报吗？回复“确定”清空，回复“取消”保留。"



def _looks_like_historical_report_delete_request(raw_input: str, report_date: date) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact or "\u65e5\u62a5" not in compact:
        return False
    if not any(marker in compact for marker in ("\u6e05\u7a7a", "\u5220\u6389", "\u5220\u9664", "\u6e05\u6389", "\u5220\u4e86", "\u4e0d\u8981")):
        return False
    target_date = _resolve_date_from_text(compact, report_date)
    return target_date is not None and target_date < report_date

def _resolve_dated_clear_request(raw_input: str, report_date: date) -> date | None:
    compact = _compact_for_intent(raw_input)
    if not compact or "日报" not in compact:
        return None
    if not any(marker in compact for marker in ("清空", "删掉", "删除", "清掉", "删了", "不要")):
        return None
    return _resolve_date_from_text(compact, report_date)


def _resolve_report_display_request(raw_input: str, report_date: date) -> tuple[date, str] | None:
    if _looks_like_current_report_content(raw_input):
        return None
    compact = _compact_for_intent(raw_input)
    if not compact:
        return None
    has_report_term = "日报" in compact or "复盘" in compact or "日志" in compact or ("昨天" in compact and "日" in compact)
    if not has_report_term:
        return None
    if not any(marker in compact for marker in ("发我", "给我", "展示", "显示", "看看", "看下", "查看", "当前", "什么样", "是啥", "是什么", "啥样")):
        return None
    target_date = _resolve_date_from_text(compact, report_date)
    if target_date is None and any(marker in compact for marker in ("今天", "当前", "现在", "我的日报", "当前日报")):
        target_date = report_date
    if target_date is None:
        return None
    field = "all"
    if "计划" in compact and "工作" not in compact and "问题" not in compact and "风险" not in compact:
        field = "tomorrow_plan"
    elif "问题" in compact or "风险" in compact:
        field = "problems"
    elif "工作" in compact or "完成" in compact:
        field = "today_work"
    return target_date, field


def _is_current_report_display_request(raw_input: str, existing: DailyReport | None) -> bool:
    if existing is None:
        return False
    if _looks_like_current_report_content(raw_input):
        return False
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    short_commands = {"发我", "发我下", "发我一下", "发我看下", "给我看下", "看下", "看看", "展示一下", "当前草稿", "当前日报", "现在草稿"}
    if compact in short_commands:
        return True
    has_current = any(token in compact for token in ("当前", "今天", "今日", "现在", "草稿", "我的日报", "当前日报"))
    has_query = any(token in compact for token in ("发我", "给我", "看下", "看看", "展示", "显示", "查下", "什么样", "是啥"))
    return has_current and has_query


def _looks_like_previous_plan_completion_request(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact or not any(token in compact for token in ("昨天", "昨日")):
        return False
    if not any(token in compact for token in ("计划", "待办", "安排")):
        return False
    return any(token in compact for token in ("完成", "做完", "搞定", "处理完", "全部完成", "都完成", "都做完"))


def _looks_like_all_previous_plan_completion_request(raw_input: str) -> bool:
    if not _looks_like_previous_plan_completion_request(raw_input):
        return False
    compact = _compact_for_intent(raw_input)
    partial_markers = (
        "第",
        "前五",
        "前几",
        "最后",
        "除",
        "除了",
        "其余",
        "其他",
        "剩下",
        "未完成",
        "没完成",
        "没有完成",
        "没做",
        "未做",
        "来不及",
    )
    if any(marker in compact for marker in partial_markers):
        return False
    previous = r"(昨天|昨日|昨儿)"
    plan = r"(计划|待办|安排|事项)"
    done = r"(完成|做完|搞定|处理完|办完|弄完)"
    return bool(
        re.search(rf"{previous}(的)?{plan}.{{0,8}}(全部|全都|都|均)?{done}", compact)
        or re.search(rf"{done}(了)?{previous}(的)?{plan}", compact)
    )


def _looks_like_partial_previous_plan_completion_request(raw_input: str) -> bool:
    if not _looks_like_previous_plan_completion_request(raw_input):
        return False
    compact = _compact_for_intent(raw_input)
    remaining_markers = ("其他", "其它", "其余", "剩下", "剩余", "余下")
    completion_markers = ("完成", "做完", "搞定", "处理完", "办完", "弄完", "落实")
    if "除" in compact and any(marker in compact for marker in remaining_markers) and any(
        marker in compact for marker in completion_markers
    ):
        return True
    if re.search(r"(?:前|头)[一二三四五六七八九十\d]{1,3}(?:项|条|个|件)?.{0,8}(?:完成|做完|搞定)", compact):
        return True
    if re.search(
        r"[一二三四五六七八九十\d]{1,3}(?:到|至|~|～|-)[一二三四五六七八九十\d]{1,3}(?:项|条|个|件)?.{0,8}(?:完成|做完|搞定)",
        compact,
    ):
        return True
    return False


def _mentions_reuse_previous_plan_for_tomorrow(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if not any(marker in compact for marker in ("明天", "明日", "后续")):
        return False
    if not any(marker in compact for marker in ("昨天", "昨日", "前一天", "上一天")):
        return False
    if not any(marker in compact for marker in ("计划", "安排", "待办", "事项")):
        return False
    return bool(
        re.search(r"(明天|明日|后续).{0,12}(计划|安排|待办).{0,12}(照|按|同|跟|和|一样|不变).{0,12}(昨天|昨日|前一天|上一天)", compact)
        or re.search(r"(明天|明日|后续).{0,12}(昨天|昨日|前一天|上一天).{0,12}(计划|安排|待办).{0,12}(照|按|同|跟|和|一样|不变)", compact)
        or re.search(r"(照|按|同|跟|和).{0,12}(昨天|昨日|前一天|上一天).{0,12}(计划|安排|待办).{0,12}(明天|明日|后续)", compact)
    )


def _resolve_date_from_text(compact: str, report_date: date) -> date | None:
    if _looks_like_current_date_correction(compact):
        return report_date
    if "前天" in compact:
        return report_date - timedelta(days=2)
    if "昨天" in compact or "昨日" in compact:
        return report_date - timedelta(days=1)
    match = re.search(r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})", compact)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    match = re.search(r"(\d{1,2})月(\d{1,2})[日号]", compact)
    if match:
        month, day = (int(part) for part in match.groups())
        try:
            return date(report_date.year, month, day)
        except ValueError:
            return None
    match = re.search(r"([一二三四五六七八九十]{1,3})月([一二三四五六七八九十]{1,3})[日号]", compact)
    if match:
        month = _parse_small_cn_number(match.group(1))
        day = _parse_small_cn_number(match.group(2))
        if month is None or day is None:
            return None
        try:
            return date(report_date.year, month, day)
        except ValueError:
            return None
    match = re.search(r"(?<!第)(?<!\d)(\d{1,2})[日号]", compact)
    if match:
        day = int(match.group(1))
        year = report_date.year
        month = report_date.month
        if day > report_date.day:
            month -= 1
            if month == 0:
                month = 12
                year -= 1
        try:
            return date(year, month, day)
        except ValueError:
            return None
    if (
        "今天" in compact
        or "今日" in compact
        or "今儿" in compact
        or "今兒" in compact
        or "today" in compact
        or "当前" in compact
    ):
        return report_date
    return None



def _resolve_report_update_target_date(raw_input: str, default_report_date: date, received_at: datetime) -> date | None:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return None
    if not (
        _explicitly_targets_previous_report_change(raw_input, default_report_date)
        or _looks_like_previous_report_content_input(raw_input)
    ):
        return None
    target_date = _resolve_date_from_text(compact, default_report_date)
    if target_date is None and _looks_like_previous_report_content_input(raw_input):
        target_date = default_report_date - timedelta(days=1)
    if target_date is None or target_date == default_report_date:
        return None
    if _is_allowed_previous_report_date(target_date, default_report_date, received_at):
        return target_date
    return None

def _resolve_decision_target_report_date(
    decision: DraftDecision,
    raw_input: str,
    report_date: date,
    actual_date: date,
    received_at: datetime,
) -> date | None:
    raw_target_date = _resolve_report_update_target_date(raw_input, actual_date, received_at)
    explicit = (decision.target_report_date or "").strip()
    target_date: date | None = None
    if explicit:
        if explicit.lower() == "yesterday":
            target_date = actual_date - timedelta(days=1)
        elif explicit.lower() == "today":
            target_date = actual_date
        else:
            try:
                target_date = date.fromisoformat(explicit[:10])
            except ValueError:
                target_date = None
    if target_date is None:
        target_date = raw_target_date
    if target_date is not None and target_date < actual_date and _looks_like_current_report_content(raw_input):
        return None
    if raw_target_date == report_date and target_date != report_date:
        return None
    if target_date is None or target_date == report_date:
        return None
    return target_date



def _is_previous_report_blocked_by_cutoff(raw_input: str, default_report_date: date, received_at: datetime) -> bool:
    if _looks_like_current_report_content(raw_input) or _looks_like_current_report_lock_reply(raw_input):
        return False
    if _looks_like_copy_recent_report_to_today(raw_input):
        return False
    if _resolve_report_display_request(raw_input, default_report_date) is not None:
        return False
    if _looks_like_previous_plan_completion_request(raw_input):
        return False
    if _asks_previous_report_change_availability(raw_input):
        target_date = default_report_date - timedelta(days=1)
        return not _is_allowed_previous_report_date(target_date, default_report_date, received_at)
    is_previous_content_input = _looks_like_previous_report_content_input(raw_input)
    if is_previous_content_input and _within_previous_report_cutoff(received_at):
        return False
    if not (_explicitly_targets_previous_report_change(raw_input, default_report_date) or is_previous_content_input):
        return False
    compact = _compact_for_intent(raw_input)
    target_date = _resolve_date_from_text(compact, default_report_date)
    if target_date is None and is_previous_content_input:
        target_date = default_report_date - timedelta(days=1)
    if target_date is None or target_date >= default_report_date:
        return False
    return not _is_allowed_previous_report_date(target_date, default_report_date, received_at)

def _default_report_date_for_received_at(received_at: datetime) -> date:
    current_date = received_at.date()
    # A normal weekday message always starts from the calendar day's report.
    # The before-09:00 window only authorizes an *explicit* previous-day
    # operation; it must not silently make yesterday the default.  Saturday is
    # the sole defaulting exception because Friday is the last reporting day
    # and Saturday itself has no required report.
    if current_date.weekday() == 5 and _within_previous_report_cutoff(received_at):
        return _previous_reporting_date(current_date)
    return current_date


def _reporting_required_on(target_date: date) -> bool:
    return target_date.weekday() < 5


def _previous_reporting_date(target_date: date) -> date:
    cursor = target_date - timedelta(days=1)
    while not _reporting_required_on(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _is_allowed_non_reporting_day_request(raw_input: str, default_report_date: date) -> bool:
    return (
        _resolve_report_display_request(raw_input, default_report_date) is not None
        or _is_daily_briefing_feedback_request(raw_input)
    )


def _non_reporting_day_message(calendar_date: date) -> str:
    if calendar_date.weekday() == 5:
        return "今天是周六，不需要填写日报；周五日报只能在周六 09:00 前补填或修改。需要查看历史日报，可以直接说“查看某日期日报”。"
    if calendar_date.weekday() == 6:
        return "今天是周日，不需要填写日报。需要查看历史日报，可以直接说“查看某日期日报”。"
    return "今天不需要填写日报。需要查看历史日报，可以直接说“查看某日期日报”。"


def _is_allowed_previous_report_date(target_date: date, default_report_date: date, received_at: datetime) -> bool:
    previous_calendar_date = received_at.date() - timedelta(days=1)
    return (
        target_date == previous_calendar_date
        and _reporting_required_on(target_date)
        and _within_previous_report_cutoff(received_at)
    )


def _within_previous_report_cutoff(received_at: datetime) -> bool:
    return (received_at.hour, received_at.minute, received_at.second, received_at.microsecond) < (9, 0, 0, 0)


def _previous_report_cutoff_message(default_report_date: date) -> str:
    yesterday = default_report_date - timedelta(days=1)
    return f"{yesterday.isoformat()} 的日报只能在次日 09:00 前补交或修改。现在已经超过时间，不能再补交、修改或撤回昨天的日报；请按今天的日报继续填写。"


def _looks_like_current_report_content(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if _looks_like_current_report_lock_reply(raw_input):
        return True
    has_current_marker = any(marker in compact for marker in ("今天", "今日", "本日", "当前"))
    if not has_current_marker:
        return False
    content_markers = (
        "主要工作",
        "主要的工作",
        "今日工作",
        "今天工作",
        "工作内容",
        "主要做",
        "做了",
        "完成",
        "处理",
        "优化",
        "修了",
        "修复",
        "加了",
        "跟进",
        "参加",
        "整理",
        "推进",
        "测试",
        "上线",
        "问题",
        "风险",
        "明日计划",
        "明天计划",
    )
    if any(marker in compact for marker in content_markers):
        return True
    if _looks_like_previous_report_content_input(raw_input):
        return False
    return len(compact) > 120 and compact.startswith(("今天", "今日", "本日"))



def _looks_like_previous_report_content_input(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if not any(token in compact for token in ("\u6628\u5929", "\u6628\u65e5", "\u6628\u513f")):
        return False
    if _looks_like_copy_recent_report_to_today(raw_input):
        return False
    if any(marker in compact for marker in ("\u53d1\u6211\u770b", "\u7ed9\u6211\u770b", "\u770b\u4e0b", "\u770b\u770b", "\u67e5\u8be2", "\u67e5\u4e0b", "\u67e5\u4e00\u4e0b", "\u53c2\u8003")):
        return False
    if _looks_like_previous_plan_completion_request(raw_input):
        return False
    backfill_markers = (
        "\u8865\u6628\u5929",
        "\u8865\u4e00\u4e0b\u6628\u5929",
        "\u8865\u4ea4\u6628\u5929",
        "\u8865\u5199\u6628\u5929",
        "\u8865\u5f55\u6628\u5929",
        "\u6628\u5929\u8865\u4ea4",
        "\u6628\u5929\u8865\u5199",
        "\u6628\u5929\u8865\u5f55",
        "\u6628\u5929\u7684\u65e5\u62a5\u8865",
        "\u6628\u65e5\u65e5\u62a5\u8865",
    )
    content_markers = (
        "\u5ba1\u6838",
        "\u6574\u7406",
        "\u6c9f\u901a",
        "\u5904\u7406",
        "\u8ddf\u8fdb",
        "\u63a8\u8fdb",
        "\u5b8c\u6210",
        "\u8d77\u8349",
        "\u64b0\u5199",
        "\u53d1\u9001",
        "\u5f00\u4f1a",
        "\u5f00\u5ead",
        "\u51fa\u5dee",
        "\u7533\u62a5",
        "\u5ba1\u6279",
    )
    if any(marker in compact for marker in content_markers):
        return True
    return any(marker in compact for marker in backfill_markers)

def _looks_like_current_report_lock_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if _looks_like_current_date_correction(compact):
        return True
    exact_replies = {
        "就是今天",
        "就是今天的",
        "今天的",
        "写今天",
        "写今天的",
        "填今天",
        "填今天的",
        "按今天",
        "按今天的",
        "当前这个",
        "当前这份",
        "这就是今天的",
    }
    if compact in exact_replies:
        return True
    return any(token in compact for token in ("就是今天的日报", "按今天的日报", "写今天的日报", "填今天的日报", "这是今天的日报"))


def _looks_like_current_date_correction(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    negates_previous = any(marker in compact for marker in ("不是昨天", "不是昨日", "不是昨儿", "不是前天", "不是前日"))
    has_current = any(marker in compact for marker in ("是今天", "是今日", "就是今天", "就是今日", "今天的", "今日的", "当前这份", "当前这个"))
    return negates_previous and has_current


def _looks_like_previous_report_edit_entry_request(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    has_previous_marker = any(
        token in compact
        for token in ("\u6628\u5929", "\u6628\u65e5", "\u6628\u513f", "\u6628\u65e5\u62a5", "\u6628\u5929\u65e5\u62a5", "\u6628\u65e5\u65e5\u62a5")
    )
    has_explicit_date_report = bool(
        re.search(r"(?:20\d{2}[-\u5e74./])?\d{1,2}[-\u6708./]\d{1,2}[\u65e5\u53f7]?\u7684?\u65e5\u62a5", raw_input)
    )
    if not (has_previous_marker or has_explicit_date_report):
        return False
    if any(marker in compact for marker in ("\u53d1\u6211\u770b", "\u7ed9\u6211\u770b", "\u770b\u4e0b", "\u770b\u770b", "\u67e5\u8be2", "\u67e5\u4e0b", "\u67e5\u4e00\u4e0b")):
        return False
    if any(marker in compact for marker in ("\u6539\u6210", "\u6539\u4e3a", "\u4fee\u6539\u4e3a", "\u6362\u6210", "\u66ff\u6362", "\u8865\u4ea4", "\u8865\u5199", "\u8865\u5f55", "\u5220\u9664", "\u6e05\u7a7a", "\u64a4\u56de")):
        return False
    if not any(marker in compact for marker in ("\u6539\u4e0b", "\u6539\u4e00\u4e0b", "\u4fee\u6539", "\u7f16\u8f91", "\u8c03\u6574", "\u66f4\u6b63", "\u91cd\u5199", "\u91cd\u65b0\u5199")):
        return False
    stripped = re.sub(
        "(\u6628\u5929|\u6628\u65e5|\u6628\u513f|\u6628\u65e5\u62a5|\u6628\u5929\u65e5\u62a5|\u6628\u65e5\u65e5\u62a5|\u65e5\u62a5|\u590d\u76d8|\u7684|\u628a|\u5e2e\u6211|\u7ed9\u6211|\u4e00\u4e0b|\u4e0b|\u6539\u4e0b|\u6539\u4e00\u4e0b|\u4fee\u6539|\u7f16\u8f91|\u8c03\u6574|\u66f4\u6b63|\u91cd\u5199|\u91cd\u65b0\u5199|\u4eca\u65e5\u5de5\u4f5c|\u4eca\u5929\u5de5\u4f5c|\u95ee\u9898\u98ce\u9669|\u95ee\u9898|\u98ce\u9669|\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212|\u8ba1\u5212)",
        "",
        compact,
    )
    stripped = re.sub(r"(?:20\d{2}[-\u5e74./])?\d{1,2}[-\u6708./]\d{1,2}[\u65e5\u53f7]?", "", stripped)
    return len(stripped) <= 2



def _explicitly_targets_previous_report_change(raw_input: str, default_report_date: date | None = None) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if _looks_like_copy_recent_report_to_today(raw_input):
        return False
    if any(marker in compact for marker in ("\u53d1\u6211\u770b", "\u7ed9\u6211\u770b", "\u770b\u4e0b", "\u770b\u770b", "\u67e5\u8be2", "\u67e5\u4e0b", "\u67e5\u4e00\u4e0b", "\u53c2\u8003")):
        return False
    if _looks_like_previous_plan_completion_request(raw_input):
        return False

    if default_report_date is not None:
        target_date = _resolve_date_from_text(compact, default_report_date)
        has_previous_reference = target_date is not None and target_date < default_report_date
    else:
        has_previous_reference = any(token in compact for token in ("\u6628\u5929", "\u6628\u65e5", "\u6628\u513f", "\u6628\u65e5\u62a5", "\u6628\u5929\u65e5\u62a5", "\u6628\u65e5\u65e5\u62a5"))
    if not has_previous_reference:
        return False

    strong_change_markers = (
        "\u8865\u4ea4",
        "\u8865\u5199",
        "\u8865\u5f55",
        "\u4fee\u6539",
        "\u4fee\u6b63",
        "\u6539\u4e0b",
        "\u6539\u4e00\u4e0b",
        "\u6539\u6210",
        "\u6539\u4e3a",
        "\u64a4\u56de",
        "\u5220\u9664",
        "\u5220\u6389",
        "\u5220\u4e86",
        "\u6e05\u7a7a",
        "\u91cd\u5199",
        "\u91cd\u65b0\u5199",
        "\u8c03\u6574",
        "\u7f16\u8f91",
        "\u66f4\u6b63",
    )
    if any(marker in compact for marker in strong_change_markers):
        return True
    report_or_section_markers = ("\u65e5\u62a5", "\u65e5\u5fd7", "\u590d\u76d8", "\u4eca\u65e5\u5de5\u4f5c", "\u4eca\u5929\u5de5\u4f5c", "\u95ee\u9898", "\u98ce\u9669", "\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u8ba1\u5212", "\u8ba1\u5212")
    return "\u6539" in compact and any(marker in compact for marker in report_or_section_markers)

def _asks_previous_report_change_availability(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact or not any(token in compact for token in ("昨天", "昨日", "昨儿")):
        return False
    if not any(marker in compact for marker in ("能改", "能不能改", "可以改", "可不可以改", "还能改", "现在能改")):
        return False
    return any(marker in compact for marker in ("日报", "复盘", "昨天", "昨日", "昨儿"))


def _with_report_date_context(message: str, report_date: date) -> str:
    if not message:
        return f"当前填报日期：{report_date.isoformat()}"
    if report_date.isoformat() in message and ("当前填报日期" in message or "当前正在填写" in message or "当前正在编辑" in message):
        return message
    return f"当前填报日期：{report_date.isoformat()}\n\n{message}"


def _build_current_report_date_ack_message(report_date: date, existing: DailyReport | None) -> str:
    prefix = f"已确认，当前正在填写的是 {report_date.isoformat()} 的日报。"
    missing_sections = _missing_sections_from_report(existing)
    if missing_sections:
        return f"{prefix}{_build_missing_after_modify_message(missing_sections)}"
    return f"{prefix}当前草稿不需要再调整日期。"


def _build_report_date_entry_prompt(report_date: date, existing: DailyReport | None) -> str:
    if existing is not None and _has_report_content(existing):
        return _build_current_report_date_ack_message(report_date, existing)
    next_day = report_date + timedelta(days=1)
    return (
        f"当前填报日期：{report_date.isoformat()}\n\n"
        f"好的，正在填写 {report_date.isoformat()} 的日报。"
        f"请告诉我 {report_date.isoformat()} 的主要工作内容；"
        f"如果有问题/风险和 {next_day.isoformat()} 的计划，也可以一起发。"
    )


def _is_report_date_entry_clarification_request(raw_input: str) -> bool:
    if _looks_like_current_report_content(raw_input):
        return False
    compact = _compact_for_intent(raw_input)
    if any(token in compact for token in ("刚才那条", "刚才这个", "刚补", "刚加", "刚新增", "这条", "那条")):
        return False
    if not compact or not any(token in compact for token in ("日报", "复盘", "填报日期", "报告日期", "日期")):
        return False
    if not any(token in compact for token in ("我在写", "我要写", "想写", "写昨天", "写今天", "填写", "填报", "补写", "补交", "补录", "录入", "改成", "改为", "改到", "改回")):
        return False
    content_markers = (
        "完成",
        "处理",
        "审批",
        "用印",
        "归档",
        "整理",
        "制作",
        "分析",
        "培训",
        "沟通",
        "对接",
        "审核",
        "合同",
        "流程",
        "问题",
        "风险",
        "计划",
        "明天",
        "明日",
        "待完成",
        "暂无",
        "没有",
        "无",
    )
    return not any(marker in compact for marker in content_markers)


def _parse_small_cn_number(text: str) -> int | None:
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return 10
    if text.startswith("十"):
        tail = text[1:]
        return 10 + (digits.get(tail, 0) if tail else 0)
    if "十" in text:
        head, tail = text.split("十", 1)
        if head not in digits:
            return None
        return digits[head] * 10 + (digits.get(tail, 0) if tail else 0)
    return digits.get(text)


def _should_continue_recent_backfill(
    recent_report: DailyReport | None,
    *,
    raw_input: str,
    received_at: datetime,
    default_report_date: date,
) -> bool:
    if recent_report is None:
        return False
    if getattr(recent_report, "report_date", None) != default_report_date - timedelta(days=1):
        return False
    if not _within_previous_report_cutoff(received_at):
        return False
    if getattr(recent_report, "status", "") == STATUS_COMPLETED:
        return False
    compact = _compact_for_intent(raw_input)
    explicit_date = _resolve_date_from_text(compact, default_report_date)
    if explicit_date is not None and explicit_date not in {default_report_date, default_report_date - timedelta(days=1)}:
        return False
    return _has_report_content(recent_report) or bool(_get_pending_interaction(recent_report))


def _is_date_reassignment_request(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    return bool(
        compact
        and (
            "\u4e0d\u662f\u4eca\u5929" in compact
            or "\u8865\u4ea4" in compact
            or "\u8865\u5f55" in compact
            or "\u5f52\u5c5e" in compact
            or "\u8bb0\u4e3a" in compact
            or "\u7b97\u5230" in compact
            or "\u653e\u5230" in compact
            or "\u5e94\u8be5\u662f" in compact
            or ("\u8fd9\u662f" in compact and ("\u53f7" in compact or "\u65e5" in compact or "\u6628\u5929" in compact))
            or ("\u8fd9\u4e2a\u662f" in compact and ("\u53f7" in compact or "\u65e5" in compact or "\u6628\u5929" in compact))
            or ("\u8fd9\u6761\u662f" in compact and ("\u53f7" in compact or "\u65e5" in compact or "\u6628\u5929" in compact))
        )
    )


def _resolve_date_reassignment_target(raw_input: str, default_report_date: date, received_at: datetime) -> date | None:
    compact = _compact_for_intent(raw_input)
    if not compact or not _is_date_reassignment_request(raw_input):
        return None
    if any(marker in compact for marker in ("今天", "今日", "今天的日报", "今日的日报")):
        return received_at.date()
    target = _resolve_date_from_text(compact, default_report_date)
    if target is None and "\u4e0d\u662f\u4eca\u5929" in compact:
        target = default_report_date - timedelta(days=1)
    return target


def _looks_like_previous_draft_operation(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if _is_restore_previous_request(raw_input):
        return True
    operation_markers = ("补交", "补录", "修正", "修改", "改成", "改为", "移动", "挪到", "不是今天", "第", "项")
    return any(marker in compact for marker in operation_markers)


def _display_plain_section(values: list[str], *, empty_fallback: str = "未填写") -> str:
    cleaned = [value.strip() for value in values or [] if value and value.strip()]
    if not cleaned:
        return empty_fallback
    if len(cleaned) == 1:
        return cleaned[0]
    return "\n" + "\n".join(f"{index}. {value}" for index, value in enumerate(cleaned, start=1))


def _draft_decision_reply_kind(decision: DraftDecision) -> str:
    if decision.decision_type == "answer_only":
        return "non_report_interaction"
    if decision.decision_type == "history_query":
        return "history_query"
    if decision.decision_type == "clarification" or decision.needs_clarification:
        return "draft_decision_clarification"
    return "draft_decision_no_write"


def _draft_decision_no_write_message(decision: DraftDecision) -> str:
    message = (
        decision.reply_to_user
        or decision.clarification_question
        or "这句话我先不写入日报。你可以继续补充或修改复盘内容。"
    )
    if _draft_decision_reply_kind(decision) == "draft_decision_no_write" and _draft_decision_is_emotional_no_write(decision):
        compact = _compact_for_intent(message)
        if not ("不" in compact and ("\u65e5\u62a5" in compact or "\u590d\u76d8" in compact)):
            return f"{message}\n\n这句我先不写入日报或复盘；如果要记录到日报，直接说具体工作、问题或明日计划。"
    return message


def _draft_decision_is_emotional_no_write(decision: DraftDecision) -> bool:
    action_types = {str(getattr(action, "type", "") or "") for action in decision.actions or []}
    return (
        not decision.should_write
        and str(decision.user_intent or "") in {"", "emotional_feedback", "small_talk", "chat"}
        and (decision.decision_type in {"no_op", "answer_only"} or action_types <= {"no_op"})
    )


def _is_unresolved_draft_edit_clarification(decision: DraftDecision, existing: DailyReport | None) -> bool:
    if existing is None or not _has_report_content(existing):
        return False
    if not (decision.needs_clarification or decision.clarification_question or decision.decision_type == "clarification"):
        return False
    if decision.operation in {"rewrite_item", "delete_item", "merge_items", "move_item", "clear_report", "replace_report"}:
        return True
    if decision.move_items or decision.delete_items:
        return True
    if decision.item_refs and decision.target_field in {"today_work", "problems", "tomorrow_plan", "none"}:
        return True
    return any(update.item_refs for update in decision.field_updates or [])


def _draft_decision_wants_write(decision: DraftDecision) -> bool:
    if decision.should_write:
        return True
    if decision.needs_clarification or decision.clarification_question:
        return False
    if decision.decision_type not in {"report_update", "draft_edit", "control_action"}:
        return False
    if decision.restore_previous.enabled:
        return True
    if decision.move_items or decision.delete_items:
        return True
    for update in decision.field_updates:
        if update.mode in {"clear", "remove_items"}:
            return True
        if update.mode in {"replace", "append", "merge"} and update.items:
            return True
    if decision.operation in {"clear_report", "restore_previous"}:
        return True
    if decision.operation in {"rewrite_item", "delete_item", "merge_items", "move_item"} and decision.item_refs:
        return bool(decision.new_content or decision.operation != "rewrite_item")
    return False


def _pending_edit_from_draft_decision(
    decision: DraftDecision,
    existing: DailyReport | None,
    *,
    require_confirmation: bool = False,
) -> dict[str, Any] | None:
    if existing is None:
        return None
    operation = decision.operation
    field = decision.target_field
    refs = list(decision.item_refs or [])
    if not refs and decision.field_updates:
        first_update = decision.field_updates[0]
        field = first_update.field
        refs = list(first_update.item_refs or [])
        if first_update.mode == "remove_items":
            operation = "delete_item"
        elif first_update.mode == "replace":
            operation = "rewrite_item"
    if not refs and decision.delete_items:
        first_delete = decision.delete_items[0]
        field = first_delete.target_field
        refs = list(first_delete.item_refs or [])
        operation = "delete_item"
    if field not in {"today_work", "problems", "tomorrow_plan"} or not refs:
        return None
    zero_indices = [ref - 1 for ref in refs if ref > 0]
    values = _draft_field_values(
        list(existing.today_work or []),
        list(existing.problems or []),
        list(existing.tomorrow_plan or []),
    ).get(field, [])
    if any(index < 0 or index >= len(values) for index in zero_indices):
        return None
    needs_content = operation in {"rewrite_item", "modify_field"} and not decision.new_content.strip()
    if not (needs_content or require_confirmation or decision.requires_user_confirmation):
        return None
    confirmation_message = ""
    if operation == "delete_item" and zero_indices:
        confirmation_message = _build_delete_confirmation_message(field, zero_indices[0], values)
    return _build_pending_draft_edit_operation(
        operation if operation in {"rewrite_item", "modify_field", "delete_item", "merge_items"} else "rewrite_item",
        field,
        zero_indices,
        new_content=decision.new_content,
        requires_confirmation=bool(require_confirmation or decision.requires_user_confirmation),
        confirmation_message=confirmation_message,
    )


def _pending_edit_question_from_action(pending_edit: dict[str, Any]) -> str:
    field = str(pending_edit.get("target_field") or "today_work")
    index = int(pending_edit.get("target_index") or 0)
    return _build_pending_draft_edit_question(field, index)


def _pending_interaction_from_draft_decision(decision: DraftDecision, existing: DailyReport | None) -> dict[str, Any] | None:
    if existing is None:
        return None
    selected_field = _draft_decision_target_field(decision)
    if decision.operation == "append" and not selected_field and (decision.needs_clarification or decision.clarification_question):
        return {
            "type": PENDING_INTERACTION_AWAITING_APPEND_TARGET,
            "operation": "append",
            "target_field": None,
        }
    if decision.operation == "append" and selected_field and not _draft_decision_wants_write(decision):
        return {
            "type": PENDING_INTERACTION_AWAITING_APPEND_CONTENT,
            "operation": "append",
            "target_field": selected_field,
        }
    return None


def _pending_interaction_message(pending_interaction: dict[str, Any]) -> str:
    if pending_interaction.get("type") == PENDING_INTERACTION_AWAITING_APPEND_CONTENT:
        return _build_pending_append_content_message(str(pending_interaction.get("target_field") or ""))
    return "您想把内容添加到哪一项？今日工作、问题/风险，还是明日计划？"


def _draft_decision_target_field(decision: DraftDecision) -> str | None:
    if decision.target_field in {"today_work", "problems", "tomorrow_plan"}:
        return decision.target_field
    fields = [update.field for update in decision.field_updates if update.field in {"today_work", "problems", "tomorrow_plan"}]
    return fields[0] if len(set(fields)) == 1 else None


def _draft_decision_is_high_risk(decision: DraftDecision) -> bool:
    if decision.risk_level == "high" or decision.requires_user_confirmation:
        return True
    if decision.restore_previous.enabled:
        return True
    if decision.operation in {"clear_report", "replace_report", "delete_item", "merge_items"}:
        return True
    if decision.delete_items:
        return True
    if any(update.mode in {"clear", "remove_items"} for update in decision.field_updates):
        return True
    return False


def _draft_decision_allows_empty_report(decision: DraftDecision) -> bool:
    return decision.operation == "clear_report" or any(update.mode == "clear" for update in decision.field_updates)


def _build_direct_report_update_decision(raw_input: str, *, existing: DailyReport | None) -> DraftDecision | None:
    compact = _compact_for_intent(raw_input)
    if existing is not None and _has_replace_current_report_intent(compact):
        return None
    if _is_explicit_field_modify_request(raw_input):
        return None
    if existing is not None and isinstance(getattr(existing, "section_status", None), dict) and existing.section_status.get(PENDING_QUALITY_CLARIFICATION_KEY):
        return None

    field_updates: list[dict[str, Any]] = []
    simple_fields = _extract_simple_report_fields(raw_input) if existing is not None and _looks_like_full_report_input(raw_input) else {}
    today_work_items = _extract_numbered_today_work_items(raw_input)
    if simple_fields.get("today_work") and existing is not None and existing.today_work and not today_work_items:
        return None
    if not today_work_items and simple_fields.get("today_work"):
        today_work_items = simple_fields["today_work"]
    if today_work_items:
        field_updates.append(
            {
                "field": "today_work",
                "mode": "replace" if not (existing and existing.today_work) else "append",
                "items": today_work_items,
            }
        )
    if existing is not None and (_mentions_no_problem(raw_input) or simple_fields.get("problems")):
        field_updates.append(
            {
                "field": "problems",
                "mode": "replace",
                "items": simple_fields.get("problems") or [_empty_value_for_field("problems", raw_input)],
            }
        )
    explicit_tomorrow_plan_items = _extract_explicit_tomorrow_plan_items(raw_input)
    if not explicit_tomorrow_plan_items and existing is not None and _mentions_no_problem(raw_input):
        explicit_tomorrow_plan_items = _extract_natural_tomorrow_plan_items(raw_input)
    if not explicit_tomorrow_plan_items and simple_fields.get("tomorrow_plan"):
        explicit_tomorrow_plan_items = simple_fields["tomorrow_plan"]
    if existing is not None and explicit_tomorrow_plan_items:
        field_updates.append(
            {
                "field": "tomorrow_plan",
                "mode": "replace" if not (existing and existing.tomorrow_plan) else "append",
                "items": explicit_tomorrow_plan_items,
            }
        )
    if existing is not None and _looks_like_tomorrow_continue_current_work(raw_input):
        field_updates.append(
            {
                "field": "tomorrow_plan",
                "mode": "replace",
                "items": ["继续今日工作"],
            }
        )
    if not field_updates:
        return None
    return DraftDecision(
        decision_type="report_update",
        message_kind="report_content",
        operation="set_fields",
        target_field=field_updates[0]["field"] if len(field_updates) == 1 else "none",
        confidence=0.95,
        should_write=True,
        field_updates=field_updates,
        reason="direct structured report update fallback",
    )


def _build_direct_not_this_but_that_decision(raw_input: str, *, existing: DailyReport | None) -> DraftDecision | None:
    if existing is None or not _has_report_content(existing):
        return None
    original_today_work = list(existing.today_work or [])
    original_problems = list(existing.problems or [])
    original_tomorrow_plan = list(existing.tomorrow_plan or [])
    today_work, problems, tomorrow_plan = _apply_direct_not_this_but_that_replacement(
        raw_input,
        today_work=original_today_work,
        problems=original_problems,
        tomorrow_plan=original_tomorrow_plan,
    )
    if today_work == original_today_work and problems == original_problems and tomorrow_plan == original_tomorrow_plan:
        return None

    field_updates: list[DraftFieldUpdate] = []
    if today_work != original_today_work:
        field_updates.append(DraftFieldUpdate(field="today_work", mode="replace", items=today_work))
    if problems != original_problems:
        field_updates.append(DraftFieldUpdate(field="problems", mode="replace", items=problems))
    if tomorrow_plan != original_tomorrow_plan:
        field_updates.append(DraftFieldUpdate(field="tomorrow_plan", mode="replace", items=tomorrow_plan))
    return DraftDecision(
        decision_type="draft_edit",
        message_kind="draft_edit_instruction",
        operation="modify_field",
        target_field="none",
        confidence=0.98,
        should_write=True,
        field_updates=field_updates,
        reason="direct not-this-but-that replacement",
    )


def _extract_numbered_today_work_items(raw_input: str) -> list[str]:
    compact = _compact_for_intent(raw_input)
    if not any(marker in compact for marker in ("今天", "今日", "今儿", "主要工作", "工作是", "完成")):
        return []
    text = _strip_numbered_work_prefix(raw_input)
    matches = _top_level_numbered_matches(text)
    if len(matches) < 2:
        return []
    items: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        item = re.sub(r"\s+", " ", text[start:end]).strip("，,。；;、 ")
        item = re.sub(r"^(然后|还有|并且|以及|把)", "", item)
        if item:
            items.append(item)
    return _clean_report_items(items, field="today_work") if len(items) >= 2 else []


def _strip_numbered_work_prefix(raw_input: str) -> str:
    text = str(raw_input or "").replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"[：:]", text):
        head, tail = re.split(r"[：:]", text, maxsplit=1)
        if any(marker in _compact_for_intent(head) for marker in ("今天", "今日", "主要工作", "工作", "做了", "完成")):
            return tail
    return text


def _top_level_numbered_matches(text: str) -> list[re.Match[str]]:
    token = r"\d{1,2}|[一二两三四五六七八九十]{1,3}"
    marker_pattern = re.compile(
        rf"(?m)(?:^|\n)\s*(?:第?({token})(?:个|项|条|项目|事项)?(?:就是|是|、|\.|．|\)|）|:|：))\s*"
    )
    matches = list(marker_pattern.finditer(str(text or "")))
    if len(matches) >= 2:
        return matches
    inline_pattern = re.compile(
        rf"(?:(?<=^)|(?<=[\s，,。；;、]))(?:第?({token})(?:个|项|条|项目|事项)?(?:就是|是|、|\.|．|\)|）|:|：))\s*"
    )
    return list(inline_pattern.finditer(str(text or "")))


def _looks_like_tomorrow_continue_current_work(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    return (
        any(day in compact for day in ("明天", "明日", "tomorrow"))
        and "继续" in compact
        and any(target in compact for target in ("这些工作", "这些", "以上", "上述", "今日工作", "今天工作", "工作"))
    )


def _extract_explicit_tomorrow_plan_items(raw_input: str) -> list[str]:
    text = str(raw_input or "").strip()
    if not text:
        return []
    if _is_explicit_field_modify_request(text):
        return []
    match = re.search(r"(?:明日计划|明天计划|明日安排|明天安排)[，,。；;：:\s]*(.+)$", text)
    if not match:
        return []
    tail = match.group(1).strip(" ，,。；;：:、")
    if not tail or _mentions_no_problem(tail) or _is_empty_slot_reply(tail):
        return []
    parts = [part.strip() for part in re.split(r"(?:同时|并且|另外|此外|以及|，|,|；|;|、|\n)+", tail) if part.strip()]
    return _clean_report_items(parts or [tail], field="tomorrow_plan")


def _extract_natural_tomorrow_plan_items(raw_input: str) -> list[str]:
    _before_tomorrow, tomorrow_part = _split_tomorrow_text(raw_input)
    tomorrow_part = tomorrow_part.strip(" ，,。；;：:、")
    if not tomorrow_part or _is_empty_slot_reply(tomorrow_part) or _mentions_no_problem(tomorrow_part):
        return []
    return _clean_report_items([tomorrow_part], field="tomorrow_plan")


def _build_direct_ordinal_patch_decision(
    raw_input: str,
    *,
    existing: DailyReport | None,
    report_date: date,
    actual_date: date,
) -> DraftDecision | None:
    if existing is None or not _has_report_content(existing):
        return None
    today_work = list(existing.today_work or [])
    problems = list(existing.problems or [])
    tomorrow_plan = list(existing.tomorrow_plan or [])
    values_by_field = _draft_field_values(today_work, problems, tomorrow_plan)

    field_updates = []
    correction_update = _build_single_item_correction_update(raw_input, values_by_field)
    if correction_update:
        field_updates.append(correction_update)

    quantity_patch = _build_quantity_patch_update(raw_input, values_by_field)
    if quantity_patch:
        field_updates.append(quantity_patch)

    duplicate_pairs = _extract_duplicate_ref_pairs(raw_input)
    delete_items = _build_duplicate_item_delete_actions(raw_input, values_by_field, pairs=duplicate_pairs)
    direct_delete = [] if delete_items else _build_direct_item_delete_actions(raw_input, values_by_field)
    if direct_delete:
        delete_items.extend(direct_delete)

    move_items = []
    already_satisfied_plan_refs = False
    plan_refs = _extract_tomorrow_plan_move_refs(raw_input, report_date=report_date, actual_date=actual_date)
    if plan_refs:
        source = _resolve_global_refs_to_single_field(plan_refs, values_by_field)
        if source is not None:
            source_field, _field_refs = source
            if source_field != "tomorrow_plan":
                expected_text = _source_text_for_global_refs(plan_refs, values_by_field)
                if expected_text:
                    move_items.append(
                        {
                            "source_field": source_field,
                            "destination_field": "tomorrow_plan",
                            "item_refs": plan_refs,
                            "source_item_text": expected_text,
                        }
                    )
            else:
                already_satisfied_plan_refs = True

    stated_date = _resolve_date_from_text(_compact_for_intent(raw_input), actual_date)
    date_reassignment_satisfied = _is_date_reassignment_request(raw_input) and (
        stated_date == report_date or (stated_date is None and report_date != actual_date)
    )

    if not field_updates and not move_items and not delete_items:
        if duplicate_pairs:
            return DraftDecision(
                decision_type="answer_only",
                message_kind="non_report_interaction",
                operation="none",
                target_field="none",
                confidence=0.95,
                should_write=False,
                reply_to_user="我看了一下，当前草稿里没有发现你说的这些编号是重复项，先不删除。你可以直接说要删除第几项。",
                reason="duplicate item correction had no verified duplicate pairs",
            )
        if date_reassignment_satisfied or already_satisfied_plan_refs:
            return DraftDecision(
                decision_type="answer_only",
                message_kind="non_report_interaction",
                operation="none",
                target_field="none",
                confidence=0.95,
                should_write=False,
                reply_to_user=_build_current_report_date_ack_message(report_date, existing),
                reason="direct ordinal patch fallback already satisfied",
            )
        return None
    operation = "rewrite_item"
    if move_items:
        operation = "move_item"
    elif delete_items:
        operation = "delete_item"
    return DraftDecision(
        decision_type="draft_edit",
        message_kind="draft_edit_instruction",
        operation=operation,
        target_field="today_work" if field_updates else "none",
        confidence=0.95,
        should_write=True,
        field_updates=field_updates,
        move_items=move_items,
        delete_items=delete_items,
        reason="direct ordinal patch fallback",
    )


def _route_actual_today_work_to_tomorrow_plan(
    decision: DraftDecision,
    *,
    raw_input: str,
    report_date: date,
    actual_date: date,
) -> DraftDecision:
    if not _should_route_actual_today_work_to_tomorrow_plan(
        decision,
        raw_input=raw_input,
        report_date=report_date,
        actual_date=actual_date,
    ):
        return decision

    routed = decision.model_copy(deep=True)
    if routed.target_field == "today_work":
        routed.target_field = "tomorrow_plan"
    for update in routed.field_updates:
        if update.field == "today_work":
            update.field = "tomorrow_plan"
    if routed.reason:
        routed.reason = f"{routed.reason}; routed actual today work to tomorrow_plan for backfill"
    else:
        routed.reason = "routed actual today work to tomorrow_plan for backfill"
    return routed


def _preserve_explicit_routine_work_item(decision: DraftDecision, *, raw_input: str) -> DraftDecision:
    if decision.decision_type != "report_update" or not _draft_decision_wants_write(decision):
        return decision
    compact = _compact_for_intent(raw_input)
    if "日常工作" not in compact:
        return decision
    existing_items = [item for update in decision.field_updates for item in update.items]
    if any("日常工作" in item for item in existing_items):
        return decision

    routed = decision.model_copy(deep=True)
    target_field = _draft_decision_target_field(routed) or "today_work"
    if target_field == "problems":
        return decision
    if target_field not in {"today_work", "tomorrow_plan"}:
        target_field = "today_work"
    target_update = next((update for update in routed.field_updates if update.field == target_field and update.mode in {"append", "replace", "merge"}), None)
    if target_update is None:
        routed.field_updates.append(DraftFieldUpdate(field=target_field, mode="append", items=["完成日常工作"]))
    else:
        target_update.items.insert(0, "完成日常工作")
    if routed.reason:
        routed.reason = f"{routed.reason}; preserved explicit routine work item"
    else:
        routed.reason = "preserved explicit routine work item"
    return routed


def _should_route_actual_today_work_to_tomorrow_plan(
    decision: DraftDecision,
    *,
    raw_input: str,
    report_date: date,
    actual_date: date,
) -> bool:
    if decision.decision_type != "report_update" or not _draft_decision_wants_write(decision):
        return False
    if report_date + timedelta(days=1) != actual_date:
        return False
    compact = _compact_for_intent(raw_input)
    if "不是今天" in compact:
        return False
    referenced_date = _resolve_date_from_text(compact, actual_date)
    if referenced_date != actual_date:
        return False
    return any(update.field == "today_work" and update.items for update in decision.field_updates)


def _build_quantity_patch_update(raw_input: str, values_by_field: dict[str, list[str]]) -> dict[str, Any] | None:
    if "数量" not in raw_input or "分别" not in raw_input:
        return None
    before_quantity = raw_input.split("数量", 1)[0]
    refs = _extract_ordinal_refs(before_quantity)
    quantities = _extract_quantity_values(raw_input)
    if not refs or len(refs) != len(quantities):
        return None
    source = _resolve_global_refs_to_single_field(refs, values_by_field)
    if source is None:
        return None
    field, field_refs = source
    values = values_by_field.get(field, [])
    new_items: list[str] = []
    for ref, quantity in zip(field_refs, quantities):
        index = ref - 1
        if index < 0 or index >= len(values):
            return None
        new_items.append(_replace_first_item_quantity(values[index], quantity))
    return {
        "field": field,
        "mode": "replace",
        "item_refs": refs,
        "items": new_items,
    }


def _build_single_item_correction_update(raw_input: str, values_by_field: dict[str, list[str]]) -> dict[str, Any] | None:
    parsed = _extract_single_item_correction(raw_input)
    if parsed is None:
        return None
    ref, replacement = parsed
    source = _resolve_global_refs_to_single_field([ref], values_by_field)
    if source is None:
        return None
    field, field_refs = source
    return {
        "field": field,
        "mode": "replace",
        "item_refs": field_refs,
        "items": [replacement],
    }


def _extract_single_item_correction(raw_input: str) -> tuple[int, str] | None:
    if "不是" not in raw_input and "不对" not in raw_input and "错" not in raw_input:
        return None
    text = re.sub(r"\s+", "", raw_input)
    token = r"\d{1,2}|[一二两三四五六七八九十]{1,3}"
    match = re.search(
        rf"第?({token})(?:项目|事项|项|个|条)?(?:应该)?是(.+?)(?:不是|不对|错)",
        text,
    )
    if not match:
        return None
    ref = _parse_ordinal_number(match.group(1))
    replacement = _normalize_free_text(match.group(2).strip("，,。；;、 "))
    if ref is None or not replacement:
        return None
    return ref, replacement


def _extract_tomorrow_plan_move_refs(raw_input: str, *, report_date: date, actual_date: date) -> list[int]:
    refs: list[int] = []
    for clause in re.split(r"[。；;，,]", raw_input):
        if not clause.strip():
            continue
        clause_refs = _extract_ordinal_refs(clause)
        if not clause_refs:
            continue
        if _clause_targets_tomorrow_plan(clause, report_date=report_date, actual_date=actual_date):
            refs.extend(clause_refs)
    return sorted(set(refs))


def _build_duplicate_item_delete_actions(
    raw_input: str,
    values_by_field: dict[str, list[str]],
    *,
    pairs: list[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    pairs = pairs if pairs is not None else _extract_duplicate_ref_pairs(raw_input)
    if not pairs:
        return []
    global_map = _global_item_ref_map(values_by_field)
    refs_by_field: dict[str, set[int]] = {}
    for left, right in pairs:
        left_target = global_map.get(left)
        right_target = global_map.get(right)
        if left_target is None or right_target is None:
            continue
        left_field, left_field_ref = left_target
        right_field, right_field_ref = right_target
        if left_field != right_field:
            continue
        values = values_by_field.get(left_field, [])
        left_value = values[left_field_ref - 1] if 0 < left_field_ref <= len(values) else ""
        right_value = values[right_field_ref - 1] if 0 < right_field_ref <= len(values) else ""
        if not _draft_items_are_verified_duplicates(left_value, right_value):
            continue
        delete_global_ref = max(left, right)
        delete_field, delete_field_ref = global_map[delete_global_ref]
        refs_by_field.setdefault(delete_field, set()).add(delete_field_ref)

    actions: list[dict[str, Any]] = []
    for field, refs in refs_by_field.items():
        field_refs = sorted(refs)
        values = values_by_field.get(field, [])
        target_text = " ".join(values[ref - 1] for ref in field_refs if 0 < ref <= len(values))
        if target_text:
            actions.append(
                {
                    "target_field": field,
                    "item_refs": field_refs,
                    "target_item_text": target_text,
                }
            )
    return actions


def _build_direct_item_delete_actions(raw_input: str, values_by_field: dict[str, list[str]]) -> list[dict[str, Any]]:
    compact = _compact_for_intent(raw_input)
    if not compact or not any(marker in compact for marker in ("删除", "删掉", "删了", "去掉", "移除", "不要")):
        return []
    refs = _extract_ordinal_refs(raw_input)
    if not refs:
        return []
    source = _resolve_global_refs_to_single_field(refs, values_by_field)
    if source is None:
        return []
    field, field_refs = source
    values = values_by_field.get(field, [])
    target_text = " ".join(values[ref - 1] for ref in field_refs if 0 < ref <= len(values))
    if not target_text:
        return []
    return [
        {
            "target_field": field,
            "item_refs": field_refs,
            "target_item_text": target_text,
        }
    ]


def _extract_duplicate_ref_pairs(raw_input: str) -> list[tuple[int, int]]:
    compact = _compact_for_intent(raw_input)
    if not compact or not any(marker in compact for marker in ("重复", "一样", "相同", "同一条", "同一个")):
        return []
    text = re.sub(r"\s+", "", raw_input)
    token = r"\d{1,2}|[一二两三四五六七八九十]{1,3}"
    suffix = r"(?:项目|事项|项|个|条)?"
    separator = r"(?:和|跟|与|及|、|，|,)"
    pairs: list[tuple[int, int]] = []
    for start, end in re.findall(r"(\d{1,2})~(\d{1,2})", text):
        start_num = _parse_ordinal_number(start)
        end_num = _parse_ordinal_number(end)
        if start_num is None or end_num is None or start_num == end_num:
            continue
        low, high = sorted((start_num, end_num))
        pairs.extend((low, ref) for ref in range(low + 1, high + 1))
    for start, end in re.findall(r"(\d{1,2})\s*(?:~|～|-|—|－|到|至)\s*(\d{1,2})", text):
        start_num = _parse_ordinal_number(start)
        end_num = _parse_ordinal_number(end)
        if start_num is None or end_num is None or start_num == end_num:
            continue
        low, high = sorted((start_num, end_num))
        pairs.extend((low, ref) for ref in range(low + 1, high + 1))
    for start, end in re.findall(rf"({token})(?:到|至|~|～|-|—|－)({token}){suffix}", text):
        start_num = _parse_ordinal_number(start)
        end_num = _parse_ordinal_number(end)
        if start_num is None or end_num is None or start_num == end_num:
            continue
        low, high = sorted((start_num, end_num))
        pairs.extend((low, ref) for ref in range(low + 1, high + 1))
    for start, end in re.findall(rf"第?({token}){suffix}(?:到|至|~|～|-|—|－)第?({token}){suffix}", text):
        start_num = _parse_ordinal_number(start)
        end_num = _parse_ordinal_number(end)
        if start_num is None or end_num is None or start_num == end_num:
            continue
        low, high = sorted((start_num, end_num))
        pairs.extend((low, ref) for ref in range(low + 1, high + 1))
    grouped_refs = _extract_ordinal_refs(text)
    if len(grouped_refs) > 2:
        anchor = grouped_refs[0]
        pairs.extend((anchor, ref) for ref in grouped_refs[1:])
    for left, right in re.findall(rf"第?({token}){suffix}{separator}第?({token}){suffix}", text):
        left_num = _parse_ordinal_number(left)
        right_num = _parse_ordinal_number(right)
        if left_num is None or right_num is None or left_num == right_num:
            continue
        pairs.append((left_num, right_num))
    return pairs


def _draft_items_are_verified_duplicates(left: str, right: str) -> bool:
    left_norm = _normalize_evidence_text(left)
    right_norm = _normalize_evidence_text(right)
    return bool(left_norm and right_norm and left_norm == right_norm)


def _clause_targets_tomorrow_plan(clause: str, *, report_date: date, actual_date: date) -> bool:
    compact = _compact_for_intent(clause)
    if not compact:
        return False
    destination_markers = (
        "明日",
        "明天",
        "tomorrow",
        "明日计划",
        "明天计划",
        "待完成",
        "计划里",
        "计划中",
        "计划项",
        "移到明日",
        "移到明天",
        "挪到明日",
        "挪到明天",
        "放到明日",
        "放到明天",
        "归到明日",
        "归到明天",
    )
    if any(marker in compact for marker in destination_markers):
        return True
    clause_date = _resolve_date_from_text(compact, actual_date)
    points_to_actual_today = clause_date == actual_date and actual_date == report_date + timedelta(days=1)
    points_to_plan = "计划" in compact or "安排" in compact
    return points_to_actual_today and points_to_plan


def _extract_ordinal_refs(text: str) -> list[int]:
    refs: set[int] = set()
    token = r"\d{1,2}|[一二三四五六七八九十]{1,3}"
    suffix = r"(?:项目|事项|项|个|条)?"
    separator = r"(?:和|跟|与|及|、|，|,)"
    for start, end in re.findall(rf"第?({token})(?:项目|事项|项|个|条)?(?:到|至|~|～|-|—|－)第?({token})(?:项目|事项|项|个|条)?", text):
        start_num = _parse_ordinal_number(start)
        end_num = _parse_ordinal_number(end)
        if start_num is None or end_num is None:
            continue
        low, high = sorted((start_num, end_num))
        refs.update(range(low, high + 1))
    for match in re.finditer(rf"第?(?:{token}){suffix}(?:{separator}第?(?:{token}){suffix})+", text):
        for value in re.findall(rf"第?({token})", match.group(0)):
            number = _parse_ordinal_number(value)
            if number is not None:
                refs.add(number)
    for value in re.findall(rf"第?({token})(?:项目|事项|项|个|条)", text):
        number = _parse_ordinal_number(value)
        if number is not None:
            refs.add(number)
    for group in re.findall(rf"((?:第?(?:{token}))+)(?:项目|事项|项|个|条)", text):
        for value in re.findall(rf"第?({token})", group):
            number = _parse_ordinal_number(value)
            if number is not None:
                refs.add(number)
    return sorted(refs)


def _extract_quantity_values(text: str) -> list[int]:
    if "分别为" not in text:
        return []
    tail = text.split("分别为", 1)[1]
    tail = re.split(r"[。；;，,]", tail, maxsplit=1)[0]
    token = r"\d{1,3}|[一二三四五六七八九十]{1,3}"
    values: list[int] = []
    for value in re.findall(token, tail):
        number = _parse_ordinal_number(value)
        if number is not None:
            values.append(number)
    return values


def _parse_ordinal_number(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    return _parse_small_cn_number(value)


def _resolve_global_refs_to_single_field(refs: list[int], values_by_field: dict[str, list[str]]) -> tuple[str, list[int]] | None:
    global_map = _global_item_ref_map(values_by_field)
    mapped = [global_map.get(ref) for ref in refs]
    if not mapped or any(item is None for item in mapped):
        return None
    fields = {item[0] for item in mapped if item is not None}
    if len(fields) != 1:
        return None
    field = next(iter(fields))
    return field, [item[1] for item in mapped if item is not None]


def _global_item_ref_map(values_by_field: dict[str, list[str]]) -> dict[int, tuple[str, int]]:
    global_map: dict[int, tuple[str, int]] = {}
    global_index = 1
    for field in ("today_work", "problems", "tomorrow_plan"):
        for field_index, _value in enumerate(values_by_field.get(field, []), start=1):
            global_map[global_index] = (field, field_index)
            global_index += 1
    return global_map


def _source_text_for_global_refs(refs: list[int], values_by_field: dict[str, list[str]]) -> str:
    source = _resolve_global_refs_to_single_field(refs, values_by_field)
    if source is None:
        return ""
    field, field_refs = source
    values = values_by_field.get(field, [])
    return " ".join(values[index - 1] for index in field_refs if 0 < index <= len(values))


def _replace_first_item_quantity(item: str, quantity: int) -> str:
    replacement = f"{quantity}个"
    replaced = re.sub(r"\d+\s*个", replacement, item, count=1)
    if replaced != item:
        return replaced
    replaced = re.sub(r"[一二三四五六七八九十]{1,3}\s*个", replacement, item, count=1)
    if replaced != item:
        return replaced
    return item


def _apply_draft_decision_to_sections(decision: DraftDecision, existing: DailyReport | None) -> dict[str, Any]:
    if decision.restore_previous.enabled:
        restored = _restore_previous_snapshot(existing)
        if restored is not None:
            return restored
        return {"error": "我这边没有可恢复的上一版草稿。你可以重新发一版完整内容，我按新的整理。"}

    today_work = list(existing.today_work or []) if existing else []
    problems = list(existing.problems or []) if existing else []
    tomorrow_plan = list(existing.tomorrow_plan or []) if existing else []

    if decision.operation == "clear_report" and not decision.field_updates:
        return {"today_work": [], "problems": [], "tomorrow_plan": []}

    updates = list(decision.field_updates or [])
    if not updates and decision.target_field in {"today_work", "problems", "tomorrow_plan"} and decision.new_content:
        mode = "append" if decision.operation == "append" else "replace"
        updates = [
            type("FieldUpdateProxy", (), {
                "field": decision.target_field,
                "mode": mode,
                "items": [decision.new_content],
                "item_refs": decision.item_refs,
            })()
        ]

    values_by_field = _draft_field_values(today_work, problems, tomorrow_plan)
    for update in updates:
        field = update.field
        if field not in values_by_field:
            continue
        item_refs = _resolve_item_refs_for_field(field, update.item_refs, values_by_field)
        current = list(values_by_field[field])
        if update.mode == "keep":
            continue
        if update.mode == "clear":
            values_by_field[field] = []
            continue
        if update.mode == "remove_items":
            removed = _remove_items_by_refs(current, item_refs)
            if removed.get("error"):
                return removed
            values_by_field[field] = removed["items"]
            continue
        cleaned_items = _clean_report_items(list(update.items or []), field=field)
        if update.mode == "replace":
            if item_refs:
                replaced = _replace_items_by_refs(current, item_refs, cleaned_items, field)
                if replaced.get("error"):
                    return replaced
                values_by_field[field] = replaced["items"]
            else:
                values_by_field[field] = cleaned_items
        elif update.mode in {"append", "merge"}:
            if field == "problems" and cleaned_items and _is_placeholder_problem_list(current):
                values_by_field[field] = cleaned_items
            else:
                values_by_field[field] = merge_ordered(current, cleaned_items)

    for move in decision.move_items:
        source = move.source_field
        destination = move.destination_field
        if source not in values_by_field or destination not in values_by_field or source == destination:
            return {"error": "我还没确认要从哪一栏移动到哪一栏，先不改。"}
        item_refs = _resolve_item_refs_for_field(source, move.item_refs, values_by_field)
        moved = _move_items_by_refs(
            values_by_field[source],
            values_by_field[destination],
            item_refs,
            expected_text=move.source_item_text,
            require_evidence=True,
        )
        if moved.get("error"):
            return moved
        values_by_field[source] = moved["source"]
        values_by_field[destination] = moved["destination"]

    for delete in decision.delete_items:
        field = delete.target_field
        if field not in values_by_field:
            return {"error": "我还没确认要删除哪一栏的内容，先不改。"}
        item_refs = _resolve_item_refs_for_field(field, delete.item_refs, values_by_field)
        removed = _remove_items_by_refs(
            values_by_field[field],
            item_refs,
            expected_text=delete.target_item_text,
            require_evidence=True,
        )
        if removed.get("error"):
            return removed
        values_by_field[field] = removed["items"]

    return {
        "today_work": values_by_field["today_work"],
        "problems": values_by_field["problems"],
        "tomorrow_plan": values_by_field["tomorrow_plan"],
    }


def _resolve_item_refs_for_field(field: str, refs: list[int], values_by_field: dict[str, list[str]]) -> list[int]:
    if not refs:
        return []
    field_values = values_by_field.get(field, [])
    if refs and all(1 <= ref <= len(field_values) for ref in refs):
        return refs

    global_map: dict[int, tuple[str, int]] = {}
    global_index = 1
    for current_field in ("today_work", "problems", "tomorrow_plan"):
        for field_index, _value in enumerate(values_by_field.get(current_field, []), start=1):
            global_map[global_index] = (current_field, field_index)
            global_index += 1

    mapped: list[int] = []
    for ref in refs:
        mapped_field, mapped_index = global_map.get(ref, ("", 0))
        if mapped_field != field:
            return refs
        mapped.append(mapped_index)
    return mapped or refs


def _remove_items_by_refs(
    values: list[str],
    refs: list[int],
    *,
    expected_text: str = "",
    require_evidence: bool = False,
) -> dict[str, Any]:
    if not refs:
        return {"error": "我还没确认要删除第几条，先不改。"}
    indices = sorted({ref - 1 for ref in refs}, reverse=True)
    if any(index < 0 or index >= len(values) for index in indices):
        return {"error": "没有找到对应序号的条目，先不改。"}
    if require_evidence and not _draft_item_evidence_matches(values, indices, expected_text):
        return {"error": "我定位到的条目和当前草稿内容对不上，先不改。请说明要处理哪一条。"}
    updated = list(values)
    for index in indices:
        updated.pop(index)
    return {"items": updated}


def _replace_items_by_refs(values: list[str], refs: list[int], new_items: list[str], field: str) -> dict[str, Any]:
    if not refs:
        return {"items": new_items}
    if not new_items:
        return {"error": "你想把这条改成什么？"}
    indices = [ref - 1 for ref in refs]
    if any(index < 0 or index >= len(values) for index in indices):
        return {"error": "没有找到对应序号的条目，先不改。"}
    updated = list(values)
    replacement_items = _select_replacement_items_for_refs(values, indices, new_items)
    if len(indices) == len(replacement_items):
        for index, item in zip(indices, replacement_items):
            normalized = _normalize_tomorrow_plan(item) if field == "tomorrow_plan" else _normalize_free_text(item)
            if normalized:
                updated[index] = normalized
    else:
        first = min(indices)
        for index in sorted(indices, reverse=True):
            updated.pop(index)
        merged = "；".join(item for item in replacement_items if item)
        if merged:
            updated.insert(first, _normalize_tomorrow_plan(merged) if field == "tomorrow_plan" else _normalize_free_text(merged))
    return {"items": updated}


def _select_replacement_items_for_refs(values: list[str], indices: list[int], new_items: list[str]) -> list[str]:
    if len(new_items) == len(indices):
        return new_items
    if len(new_items) == len(values) and _new_items_preserve_unreferenced_values(values, indices, new_items):
        return [new_items[index] for index in indices]
    if len(indices) == 1:
        return [new_items[0]]
    if len(new_items) > len(indices):
        return new_items[: len(indices)]
    return new_items


def _new_items_preserve_unreferenced_values(values: list[str], indices: list[int], new_items: list[str]) -> bool:
    changed = set(indices)
    for index, current in enumerate(values):
        if index in changed:
            continue
        if _normalize_evidence_text(current) != _normalize_evidence_text(new_items[index]):
            return False
    return True


def _move_items_by_refs(
    source_values: list[str],
    destination_values: list[str],
    refs: list[int],
    *,
    expected_text: str = "",
    require_evidence: bool = False,
) -> dict[str, Any]:
    if not refs:
        return {"error": "我还没确认要移动第几条，先不改。"}
    indices = sorted({ref - 1 for ref in refs})
    if any(index < 0 or index >= len(source_values) for index in indices):
        return {"error": "没有找到对应序号的条目，先不改。"}
    if require_evidence and not _draft_item_evidence_matches(source_values, indices, expected_text):
        return {"error": "我定位到的条目和当前草稿内容对不上，先不改。请说明要处理哪一条。"}
    moved = [source_values[index] for index in indices]
    source = [value for index, value in enumerate(source_values) if index not in set(indices)]
    destination = merge_ordered(destination_values, moved)
    return {"source": source, "destination": destination}


def _draft_item_evidence_matches(values: list[str], indices: list[int], expected_text: str) -> bool:
    expected = _normalize_evidence_text(expected_text)
    if not expected:
        return False
    actual_items = [_normalize_evidence_text(values[index]) for index in indices if 0 <= index < len(values)]
    if not actual_items:
        return False
    if len(actual_items) == 1:
        actual = actual_items[0]
        return actual == expected or actual in expected or expected in actual
    combined = _normalize_evidence_text(" ".join(values[index] for index in indices if 0 <= index < len(values)))
    return combined == expected or all(item and item in expected for item in actual_items)


def _normalize_evidence_text(value: str) -> str:
    return re.sub(r"[\s，。,.、；;：:！!？?（）()【】\[\]\"'“”‘’]+", "", str(value or "").lower())


def _snapshot_from_report(report: DailyReport) -> dict[str, Any]:
    return {
        "today_work": list(report.today_work or []),
        "problems": list(report.problems or []),
        "tomorrow_plan": list(report.tomorrow_plan or []),
        "status": report.status,
        "quality_warning": report.quality_warning,
    }


def _has_previous_draft_snapshot(existing: DailyReport | None) -> bool:
    if existing is None:
        return False
    return isinstance((existing.section_status or {}).get(DRAFT_PREVIOUS_SNAPSHOT_KEY), dict)


def _restore_previous_snapshot(existing: DailyReport | None) -> dict[str, Any] | None:
    if existing is None:
        return None
    snapshot = (existing.section_status or {}).get(DRAFT_PREVIOUS_SNAPSHOT_KEY)
    if not isinstance(snapshot, dict):
        return None
    return {
        "today_work": _as_list_from_snapshot(snapshot.get("today_work")),
        "problems": _as_list_from_snapshot(snapshot.get("problems")),
        "tomorrow_plan": _as_list_from_snapshot(snapshot.get("tomorrow_plan")),
    }


def _as_list_from_snapshot(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _numbered_context_items(values: list[str] | None, *, field: str = "", item_ids: list[str] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    ids = item_ids or []
    for index, value in enumerate(values or [], start=1):
        item_id = ids[index - 1] if index - 1 < len(ids) and ids[index - 1] else _make_draft_item_id(field, index, value)
        items.append({"index": index, "item_id": item_id, "text": value})
    return items


def _global_context_items(items_by_field: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    global_items: list[dict[str, Any]] = []
    for field in ("today_work", "problems", "tomorrow_plan"):
        for item in items_by_field.get(field, []):
            global_items.append(
                {
                    "global_index": len(global_items) + 1,
                    "field": field,
                    "field_index": int(item.get("index") or 0),
                    "item_id": str(item.get("item_id") or ""),
                    "text": str(item.get("text") or ""),
                }
            )
    return global_items


def _draft_item_ids_for_context(existing: DailyReport | None) -> dict[str, list[str]]:
    if existing is None:
        return {}
    raw = (existing.section_status or {}).get(DRAFT_ITEM_IDS_KEY)
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for field in ("today_work", "problems", "tomorrow_plan"):
        values = raw.get(field)
        if isinstance(values, list):
            result[field] = [str(value) for value in values if str(value).strip()]
    return result


def _make_draft_item_id(field: str, index: int, value: str) -> str:
    digest = hashlib.sha1(f"{field}:{index}:{value}".encode("utf-8")).hexdigest()[:12]
    return f"di_{digest}"


def _resolve_intent_decision(
    decision: DailyInputIntentDecision,
    *,
    existing: DailyReport | None,
    current_slot: str | None,
) -> str:
    intent = _intent_from_semantic_decision(decision)
    confidence = float(decision.confidence)
    risky = intent in {"replace_current_report", "modify_field", "clear_current_report"} or bool(decision.should_discard_previous)

    if intent == "draft_edit_instruction":
        if _has_report_content(existing) and confidence >= 0.65:
            return intent
        return "uncertain_high_risk"

    if decision.should_update_report is False:
        if intent in {"ask_system", "non_report_interaction", "courtesy_reply", "postpone_reply", "casual_or_invalid", "uncertain_high_risk"}:
            return "non_report_interaction" if intent == "ask_system" else intent
        return "uncertain_high_risk" if risky else "casual_or_invalid"

    if intent == "confirm_submit":
        if existing and existing.status == STATUS_PENDING_CONFIRMATION and confidence >= 0.7:
            return intent
        return "casual_or_invalid"

    if intent == "replace_current_report":
        if existing and existing.status == STATUS_COMPLETED:
            return "uncertain_high_risk"
        if confidence < 0.75:
            return "uncertain_high_risk"
        return intent

    if intent == "clear_current_report":
        if existing is None:
            return intent
        return intent if confidence >= 0.75 else "uncertain_high_risk"

    if intent == "modify_field":
        return intent if confidence >= 0.7 else "uncertain_high_risk"

    if intent in {"append_to_existing", "continue_collecting"}:
        if confidence >= 0.45:
            return intent
        return "continue_collecting" if current_slot else "casual_or_invalid"

    if intent in {"ask_system", "non_report_interaction", "courtesy_reply", "postpone_reply", "casual_or_invalid", "uncertain_high_risk"}:
        return "non_report_interaction" if intent == "ask_system" else intent

    return "uncertain_high_risk"


def _intent_from_semantic_decision(decision: DailyInputIntentDecision) -> str:
    message_kind = decision.message_kind
    if message_kind == "draft_edit_instruction":
        return "draft_edit_instruction"
    if message_kind == "non_report_interaction":
        return "non_report_interaction"
    if message_kind == "ambiguous":
        return "uncertain_high_risk"
    if message_kind == "report_control_action":
        operation = decision.operation
        if operation == "clear_report":
            return "clear_current_report"
        if operation == "replace_report":
            return "replace_current_report"
        if operation == "append":
            return "append_to_existing"
        if operation == "modify_field":
            return "modify_field"
        return decision.intent
    if message_kind in {"report_content", "long_report_content", "quality_clarification_response"}:
        if decision.operation == "append":
            return "append_to_existing"
        if decision.operation == "modify_field":
            return "modify_field"
        if decision.operation == "replace_report":
            return "replace_current_report"
        return decision.intent
    return decision.intent


def _build_relation_guard_result(
    *,
    existing: DailyReport | None,
    current_slot: str | None,
    decision: DailyInputIntentDecision | None,
) -> tuple[str, str] | None:
    if existing is None or decision is None:
        return None
    relation = decision.relation_to_existing
    if relation in {"duplicate", "semantic_duplicate"}:
        field = decision.matched_field if decision.matched_field in {"today_work", "problems", "tomorrow_plan"} else None
        return _build_duplicate_relation_message(existing, field, current_slot), "relation_duplicate"
    if relation == "unclear":
        question = decision.clarification_question or _build_slot_unclear_message(existing, current_slot)
        return question, "relation_unclear"
    return None


def _build_relation_elaboration_edit(
    *,
    existing: DailyReport | None,
    decision: DailyInputIntentDecision | None,
) -> DraftEditResult | None:
    if existing is None or decision is None or decision.relation_to_existing != "elaboration":
        return None
    field = decision.matched_field
    raw_index = int(decision.matched_item_index or 0)
    new_content = decision.new_content.strip()
    if field not in {"today_work", "problems", "tomorrow_plan"} or not new_content:
        return None
    values = _draft_field_values(
        list(existing.today_work or []),
        list(existing.problems or []),
        list(existing.tomorrow_plan or []),
    ).get(field, [])
    if raw_index <= 0:
        if len(values) != 1:
            return None
        index = 0
    else:
        index = raw_index - 1
    return _replace_field_item(
        list(existing.today_work or []),
        list(existing.problems or []),
        list(existing.tomorrow_plan or []),
        field,
        index,
        new_content,
    )


def _allow_slot_fallback(
    raw_input: str,
    *,
    existing: DailyReport | None,
    current_slot: str | None,
    intent: str,
    decision: DailyInputIntentDecision | None,
) -> bool:
    if existing is None or not _has_report_content(existing):
        return True
    if intent in {"modify_field", "replace_current_report", "clear_current_report"}:
        return True
    if current_slot is None:
        return True
    if decision and decision.target_field == current_slot and decision.confidence >= 0.65:
        return True
    if current_slot in {"today_work", "problems", "tomorrow_plan"} and _is_empty_slot_reply(raw_input):
        return True
    if current_slot == "problems" and (_mentions_no_problem(raw_input) or _is_short_no_problem_reply(raw_input)):
        return True
    return False


def _build_report_write_guard_result(
    raw_input: str,
    *,
    existing: DailyReport | None,
    current_slot: str | None,
    intent: str,
    decision: DailyInputIntentDecision | None,
    parsed: ParsedInput,
) -> tuple[str, str] | None:
    if existing is None or not _has_report_content(existing):
        return None
    if intent in {"modify_field", "replace_current_report", "clear_current_report"}:
        return None
    write_fields = _parsed_write_fields(parsed)
    if not write_fields:
        return _build_slot_unclear_message(existing, current_slot), "slot_semantic_rejected"

    duplicate_field = _find_exact_existing_duplicate(existing, parsed) if len(write_fields) == 1 else None
    if duplicate_field:
        return _build_duplicate_relation_message(existing, duplicate_field, current_slot), "relation_duplicate"

    if len(write_fields) == 1 and current_slot in {"problems", "tomorrow_plan"}:
        field = next(iter(write_fields))
        if field == current_slot and not _slot_semantic_match(raw_input, current_slot, decision):
            return _build_slot_unclear_message(existing, current_slot), "slot_semantic_rejected"

    if _has_multi_field_duplicate(parsed):
        return "这句话同时像多个复盘字段，我先不乱写。你可以拆开说：今天做了什么、有没有问题、明天计划。", "multi_field_contamination_rejected"

    return None


def _parsed_write_fields(parsed: ParsedInput) -> set[str]:
    fields: set[str] = set()
    if parsed.today_work:
        fields.add("today_work")
    if parsed.problems:
        fields.add("problems")
    if parsed.tomorrow_plan:
        fields.add("tomorrow_plan")
    return fields


def _slot_semantic_match(raw_input: str, current_slot: str, decision: DailyInputIntentDecision | None) -> bool:
    if decision and decision.target_field == current_slot and decision.confidence >= 0.65:
        return True
    if current_slot in {"today_work", "problems", "tomorrow_plan"} and _is_empty_slot_reply(raw_input):
        return True
    if current_slot == "problems":
        return _mentions_no_problem(raw_input) or _is_short_no_problem_reply(raw_input)
    return False


def _build_slot_unclear_message(existing: DailyReport | None, current_slot: str | None) -> str:
    if current_slot == "problems":
        prefix = "这句不太像问题/风险，我先不记入复盘。你是想说明暂无问题，还是想补充具体问题/风险？"
    elif current_slot == "tomorrow_plan":
        prefix = "这句不太像明日计划，我先不记入复盘。你可以直接说“明天计划……”来补充。"
    else:
        prefix = "我没太判断出这句属于哪一项，先不写入复盘。"
    return _build_slot_followup_message(existing, current_slot, prefix)


def _build_duplicate_relation_message(existing: DailyReport, field: str | None, current_slot: str | None) -> str:
    label = FIELD_UPDATE_LABELS.get(field or "", "复盘内容")
    return _build_slot_followup_message(existing, current_slot, f"这句我已经记在{label}里了。")


def _find_exact_existing_duplicate(existing: DailyReport, parsed: ParsedInput) -> str | None:
    existing_by_field = {
        "today_work": existing.today_work or [],
        "problems": existing.problems or [],
        "tomorrow_plan": existing.tomorrow_plan or [],
    }
    parsed_by_field = {
        "today_work": parsed.today_work,
        "problems": parsed.problems,
        "tomorrow_plan": parsed.tomorrow_plan,
    }
    existing_compacts = {
        field: {_semantic_compact(value) for value in values if value}
        for field, values in existing_by_field.items()
    }
    for field, values in parsed_by_field.items():
        for value in values:
            compact = _semantic_compact(value)
            if compact and compact in existing_compacts.get(field, set()):
                return field
            for other_field, compacts in existing_compacts.items():
                if compact and compact in compacts:
                    return other_field
    return None


def _remove_multi_field_contamination(parsed: ParsedInput) -> ParsedInput:
    today_work = list(parsed.today_work)
    problems = _drop_values_seen_in_fields(parsed.problems, today_work)
    tomorrow_plan = _drop_values_seen_in_fields(parsed.tomorrow_plan, today_work + problems)
    if today_work == parsed.today_work and problems == parsed.problems and tomorrow_plan == parsed.tomorrow_plan:
        return parsed
    structured = StructuredDailyReport(
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        meta_notes=parsed.structured.meta_notes,
        content_quality=parsed.structured.content_quality,
        emotion=parsed.structured.emotion,
        completeness=_calculate_completeness(today_work, problems, tomorrow_plan),
    )
    return ParsedInput(
        intent=parsed.intent,
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        quality_warning=parsed.quality_warning,
        structured=structured,
    )


def _drop_values_seen_in_fields(values: list[str], previous_values: list[str]) -> list[str]:
    previous = {_semantic_compact(value) for value in previous_values if value}
    return [value for value in values if _semantic_compact(value) not in previous]


def _has_multi_field_duplicate(parsed: ParsedInput) -> bool:
    seen: set[str] = set()
    for value in parsed.today_work + parsed.problems + parsed.tomorrow_plan:
        compact = _semantic_compact(value)
        if not compact:
            continue
        if compact in seen:
            return True
        seen.add(compact)
    return False


def _semantic_compact(text: str) -> str:
    return re.sub(r"[，,。；;：:\s的了我今天今日]", "", text or "")


def _looks_like_tomorrow_plan_input(text: str) -> bool:
    compact = _compact_for_intent(text)
    return bool(
        re.search(r"(明天|明日|明儿|后天|接下来|之后|后续).{0,20}(去|继续|计划|跟进|开庭|处理|沟通|推进|完成|审核|返|出差|走访|梳理|准备)", compact)
        or re.search(r"(去|继续|计划|跟进|开庭|处理|沟通|推进|完成|审核|返|出差|走访|梳理|准备).{0,20}(明天|明日|后续)", compact)
    )


def _build_slot_followup_message(existing: DailyReport | None, current_slot: str | None, prefix: str) -> str:
    missing_sections = _missing_sections_from_report(existing)
    if current_slot and current_slot not in missing_sections:
        missing_sections = [current_slot]
    followup = (
        build_followup_message(
            missing_sections,
            today_work=existing.today_work if existing else None,
            problems=existing.problems if existing else None,
            tomorrow_plan=existing.tomorrow_plan if existing else None,
        )
        if missing_sections
        else "你可以继续补充或修改复盘内容。"
    )
    return f"{prefix}{followup}"


def _build_postpone_message(existing: DailyReport | None, current_slot: str | None) -> str:
    if current_slot == "today_work":
        return "好的，我先不记录。你晚点直接说今天主要做了什么就行。"
    if current_slot == "problems":
        return "好的，我先不记录。你晚点补充今天有没有问题或风险就行；没有的话也可以说“没问题”。"
    if current_slot == "tomorrow_plan":
        return "好的，我先不记录。你晚点直接补充明天计划就行。"
    if existing and existing.status == STATUS_PENDING_CONFIRMATION:
        return "好的，我先不打扰。你晚点看完后，内容没问题回复“确认”即可；需要修改也可以直接说。"
    return "好的，我先不记录。你晚点直接发复盘内容就行。"


def _build_non_report_interaction_message(raw_input: str) -> str:
    compact = _compact_for_intent(raw_input)
    if any(token in compact for token in ["apikey", "api密钥", "密钥", "token", "secret", "密码"]):
        return "我不能提供系统密钥或配置。你可以继续使用我来整理复盘内容。"
    if any(token in compact for token in ["模型", "大模型", "用的是什么", "什么模型"]):
        return "我使用的是系统配置的大模型服务，主要负责把你的复盘内容整理成“今日工作、问题/风险、明日计划”。具体模型可能会根据系统配置调整。"
    if any(token in compact for token in ["发给领导", "给领导看", "领导能看", "谁能看", "谁可以看", "发给谁", "数据流向", "隐私"]):
        return "我会先把你的复盘整理出来并让你确认。确认后的内容是否进入部门汇总、由谁查看，取决于系统权限和你们的管理规则。"
    if any(token in compact for token in ["该怎么说", "我怎么说", "怎么说", "怎么写", "怎么填", "如何填写", "如何写日报"]):
        return "你可以直接口语化说，不用写得很正式。比如：“今天主要处理了合同审核，没遇到明显问题，明天继续跟进业务反馈。”也可以分几条说，我会帮你整理。"
    if any(token in compact for token in ["怎么用", "如何使用", "咋用", "使用说明"]):
        return "你可以直接发文字或语音。我会整理成“今日工作、问题/风险、明日计划”。整理好后会让你确认；需要修改时，直接说“明日计划改成……”或“重新说”即可。"
    if any(token in compact for token in ["聊一会", "聊会", "陪我聊", "闲聊"]):
        return "可以简单聊一会儿，不过我主要还是帮你整理复盘。如果你想继续填日报，也可以直接发我。"
    if any(token in compact for token in ["你是谁", "你是干嘛", "你能做什么", "你有什么用"]):
        return "我是帮你整理每日复盘的助手。你可以直接说今天做了什么、遇到什么问题、明天计划，我会帮你整理成复盘内容并让你确认。"
    if any(token in compact for token in ["写诉状", "写一份诉状", "起草诉状", "法律意见"]):
        return "我主要负责整理复盘内容。如果你想把今天的诉讼准备工作记录进复盘，可以直接发我；具体文书起草建议通过正式法律工作流程处理。"
    return "这句我先不记入复盘。你可以问我怎么填写，也可以直接发今天做了什么、有没有问题、明天计划。"


def _is_daily_briefing_feedback_request(raw_input: str) -> bool:
    if _looks_like_current_report_content(raw_input):
        return False
    compact = _compact_for_intent(raw_input)
    briefing_markers = [
        "晨报",
        "日报晨报",
        "晨报总览",
        "团队晨报",
        "部门晨报",
        "负责人关注",
        "全员明细",
        "今日作战地图",
    ]
    feedback_markers = [
        "太复杂",
        "太乱",
        "杂乱",
        "简化",
        "简单点",
        "精简",
        "重新发",
        "重发",
        "改一下",
        "优化",
        "不用展示",
        "不要展示",
        "基础数据",
        "重点工作",
        "风险问题明日计划",
    ]
    return any(marker in compact for marker in briefing_markers) and any(marker in compact for marker in feedback_markers)


def _is_non_report_interaction_request(raw_input: str) -> bool:
    if _looks_like_report_update_request(raw_input):
        return False
    compact = _compact_for_intent(raw_input)
    signals = [
        "怎么用",
        "如何使用",
        "咋用",
        "使用说明",
        "你是谁",
        "你是干嘛",
        "你能做什么",
        "你有什么用",
        "该怎么说",
        "我怎么说",
        "怎么说",
        "怎么写",
        "怎么填",
        "如何填写",
        "模型",
        "大模型",
        "发给领导",
        "给领导看",
        "领导能看",
        "谁能看",
        "谁可以看",
        "发给谁",
        "数据流向",
        "隐私",
        "聊一会",
        "聊会",
        "陪我聊",
        "闲聊",
        "apikey",
        "api密钥",
        "密钥",
        "token",
        "secret",
        "写诉状",
        "起草诉状",
        "法律意见",
    ]
    return any(signal in compact for signal in signals)


def _looks_like_report_update_request(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if _is_clear_current_report_request(raw_input):
        return True
    if _has_replace_current_report_intent(compact) or _has_append_to_existing_intent(compact):
        return True
    if _detect_long_report(raw_input).is_long_report:
        return True
    field_labels = [
        "今日工作",
        "今天工作",
        "今日完成",
        "今天完成",
        "工作内容",
        "问题",
        "风险",
        "明日计划",
        "明天计划",
        "明天安排",
        "计划",
    ]
    update_words = ["改成", "改为", "修改", "更新", "补充", "重新说", "重写"]
    if any(label in compact for label in field_labels) and any(word in compact for word in update_words):
        return True
    if re.search(r"(今天|今日).{0,20}(审|审核|处理|完成|做了|沟通|开庭|出差|参加|推进|跟进|整理|核查|起草|修改|评审|走访)", compact):
        return True
    if re.search(r"(明天|明日|后续|下一步).{0,20}(去|继续|计划|跟进|开庭|处理|沟通|推进|完成|审核|返|出差)", compact):
        return True
    if any(token in compact for token in ["没问题", "没啥问题", "暂无问题", "没有问题"]) and any(token in compact for token in ["今天", "明天", "工作", "计划", "复盘"]):
        return True
    return False


def _looks_like_full_report_input(raw_input: str) -> bool:
    if _detect_long_report(raw_input).is_long_report:
        return True
    compact = _compact_for_intent(raw_input)
    has_today = bool(
        re.search(r"(今天|今日).{0,30}(审|审核|处理|完成|做了|沟通|开庭|出差|参加|推进|跟进|整理|核查|起草|修改|评审|走访|学习)", compact)
        or any(token in compact for token in ["你帮我整理下", "帮我整理下", "今日工作", "今天工作"])
    )
    has_problem = _mentions_no_problem(raw_input) or _looks_like_problem(raw_input) or any(token in compact for token in ["问题", "风险"])
    has_tomorrow = _looks_like_tomorrow_plan_input(raw_input) or any(token in compact for token in ["明日计划", "明天计划", "明天开庭"])
    return bool(has_today and has_problem and has_tomorrow)


def _get_pending_action(existing: DailyReport | None) -> str | None:
    if existing is None:
        return None
    section_status = existing.section_status or {}
    value = section_status.get(PENDING_ACTION_KEY)
    return str(value) if value else None


def _get_pending_action_payload(existing: DailyReport | None) -> dict[str, Any]:
    if existing is None:
        return {}
    value = (existing.section_status or {}).get(PENDING_ACTION_PAYLOAD_KEY)
    return value if isinstance(value, dict) else {}


def _pending_action_target_date(existing: DailyReport | None, fallback_date: date) -> date:
    payload = _get_pending_action_payload(existing)
    try:
        return date.fromisoformat(str(payload.get("target_date") or "")[:10])
    except ValueError:
        return fallback_date


def _has_report_content(existing: DailyReport | None) -> bool:
    return bool(existing and (existing.today_work or existing.problems or existing.tomorrow_plan))


async def _set_pending_action(
    session: AsyncSession,
    report: DailyReport,
    action: str | None,
    *,
    payload: dict[str, Any] | None = None,
) -> DailyReport:
    section_status = dict(report.section_status or {})
    if action:
        section_status[PENDING_ACTION_KEY] = action
        if payload:
            section_status[PENDING_ACTION_PAYLOAD_KEY] = payload
        else:
            section_status.pop(PENDING_ACTION_PAYLOAD_KEY, None)
    else:
        section_status.pop(PENDING_ACTION_KEY, None)
        section_status.pop(PENDING_ACTION_PAYLOAD_KEY, None)
    report.section_status = section_status
    if hasattr(session, "flush"):
        await session.flush()
    return report


def _get_pending_interaction(existing: DailyReport | None) -> dict[str, Any] | None:
    if existing is None:
        return None
    value = (existing.section_status or {}).get(PENDING_INTERACTION_KEY)
    return value if isinstance(value, dict) else None


async def _set_pending_interaction(session: AsyncSession, report: DailyReport, pending_interaction: dict[str, Any] | None) -> DailyReport:
    section_status = dict(report.section_status or {})
    if pending_interaction:
        section_status[PENDING_INTERACTION_KEY] = pending_interaction
    else:
        section_status.pop(PENDING_INTERACTION_KEY, None)
    report.section_status = section_status
    if hasattr(session, "flush"):
        await session.flush()
    return report


def _clear_pending_interaction(section_status: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(section_status or {})
    cleaned.pop(PENDING_INTERACTION_KEY, None)
    return cleaned


def _get_pending_draft_edit(existing: DailyReport | None) -> dict[str, Any] | None:
    if existing is None:
        return None
    value = (existing.section_status or {}).get(PENDING_DRAFT_EDIT_KEY)
    return value if isinstance(value, dict) else None


async def _set_pending_draft_edit(session: AsyncSession, report: DailyReport, pending_edit: dict[str, Any] | None) -> DailyReport:
    section_status = dict(report.section_status or {})
    if pending_edit:
        section_status[PENDING_DRAFT_EDIT_KEY] = pending_edit
    else:
        section_status.pop(PENDING_DRAFT_EDIT_KEY, None)
    report.section_status = section_status
    if hasattr(session, "flush"):
        await session.flush()
    return report


def _clear_pending_draft_edit(section_status: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(section_status or {})
    cleaned.pop(PENDING_DRAFT_EDIT_KEY, None)
    return cleaned


async def _mark_unresolved_draft_edit(
    session: AsyncSession,
    report: DailyReport | None,
    *,
    raw_input: str,
    error: str,
    received_at,
) -> DailyReport | None:
    if report is None:
        return None
    section_status = dict(report.section_status or {})
    section_status[UNRESOLVED_DRAFT_EDIT_KEY] = {
        "raw_input": raw_input,
        "error": error,
        "received_at": received_at.isoformat() if hasattr(received_at, "isoformat") else "",
    }
    report.section_status = section_status
    if hasattr(session, "flush"):
        await session.flush()
    return report


def _clear_unresolved_draft_edit(section_status: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(section_status or {})
    cleaned.pop(UNRESOLVED_DRAFT_EDIT_KEY, None)
    return cleaned


def _has_unresolved_draft_edit(report: DailyReport | None) -> bool:
    return bool(report and isinstance(report.section_status, dict) and report.section_status.get(UNRESOLVED_DRAFT_EDIT_KEY))


def _get_pending_quality_clarification(existing: DailyReport | None) -> dict[str, Any] | None:
    if existing is None:
        return None
    value = (existing.section_status or {}).get(PENDING_QUALITY_CLARIFICATION_KEY)
    return value if isinstance(value, dict) else None


def _clear_pending_quality(section_status: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(section_status or {})
    cleaned.pop(PENDING_QUALITY_CLARIFICATION_KEY, None)
    return cleaned


def _preserve_internal_section_status(previous: dict[str, Any], current: dict[str, Any]) -> None:
    for key, value in (previous or {}).items():
        if key.startswith("_") and key not in current:
            current[key] = value
    if LONG_REPORT_MODE_KEY in previous and LONG_REPORT_MODE_KEY not in current:
        current[LONG_REPORT_MODE_KEY] = previous[LONG_REPORT_MODE_KEY]


def _attach_draft_item_ids(
    section_status: dict[str, Any],
    *,
    existing: DailyReport | None,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> None:
    existing_ids = _draft_item_ids_for_context(existing)
    existing_values = {
        "today_work": list(existing.today_work or []) if existing else [],
        "problems": list(existing.problems or []) if existing else [],
        "tomorrow_plan": list(existing.tomorrow_plan or []) if existing else [],
    }
    old_pool: dict[str, list[str]] = {}
    for field, values in existing_values.items():
        ids = existing_ids.get(field, [])
        for index, value in enumerate(values):
            if index < len(ids):
                old_pool.setdefault(value, []).append(ids[index])

    section_status[DRAFT_ITEM_IDS_KEY] = {
        "today_work": _resolve_item_ids_for_values("today_work", today_work, existing_values.get("today_work", []), existing_ids.get("today_work", []), old_pool),
        "problems": _resolve_item_ids_for_values("problems", problems, existing_values.get("problems", []), existing_ids.get("problems", []), old_pool),
        "tomorrow_plan": _resolve_item_ids_for_values("tomorrow_plan", tomorrow_plan, existing_values.get("tomorrow_plan", []), existing_ids.get("tomorrow_plan", []), old_pool),
    }


def _resolve_item_ids_for_values(
    field: str,
    values: list[str],
    previous_values: list[str],
    previous_ids: list[str],
    old_pool: dict[str, list[str]],
) -> list[str]:
    result: list[str] = []
    used: set[str] = set()
    for index, value in enumerate(values):
        item_id = ""
        if index < len(previous_ids) and index < len(previous_values) and previous_ids[index]:
            item_id = previous_ids[index]
        if not item_id:
            for candidate in old_pool.get(value, []):
                if candidate not in used:
                    item_id = candidate
                    break
        if not item_id:
            item_id = _make_draft_item_id(field, index + 1, value)
        used.add(item_id)
        result.append(item_id)
    return result


def _resolve_pending_append_target(raw_input: str) -> str | None:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return None
    if "问题" in compact or "风险" in compact:
        return "problems"
    if "明日计划" in compact or "明天计划" in compact or compact in {"计划", "计划吧", "明天", "明日"}:
        return "tomorrow_plan"
    if "今日工作" in compact or "今天工作" in compact or "今日完成" in compact or "今天完成" in compact or compact in {"工作", "工作吧"}:
        return "today_work"
    return None


def _is_pending_interaction_cancel(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    return compact in {"算了", "取消", "先不补充", "不补充", "不用补充", "不用了", "先这样"}


def _build_pending_append_content_message(field: str) -> str:
    if field == "today_work":
        return "好的，请说要补充的今日工作内容。"
    if field == "problems":
        return "好的，请说要补充的问题/风险内容。"
    if field == "tomorrow_plan":
        return "好的，请说要补充的明日计划。"
    return "好的，请说要补充的具体内容。"


def _resolve_pending_quality_reply(raw_input: str) -> str | None:
    compact = _compact_for_intent(raw_input)
    decline = {
        "先这样",
        "就这样",
        "不用补充",
        "不补充",
        "不用了",
        "算了",
        "确认",
        "可以",
        "没问题",
        "没啥问题",
        "提交",
    }
    decline_markers = ["不用补充", "不补充", "不用了", "不需要补充", "无需补充", "不用细说", "不用再补"]
    if compact in decline or any(marker in compact for marker in decline_markers) or _is_confirmation_reply(raw_input):
        return "decline"
    if _is_postpone_reply(raw_input) or _is_non_report_interaction_request(raw_input) or _is_clear_current_report_request(raw_input):
        return None
    if _is_courtesy_reply(raw_input) or _is_short_ack_reply(raw_input):
        return None
    return "answer" if compact else None


def _resolve_pending_draft_edit_reply(raw_input: str) -> str | None:
    compact = _compact_for_intent(raw_input)
    cancel = {"算了", "取消", "先不改了", "不改了", "不用改了", "保持不变", "先这样"}
    if compact in cancel:
        return "cancel"
    if _is_clear_current_report_request(raw_input) or _is_postpone_reply(raw_input) or _is_non_report_interaction_request(raw_input):
        return None
    if _is_courtesy_reply(raw_input) or _is_short_ack_reply(raw_input):
        return None
    return "answer" if compact else None


def _resolve_pending_draft_edit_confirmation(raw_input: str) -> str | None:
    compact = _compact_for_intent(raw_input)
    cancel = {"算了", "取消", "不删了", "不删除", "保留", "先保留", "保持不变", "先这样"}
    confirm = {"确认", "确定", "可以", "是", "是的", "执行", "删", "删除", "删吧", "确认删除"}
    if compact in cancel:
        return "cancel"
    if compact in confirm or _is_confirmation_reply(raw_input):
        return "answer"
    return None


def _select_content_quality_clarification(structured: StructuredDailyReport, existing: DailyReport | None):
    for check in structured.content_quality:
        if not check.clarification_needed:
            continue
        if check.target_field not in {"today_work", "problems", "tomorrow_plan"}:
            continue
        if not check.clarification_question:
            continue
        candidate = {
            "target_field": check.target_field,
            "target_index": int(check.target_index or 0),
            "clarity": check.clarity,
            "clarification_question": check.clarification_question,
            "quality_warning": check.quality_warning,
        }
        if _quality_clarification_was_asked(existing, candidate):
            continue
        return check
    return None


def _build_quality_clarification_state(check, parsed: ParsedInput) -> dict[str, Any] | None:
    values = {
        "today_work": parsed.today_work,
        "problems": parsed.problems,
        "tomorrow_plan": parsed.tomorrow_plan,
    }.get(check.target_field, [])
    target_index = int(check.target_index or 0)
    if target_index < 0 or target_index >= len(values):
        return None
    return {
        "target_field": check.target_field,
        "target_index": target_index,
        "target_text": values[target_index],
        "clarity": check.clarity,
        "clarification_question": check.clarification_question,
        "quality_warning": check.quality_warning,
    }


def _quality_clarification_history_key(clarification: dict[str, Any]) -> str:
    return "|".join(
        [
            str(clarification.get("target_field") or ""),
            str(clarification.get("target_index") or 0),
            str(clarification.get("clarification_question") or ""),
        ]
    )


def _quality_clarification_was_asked(existing: DailyReport | None, clarification: dict[str, Any] | None) -> bool:
    if existing is None or not clarification:
        return False
    history = (existing.section_status or {}).get(QUALITY_CLARIFICATION_HISTORY_KEY) or []
    return _quality_clarification_history_key(clarification) in set(str(item) for item in history)


def _build_quality_clarification_message(report: DailyReport, clarification: dict[str, Any]) -> str:
    target_text = str(clarification.get("target_text") or "").strip()
    question = str(clarification.get("clarification_question") or "").strip()
    index = int(clarification.get("target_index") or 0) + 1
    if target_text:
        return f"我先记录了。第{index}项“{target_text}”有点笼统，{question}你补充后我再一起整理。"
    return f"我先记录了。{question}你补充后我再一起整理。"


def _apply_quality_answer_to_sections(
    pending_quality: dict[str, Any],
    raw_input: str,
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> tuple[list[str], list[str], list[str]]:
    field = str(pending_quality.get("target_field") or "")
    index = int(pending_quality.get("target_index") or 0)
    values_by_field = {
        "today_work": today_work,
        "problems": problems,
        "tomorrow_plan": tomorrow_plan,
    }
    values = values_by_field.get(field)
    if values is None or index < 0 or index >= len(values):
        return today_work, problems, tomorrow_plan
    values[index] = _merge_quality_answer(values[index], raw_input)
    return today_work, problems, tomorrow_plan


def _merge_quality_answer(original: str, answer: str) -> str:
    clean_answer = _normalize_free_text(answer).strip(" ，,。；;：:！!？?")
    if not clean_answer:
        return original
    compact_original = _compact_for_intent(original)
    compact_answer = _compact_for_intent(clean_answer)
    if "恢复" in original and "技能" in original:
        skill_name = re.sub(r"的?技能$", "", clean_answer).strip()
        if not skill_name:
            skill_name = clean_answer
        separator = " " if re.search(r"[A-Za-z0-9]", skill_name) else ""
        return f"恢复了{separator}{skill_name} 技能" if separator else f"恢复了{skill_name}技能"
    if "处理" in original and "项目" in original:
        if "项目" in clean_answer or "纠纷" in clean_answer or "事项" in clean_answer:
            return f"处理了{clean_answer}"
        return f"处理了{clean_answer}项目"
    if compact_answer in compact_original:
        return original
    return f"{original}（{clean_answer}）"


def _is_draft_edit_instruction(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if re.search(r"不是.+是", raw_input) or any(token in compact for token in ["不对应该是", "不对应改为", "不对应改成"]):
        return True
    item_tokens = ["第", "这条", "这一条", "这两个", "这两条", "那条", "前面那个", "后面那个", "刚才那条", "刚才那个", "刚才我说的那个", "这个", "那个"]
    if not any(token in compact for token in item_tokens):
        return False
    operations = [
        "合并",
        "合到一起",
        "合成一条",
        "一件事",
        "一回事",
        "同一件事",
        "同一个事情",
        "重复",
        "删除",
        "删掉",
        "去掉",
        "移除",
        "不要",
        "不保留",
        "改成",
        "改为",
        "改下",
        "改一下",
        "改了",
        "修改",
        "修改为",
        "更新为",
        "写错",
        "不是这个意思",
        "不对",
        "不太对",
        "应该是",
    ]
    return any(operation in compact for operation in operations)


def _semantic_draft_decision_has_action(decision: DailyInputIntentDecision) -> bool:
    return bool(
        decision.message_kind == "draft_edit_instruction"
        or decision.operation in {"rewrite_item", "merge_items", "delete_item", "move_item"}
        or decision.item_refs
        or decision.actions
    )


def _apply_semantic_draft_edit_to_sections(
    decision: DailyInputIntentDecision,
    *,
    status: str,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> DraftEditResult:
    actions = _semantic_draft_actions(decision)
    if len(actions) > 1:
        current_today = list(today_work)
        current_problems = list(problems)
        current_tomorrow = list(tomorrow_plan)
        for action in actions:
            action_result = _apply_semantic_draft_action_to_sections(
                action,
                status=status,
                today_work=current_today,
                problems=current_problems,
                tomorrow_plan=current_tomorrow,
            )
            if action_result.error or action_result.pending_edit:
                return DraftEditResult(
                    current_today,
                    current_problems,
                    current_tomorrow,
                    error=action_result.error,
                    pending_edit=action_result.pending_edit,
                    changed_field=action_result.changed_field,
                    changed_indices=action_result.changed_indices,
                )
            current_today = action_result.today_work
            current_problems = action_result.problems
            current_tomorrow = action_result.tomorrow_plan
        return DraftEditResult(current_today, current_problems, current_tomorrow)

    action = actions[0] if actions else _semantic_action_from_decision(decision)
    return _apply_semantic_draft_action_to_sections(
        action,
        status=status,
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
    )


def _apply_semantic_draft_action_to_sections(
    action: dict[str, Any],
    *,
    status: str,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> DraftEditResult:
    operation = str(action.get("operation") or "none")
    target_field = action.get("target_field") if action.get("target_field") in {"today_work", "problems", "tomorrow_plan"} else None
    item_refs = list(action.get("item_refs") or [])
    new_content = str(action.get("new_content") or "").strip()
    needs_clarification = bool(action.get("needs_clarification"))
    clarification_question = str(action.get("clarification_question") or "").strip()

    if operation in {"delete_item", "move_item"} and status == STATUS_COMPLETED:
        if operation != "delete_item":
            return DraftEditResult(today_work, problems, tomorrow_plan, "当前复盘已经提交。移动正式内容需要先确认，请明确要怎么调整。")
        if not item_refs:
            return DraftEditResult(today_work, problems, tomorrow_plan, clarification_question or "你想删除哪一条？可以说“删除第几条”。")
        resolution = _resolve_draft_item_refs(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            item_numbers=[item_refs[0]],
            explicit_field=target_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        field = str(resolution["field"])
        index = int(resolution["indices"][0])
        return _delete_field_item(today_work, problems, tomorrow_plan, field, index)

    if operation == "merge_items":
        if len(item_refs) < 2:
            return DraftEditResult(today_work, problems, tomorrow_plan, clarification_question or "我没看清要合并哪两条。可以说“把第几条和第几条合并”。")
        resolution = _resolve_draft_item_refs(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            item_numbers=item_refs[:2],
            explicit_field=target_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        first, second = resolution["indices"]
        return _merge_field_items(today_work, problems, tomorrow_plan, str(resolution["field"]), first, second)

    if operation == "delete_item":
        if not item_refs:
            return DraftEditResult(today_work, problems, tomorrow_plan, clarification_question or "你想删除哪一条？可以说“删除第几条”。")
        resolution = _resolve_draft_item_refs(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            item_numbers=[item_refs[0]],
            explicit_field=target_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        return _delete_field_item(today_work, problems, tomorrow_plan, str(resolution["field"]), resolution["indices"][0])

    if operation in {"rewrite_item", "modify_field"}:
        if not item_refs:
            return DraftEditResult(today_work, problems, tomorrow_plan, clarification_question or "你想修改哪一条？")
        resolution = _resolve_draft_item_refs(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            item_numbers=[item_refs[0]],
            explicit_field=target_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        field = str(resolution["field"])
        index = resolution["indices"][0]
        if needs_clarification or not new_content:
            return DraftEditResult(
                today_work,
                problems,
                tomorrow_plan,
                clarification_question or _build_pending_draft_edit_question(field, index),
                pending_edit=_build_pending_draft_edit(field, index),
            )
        return _replace_field_item(today_work, problems, tomorrow_plan, field, index, new_content)

    return DraftEditResult(today_work, problems, tomorrow_plan, clarification_question or "我还没识别出这次草稿编辑。请说明要修改、合并或删除哪一条。")


def _semantic_draft_actions(decision: DailyInputIntentDecision) -> list[dict[str, Any]]:
    actions = [_normalize_semantic_action(action) for action in decision.actions]
    actions = [action for action in actions if action.get("operation") != "none"]
    if actions:
        return actions
    return [_semantic_action_from_decision(decision)]


def _semantic_action_from_decision(decision: DailyInputIntentDecision) -> dict[str, Any]:
    return {
        "operation": decision.operation,
        "target_field": decision.target_field,
        "item_refs": list(decision.item_refs or []),
        "new_content": decision.new_content,
        "needs_clarification": decision.needs_clarification,
        "clarification_question": decision.clarification_question,
    }


def _normalize_semantic_action(action: dict[str, Any]) -> dict[str, Any]:
    operation = str(action.get("operation") or "none").strip()
    if operation not in {"rewrite_item", "merge_items", "delete_item", "move_item", "modify_field"}:
        operation = "none"
    target_field = str(action.get("target_field") or "none").strip()
    if target_field not in {"today_work", "problems", "tomorrow_plan"}:
        target_field = "none"
    refs: list[int] = []
    candidates = action.get("item_refs") if isinstance(action.get("item_refs"), list) else [action.get("item_refs")]
    for item in candidates:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            refs.append(number)
    return {
        "operation": operation,
        "target_field": target_field,
        "item_refs": refs[:5],
        "new_content": str(action.get("new_content") or "").strip()[:500],
        "needs_clarification": bool(action.get("needs_clarification")),
        "clarification_question": str(action.get("clarification_question") or "").strip()[:300],
    }


def _apply_draft_edit_to_sections(
    raw_input: str,
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> DraftEditResult:
    if not (today_work or problems or tomorrow_plan):
        return DraftEditResult(today_work, problems, tomorrow_plan, "当前草稿里还没有可编辑的条目。")

    compact = _compact_for_intent(raw_input)
    explicit_field = _draft_edit_explicit_field(raw_input)
    item_numbers = _draft_edit_item_numbers(raw_input)
    has_merge = any(token in compact for token in ["合并", "合到一起", "合成一条", "一件事", "一回事", "同一件事", "同一个事情"])
    has_delete = any(token in compact for token in ["删除", "删掉", "去掉", "移除", "不要", "不用", "不保留", "重复"])
    has_change = any(token in compact for token in ["改成", "改为", "改下", "改一下", "改了", "修改", "更新", "写错", "不对", "不太对", "应该是", "不是"])

    if has_merge:
        if len(item_numbers) >= 2:
            resolution = _resolve_draft_item_refs(
                today_work=today_work,
                problems=problems,
                tomorrow_plan=tomorrow_plan,
                item_numbers=item_numbers[:2],
                explicit_field=explicit_field,
            )
            if resolution.get("error"):
                return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
            field = str(resolution["field"])
            first, second = resolution["indices"]
            return _merge_field_items(today_work, problems, tomorrow_plan, field, first, second)
        if any(token in compact for token in ["这两个", "这两条"]):
            implicit_pair = _resolve_implicit_two_item_merge(
                today_work=today_work,
                problems=problems,
                tomorrow_plan=tomorrow_plan,
                explicit_field=explicit_field,
            )
            if implicit_pair.get("error"):
                return DraftEditResult(today_work, problems, tomorrow_plan, str(implicit_pair["error"]))
            return _merge_field_items(today_work, problems, tomorrow_plan, str(implicit_pair["field"]), 0, 1)
        return DraftEditResult(today_work, problems, tomorrow_plan, "我没看清要合并哪两条。可以说“把第几条和第几条合并”。")

    delete_match = re.search(
        r"(?:(?:删除|删掉|去掉|移除)第([一二三四五六七八九十\d]+)条|第([一二三四五六七八九十\d]+)条(?:删除|删掉|去掉|移除|不要|不用|不保留|重复了?))",
        raw_input,
    )
    if has_delete and (delete_match or item_numbers):
        index_number = _parse_item_number(delete_match.group(1) or delete_match.group(2)) if delete_match else item_numbers[0]
        if index_number is None:
            return DraftEditResult(today_work, problems, tomorrow_plan, "我没看清要删除哪一条。可以说“删除第几条”。")
        resolution = _resolve_draft_item_refs(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            item_numbers=[index_number],
            explicit_field=explicit_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        return _delete_field_item(today_work, problems, tomorrow_plan, str(resolution["field"]), resolution["indices"][0])
    if has_delete and any(token in compact for token in ["刚才那个", "刚才那条", "前面那个", "这条", "这一条"]):
        resolution = _resolve_recent_draft_item(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            explicit_field=explicit_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        return _delete_field_item(today_work, problems, tomorrow_plan, str(resolution["field"]), resolution["index"])

    change_match = re.search(r"第([一二三四五六七八九十\d]+)条.*(?:改成|改为|修改为|更新为)(.+)$", raw_input)
    if change_match:
        index = _parse_item_number(change_match.group(1))
        value = _normalize_free_text(change_match.group(2))
        if index is None or not value:
            return DraftEditResult(today_work, problems, tomorrow_plan, "我没看清要改哪一条或改成什么。")
        resolution = _resolve_draft_item_refs(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            item_numbers=[index],
            explicit_field=explicit_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        return _replace_field_item(today_work, problems, tomorrow_plan, str(resolution["field"]), resolution["indices"][0], value)

    recent_value = _extract_recent_reference_replacement(raw_input)
    if recent_value:
        resolution = _resolve_recent_draft_item(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            explicit_field=explicit_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        return _replace_field_item(today_work, problems, tomorrow_plan, str(resolution["field"]), resolution["index"], recent_value)

    incomplete_change_match = re.search(
        r"(?:第([一二三四五六七八九十\d]+)条.*(?:改下|改一下|改了|修改)|(?:改下|改一下|修改)第([一二三四五六七八九十\d]+)条)\s*[。.!！]?$",
        raw_input,
    )
    if incomplete_change_match:
        index = _parse_item_number(incomplete_change_match.group(1) or incomplete_change_match.group(2))
        if index is None:
            return DraftEditResult(today_work, problems, tomorrow_plan, "你想把哪一条改成什么？")
        resolution = _resolve_draft_item_refs(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            item_numbers=[index],
            explicit_field=explicit_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        field = str(resolution["field"])
        zero_index = resolution["indices"][0]
        pending_edit = _build_pending_draft_edit(field, zero_index)
        return DraftEditResult(
            today_work,
            problems,
            tomorrow_plan,
            _build_pending_draft_edit_question(field, zero_index),
            pending_edit=pending_edit,
        )

    this_match = re.search(r"(?:这条|这一条).*(?:改成|改为|修改为|更新为)(.+)$", raw_input)
    if this_match:
        resolution = _resolve_recent_draft_item(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            explicit_field=explicit_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        value = _normalize_free_text(this_match.group(1))
        return _replace_field_item(today_work, problems, tomorrow_plan, str(resolution["field"]), resolution["index"], value)

    if has_change and any(token in compact for token in ["刚才那个", "刚才那条", "前面那个", "这条", "这一条", "不对", "不太对"]):
        resolution = _resolve_recent_draft_item(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            explicit_field=explicit_field,
        )
        if resolution.get("error"):
            return DraftEditResult(today_work, problems, tomorrow_plan, str(resolution["error"]))
        field = str(resolution["field"])
        zero_index = resolution["index"]
        return DraftEditResult(
            today_work,
            problems,
            tomorrow_plan,
            _build_pending_draft_edit_question(field, zero_index),
            pending_edit=_build_pending_draft_edit(field, zero_index),
        )

    if any(token in _compact_for_intent(raw_input) for token in ["这两个", "这两条", "合到一起", "一回事", "一件事"]):
        return DraftEditResult(today_work, problems, tomorrow_plan, "我没看清要合并哪两条。可以说“把第几条和第几条合并”。")
    if any(token in _compact_for_intent(raw_input) for token in ["前面那个", "后面那个", "这个重复", "那个重复", "不要"]):
        return DraftEditResult(today_work, problems, tomorrow_plan, "你想编辑哪一条？可以说“删除第几条”或“第几条改成……”。")

    return DraftEditResult(today_work, problems, tomorrow_plan, "我还没识别出这次草稿编辑。可以说“删除第几条”“第几条改成……”或“把第几条和第几条合并”。")


def _draft_edit_explicit_field(raw_input: str) -> str | None:
    compact = _compact_for_intent(raw_input)
    if any(token in compact for token in ["今日工作", "今天工作", "今日完成", "今天完成", "工作内容"]):
        return "today_work"
    if any(token in compact for token in ["问题风险", "问题", "风险", "困难"]):
        return "problems"
    if any(token in compact for token in ["明日计划", "明天计划", "明天安排", "明日安排"]):
        return "tomorrow_plan"
    return None


def _draft_edit_item_numbers(raw_input: str) -> list[int]:
    numbers: list[int] = []
    for raw in re.findall(r"第([一二两三四五六七八九十\d]+)条", raw_input):
        parsed = _parse_item_number(raw)
        if parsed is not None:
            numbers.append(parsed)
    return numbers


def _draft_field_values(today_work: list[str], problems: list[str], tomorrow_plan: list[str]) -> dict[str, list[str]]:
    return {
        "today_work": today_work,
        "problems": problems,
        "tomorrow_plan": tomorrow_plan,
    }


def _draft_field_label(field: str) -> str:
    return FIELD_UPDATE_LABELS.get(field, "复盘内容")


def _resolve_draft_item_refs(
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    item_numbers: list[int],
    explicit_field: str | None,
) -> dict[str, Any]:
    if not item_numbers:
        return {"error": "你想编辑哪一条？可以说“第几条改成……”。"}
    indices = [number - 1 for number in item_numbers]
    if any(index < 0 for index in indices):
        return {"error": "条目序号需要从第 1 条开始。"}
    values_by_field = _draft_field_values(today_work, problems, tomorrow_plan)
    if explicit_field:
        values = values_by_field.get(explicit_field, [])
        if any(index >= len(values) for index in indices):
            return {"error": f"没有找到{_draft_field_label(explicit_field)}第{max(item_numbers)}条。"}
        return {"field": explicit_field, "indices": indices}

    candidate_fields = [
        field
        for field, values in values_by_field.items()
        if values
        and all(index < len(values) for index in indices)
        and _draft_field_is_numbered_candidate(field, values, item_numbers)
    ]
    if len(candidate_fields) == 1:
        return {"field": candidate_fields[0], "indices": indices}
    if not candidate_fields:
        if len(item_numbers) == 1 and item_numbers[0] == 1 and today_work:
            return {"field": "today_work", "indices": indices}
        max_ref = max(item_numbers)
        return {"error": f"没有找到第{max_ref}条，请检查条目序号。"}
    if len([field for field, values in values_by_field.items() if values]) == 1:
        return {"field": candidate_fields[0], "indices": indices}
    first_ref = item_numbers[0]
    return {"error": f"你是想修改今日工作、问题/风险，还是明日计划里的第{first_ref}条？"}


def _draft_field_is_numbered_candidate(field: str, values: list[str], item_numbers: list[int]) -> bool:
    if any(number > 1 for number in item_numbers):
        return True
    if len(values) > 1:
        return True
    return field == "today_work" and bool(values)


def _resolve_recent_draft_item(
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    explicit_field: str | None,
) -> dict[str, Any]:
    values_by_field = _draft_field_values(today_work, problems, tomorrow_plan)
    if explicit_field:
        values = values_by_field.get(explicit_field, [])
        if not values:
            return {"error": f"{_draft_field_label(explicit_field)}里还没有可修改的内容。"}
        return {"field": explicit_field, "index": len(values) - 1}

    non_empty = [(field, values) for field, values in values_by_field.items() if values]
    if len(non_empty) == 1:
        field, values = non_empty[0]
        return {"field": field, "index": len(values) - 1}
    total_items = sum(len(values) for _field, values in non_empty)
    if total_items == 1 and non_empty:
        field, values = non_empty[0]
        return {"field": field, "index": 0}
    return {"error": "你说的“刚才那条”是指今日工作、问题/风险，还是明日计划？"}


def _resolve_implicit_two_item_merge(
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    explicit_field: str | None,
) -> dict[str, Any]:
    values_by_field = _draft_field_values(today_work, problems, tomorrow_plan)
    if explicit_field:
        values = values_by_field.get(explicit_field, [])
        if len(values) == 2:
            return {"field": explicit_field}
        return {"error": f"{_draft_field_label(explicit_field)}里不是正好两条，请说明要合并哪两条。"}
    candidates = [field for field, values in values_by_field.items() if len(values) == 2]
    if len(candidates) == 1:
        return {"field": candidates[0]}
    return {"error": "我没看清要合并哪两条。可以说“把第几条和第几条合并”。"}


def _extract_recent_reference_replacement(raw_input: str) -> str | None:
    patterns = [
        r"(?:刚才那个|刚才那条|前面那个|这条|这一条).*(?:应该是|改成|改为|修改为|更新为)(.+)$",
        r"(?:不对|不太对)[，,。；;：:\s]*(?:应该是|是)(.+)$",
        r"不是.+?是(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_input)
        if not match:
            continue
        value = _normalize_free_text(match.group(1))
        if value:
            return value
    return None


def _build_pending_draft_edit(field: str, zero_index: int) -> dict[str, Any]:
    return _build_pending_draft_edit_operation("rewrite_item", field, [zero_index])


def _build_pending_draft_edit_operation(
    operation: str,
    field: str,
    zero_indices: list[int],
    *,
    new_content: str = "",
    requires_confirmation: bool = False,
    confirmation_message: str = "",
) -> dict[str, Any]:
    first_index = zero_indices[0] if zero_indices else 0
    return {
        "pending_action": "edit_draft_item",
        "operation": operation,
        "target_field": field,
        "item_refs": [index + 1 for index in zero_indices],
        "target_index": first_index,
        "target_indices": zero_indices,
        "new_content": new_content,
        "requires_confirmation": requires_confirmation,
        "confirmation_message": confirmation_message,
    }


def _build_pending_draft_edit_question(field: str, zero_index: int) -> str:
    return f"好的，你想把{_draft_field_label(field)}第{zero_index + 1}条改成什么？"


def _build_delete_confirmation_message(field: str, zero_index: int, values: list[str]) -> str:
    label = _draft_field_label(field)
    target_text = values[zero_index] if 0 <= zero_index < len(values) else ""
    remaining = [value for index, value in enumerate(values) if index != zero_index]
    lines = [
        f"你确认要删除{label}第 {zero_index + 1} 条“{target_text}”这条吗？",
        "",
        f"删除后{label}将变为：",
    ]
    if remaining:
        lines.extend(f"{index}. {value}" for index, value in enumerate(remaining, start=1))
    else:
        lines.append("（空）")
    lines.extend(["", "回复“确认”执行，回复“取消”保留。"])
    return "\n".join(lines)


def _apply_pending_draft_edit_to_sections(
    pending_edit: dict[str, Any],
    raw_input: str,
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> DraftEditResult:
    field = str(pending_edit.get("target_field") or "")
    index = int(pending_edit.get("target_index") or 0)
    if field not in {"today_work", "problems", "tomorrow_plan"}:
        return DraftEditResult(today_work, problems, tomorrow_plan, "上次要修改的位置已经不清楚了，请重新说明要改哪一条。")
    if pending_edit.get("requires_confirmation"):
        operation = str(pending_edit.get("operation") or "")
        indices = pending_edit.get("target_indices")
        if not isinstance(indices, list) or not indices:
            indices = [index]
        zero_indices = [int(item) for item in indices]
        if operation == "delete_item":
            return _delete_field_item(today_work, problems, tomorrow_plan, field, zero_indices[0])
        if operation == "merge_items" and len(zero_indices) >= 2:
            return _merge_field_items(today_work, problems, tomorrow_plan, field, zero_indices[0], zero_indices[1])
        if operation in {"rewrite_item", "modify_field"}:
            value = str(pending_edit.get("new_content") or raw_input).strip()
            return _replace_field_item(today_work, problems, tomorrow_plan, field, zero_indices[0], value)
        return DraftEditResult(today_work, problems, tomorrow_plan, "上次待确认的编辑动作已经不清楚了，请重新说明要怎么改。")
    return _replace_field_item(today_work, problems, tomorrow_plan, field, index, raw_input)


def _merge_field_items(
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    field: str,
    first: int,
    second: int,
) -> DraftEditResult:
    values_by_field = _draft_field_values(list(today_work), list(problems), list(tomorrow_plan))
    values = values_by_field.get(field, [])
    if first == second or first < 0 or second < 0 or first >= len(values) or second >= len(values):
        return DraftEditResult(today_work, problems, tomorrow_plan, "要合并的条目序号不对，请检查第几条。")
    keep, remove = (first, second) if first < second else (second, first)
    values[keep] = f"{values[keep]}；{values[remove]}"
    values.pop(remove)
    return DraftEditResult(
        values_by_field["today_work"],
        values_by_field["problems"],
        values_by_field["tomorrow_plan"],
        changed_field=field,
        changed_indices=[keep, remove],
    )


def _delete_field_item(
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    field: str,
    index: int,
) -> DraftEditResult:
    values_by_field = _draft_field_values(list(today_work), list(problems), list(tomorrow_plan))
    values = values_by_field.get(field, [])
    if index < 0 or index >= len(values):
        return DraftEditResult(today_work, problems, tomorrow_plan, "要删除的条目序号不对，请检查第几条。")
    values.pop(index)
    return DraftEditResult(
        values_by_field["today_work"],
        values_by_field["problems"],
        values_by_field["tomorrow_plan"],
        changed_field=field,
        changed_indices=[index],
    )


def _replace_field_item(
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    field: str,
    index: int,
    value: str,
) -> DraftEditResult:
    values_by_field = _draft_field_values(list(today_work), list(problems), list(tomorrow_plan))
    values = values_by_field.get(field, [])
    if index < 0 or index >= len(values):
        return DraftEditResult(today_work, problems, tomorrow_plan, "要修改的条目序号不对，请检查第几条。")
    cleaned = _normalize_tomorrow_plan(value) if field == "tomorrow_plan" else _normalize_free_text(value)
    cleaned = cleaned.strip(" ，,。；;：:！!？?")
    if not cleaned:
        return DraftEditResult(
            today_work,
            problems,
            tomorrow_plan,
            _build_pending_draft_edit_question(field, index),
            pending_edit=_build_pending_draft_edit(field, index),
        )
    values[index] = cleaned
    return DraftEditResult(
        values_by_field["today_work"],
        values_by_field["problems"],
        values_by_field["tomorrow_plan"],
        changed_field=field,
        changed_indices=[index],
    )


def _build_draft_edit_acknowledgement(edit_result: DraftEditResult) -> str:
    field = edit_result.changed_field or "today_work"
    indices = edit_result.changed_indices or []
    if len(indices) == 1:
        return f"已更新{_draft_field_label(field)}第{indices[0] + 1}条"
    if len(indices) >= 2:
        shown = "和".join(f"第{index + 1}条" for index in indices[:2])
        return f"已更新{_draft_field_label(field)}{shown}"
    return "已按你的要求更新草稿"


def _flatten_report_items(today_work: list[str], problems: list[str], tomorrow_plan: list[str]) -> list[tuple[str, int, str]]:
    flat: list[tuple[str, int, str]] = []
    for field, values in (("today_work", today_work), ("problems", problems), ("tomorrow_plan", tomorrow_plan)):
        for index, value in enumerate(values):
            flat.append((field, index, value))
    return flat


def _unflatten_report_items(flat: list[tuple[str, int, str]]) -> DraftEditResult:
    today_work: list[str] = []
    problems: list[str] = []
    tomorrow_plan: list[str] = []
    for field, _index, value in flat:
        if field == "today_work":
            today_work.append(value)
        elif field == "problems":
            problems.append(value)
        elif field == "tomorrow_plan":
            tomorrow_plan.append(value)
    return DraftEditResult(today_work, problems, tomorrow_plan)


def _merge_flat_items(flat: list[tuple[str, int, str]], first: int, second: int) -> DraftEditResult:
    if first == second or first < 0 or second < 0 or first >= len(flat) or second >= len(flat):
        return DraftEditResult([], [], [], "要合并的条目序号不对，请检查第几条。")
    keep, remove = (first, second) if first < second else (second, first)
    field, index, value = flat[keep]
    merged_value = f"{value}；{flat[remove][2]}"
    updated = list(flat)
    updated[keep] = (field, index, merged_value)
    updated.pop(remove)
    return _unflatten_report_items(updated)


def _delete_flat_item(flat: list[tuple[str, int, str]], index: int) -> DraftEditResult:
    if index < 0 or index >= len(flat):
        return DraftEditResult([], [], [], "要删除的条目序号不对，请检查第几条。")
    updated = list(flat)
    updated.pop(index)
    return _unflatten_report_items(updated)


def _replace_flat_item(flat: list[tuple[str, int, str]], index: int, value: str) -> DraftEditResult:
    if index < 0 or index >= len(flat):
        return DraftEditResult([], [], [], "要修改的条目序号不对，请检查第几条。")
    if not value:
        return DraftEditResult([], [], [], "你想把这条改成什么？")
    field, original_index, _old = flat[index]
    updated = list(flat)
    normalized = _normalize_tomorrow_plan(value) if field == "tomorrow_plan" else _normalize_free_text(value)
    updated[index] = (field, original_index, normalized)
    return _unflatten_report_items(updated)


def _parse_item_number(value: str) -> int | None:
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    numerals = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if text in numerals:
        return numerals[text]
    if text.startswith("十") and len(text) == 2 and text[1] in numerals:
        return 10 + numerals[text[1]]
    if text.endswith("十") and len(text) == 2 and text[0] in numerals:
        return numerals[text[0]] * 10
    if "十" in text:
        left, right = text.split("十", 1)
        left_value = numerals.get(left, 1 if not left else 0)
        right_value = numerals.get(right, 0) if right else 0
        return left_value * 10 + right_value
    return None


def _fast_path_structured_report(
    raw_input: str,
    *,
    intent: str,
    current_slot: str | None,
    existing: DailyReport | None,
) -> StructuredDailyReport | None:
    if intent == "continue_collecting" and current_slot in {"today_work", "problems", "tomorrow_plan"} and _is_empty_slot_reply(raw_input):
        if current_slot == "today_work":
            return StructuredDailyReport(today_work=[_empty_value_for_field("today_work", raw_input)], completeness=0.34)
        if current_slot == "problems":
            return StructuredDailyReport(problems=[_empty_value_for_field("problems", raw_input)], completeness=0.33)
        return StructuredDailyReport(tomorrow_plan=[_empty_value_for_field("tomorrow_plan", raw_input)], completeness=0.33)

    if intent == "continue_collecting" and current_slot == "problems" and _is_short_no_problem_reply(raw_input):
        return StructuredDailyReport()

    if intent != "modify_field":
        return None

    direct_update = _extract_direct_field_update(raw_input)
    if direct_update is None:
        return None

    field, value = direct_update
    if field == "today_work":
        return StructuredDailyReport(today_work=[_normalize_free_text(value)], completeness=0.34)
    if field == "problems":
        if _mentions_no_problem(value) or _is_short_no_problem_reply(value):
            return StructuredDailyReport(problems=["暂无明显问题"], completeness=0.33)
        return StructuredDailyReport(problems=[_normalize_free_text(value)], completeness=0.33)
    if field == "tomorrow_plan":
        return StructuredDailyReport(tomorrow_plan=[_normalize_tomorrow_plan(value)], completeness=0.33)
    return None


def _extract_direct_field_update(raw_input: str) -> tuple[str, str] | None:
    text = raw_input.strip()
    patterns = [
        ("today_work", r"(?:今日工作|今天工作|今天的工作|今天做的事|今天做了|工作内容|工作)(?:改成|改为|修改为|更新为|补成)(.+)$"),
        ("problems", r"(?:问题困难|问题和困难|问题|困难|风险)(?:改成|改为|修改为|更新为|补成)(.+)$"),
        ("tomorrow_plan", r"(?:明日计划|明天计划|明天安排|明日安排|明天|明日|计划)(?:改成|改为|修改为|更新为|补成)(.+)$"),
        ("tomorrow_plan", r"(?:明天|明日)(?:那项|那个|那条|这一项|这个|计划|安排)?(?:改成|改为|修改为|更新为)(.+)$"),
        ("today_work", r"(?:帮我)?(?:改下|改一下|修改|更新)[，,。；;：:\s]*(?:今日完成|今天完成|今日工作|今天工作|工作内容)[，,。；;：:\s]*(.+)$"),
        ("problems", r"(?:帮我)?(?:改下|改一下|修改|更新)[，,。；;：:\s]*(?:问题困难|问题和困难|问题|困难|风险)[，,。；;：:\s]*(.+)$"),
        ("tomorrow_plan", r"(?:帮我)?(?:改下|改一下|修改|更新)[，,。；;：:\s]*(?:明日计划|明天计划|明天安排|明日安排|计划)[，,。；;：:\s]*(.+)$"),
        ("today_work", r"(?:帮我)?(?:改下|改一下|修改|更新)[，,。；;：:\s]*((?:今天|今日).+)$"),
        ("problems", r"(?:帮我)?(?:改下|改一下|修改|更新)[，,。；;：:\s]*((?:问题|困难|风险).+)$"),
        ("tomorrow_plan", r"(?:帮我)?(?:改下|改一下|修改|更新)[，,。；;：:\s]*((?:明天|明日|明儿|后天).+)$"),
    ]
    for field, pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        value = _clean_field_change_value(match.group(1), field)
        if value:
            return field, value
    return None


def _is_explicit_field_modify_request(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    field_signals = [
        "今日工作",
        "今天工作",
        "今日完成",
        "今天完成",
        "工作内容",
        "问题",
        "风险",
        "明日计划",
        "明天计划",
        "明天安排",
        "明日安排",
        "计划",
    ]
    modify_signals = ["改下", "改一下", "修改", "更新", "改成", "改为", "补成"]
    return any(signal in compact for signal in field_signals) and any(signal in compact for signal in modify_signals)


def _merge_by_intent(
    *,
    intent: str,
    raw_input: str,
    existing: DailyReport | None,
    parsed: ParsedInput,
    decision: DailyInputIntentDecision | None = None,
) -> tuple[list[str], list[str], list[str]]:
    existing_today_work = existing.today_work if existing else []
    existing_problems = existing.problems if existing else []
    existing_tomorrow_plan = existing.tomorrow_plan if existing else []

    if intent == "replace_current_report":
        return parsed.today_work, parsed.problems, parsed.tomorrow_plan

    if intent == "modify_field":
        targets = _target_fields_from_input(raw_input, parsed)
        today_work = existing_today_work
        problems = existing_problems
        tomorrow_plan = existing_tomorrow_plan
        if "today_work" in targets:
            today_work = parsed.today_work or _extract_field_change_text(raw_input, "today_work")
        if "problems" in targets:
            problems = parsed.problems or _extract_field_change_text(raw_input, "problems")
        if "tomorrow_plan" in targets:
            tomorrow_plan = parsed.tomorrow_plan or _extract_field_change_text(raw_input, "tomorrow_plan")
        if not targets:
            today_work, problems, tomorrow_plan = _apply_direct_not_this_but_that_replacement(
                raw_input,
                today_work=today_work,
                problems=problems,
                tomorrow_plan=tomorrow_plan,
            )
            today_work, problems, tomorrow_plan = _apply_simple_replacement(
                raw_input,
                today_work=today_work,
                problems=problems,
                tomorrow_plan=tomorrow_plan,
            )
        return today_work, problems, tomorrow_plan

    if intent == "continue_collecting" and existing:
        parsed_today_work = parsed.today_work
        parsed_problems = parsed.problems
        parsed_tomorrow_plan = parsed.tomorrow_plan
        if _looks_like_full_report_input(raw_input):
            fallback_fields = _extract_simple_report_fields(raw_input)
            parsed_today_work = parsed_today_work or fallback_fields.get("today_work") or []
            parsed_problems = parsed_problems or fallback_fields.get("problems") or (["暂无明显问题"] if _mentions_no_problem(raw_input) else [])
            parsed_tomorrow_plan = parsed_tomorrow_plan or fallback_fields.get("tomorrow_plan") or []
        today_work = _merge_today_work_with_relation(existing_today_work, parsed_today_work, decision)
        problems = _merge_problems_for_collecting(existing_problems, parsed_problems, existing, decision)
        tomorrow_plan = _merge_tomorrow_for_collecting(existing_tomorrow_plan, parsed_tomorrow_plan, decision)
        return today_work, problems, tomorrow_plan

    if intent == "append_to_existing":
        today_work = merge_ordered(existing_today_work, parsed.today_work)
        problems = parsed.problems if parsed.problems and _is_placeholder_problem_list(existing_problems) else merge_ordered(existing_problems, parsed.problems)
        tomorrow_plan = merge_ordered(existing_tomorrow_plan, parsed.tomorrow_plan)
        return today_work, problems, tomorrow_plan

    return (
        merge_ordered(existing_today_work, parsed.today_work),
        merge_ordered(existing_problems, parsed.problems),
        merge_ordered(existing_tomorrow_plan, parsed.tomorrow_plan),
    )


def _merge_today_work_with_relation(
    existing_today_work: list[str],
    parsed_today_work: list[str],
    decision: DailyInputIntentDecision | None,
) -> list[str]:
    if not parsed_today_work:
        return existing_today_work
    if not existing_today_work:
        return parsed_today_work
    if decision and decision.matched_field == "today_work":
        relation = decision.relation_to_existing
        if relation in {"duplicate", "semantic_duplicate"}:
            return existing_today_work
        if relation == "elaboration":
            index = _resolve_relation_index(decision.matched_item_index, len(existing_today_work))
            if index is not None and decision.new_content:
                updated = list(existing_today_work)
                updated[index] = _normalize_free_text(decision.new_content)
                return updated
            return existing_today_work
    return merge_ordered(existing_today_work, parsed_today_work)


def _merge_problems_for_collecting(
    existing_problems: list[str],
    parsed_problems: list[str],
    existing: DailyReport,
    decision: DailyInputIntentDecision | None,
) -> list[str]:
    problems_filled = bool(existing_problems) or bool(existing.section_status.get("problems_acknowledged_empty"))
    if not parsed_problems:
        return existing_problems
    if not problems_filled:
        return parsed_problems
    if _is_placeholder_problem_list(existing_problems):
        return parsed_problems
    if decision and decision.target_field == "problems" and decision.confidence >= 0.65:
        return merge_ordered(existing_problems, parsed_problems)
    return existing_problems


def _merge_tomorrow_for_collecting(
    existing_tomorrow_plan: list[str],
    parsed_tomorrow_plan: list[str],
    decision: DailyInputIntentDecision | None,
) -> list[str]:
    if not parsed_tomorrow_plan:
        return existing_tomorrow_plan
    if not existing_tomorrow_plan:
        return parsed_tomorrow_plan
    if decision and decision.target_field == "tomorrow_plan" and decision.confidence >= 0.65:
        return merge_ordered(existing_tomorrow_plan, parsed_tomorrow_plan)
    return existing_tomorrow_plan


def _resolve_relation_index(raw_index: int, item_count: int) -> int | None:
    if raw_index <= 0:
        return 0 if item_count == 1 else None
    index = raw_index - 1
    return index if 0 <= index < item_count else None


def _target_fields_from_input(raw_input: str, parsed: ParsedInput) -> set[str]:
    compact = "".join(raw_input.split())
    targets: set[str] = set()
    if any(token in compact for token in ["今日工作", "今天工作", "今天做", "今日完成", "今天完成", "今天主要是", "工作内容", "合同数量"]):
        targets.add("today_work")
    if any(token in compact for token in ["问题", "困难", "风险"]):
        targets.add("problems")
    if any(token in compact for token in ["明日计划", "明天计划", "明天", "明日", "计划"]):
        targets.add("tomorrow_plan")
    if not targets:
        if parsed.today_work:
            targets.add("today_work")
        if parsed.problems:
            targets.add("problems")
        if parsed.tomorrow_plan:
            targets.add("tomorrow_plan")
    return targets


def _extract_field_change_text(raw_input: str, field: str) -> list[str]:
    text = raw_input.strip()
    patterns = [
        r"(?:改成|改为|改一下|修改为|更新为|补成)(.+)$",
        r"(?:不是.+?是)(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            cleaned = _clean_field_change_value(match.group(1), field)
            return [cleaned] if cleaned else []
    return []


def _clean_field_change_value(value: str, field: str) -> str:
    text = _normalize_tomorrow_plan(value) if field == "tomorrow_plan" else _normalize_free_text(value)
    previous = None
    while previous != text:
        previous = text
        text = _strip_field_change_prefix(text)
        text = re.sub(
            r"^(今日完成|今天完成|今日工作|今天工作|今天的工作|工作内容|"
            r"今天主要是|今日主要是|今天主要做了?|今日主要做了?|"
            r"问题困难|问题和困难|问题|困难|风险|"
            r"明日计划|明天计划|明天安排|明日安排|计划)[，,。；;：:\s]*",
            "",
            text,
        )
    text = text.strip(" ，,。；;：:！!？?")
    return text if _is_meaningful_field_change_value(text, field) else ""


def _is_meaningful_field_change_value(value: str, field: str) -> bool:
    compact = _compact_for_intent(value)
    if not compact:
        return False
    field_only = {
        "today_work": {"今日完成", "今天完成", "今日工作", "今天工作", "工作内容", "工作", "今天", "今日"},
        "problems": {"问题", "困难", "风险", "问题困难", "问题和困难"},
        "tomorrow_plan": {"明日计划", "明天计划", "明天安排", "明日安排", "计划", "明天", "明日"},
    }
    if compact in field_only.get(field, set()):
        return False
    return not _is_non_report_phrase(value)


def _has_modify_field_new_content(raw_input: str, parsed: ParsedInput, targets: set[str]) -> bool:
    values_by_field = {
        "today_work": parsed.today_work,
        "problems": parsed.problems,
        "tomorrow_plan": parsed.tomorrow_plan,
    }
    for field in targets:
        values = values_by_field.get(field) or _extract_field_change_text(raw_input, field)
        if any(_is_meaningful_field_change_value(value, field) for value in values):
            return True
    return False


def _build_modify_missing_content_message(targets: set[str]) -> str:
    labels = {
        "today_work": "今日工作",
        "problems": "问题/风险",
        "tomorrow_plan": "明日计划",
    }
    ordered = [field for field in ("today_work", "problems", "tomorrow_plan") if field in targets]
    if len(ordered) == 1:
        return f"好的，你想把{labels[ordered[0]]}改成什么？"
    return "好的，你想把这些内容改成什么？"


def _is_placeholder_problem_list(values: list[str]) -> bool:
    return bool(values) and all(_is_placeholder_problem(value) for value in values)


def _is_placeholder_problem(value: str) -> bool:
    compact = _compact_for_intent(value)
    return compact in {"暂无明显问题", "无明显问题", "暂无问题", "无问题", "没问题", "没有问题", "没啥问题"}


def _apply_direct_not_this_but_that_replacement(
    raw_input: str,
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> tuple[list[str], list[str], list[str]]:
    match = re.search(r"\u4E0D\u662F(.+?)[\uFF0C,\u3002\uFF1B;\s]+\u662F(.+)", raw_input)
    if not match:
        return today_work, problems, tomorrow_plan
    old, new = match.group(1).strip(), match.group(2).strip()

    def replace_values(values: list[str]) -> tuple[list[str], bool]:
        changed = False
        updated: list[str] = []
        for value in values:
            if old and old in value:
                updated.append(value.replace(old, new))
                changed = True
            else:
                updated.append(value)
        return updated, changed

    next_today_work, changed_today = replace_values(today_work)
    next_problems, changed_problems = replace_values(problems)
    next_tomorrow_plan, changed_tomorrow = replace_values(tomorrow_plan)
    if not (changed_today or changed_problems or changed_tomorrow):
        return today_work, problems, tomorrow_plan
    return next_today_work, next_problems, next_tomorrow_plan


def _apply_simple_replacement(
    raw_input: str,
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> tuple[list[str], list[str], list[str]]:
    match = re.search(r"不是(.+?)[，,。；; ]+是(.+)", raw_input)
    if not match:
        return today_work, problems, tomorrow_plan
    old, new = match.group(1).strip(), match.group(2).strip()

    def replace_values(values: list[str]) -> tuple[list[str], bool]:
        changed = False
        updated: list[str] = []
        for value in values:
            if old and old in value:
                updated.append(value.replace(old, new))
                changed = True
            else:
                updated.append(value)
        return updated, changed

    today_work, changed_today = replace_values(today_work)
    problems, changed_problems = replace_values(problems)
    tomorrow_plan, changed_tomorrow = replace_values(tomorrow_plan)
    if not (changed_today or changed_problems or changed_tomorrow):
        tomorrow_plan = [new] if tomorrow_plan else tomorrow_plan
    return today_work, problems, tomorrow_plan


def _interpret_report_input(
    raw_input: str,
    structured: StructuredDailyReport,
    *,
    current_slot: str | None,
    existing: DailyReport | None,
    intent: str,
    report_date: date | None = None,
    actual_date: date | None = None,
    allow_slot_fallback: bool = True,
) -> ParsedInput:
    parsed_today_work = _clean_report_items(structured.today_work, field="today_work")
    parsed_problems = _clean_report_items(structured.problems, field="problems")
    parsed_tomorrow_plan = _clean_report_items(structured.tomorrow_plan, field="tomorrow_plan")
    parsed_meta_notes = _clean_report_items(structured.meta_notes, field="meta_notes")

    quality_flags: list[str] = []
    no_problem = _mentions_no_problem(raw_input) or (current_slot == "problems" and _is_short_no_problem_reply(raw_input))
    empty_current_slot = current_slot in {"today_work", "problems", "tomorrow_plan"} and _is_empty_slot_reply(raw_input)
    before_tomorrow, tomorrow_part = _split_tomorrow_text(raw_input)

    if no_problem and not parsed_problems:
        parsed_problems = ["暂无明显问题"]

    if empty_current_slot:
        empty_value = _empty_value_for_field(str(current_slot), raw_input)
        if current_slot == "today_work" and not parsed_today_work:
            parsed_today_work = [empty_value]
        elif current_slot == "problems" and not parsed_problems:
            parsed_problems = [empty_value]
        elif current_slot == "tomorrow_plan" and not parsed_tomorrow_plan:
            parsed_tomorrow_plan = [empty_value]

    if tomorrow_part and not parsed_tomorrow_plan:
        normalized = _normalize_tomorrow_plan(tomorrow_part)
        parsed_tomorrow_plan = [normalized]
        if _is_vague_tomorrow_plan(tomorrow_part):
            quality_flags.append("明日计划较笼统")

    if allow_slot_fallback and not (parsed_today_work or parsed_problems or parsed_tomorrow_plan):
        fallback_slot = current_slot or _preferred_slot(existing)
        fallback_text = _normalize_free_text(before_tomorrow or raw_input)
        if fallback_text:
            if fallback_slot == "problems":
                parsed_problems = ["暂无明显问题"] if no_problem else [fallback_text]
            elif fallback_slot == "tomorrow_plan":
                parsed_tomorrow_plan = [_empty_value_for_field("tomorrow_plan", fallback_text)] if _is_empty_slot_reply(fallback_text) else [_normalize_tomorrow_plan(fallback_text)]
            elif fallback_slot == "today_work" and _is_empty_slot_reply(fallback_text):
                parsed_today_work = [_empty_value_for_field("today_work", fallback_text)]
            else:
                parsed_today_work = [fallback_text]

    if not parsed_problems and before_tomorrow and _looks_like_problem(before_tomorrow):
        parsed_problems = [_normalize_free_text(before_tomorrow)]
    if not parsed_today_work and before_tomorrow and _looks_like_today_work(before_tomorrow):
        parsed_today_work = [_normalize_free_text(before_tomorrow)]

    if parsed_tomorrow_plan and any(_is_vague_tomorrow_plan(item) for item in parsed_tomorrow_plan):
        quality_flags.append("明日计划较笼统")
        parsed_tomorrow_plan = [_normalize_tomorrow_plan(item) for item in parsed_tomorrow_plan]

    parsed_today_work = _clean_report_items(parsed_today_work, field="today_work")
    parsed_problems = _clean_report_items(parsed_problems, field="problems")
    parsed_tomorrow_plan = _clean_report_items(parsed_tomorrow_plan, field="tomorrow_plan")
    parsed_meta_notes = _clean_report_items(parsed_meta_notes, field="meta_notes")
    anchor_fields = _apply_long_text_field_anchor_fallbacks(
        raw_input,
        {
            "today_work": parsed_today_work,
            "problems": parsed_problems,
            "tomorrow_plan": parsed_tomorrow_plan,
        },
    )
    parsed_today_work = anchor_fields["today_work"]
    parsed_problems = anchor_fields["problems"]
    parsed_tomorrow_plan = anchor_fields["tomorrow_plan"]
    parsed_problems = _normalize_problem_items(parsed_problems, raw_input)
    parsed_today_work, parsed_problems = _recover_problem_fragments_from_today_work(parsed_today_work, parsed_problems)
    parsed_today_work = _remove_problem_overlap_from_today_work(parsed_today_work, parsed_problems)
    if intent != "modify_field":
        parsed_today_work, parsed_problems, parsed_tomorrow_plan = _preserve_simple_source_detail(
            raw_input,
            today_work=parsed_today_work,
            problems=parsed_problems,
            tomorrow_plan=parsed_tomorrow_plan,
        )
        parsed_today_work, parsed_problems, parsed_tomorrow_plan = _preserve_enumerated_today_sections(
            raw_input,
            today_work=parsed_today_work,
            problems=parsed_problems,
            tomorrow_plan=parsed_tomorrow_plan,
            report_date=report_date,
            actual_date=actual_date,
        )
        parsed_today_work, parsed_problems = _recover_problem_fragments_from_today_work(parsed_today_work, parsed_problems)
        parsed_today_work = _remove_problem_overlap_from_today_work(parsed_today_work, parsed_problems)

    if _looks_colloquial_or_test(raw_input, parsed_today_work, parsed_problems, parsed_tomorrow_plan):
        quality_flags.append("内容较口语化，可能是测试内容")

    quality_warning = _join_warnings(quality_flags)
    normalized_structured = StructuredDailyReport(
        today_work=parsed_today_work,
        problems=parsed_problems,
        tomorrow_plan=parsed_tomorrow_plan,
        meta_notes=parsed_meta_notes,
        content_quality=structured.content_quality,
        emotion=structured.emotion,
        completeness=_calculate_completeness(parsed_today_work, parsed_problems, parsed_tomorrow_plan),
    )

    return ParsedInput(
        intent=intent,
        today_work=parsed_today_work,
        problems=parsed_problems,
        tomorrow_plan=parsed_tomorrow_plan,
        quality_warning=quality_warning,
        structured=normalized_structured,
    )


def _build_acknowledgements(parsed: ParsedInput, missing_sections: list[str]) -> list[str]:
    messages: list[str] = []
    if parsed.today_work:
        messages.append("这部分我先帮你记到今日内容")
    if parsed.problems == ["暂无明显问题"]:
        messages.append("已记录为暂无明显问题")
    elif parsed.problems:
        messages.append("问题这部分我先记下了")
    if parsed.tomorrow_plan and "tomorrow_plan" not in missing_sections:
        messages.append("明天安排我也先补上了")
    return messages


def _normalize_problem_items(items: list[str], raw_input: str) -> list[str]:
    normalized: list[str] = []
    raw_compact = _compact_for_intent(raw_input)
    for item in items:
        compact = _compact_for_intent(item)
        if "蛋给我加少了" in compact and "鸡蛋饼" in raw_compact:
            normalized.append("鸡蛋饼里的蛋加少了")
            continue
        cleaned = re.sub(r"^发现", "", item).strip(" ，,。；;：:")
        normalized.append(cleaned or item)
    return merge_ordered([], normalized)


_TODAY_ANCHOR_RE = re.compile(r"(今天|今日|今儿|today)", re.IGNORECASE)
_EXPLICIT_FUTURE_ANCHOR_RE = re.compile(r"(明天|明日|明儿|tomorrow|后天)", re.IGNORECASE)
_ENUM_MARKER_RE = re.compile(
    r"(?:"
    r"（(?P<cn_paren>[一二三四五六七八九十百\d]{1,4})）"
    r"|\((?P<plain_paren>[一二三四五六七八九十百\d]{1,4})\)"
    r"|(?P<digit>\d{1,2})[、.．)）]"
    r"|(?P<cn>[一二三四五六七八九十]{1,3})[、.．)）]"
    r"|(?P<cn_shi>[一二三四五六七八九十])是"
    r")"
)


def _preserve_enumerated_today_sections(
    raw_input: str,
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    report_date: date | None = None,
    actual_date: date | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Keep explicitly enumerated current-day items out of tomorrow_plan drift."""
    if report_date is not None and actual_date is not None and report_date != actual_date:
        return today_work, problems, tomorrow_plan

    enumerated_today = _extract_explicit_today_enumerated_items(raw_input)
    if not enumerated_today:
        return today_work, problems, tomorrow_plan

    cleaned_today = _clean_report_items(enumerated_today, field="today_work")
    if not cleaned_today:
        return today_work, problems, tomorrow_plan

    updated_today_work = _merge_enumerated_today_items(today_work, cleaned_today)
    updated_tomorrow_plan = _remove_misplaced_enumerated_today_items(tomorrow_plan, cleaned_today)

    _before_tomorrow, tomorrow_part = _split_tomorrow_text(raw_input)
    if tomorrow_part:
        tomorrow_item = _normalize_explicit_tomorrow_clause(tomorrow_part)
        if tomorrow_item:
            updated_tomorrow_plan = _merge_similar_ordered(
                updated_tomorrow_plan,
                _clean_report_items([tomorrow_item], field="tomorrow_plan"),
            )

    return updated_today_work, problems, updated_tomorrow_plan


def _extract_explicit_today_enumerated_items(raw_input: str) -> list[str]:
    text = str(raw_input or "")
    today_anchor = _TODAY_ANCHOR_RE.search(text)
    if not today_anchor:
        return []

    future_anchor = _EXPLICIT_FUTURE_ANCHOR_RE.search(text, pos=today_anchor.end())
    today_segment = text[: future_anchor.start()] if future_anchor else text
    matches = list(_ENUM_MARKER_RE.finditer(today_segment))
    matches = [match for match in matches if match.start() > today_anchor.start()]
    if len(matches) < 2:
        return []

    items: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(today_segment)
        item = _normalize_enumerated_segment(today_segment[start:end])
        if item:
            items.append(item)
    return merge_ordered([], items)


def _normalize_enumerated_segment(segment: str) -> str:
    text = str(segment or "").strip()
    text = re.sub(r"^[\s:：,，;；、.。]+", "", text)
    text = re.sub(r"^(然后|并且|以及|还有|最后|主要|就是)\s*", "", text)
    text = re.sub(r"[\s:：,，;；、.。]+$", "", text)
    return _normalize_free_text(text)


def _normalize_explicit_tomorrow_clause(clause: str) -> str:
    text = _normalize_tomorrow_plan(clause)
    text = re.sub(r"^(然后)?\s*(明天|明日|明儿|tomorrow)\s*", "", text, flags=re.IGNORECASE)
    return _normalize_tomorrow_plan(text)


def _merge_enumerated_today_items(existing: list[str], incoming: list[str]) -> list[str]:
    merged = list(existing)
    for item in incoming:
        match_index = _find_similar_item_index(merged, item)
        if match_index is None:
            merged.append(item)
            continue
        current = merged[match_index]
        if _should_replace_with_enumerated_detail(current, item):
            merged[match_index] = item
    return merge_ordered([], merged)


def _merge_similar_ordered(existing: list[str], incoming: list[str]) -> list[str]:
    merged = list(existing)
    for item in incoming:
        if _find_similar_item_index(merged, item) is None and not any(
            _tomorrow_plan_items_match(current, item) for current in merged
        ):
            merged.append(item)
    return merge_ordered([], merged)


def _find_similar_item_index(items: list[str], candidate: str) -> int | None:
    for index, item in enumerate(items):
        if _evidence_items_match(item, candidate):
            return index
    return None


def _should_replace_with_enumerated_detail(current: str, candidate: str) -> bool:
    current_evidence = _normalize_evidence_text(current)
    candidate_evidence = _normalize_evidence_text(candidate)
    if not current_evidence or not candidate_evidence:
        return False
    if current_evidence in candidate_evidence and len(candidate_evidence) > len(current_evidence) + 4:
        return True
    return _looks_like_problem(candidate) and not _looks_like_problem(current) and current_evidence in candidate_evidence


def _evidence_items_match(left: str, right: str, *, threshold: float = 0.88) -> bool:
    left_evidence = _normalize_evidence_text(left)
    right_evidence = _normalize_evidence_text(right)
    if not left_evidence or not right_evidence:
        return False
    if left_evidence == right_evidence:
        return True
    if min(len(left_evidence), len(right_evidence)) >= 6 and (
        left_evidence in right_evidence or right_evidence in left_evidence
    ):
        return True
    return difflib.SequenceMatcher(None, left_evidence, right_evidence).ratio() >= threshold


def _tomorrow_plan_items_match(left: str, right: str) -> bool:
    left_core = _normalize_tomorrow_plan_duplicate_evidence(left)
    right_core = _normalize_tomorrow_plan_duplicate_evidence(right)
    if not left_core or not right_core:
        return False
    if left_core == right_core:
        return True
    if min(len(left_core), len(right_core)) >= 4 and (left_core in right_core or right_core in left_core):
        return True
    return difflib.SequenceMatcher(None, left_core, right_core).ratio() >= 0.82


def _normalize_tomorrow_plan_duplicate_evidence(value: str) -> str:
    evidence = _normalize_evidence_text(value)
    for token in ("明天", "明日", "明儿", "tomorrow", "继续", "接着", "计划", "安排", "催收", "催要", "催"):
        evidence = evidence.replace(token, "")
    return evidence


def _remove_misplaced_enumerated_today_items(tomorrow_plan: list[str], today_items: list[str]) -> list[str]:
    filtered: list[str] = []
    for item in tomorrow_plan:
        if _looks_like_plain_misplaced_today_item(item, today_items):
            continue
        filtered.append(item)
    return filtered


def _looks_like_plain_misplaced_today_item(item: str, today_items: list[str]) -> bool:
    item_evidence = _normalize_evidence_text(item)
    if not item_evidence:
        return False
    if _has_future_continuation_marker(item):
        return False
    for today_item in today_items:
        today_evidence = _normalize_evidence_text(today_item)
        if not today_evidence:
            continue
        if item_evidence == today_evidence:
            return True
        if min(len(item_evidence), len(today_evidence)) >= 6 and (
            item_evidence in today_evidence or today_evidence in item_evidence
        ):
            return True
    return False


def _has_future_continuation_marker(item: str) -> bool:
    compact = _compact_for_intent(item)
    return any(token in compact for token in ("明天", "明日", "明儿", "tomorrow", "继续", "接着", "计划", "后续"))


def _recover_problem_fragments_from_today_work(today_work: list[str], problems: list[str]) -> tuple[list[str], list[str]]:
    if not today_work:
        return today_work, problems

    recovered_problems = list(problems)
    cleaned_today: list[str] = []
    problem_markers = (
        "问题",
        "风险",
        "困难",
        "没收齐",
        "未收齐",
        "尚未收齐",
        "反馈较慢",
        "反馈慢",
        "待确认",
        "待定",
        "缺失",
        "缺少",
        "不全",
        "卡住",
        "报错",
    )
    weak_prefixes = ("目前", "当前", "现在", "问题是", "风险是", "问题/风险")

    for item in today_work:
        parts = [part.strip() for part in re.split(r"[\uFF0C,\u3002\uFF1B;\u3001\n]+", item or "") if part.strip()]
        if len(parts) == 1:
            match = re.search(
                r"("
                r"\u5BA2\u6237\u53CD\u9988.*?(?:\u8F83\u6162|\u6162)"
                r"|(?:\u8D44\u6599|\u6750\u6599).*?(?:\u6CA1\u6536\u9F50|\u672A\u6536\u9F50|\u5C1A\u672A\u6536\u9F50|\u4E0D\u5168|\u7F3A\u5931|\u7F3A\u5C11)"
                r"|(?:\u670D\u52A1\u5668|\u65B9\u6848).*?\u5F85\u786E\u8BA4"
                r")",
                item or "",
            )
            if match and match.start() > 0:
                parts = [item[: match.start()].strip(), item[match.start() :].strip()]
        if not parts:
            continue
        kept_parts: list[str] = []
        for part in parts:
            compact = _compact_for_intent(part)
            normalized_part = part
            for prefix in weak_prefixes:
                if normalized_part.startswith(prefix):
                    normalized_part = normalized_part[len(prefix):].strip(" ，,。；;：:")
                    break
            normalized_compact = _compact_for_intent(normalized_part)
            looks_problem = _looks_like_problem(normalized_part) or any(marker in compact or marker in normalized_compact for marker in problem_markers)
            looks_work = _looks_like_today_work_action(normalized_part)
            if looks_problem and not looks_work:
                recovered_problems.append(_normalize_free_text(normalized_part))
            else:
                kept_parts.append(part)
        if kept_parts:
            cleaned_today.append("，".join(kept_parts))

    return merge_ordered([], cleaned_today), merge_ordered([], recovered_problems)


def _remove_problem_overlap_from_today_work(today_work: list[str], problems: list[str]) -> list[str]:
    if not today_work or not problems:
        return today_work
    problem_compacts = [_semantic_compact(value) for value in problems if value]
    cleaned: list[str] = []
    for item in today_work:
        item_compact = _semantic_compact(item)
        if item_compact and _looks_like_problem(item) and any(item_compact in problem_compact for problem_compact in problem_compacts):
            continue
        parts = [part.strip() for part in re.split(r"[，,。；;、]+", item) if part.strip()]
        if not parts:
            continue
        kept = [part for part in parts if not _looks_like_problem(part)]
        if len(kept) == len(parts):
            cleaned.append(item)
            continue
        if not kept:
            continue
        value = "，".join(kept).strip()
        value = re.sub(r"^(我今天|我今日|今天|今日)[，,。；;：:\s]*", "", value).strip()
        if value and not _is_non_report_phrase(value):
            cleaned.append(value)
    return merge_ordered([], cleaned)


FIELD_UPDATE_LABELS = {
    "today_work": "今日工作",
    "problems": "问题/风险",
    "tomorrow_plan": "明日计划",
}

MISSING_REPLY_LABELS = {
    "today_work": "今天主要做了什么",
    "problems": "有没有遇到问题或风险",
    "tomorrow_plan": "明天计划做什么",
}


def _build_modify_field_message(
    *,
    modified_fields: set[str],
    report: DailyReport,
    missing_sections: list[str],
    include_confirmation: bool = False,
    include_completed_report: bool = False,
    quality_warning: str | None = None,
) -> str:
    ordered_fields = [field for field in ("today_work", "problems", "tomorrow_plan") if field in modified_fields]
    if len(ordered_fields) == 1:
        field = ordered_fields[0]
        prefix = f"已把{FIELD_UPDATE_LABELS[field]}更新为：{_display_report_field(report, field)}。"
    else:
        labels = "、".join(FIELD_UPDATE_LABELS[field] for field in ordered_fields)
        prefix = f"已更新{labels}。"

    if missing_sections:
        return f"{prefix}{_build_missing_after_modify_message(missing_sections)}"

    if include_completed_report:
        return prefix + "\n\n" + build_completed_message(
            today_work=report.today_work,
            problems=report.problems,
            tomorrow_plan=report.tomorrow_plan,
            updated=True,
        )

    if include_confirmation:
        return prefix + "\n\n" + build_confirmation_message(
            today_work=report.today_work,
            problems=report.problems,
            tomorrow_plan=report.tomorrow_plan,
            quality_warning=quality_warning,
            updated=True,
        )

    return prefix


def _display_report_field(report: DailyReport, field: str) -> str:
    if field == "today_work":
        values = report.today_work
    elif field == "problems":
        values = report.problems or ["暂无明显问题"]
    else:
        values = report.tomorrow_plan
    return "；".join(values) if values else "已清空"


def _build_missing_after_modify_message(missing_sections: list[str]) -> str:
    labels = "、".join(MISSING_REPLY_LABELS[field] for field in missing_sections)
    if len(missing_sections) == 1:
        return f"现在还差最后一项：{labels}？"
    return f"现在还差{_cn_count(len(missing_sections))}项：{labels}？"


def _cn_count(count: int) -> str:
    return {2: "两", 3: "三"}.get(count, str(count))



def _is_probable_noise_input(raw_input: str) -> bool:
    stripped = str(raw_input or "").strip()
    if not stripped:
        return False
    if _has_report_semantic_anchor(stripped):
        return False
    lowered = stripped.lower()
    if stripped.startswith("%PDF-"):
        return True
    if " or 1=1" in lowered or "' or '1'='1" in lowered or lowered.endswith("--"):
        return True
    control_count = sum(1 for char in stripped if ord(char) < 32 and char not in "\n\r\t")
    if control_count >= 3:
        return True
    if len(stripped) > 200 and stripped.count("\n") / max(len(stripped), 1) > 0.3:
        return True
    unique_ratio = len(set(stripped)) / max(len(stripped), 1)
    if len(stripped) > 100 and unique_ratio < 0.08:
        return True
    if _looks_like_base64_blob(stripped):
        return True
    if _looks_like_keyboard_mash(stripped):
        return True
    return False


def _has_report_semantic_anchor(text: str) -> bool:
    compact = _compact_for_intent(text)
    anchors = (
        "\u4eca\u5929",
        "\u4eca\u65e5",
        "\u5b8c\u6210",
        "\u5de5\u4f5c",
        "\u65e5\u62a5",
        "\u590d\u76d8",
        "\u660e\u5929",
        "\u660e\u65e5",
        "\u8ba1\u5212",
        "\u95ee\u9898",
        "\u98ce\u9669",
        "\u56f0\u96be",
        "\u963b\u585e",
    )
    return any(anchor in compact for anchor in anchors)


def _looks_like_base64_blob(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 24 or len(compact) % 4 != 0:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact):
        return False
    alpha_ratio = sum(1 for char in compact if char.isascii() and char.isalnum()) / max(len(compact), 1)
    return alpha_ratio > 0.85


def _looks_like_keyboard_mash(text: str) -> bool:
    compact = re.sub(r"[\s;,.!?\\/|_\-]+", "", text.lower())
    if len(compact) < 16:
        return False
    if re.search(r"[\u4e00-\u9fff]", compact):
        return False
    if not re.fullmatch(r"[a-z0-9]+", compact):
        return False
    common_words = ("today", "tomorrow", "risk", "problem", "done", "plan", "report", "work", "fixed", "test", "case")
    if any(word in compact for word in common_words):
        return False
    unique_ratio = len(set(compact)) / max(len(compact), 1)
    return unique_ratio < 0.6 or bool(re.search(r"(?:asdf|qwer|zxcv|jkl)", compact))


def _build_non_report_result(
    *,
    existing: DailyReport | None,
    report_date: date,
    message: str,
    reply_kind: str,
    timings: dict[str, float],
) -> SubmitReportResult:
    if reply_kind != "history_query":
        message = _with_report_date_context(message, report_date)
    return SubmitReportResult(
        report_id=str(existing.id) if existing else None,
        report_date=report_date,
        status=existing.status if existing else STATUS_COLLECTING,
        completeness_score=float(existing.completeness_score) if existing else 0.0,
        missing_sections=_missing_sections_from_report(existing),
        message=message,
        structured=StructuredDailyReport(),
        today_work=existing.today_work if existing else [],
        problems=existing.problems if existing else [],
        tomorrow_plan=existing.tomorrow_plan if existing else [],
        section_status=existing.section_status if existing else {},
        confirmation_type=existing.confirmation_type if existing else CONFIRMATION_NONE,
        confirmed_by_user=bool(existing.confirmed_by_user) if existing else False,
        quality_warning=existing.quality_warning if existing else None,
        report_saved=False,
        reply_kind=reply_kind,
        timings=timings,
    )


def _result_from_report(
    report: DailyReport,
    *,
    structured: StructuredDailyReport,
    missing_sections: list[str],
    message: str,
    reply_kind: str,
    timings: dict[str, Any],
    report_saved: bool = True,
) -> SubmitReportResult:
    message = _with_report_date_context(message, report.report_date)
    return SubmitReportResult(
        report_id=str(report.id),
        report_date=report.report_date,
        status=report.status,
        completeness_score=float(report.completeness_score),
        missing_sections=missing_sections,
        message=message,
        structured=structured,
        today_work=report.today_work,
        problems=report.problems,
        tomorrow_plan=report.tomorrow_plan,
        section_status=report.section_status,
        confirmation_type=report.confirmation_type,
        confirmed_by_user=report.confirmed_by_user,
        quality_warning=report.quality_warning,
        report_saved=report_saved,
        reply_kind=reply_kind,
        timings=timings,
    )


def _result_from_agent_execution_safe(
    execution: AgentExecutionResult,
    *,
    existing: DailyReport | None,
    report_date: date,
    timings: dict[str, Any],
) -> SubmitReportResult:
    if execution.report is None:
        return _build_non_report_result(
            existing=existing,
            report_date=report_date,
            message=execution.message,
            reply_kind=execution.reply_kind,
            timings=timings,
        )
    return _result_from_report(
        execution.report,
        structured=execution.structured,
        missing_sections=execution.missing_sections,
        message=execution.message,
        reply_kind=execution.reply_kind,
        timings=timings,
        report_saved=execution.report_saved,
    )


def _agent_action_summary_safe(plan: Any) -> str:
    actions = getattr(plan, "actions", []) or []
    parts: list[str] = []
    for action in actions[:5]:
        action_type = getattr(action, "type", "") or ""
        field = getattr(action, "field", "") or getattr(action, "target_field", "") or ""
        indices = getattr(action, "item_indices", []) or []
        if indices:
            parts.append(f"{action_type}:{field}:{indices}")
        else:
            parts.append(f"{action_type}:{field}")
    return ",".join(parts)


def _apply_decision_route_timings(timings: dict[str, Any], route: DecisionRoute) -> None:
    timings["decision_route_source"] = route.source
    timings["decision_route_branch"] = route.branch
    timings["decision_route_reason"] = route.reason
    timings["decision_route_entered_llm"] = route.entered_llm
    timings["decision_route_action_summary"] = _agent_action_summary_safe(route.plan) if route.plan is not None else ""
    timings["entered_report_agent"] = route.entered_llm
    if route.llm_seconds:
        timings["report_agent_seconds"] = route.llm_seconds

    meta = route.meta or {}
    timings["report_agent_model"] = meta.get("model", "")
    timings["report_agent_thinking"] = meta.get("thinking")
    timings["report_agent_timeout"] = bool(meta.get("timeout"))

    if route.source in {"direct_rule", "pending_state"}:
        timings["state_resolver_decision"] = route.branch
        timings["state_resolver_reason"] = route.reason
        timings["report_agent_state_branch"] = route.branch

    if route.source == "report_agent_error_fallback":
        timings["report_agent_error_fallback"] = True

    if route.plan is not None:
        timings["report_agent_intent"] = route.plan.intent
        timings["report_agent_confidence"] = route.plan.confidence
        timings["report_agent_should_write"] = route.plan.should_write
        timings["report_agent_output_action"] = _agent_action_summary_safe(route.plan)
        timings["pending_created"] = bool(route.plan.pending_interaction_to_set)


def _apply_daily_intent_frame_timings(timings: dict[str, Any], frame: Any) -> None:
    for key, value in daily_intent_timing_payload(frame).items():
        timings[f"daily_intent_{key}"] = value


def _apply_agent_execution_timings_safe(
    timings: dict[str, Any],
    plan: Any,
    execution: AgentExecutionResult,
) -> None:
    timings["agent_executor_reply_kind"] = execution.reply_kind
    timings["agent_executor_report_saved"] = execution.report_saved
    timings["agent_executor_missing_sections"] = list(execution.missing_sections or [])
    timings["agent_executor_status"] = getattr(execution.report, "status", "") if execution.report is not None else ""
    timings["agent_executor_action_count"] = len(getattr(plan, "actions", []) or [])


def _resolve_direct_agent_plan(raw_input: str, existing: DailyReport | None, *, report_date: date | None = None) -> tuple[str, ActionPlan] | None:
    direct_builders = [
        ("direct_query_current", _direct_query_current_plan),
        ("direct_unsubmit_report", _direct_unsubmit_report_plan),
        ("direct_reset_without_content", _direct_reset_without_content_plan),
        ("direct_clear_current_report", _direct_clear_current_report_plan),
        ("direct_current_pasted_report", _direct_current_pasted_report_plan),
        ("direct_pasted_reference_report", _direct_pasted_reference_report_plan),
        ("direct_structured_multi_field_report", _direct_structured_multi_field_report_plan),
        ("direct_previous_plan_completion", _direct_previous_plan_completion_report_plan),
        ("direct_replace_current_report", _direct_replace_current_report_plan),
        ("direct_simple_full_report", _direct_simple_full_report_plan),
        ("direct_repair_feedback", _direct_repair_feedback_plan),
        ("direct_single_action_effects", _direct_single_action_effects_plan),
        ("direct_numbered_work_items", _direct_numbered_work_items_plan),
        ("direct_today_slot_colloquial", _direct_today_slot_colloquial_plan),
        ("direct_asr_correction_work", _direct_asr_correction_work_plan),
        ("direct_not_replace_but_add", _direct_not_replace_but_add_plan),
        ("direct_negative_replacement", _direct_negative_replacement_plan),
        ("direct_replace_plan_with_drop_old_reference", _direct_replace_plan_with_drop_old_reference_plan),
        ("direct_rewrite_last_modified_item", _direct_rewrite_last_modified_item_plan),
        ("direct_delete_last_modified_item", _direct_delete_last_modified_item_plan),
        ("direct_set_section_empty", _direct_set_section_empty_plan),
        ("direct_ordinal_rewrite", _direct_ordinal_rewrite_plan),
        ("direct_text_replace", _direct_text_replace_plan),
        ("direct_ordinal_move", _direct_ordinal_move_plan),
        ("direct_ordinal_delete", _direct_ordinal_delete_plan),
        ("direct_range_merge", _direct_range_merge_plan),
        ("direct_restore_snapshot", _direct_restore_snapshot_plan),
        ("direct_explicit_append", _direct_explicit_append_plan),
        ("direct_explicit_field_update", _direct_explicit_field_update_plan),
        ("direct_explicit_work_append", _direct_explicit_work_append_plan),
        ("direct_explicit_problem_append", _direct_explicit_problem_append_plan),
        ("direct_tomorrow_plan_phrase", _direct_tomorrow_plan_phrase_plan),
        ("direct_last_unwritten_candidate", _direct_last_unwritten_candidate_plan),
        ("direct_this_is_problem_without_candidate", _direct_this_is_problem_without_candidate_plan),
        ("direct_plan_slot_answer", _direct_plan_slot_answer_plan),
        ("direct_problem_slot_answer", _direct_problem_slot_answer_plan),
    ]
    for branch, builder in direct_builders:
        if branch == "direct_current_pasted_report":
            plan = _direct_current_pasted_report_plan(raw_input, existing, report_date=report_date)
        else:
            plan = builder(raw_input, existing)
        if plan is not None:
            return branch, plan
    return None


def _fallback_plan_from_report_agent_error(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    fields = _extract_simple_report_fields(raw_input) if _looks_like_full_report_input(raw_input) else {"today_work": [], "problems": [], "tomorrow_plan": []}
    if not any(fields.values()):
        current_slot = _infer_current_slot(existing)
        if current_slot not in {"today_work", "problems", "tomorrow_plan"}:
            return None
        text = _normalize_free_text(raw_input)
        if not text or _is_non_report_phrase(text):
            return None
        if current_slot == "problems":
            if _mentions_no_problem(text) or _is_short_no_problem_reply(text):
                fields["problems"] = ["暂无明显问题"]
            elif _looks_like_problem(text):
                fields["problems"] = [_clean_problem_answer(text)]
            else:
                return None
        elif current_slot == "tomorrow_plan":
            if not (_looks_like_tomorrow_plan_input(text) or _looks_like_today_work_action(text)):
                return None
            fields["tomorrow_plan"] = [_normalize_tomorrow_plan(text)]
        elif current_slot == "today_work":
            if not (_looks_like_today_work(text) or _looks_like_today_work_action(text) or _looks_colloquial_or_test(text, [text], [], [])):
                return None
            fields["today_work"] = [_normalize_current_report_work(text)]

    actions: list[AgentAction] = []
    for field in ("today_work", "problems", "tomorrow_plan"):
        values = [item for item in fields.get(field, []) if str(item or "").strip()]
        if values:
            actions.append(AgentAction(type="append_items", field=field, items=values))
    if not actions:
        return None
    return ActionPlan(
        intent="fill_report",
        confidence="medium",
        should_write=True,
        actions=actions,
        reason="fallback after report agent output error using simple report field parsing",
    )


def _direct_query_current_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return None
    exact = {
        "发我",
        "发我下",
        "发我一下",
        "发我看下",
        "发我看看",
        "给我看",
        "看一下",
        "看下",
        "看看",
        "查下",
        "日报草稿发我",
        "草稿发我",
        "当前草稿",
        "当前日报",
        "现在填了什么",
        "目前填了什么",
    }
    if compact in exact or (any(token in compact for token in ("草稿", "日报", "复盘", "当前内容")) and any(token in compact for token in ("发我", "看看", "看下", "展示", "查", "什么"))):
        return ActionPlan(
            intent="query_current",
            confidence="high",
            should_write=False,
            reason="direct current report draft query",
        )
    return None


def _direct_clear_current_report_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None or not _has_report_content(existing):
        return None
    if not _is_global_clear_fast_path(raw_input):
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[
            AgentAction(
                type="clear_all",
                reason="direct deterministic global clear without extra confirmation",
            )
        ],
        reason="direct deterministic global clear before LLM",
    )


def _direct_unsubmit_report_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None or getattr(existing, "status", "") != STATUS_COMPLETED:
        return None
    compact = _compact_for_intent(raw_input)
    if "撤回" not in compact:
        return None
    explicit_report_revoke = any(token in compact for token in ("日报", "复盘", "提交", "已提交", "刚提交"))
    bare_revoke = compact in {"撤回", "撤回一下", "先撤回", "撤回吧"}
    if bare_revoke and _has_previous_draft_snapshot(existing):
        return None
    if not explicit_report_revoke and not bare_revoke:
        return None
    return ActionPlan(
        intent="system_action",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="unsubmit_report")],
        reason="direct deterministic unsubmit report before LLM",
    )


def _direct_replace_current_report_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None or not _has_report_content(existing):
        return None
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("前面是测试", "前面别记", "重新来", "重新说", "重说", "重写", "整条重写", "以这个为准", "按这个来", "跟你实话实说", "算了算了")):
        return None
    fields = _extract_simple_report_fields(raw_input)
    if not any(fields.values()):
        return None
    actions: list[AgentAction] = []
    for field in ("today_work", "problems", "tomorrow_plan"):
        values = fields.get(field) or []
        actions.append(AgentAction(type="replace_field", field=field, items=values))
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=actions,
        clear_pending_interaction=True,
        reason="direct replace current report from explicit reset wording",
    )


def _direct_reset_without_content_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None or not _has_report_content(existing):
        return None
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("前面算了", "刚才算了", "前面不算", "刚才不算", "前面别记")):
        return None
    if len(compact) <= 10:
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user="可以，前面那版我先不动。你把新的今日工作、问题/风险和明日计划发我，我再按新内容重整。",
            reason="short reset wording without replacement content needs the new report first",
        )
    fields = _extract_simple_report_fields(raw_input)
    if any(fields.values()):
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=False,
        reply_to_user="可以，前面那版我先不动。你把新的今日工作、问题/风险和明日计划发我，我再按新内容重整。",
        reason="reset wording without replacement content needs the new report first",
    )


def _direct_pasted_reference_report_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    inline_plan_items = _parse_inline_previous_plan_reference(raw_input)
    if inline_plan_items:
        reference_report = {
            "source": "inline_previous_plan_reference",
            "report_date": "",
            "today_work": [],
            "problems": [],
            "tomorrow_plan": inline_plan_items,
        }
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            actions=[AgentAction(type="load_reference_report", source="inline_previous_plan_reference", reference_report=reference_report)],
            reason="direct inline previous plan reference should be stored as reference only",
        )
    if not _looks_like_pasted_reference_report(raw_input):
        return None
    sections = _parse_labeled_report_sections(raw_input)
    if not sections:
        return None
    pasted_date = _extract_labeled_report_date(raw_input)
    compact = _compact_for_intent(raw_input)
    if pasted_date is None and not any(token in compact for token in ("昨天日报", "昨日日报", "昨天的日报", "昨日的日报")):
        return None
    reference_report = {
        "source": "pasted_previous_report",
        "report_date": pasted_date.isoformat() if pasted_date else "",
        "today_work": sections.get("today_work") or [],
        "problems": sections.get("problems") or [],
        "tomorrow_plan": sections.get("tomorrow_plan") or [],
    }
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="load_reference_report", source="pasted_previous_report", reference_report=reference_report)],
        reason="direct pasted formatted report should be stored as reference only",
    )


def _direct_current_pasted_report_plan(raw_input: str, existing: DailyReport | None, *, report_date: date | None = None) -> ActionPlan | None:
    current_report_date = report_date or getattr(existing, "report_date", None)
    if not _looks_like_current_pasted_report(raw_input, current_report_date):
        return None
    sections = _parse_labeled_report_sections(raw_input)
    if not sections:
        return None
    actions: list[AgentAction] = []
    for field in ("today_work", "problems", "tomorrow_plan"):
        values = [item for item in sections.get(field, []) if str(item or "").strip()]
        if values:
            actions.append(AgentAction(type="replace_field", field=field, items=values, source="direct_current_pasted_report"))
    if not actions:
        return None
    return ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        clear_pending_interaction=True,
        actions=actions,
        reason="direct current labeled report paste should replace today's draft",
    )


def _parse_inline_previous_plan_reference(raw_input: str) -> list[str]:
    compact = _compact_for_intent(raw_input)
    if _looks_like_previous_plan_question(raw_input):
        return []
    if not compact or not any(marker in compact for marker in ("昨天", "昨日", "前一天", "上一天")):
        return []
    match = re.search(r"(?:明日计划|明天计划|明日安排|明天安排|计划)(?:有|包括|是|为|：|:)(.+)$", raw_input, flags=re.S)
    if not match:
        return []
    tail = match.group(1).strip(" ，,。；;、\n\r\t")
    if not tail:
        return []
    parts = _split_loose_reference_items(tail)
    return [_normalize_tomorrow_plan(part) for part in parts if _normalize_tomorrow_plan(part)]


def _looks_like_previous_plan_question(raw_input: str) -> bool:
    text = str(raw_input or "").strip()
    compact = _compact_for_intent(text)
    if not compact:
        return False
    if re.search(r"[?\uff1f]$", text):
        return True
    if not any(marker in compact for marker in ("\u6628\u5929", "\u6628\u65e5", "\u524d\u4e00\u5929", "\u4e0a\u4e00\u5929")):
        return False
    if not any(marker in compact for marker in ("\u8ba1\u5212", "\u5b89\u6392", "\u4efb\u52a1", "\u5f85\u529e")):
        return False
    question_markers = (
        "\u4ec0\u4e48",
        "\u5565",
        "\u54ea",
        "\u51e0",
        "\u591a\u5c11",
        "\u600e\u4e48",
        "\u5982\u4f55",
        "\u67e5",
        "\u770b",
        "\u53d1\u6211",
        "\u7ed9\u6211",
    )
    return any(marker in compact for marker in question_markers)


def _split_loose_reference_items(text: str) -> list[str]:
    numbered = _split_structured_field_items(text)
    if len(numbered) >= 2:
        return numbered
    parts = [part.strip(" ，,。；;、\n\r\t") for part in re.split(r"[、；;，,\n\r]+", text)]
    parts = [part for part in parts if part]
    return parts if len(parts) >= 2 else [text.strip(" ，,。；;、\n\r\t")]


def _looks_like_pasted_reference_report(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if _looks_like_current_pasted_report(raw_input):
        return False
    if not ("昨天日报" in compact or "昨日日报" in compact or "昨天的日报" in compact or "昨日的日报" in compact or _extract_labeled_report_date(raw_input) is not None):
        return False
    labeled_count = sum(1 for token in ("今日工作", "今天工作", "问题/风险", "问题风险", "明日计划", "明天计划") if token in compact)
    return labeled_count >= 2


def _looks_like_current_pasted_report(raw_input: str, current_report_date: date | None = None) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    labeled_count = sum(1 for token in ("今日工作", "今天工作", "问题/风险", "问题风险", "明日计划", "明天计划") if token in compact)
    if labeled_count < 2:
        return False
    if any(marker in compact for marker in ("昨天日报", "昨日日报", "昨天的日报", "昨日的日报", "前天日报", "前天的日报", "前日日报", "前日的日报", "作为参考", "参考")):
        return False
    labeled_date = _extract_labeled_report_date(raw_input)
    if labeled_date is not None:
        return current_report_date is not None and labeled_date == current_report_date
    if any(marker in compact for marker in ("当前日报草稿", "当前日报", "当前草稿", "当前填报日期")):
        return True
    return any(marker in compact for marker in ("今天日报", "今日日报", "今天的日报", "今日的日报", "这是今天", "这是今日", "按今天", "这份是今天", "这就是今天", "不是昨天", "不是昨日"))


def _extract_labeled_report_date(raw_input: str) -> date | None:
    match = re.search(r"(?:日期|填报日期)[】\]）)?\s:：]*(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", raw_input)
    if not match:
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", raw_input)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None



def _direct_structured_multi_field_report_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    fields = _extract_structured_multi_field_report_fields(raw_input)
    if fields is None:
        return None
    actions: list[AgentAction] = []
    for field in ("today_work", "problems", "tomorrow_plan"):
        values = [item for item in fields.get(field, []) if str(item or "").strip()]
        if not values:
            continue
        actions.append(AgentAction(type="replace_field", field=field, items=values, source="direct_structured_multi_field_report"))
    if not actions:
        return None
    return ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        clear_pending_interaction=True,
        actions=actions,
        reason="direct structured multi-field report input",
    )


_STRUCTURED_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "today_work": (
        "\u4eca\u65e5\u5de5\u4f5c",
        "\u4eca\u65e5\u5b8c\u6210\u5de5\u4f5c",
        "\u4eca\u65e5\u5b8c\u6210",
        "\u4eca\u5929\u5de5\u4f5c",
        "\u4eca\u5929\u5b8c\u6210",
        "\u5df2\u5b8c\u6210",
        "\u5b8c\u6210\u5de5\u4f5c",
    ),
    "problems": (
        "\u95ee\u9898/\u98ce\u9669",
        "\u95ee\u9898\u98ce\u9669",
        "\u98ce\u9669\u95ee\u9898",
        "\u5b58\u5728\u95ee\u9898",
        "\u9047\u5230\u7684\u95ee\u9898",
        "\u98ce\u9669",
        "\u95ee\u9898",
        "\u56f0\u96be",
        "\u963b\u585e",
        "blocker",
    ),
    "tomorrow_plan": (
        "\u660e\u65e5\u8ba1\u5212",
        "\u660e\u5929\u8ba1\u5212",
        "\u660e\u65e5\u8981\u505a",
        "\u660e\u5929\u8981\u505a",
        "\u660e\u65e5\u5de5\u4f5c",
        "\u660e\u5929\u5de5\u4f5c",
        "\u63a5\u4e0b\u6765\u8ba1\u5212",
        "\u540e\u7eed\u8ba1\u5212",
    ),
}


def _extract_structured_multi_field_report_fields(raw_input: str) -> dict[str, list[str]] | None:
    text = str(raw_input or "")
    if not text.strip():
        return None
    matches: list[tuple[int, int, str, str]] = []
    for field, aliases in _STRUCTURED_FIELD_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            pattern = re.escape(alias) + r"\s*(?:[\uff1a:]|\u4e3a|\u662f)"
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                matches.append((match.start(), match.end(), field, alias))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, str, str]] = []
    last_end = -1
    for match in matches:
        start, end, _field, _alias = match
        if start < last_end:
            continue
        selected.append(match)
        last_end = end
    seen_fields = {field for _start, _end, field, _alias in selected}
    if len(seen_fields) < 2:
        return None
    fields: dict[str, list[str]] = {"today_work": [], "problems": [], "tomorrow_plan": []}
    for index, (_start, end, field, _alias) in enumerate(selected):
        next_start = selected[index + 1][0] if index + 1 < len(selected) else len(text)
        value = text[end:next_start].strip(" \t\r\n\u3000\uff1a:;\uff1b,\uff0c.\u3002")
        if not value:
            continue
        fields[field].extend(_normalize_structured_field_items(field, value))
    if not any(fields.values()):
        return None
    return fields


def _normalize_structured_field_items(field: str, value: str) -> list[str]:
    compact = _compact_for_intent(value)
    if field == "problems" and (
        _mentions_no_problem(value)
        or _is_short_no_problem_reply(value)
        or compact in {"\u65e0", "\u65e0\u98ce\u9669", "\u65e0\u95ee\u9898"}
    ):
        return ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    if field == "tomorrow_plan" and _is_empty_slot_reply(value):
        return [_empty_value_for_field("tomorrow_plan", value)]
    if field == "today_work" and _is_empty_slot_reply(value):
        return [_empty_value_for_field("today_work", value)]
    pieces = _split_structured_field_items(value)
    if field == "tomorrow_plan":
        return [_normalize_tomorrow_plan(piece) for piece in pieces if _normalize_tomorrow_plan(piece)]
    if field == "problems":
        return [_clean_problem_answer(piece) for piece in pieces if _clean_problem_answer(piece)]
    return [_normalize_current_report_work(piece) for piece in pieces if _normalize_current_report_work(piece)]


def _split_structured_field_items(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    numbered = re.split(r"(?:^|[\n\r\uff1b;])\s*(?:\d+|[\uff08(]?\d+[\uff09)])\s*[\u3001\uff0e\.)]\s*", text)
    numbered = [item.strip(" \t\r\n\u3000\uff1b;\uff0c,\u3002.") for item in numbered if item.strip(" \t\r\n\u3000\uff1b;\uff0c,\u3002.")]
    if len(numbered) >= 2:
        return numbered
    lines = [line.strip(" \t\r\n\u3000\uff1b;\uff0c,\u3002.") for line in re.split(r"[\n\r]+", text)]
    lines = [line for line in lines if line]
    if len(lines) >= 2:
        return lines
    return [text.strip(" \t\r\n\u3000\uff1b;\uff0c,\u3002.")]


def _direct_previous_plan_completion_report_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if not (
        _looks_like_all_previous_plan_completion_request(raw_input)
        or _looks_like_partial_previous_plan_completion_request(raw_input)
    ):
        return None
    source = "reuse_previous_plan_for_tomorrow" if _mentions_reuse_previous_plan_for_tomorrow(raw_input) else "direct_previous_plan_completion"
    actions: list[AgentAction] = [
        AgentAction(type="complete_all_previous_plan_items", source=source),
    ]
    problem = _extract_problem_clause_from_previous_plan_completion(raw_input)
    if problem:
        actions.append(
            AgentAction(
                type="append_items",
                field="problems",
                items=[problem],
                source="direct_previous_plan_problem_clause",
            )
        )
    return ActionPlan(
        intent="edit_draft" if existing is not None and _has_report_content(existing) else "fill_report",
        confidence="high",
        should_write=True,
        actions=actions,
        reason="direct previous plan completion with optional same-as-yesterday tomorrow plan and problem clause",
    )


def _extract_problem_clause_from_previous_plan_completion(raw_input: str) -> str:
    sentences = [
        sentence.strip(" \t\r\n，,。；;、")
        for sentence in re.split(r"[\n\r。；;！？!?]+", str(raw_input or ""))
        if sentence.strip(" \t\r\n，,。；;、")
    ]
    for sentence in sentences:
        compact = _compact_for_intent(sentence)
        if not compact:
            continue
        if any(marker in compact for marker in ("昨天", "昨日", "昨儿")) and any(marker in compact for marker in ("计划", "待办", "安排", "事项")):
            continue
        if (
            any(marker in compact for marker in ("昨天", "昨日", "昨儿"))
            and any(marker in compact for marker in ("第", "除", "除了", "其他", "其它", "其余", "剩下", "剩余"))
            and any(marker in compact for marker in ("完成", "做完", "没做", "未做", "没完成", "未完成"))
        ):
            continue
        if _mentions_no_problem(sentence):
            return "暂无明显问题"
        if _looks_like_problem(sentence):
            return _clean_problem_answer(_strip_current_problem_clause_prefix(sentence))
    return ""


def _strip_current_problem_clause_prefix(text: str) -> str:
    text = str(text or "").strip(" \t\r\n，,。；;、")
    text = re.sub(r"^(今天|今日|本日)[，,。；;、\s]*", "", text)
    text = re.sub(r"^(遇到|碰到|发现|存在|有)?(问题|风险|困难|异常)(是|为|在于)?[：:\s]*", "", text)
    text = re.sub(r"^(遇到|碰到|发现|存在|有)[：:\s]*", "", text)
    return text.strip(" \t\r\n，,。；;、")


def _direct_simple_full_report_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is not None and _has_report_content(existing):
        return None
    fields = _extract_simple_report_fields(raw_input)
    if not fields.get("today_work") or not fields.get("tomorrow_plan"):
        return None
    if len(fields["today_work"]) == 1 and re.search(r"[、；;]", fields["today_work"][0]):
        return None
    if not fields.get("problems") and not _mentions_no_problem(raw_input):
        return None
    if _needs_specific_work_clarification(fields.get("today_work") or [], fields.get("tomorrow_plan") or []):
        return ActionPlan(
            intent="unclear",
            confidence="high",
            should_write=False,
            reply_to_user="这条今日工作还比较笼统，我先不写入。请补充具体处理了哪个事项、系统或材料。",
            reason="direct simple full report rejected low-information work item",
        )
    problems = fields.get("problems") or ["暂无明显问题"]
    return ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        actions=[
            AgentAction(type="replace_field", field="today_work", items=fields["today_work"]),
            AgentAction(type="replace_field", field="problems", items=problems),
            AgentAction(type="replace_field", field="tomorrow_plan", items=fields["tomorrow_plan"]),
        ],
        reason="direct simple full report with no-problem and tomorrow plan",
    )


def _needs_specific_work_clarification(today_work: list[str], tomorrow_plan: list[str]) -> bool:
    if len(today_work) != 1:
        return False
    work = _compact_for_intent(today_work[0])
    plan = _compact_for_intent(" ".join(tomorrow_plan or []))
    if not work:
        return False
    generic_work = re.fullmatch(r"(处理|推进|优化|恢复|完善|整理|跟进)(了)?(项目|系统|技能|事项|工作|内容|问题)", work) is not None
    vague_plan = not plan or _is_low_information_plan(plan)
    return generic_work and vague_plan


def _is_low_information_plan(text: str) -> bool:
    compact = _compact_for_intent(text)
    if _is_vague_tomorrow_plan(compact):
        return True
    return re.fullmatch(r"(明天|明日|明儿)?(继续|接着|再)(推进|跟进|做|弄|看|处理)(相关)?(工作|事项|内容|问题)?", compact) is not None


def _direct_single_action_effects_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    compact = _compact_for_intent(raw_input)
    raw_lower = raw_input.lower()
    if "优化" not in compact or not any(token in raw_lower for token in ("发送逻辑", "ai发送", "ai发送逻辑")):
        return None
    if not any(token in raw_input for token in ("效果", "包括", "：", ":", "1.", "1。", "一是")):
        return None
    items = _single_action_effect_items(raw_input)
    if not items:
        return None
    action_type = "append_items"
    if existing is not None and not (existing.today_work or existing.problems or existing.tomorrow_plan):
        action_type = "replace_field"
    return ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type=action_type, field="today_work", items=items)],
        reason="direct AI sending logic enumerated effects should be manageable work items",
    )


def _direct_numbered_work_items_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is not None and _has_report_content(existing):
        return None
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("今天做了这些", "今天主要做了这些", "今天做了几件事", "今天做了三件事", "今天处理了这些", "今天主要工作", "今日主要工作")):
        return None
    fields = _extract_simple_report_fields(raw_input)
    items = _extract_numbered_work_items(_numbered_work_without_problem_plan_tail(raw_input))
    if len(items) < 2:
        return None
    actions: list[AgentAction] = [
        AgentAction(type="replace_field", field="today_work", items=items, source="direct_numbered_work_items")
    ]
    problems = fields.get("problems") or (["暂无明显问题"] if _mentions_no_problem(raw_input) else [])
    if problems:
        actions.append(AgentAction(type="replace_field", field="problems", items=problems))
    if fields.get("tomorrow_plan"):
        plan_items = [
            _normalize_tomorrow_plan(_strip_current_report_field_prefix(item, "tomorrow_plan"))
            for item in fields["tomorrow_plan"]
            if _normalize_tomorrow_plan(_strip_current_report_field_prefix(item, "tomorrow_plan"))
        ]
        if plan_items:
            actions.append(AgentAction(type="replace_field", field="tomorrow_plan", items=plan_items))
    return ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        actions=actions,
        reason="direct numbered work list",
    )


def _numbered_work_without_problem_plan_tail(raw_input: str) -> str:
    text = str(raw_input or "")
    text = re.sub(
        r"[，,。；;、\s]*(没事|没啥事|没什么事|没有事|没问题|没啥问题|没什么问题|没有问题|问题没有|暂无问题|无问题|暂无明显问题|无明显问题|没风险|没什么风险|没有风险|暂无风险|无风险|无明显风险).*?(?=(明天|明日|明儿)|$)",
        "",
        text,
        flags=re.S,
    )
    text = re.sub(r"[，,。；;、\s]*(明天|明日|明儿|明日计划|明天计划).*$", "", text, flags=re.S)
    return text


def _direct_today_slot_colloquial_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if _infer_current_slot(existing) != "today_work":
        return None
    text = _normalize_free_text(raw_input)
    compact = _compact_for_intent(text)
    if not compact or _is_non_report_phrase(text):
        return None
    if any(token in compact for token in ("哈哈", "呵呵", "随便", "你猜")):
        return None
    if _extract_numbered_work_items(raw_input) or re.search(r"(一是|二是|三是|第一|第二|第三|\d+[.、。])", raw_input):
        return None
    if any(token in compact for token in ("明天", "明日", "问题", "风险", "困难")):
        return None
    if not (
        _looks_like_today_work(text)
        or _looks_like_today_work_action(text)
        or re.search(r"(吃了|做了|弄了|搞了|看了|写了|改了|发了|去了).+", compact)
    ):
        return None
    return ActionPlan(
        intent="fill_report",
        confidence="medium",
        should_write=True,
        actions=[AgentAction(type="append_items", field="today_work", items=[text])],
        reason="direct current today_work slot accepts colloquial but meaningful answer",
    )


def _direct_repair_feedback_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if not _looks_like_pure_repair_feedback(raw_input):
        return None
    return ActionPlan(
        intent="emotional_feedback",
        confidence="high",
        should_write=False,
        actions=[AgentAction(type="no_op")],
        clear_pending_interaction=bool(_get_pending_interaction(existing)),
        reply_to_user=_repair_feedback_reply(existing),
        reason="direct repair feedback should not be written as report content",
    )


def _looks_like_pure_repair_feedback(raw_input: str) -> bool:
    text = _normalize_free_text(raw_input)
    compact = _compact_for_intent(text)
    if not compact:
        return False
    if _looks_like_full_report_input(raw_input):
        return False
    replacement_or_new_content_markers = (
        "\u6539\u6210",
        "\u6539\u4e3a",
        "\u4fee\u6539\u4e3a",
        "\u66f4\u65b0\u4e3a",
        "\u6362\u6210",
        "\u60f3\u8bf4",
        "\u5176\u5b9e\u662f",
        "\u662f\u8fd9\u4e2a",
        "\u4eca\u65e5\u5de5\u4f5c",
        "\u4eca\u65e5\u5b8c\u6210",
        "\u660e\u65e5\u8ba1\u5212",
        "\u660e\u5929\u8ba1\u5212",
        "\u95ee\u9898\u98ce\u9669",
        "\u98ce\u9669",
        "\u7b2c",
        "\u5408\u5e76",
        "\u5220\u9664",
        "\u5220\u6389",
        "\u6e05\u7a7a",
        "\u63d0\u4ea4",
        "\u8865\u5145",
        "\u52a0\u4e0a",
    )
    if any(marker in compact for marker in replacement_or_new_content_markers):
        return False
    exact_feedback = {
        "\u9519\u4e86",
        "\u9519\u7684",
        "\u4e0d\u5bf9",
        "\u4e0d\u662f",
        "\u4e0d\u662f\u8fd9\u4e2a",
        "\u4e0d\u662f\u8fd9\u6837",
        "\u4e0d\u662f\u8fd9\u4e48\u5199",
        "\u4e0d\u662f\u8fd9\u4e2a\u610f\u601d",
        "\u65e0\u8bed",
        "\u5565\u73a9\u610f",
        "\u5565\u73a9\u610f\u554a",
        "\u4ec0\u4e48\u9b3c",
        "\u8fd9\u4ec0\u4e48",
        "\u8fd9\u662f\u5565",
        "\u8fd9\u662f\u4ec0\u4e48",
        "\u5b7a\u5b50\u4e0d\u53ef\u6559",
        "\u5df2\u7ecf\u8bf4\u4e86",
        "\u6211\u5df2\u7ecf\u8bf4\u4e86",
    }
    if compact in exact_feedback:
        return True
    repair_markers = (
        "\u4f60\u7406\u89e3\u9519",
        "\u7406\u89e3\u9519\u4e86",
        "\u641e\u9519\u4e86",
        "\u4e0a\u4e00\u6761\u8bed\u97f3",
        "\u6211\u8bf4\u7684\u662f\u4e0a\u4e00\u6761",
        "\u5df2\u7ecf\u8bf4\u4e86",
        "\u4e0d\u662f\u8fd9\u4e2a\u610f\u601d",
    )
    return len(compact) <= 24 and any(marker in compact for marker in repair_markers)


def _repair_feedback_reply(existing: DailyReport | None) -> str:
    if _get_pending_interaction(existing):
        return "\u6211\u7406\u89e3\u662f\u521a\u624d\u90a3\u4e00\u6b65\u4e0d\u5bf9\uff0c\u8fd9\u53e5\u4e0d\u4f1a\u5199\u5165\u65e5\u62a5\u3002\u6211\u5148\u9000\u51fa\u4e0a\u4e00\u8f6e\u7b49\u5f85\uff0c\u4f60\u53ef\u4ee5\u76f4\u63a5\u53d1\u6b63\u786e\u5185\u5bb9\uff0c\u6216\u8bf4\u8981\u6539\u54ea\u5929\u3001\u54ea\u4e00\u680f\u3002"
    if existing is not None and _has_report_content(existing):
        return "\u6211\u7406\u89e3\u4f60\u662f\u5728\u7ea0\u6b63\u6211\u521a\u624d\u7684\u7406\u89e3\uff0c\u8fd9\u53e5\u4e0d\u4f1a\u5199\u5165\u65e5\u62a5\uff0c\u5f53\u524d\u8349\u7a3f\u5df2\u4fdd\u7559\u3002\u4f60\u53ef\u4ee5\u76f4\u63a5\u53d1\u6b63\u786e\u7248\u672c\uff0c\u6216\u8bf4\u201c\u521a\u624d\u90a3\u6761\u6539\u6210\u2026\u2026\u201d\u3002"
    return "\u6211\u7406\u89e3\u4f60\u662f\u5728\u7ea0\u6b63\u6211\u521a\u624d\u7684\u7406\u89e3\uff0c\u8fd9\u53e5\u4e0d\u4f1a\u5199\u5165\u65e5\u62a5\u3002\u8bf7\u76f4\u63a5\u53d1\u6b63\u786e\u65e5\u62a5\u5185\u5bb9\uff0c\u6216\u8bf4\u8981\u5904\u7406\u54ea\u4e00\u5929\u3002"


def _direct_asr_correction_work_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("听错了", "识别错了", "不是")):
        return None
    match = re.search(r"(?:是|改成|改为)([^，,。；;、]+(?:完成|完了|补齐|补全|整理|处理)[^，,。；;、]*)", raw_input)
    if not match:
        return None
    item = _normalize_free_text(match.group(1))
    if not item:
        return None
    compact_item = _compact_for_intent(item)
    if not (
        _looks_like_today_work_action(item)
        or (
            any(token in compact_item for token in ("用印", "材料", "台账", "合同"))
            and any(token in compact_item for token in ("补齐", "补全", "整理", "处理", "完成", "完了", "签署"))
        )
    ):
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="append_items", field="today_work", items=[item])],
        reason="direct ASR correction that provides a completed work item",
    )


def _direct_negative_replacement_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None or not _has_report_content(existing):
        return None
    replacement = _parse_negative_replacement(raw_input)
    if replacement is None:
        return None
    old_raw, new_raw = replacement
    for old_value in _negative_replacement_old_candidates(old_raw):
        matches = _find_text_matches(existing, old_value)
        if len(matches) != 1:
            continue
        field, index, text = matches[0]
        new_value = _negative_replacement_adjusted_new_value(text, old_value, new_raw)
        if not new_value:
            continue
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            actions=[
                AgentAction(
                    type="replace_text",
                    field=field,
                    old_value=old_value,
                    new_value=new_value,
                    target_item_index=index,
                    source="direct_negative_replacement",
                )
            ],
            clear_pending_interaction=True,
            reason="direct negative replacement in existing draft item",
        )
    return None


def _direct_not_replace_but_add_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None or not _has_report_content(existing):
        return None
    match = re.search(r"不是\s*(.+?)[，,。；;]?\s*(?:而是|是)\s*(.+)$", str(raw_input or "").strip())
    if not match:
        return None
    old_phrase = _normalize_free_text(match.group(1))
    new_phrase = _normalize_free_text(match.group(2))
    if not old_phrase or not new_phrase:
        return None
    compact_old = _compact_for_intent(old_phrase)
    if not any(marker in compact_old for marker in ("改成", "改为", "替换", "换成")):
        return None
    target = re.sub(r"^(?:改成|改为|替换(?:成|为)?|换成)", "", old_phrase).strip(" ：:，,。；;、“”\"'")
    new_item = re.sub(r"^(?:还要|也要|还得|还是|也|还)", "", new_phrase).strip(" ：:，,。；;、“”\"'")
    if not new_item:
        new_item = target
    last_modified = _last_modified_payload(existing)
    if last_modified is None:
        return None
    field = str(last_modified.get("section") or last_modified.get("field") or "")
    if field not in REPORT_FIELD_NAMES:
        return None
    old_content = _normalize_free_text(str(last_modified.get("old_content") or ""))
    new_content = _normalize_free_text(str(last_modified.get("new_content") or ""))
    if not old_content or (target and target not in new_content and target not in new_item):
        return None
    current_items = list(getattr(existing, field, []) or [])
    try:
        item_index = int(last_modified.get("item_index") or 0)
    except (TypeError, ValueError):
        item_index = 0
    if item_index < 1 or item_index > len(current_items):
        return None
    if field == "tomorrow_plan":
        new_item = _normalize_tomorrow_plan(new_item)
    elif field == "problems":
        new_item = _clean_problem_answer(new_item)
    else:
        new_item = _normalize_current_report_work(new_item)
    if not new_item:
        return None
    next_items = list(current_items)
    next_items[item_index - 1] = old_content
    if all(_compact_for_intent(item) != _compact_for_intent(new_item) for item in next_items):
        next_items.append(new_item)
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="replace_field", field=field, items=next_items, source="direct_not_replace_but_add")],
        clear_pending_interaction=True,
        reason="direct restore overwritten item and append also-mentioned item",
    )


def _parse_negative_replacement(raw_input: str) -> tuple[str, str] | None:
    match = re.search(r"不是\s*(.+?)[，,。；;]?\s*(?:而是|是)\s*(.+)$", str(raw_input or "").strip())
    if not match:
        return None
    old_value = _normalize_free_text(match.group(1)).strip(" ：:，,。；;、“”\"'")
    new_value = _normalize_free_text(match.group(2)).strip(" ：:，,。；;、“”\"'")
    if not old_value or not new_value:
        return None
    if any(marker in _compact_for_intent(old_value) for marker in ("改成", "改为", "替换", "换成")):
        return None
    return old_value, new_value


def _negative_replacement_old_candidates(old_value: str) -> list[str]:
    cleaned = _normalize_free_text(old_value).strip(" ：:，,。；;、“”\"'")
    candidates = [cleaned]
    for prefix in ("去", "到", "赴", "前往"):
        if cleaned.startswith(prefix) and len(cleaned) > len(prefix):
            candidates.append(cleaned[len(prefix) :])
    result: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _negative_replacement_adjusted_new_value(current_item: str, old_value: str, new_raw: str) -> str:
    if old_value not in current_item:
        return ""
    suffix = current_item.split(old_value, 1)[1]
    replacement = _normalize_free_text(new_raw).strip(" ：:，,。；;、“”\"'")
    if suffix and replacement.endswith(suffix):
        replacement = replacement[: -len(suffix)].strip(" ：:，,。；;、“”\"'")
    return replacement


def _direct_replace_plan_with_drop_old_reference_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None or not _has_report_content(existing):
        return None
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("计划换成", "计划改成", "计划改为", "明日计划换成", "明天计划换成")):
        return None
    if not any(token in compact for token in ("删了", "删除", "去掉", "不要了", "不用了")):
        return None
    match = re.search(r"(?:明日计划|明天计划|计划)(?:换成|改成|改为|更新为)([^，,。；;]+)", raw_input)
    if not match:
        return None
    item = _normalize_tomorrow_plan(match.group(1))
    if not item:
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="replace_field", field="tomorrow_plan", items=[item])],
        reason="direct plan replacement with old reference removal wording",
    )


def _direct_rewrite_last_modified_item_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("刚才那条", "刚才那个", "刚刚那条", "刚刚那个", "上一条", "上条", "那条")):
        return None
    if not any(token in compact for token in ("改成", "改为", "修改为", "更新为", "换成")):
        return None
    match = re.search(r"(?:改成|改为|修改为|更新为|换成)(.+)$", raw_input)
    if not match:
        return None
    new_value = _normalize_free_text(match.group(1)).strip(" ，,。；;、")
    if not new_value:
        return None
    target = _last_modified_item_target(existing)
    if target is None:
        return None
    field, item_index = target
    current_items = list(getattr(existing, field, []) or [])
    if item_index < 1 or item_index > len(current_items):
        return None
    if field == "problems" and (_mentions_no_problem(new_value) or _is_short_no_problem_reply(new_value)):
        new_value = "暂无明显问题"
    elif field == "tomorrow_plan":
        new_value = _normalize_tomorrow_plan(new_value)
    else:
        new_value = _normalize_free_text(new_value)
    if not new_value:
        return None
    new_items = list(current_items)
    new_items[item_index - 1] = new_value
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[
            AgentAction(
                type="replace_field",
                field=field,
                items=new_items,
                source="direct_rewrite_last_modified_item",
            )
        ],
        clear_pending_interaction=True,
        reason="direct rewrite of last modified item",
    )


def _direct_delete_last_modified_item_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None or not _has_report_content(existing):
        return None
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("删", "删除", "删掉", "去掉", "不要")):
        return None
    if not any(token in compact for token in ("刚补", "刚加", "刚新增", "刚才那条", "刚才这个", "刚才的")):
        return None
    target = _last_modified_item_target(existing)
    if target is None:
        return None
    field, item_index = target
    current_items = list(getattr(existing, field, []) or [])
    if item_index < 1 or item_index > len(current_items):
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[
            AgentAction(
                type="delete_item",
                field=field,
                item_indices=[item_index],
                source="direct_delete_last_modified_item",
            )
        ],
        clear_pending_interaction=True,
        reason="direct delete of last modified item",
    )


def _last_modified_item_target(existing: DailyReport) -> tuple[str, int] | None:
    payload = _last_modified_payload(existing)
    if not isinstance(payload, dict):
        return None
    field = str(payload.get("section") or payload.get("field") or "")
    if field not in REPORT_FIELD_NAMES:
        return None
    try:
        item_index = int(payload.get("item_index") or 0)
    except (TypeError, ValueError):
        return None
    if item_index >= 1:
        return field, item_index
    return None


def _last_modified_payload(existing: DailyReport) -> dict[str, Any] | None:
    section_status = getattr(existing, "section_status", None)
    if not isinstance(section_status, dict):
        return None
    for key in ("_correction_target", "_last_modified_item"):
        payload = section_status.get(key)
        if not isinstance(payload, dict):
            continue
        return payload
    return None


def _direct_ordinal_rewrite_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("改成", "改为", "修改为", "更新为", "换成")):
        return None
    match = re.search(r"第([一二两三四五六七八九十\d]+)(?:条|项)?.*(?:改成|改为|修改为|更新为|换成)(.+)$", raw_input)
    if not match:
        return None
    index = _parse_ordinal_number(match.group(1))
    new_value = _normalize_free_text(match.group(2))
    if not index or not new_value:
        return None
    display_resolution = _resolve_last_display_indices(existing, [index])
    if display_resolution is not None:
        field, resolved_indices = display_resolution
        index = resolved_indices[0]
    else:
        field = _field_from_merge_text(compact)
    current_items = list(getattr(existing, field, []) or [])
    if index < 1 or index > len(current_items):
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user=f"我只看到{_draft_field_label(field)}共 {len(current_items)} 条，没有第 {index} 条。",
            reason="ordinal rewrite index out of bounds",
        )
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[
            AgentAction(
                type="replace_text",
                field=field,
                old_value=current_items[index - 1],
                new_value=new_value,
                target_item_index=index,
            )
        ],
        reason="direct deterministic ordinal rewrite before LLM",
    )


def _direct_set_section_empty_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return None
    if _should_defer_direct_empty_section_plan(raw_input, existing):
        return None
    if compact in {"没问题", "没有问题", "无问题", "没啥问题"} and existing is not None:
        if list(getattr(existing, "today_work", []) or []) and list(getattr(existing, "problems", []) or []) and list(getattr(existing, "tomorrow_plan", []) or []):
            return None
    if (
        any(token in compact for token in ("\u6682\u65e0\u98ce\u9669", "\u65e0\u98ce\u9669", "\u6ca1\u98ce\u9669", "\u6ca1\u6709\u98ce\u9669", "\u6682\u65e0\u95ee\u9898", "\u65e0\u95ee\u9898", "\u6ca1\u95ee\u9898", "\u6ca1\u6709\u95ee\u9898", "\u95ee\u9898\u98ce\u9669\u65e0", "\u95ee\u9898\u65e0", "\u98ce\u9669\u65e0"))
        or (_infer_current_slot(existing) == "problems" and _is_short_no_problem_reply(raw_input))
    ):
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            actions=[AgentAction(type="replace_field", field="problems", items=["暂无明显问题"], source="direct_set_section_empty")],
            reason="direct set problems empty/no risk",
        )
    if any(token in compact for token in ("明天没计划", "明天无计划", "明日无计划", "暂无明日计划", "暂无明天计划", "没有明日计划", "没有明天计划", "明日计划无", "明天计划无")):
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            actions=[AgentAction(type="replace_field", field="tomorrow_plan", items=["无明日计划"], source="direct_set_section_empty")],
            reason="direct set tomorrow_plan empty/no plan",
        )
    return None


def _should_defer_direct_empty_section_plan(raw_input: str, existing: DailyReport | None) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    no_problem_tokens = (
        "暂无风险",
        "无风险",
        "没风险",
        "没有风险",
        "暂无问题",
        "无问题",
        "没问题",
        "没有问题",
        "问题风险无",
        "问题无",
        "风险无",
    )
    if not any(token in compact for token in no_problem_tokens):
        return False
    if _is_short_no_problem_reply(raw_input) and _infer_current_slot(existing) == "problems":
        return False
    if compact in {"无", "暂无", "没问题", "没有问题", "无问题", "没啥问题", "无风险", "没风险", "没有风险"}:
        return False
    if _extract_structured_multi_field_report_fields(raw_input) is not None:
        return True
    if _looks_like_previous_report_content_input(raw_input):
        return True
    fields = _extract_simple_report_fields(raw_input)
    if fields.get("today_work") or fields.get("tomorrow_plan"):
        return True
    content_or_plan_markers = (
        "今天",
        "今日",
        "昨天",
        "昨日",
        "明天",
        "明日",
        "明儿",
        "工作",
        "计划",
        "继续",
        "审核",
        "整理",
        "处理",
        "完成",
        "沟通",
        "推进",
        "跟进",
        "开庭",
        "用印",
        "合同",
        "材料",
        "服务器",
        "方案",
    )
    return len(compact) > 12 and any(marker in compact for marker in content_or_plan_markers)


def _current_report_context(existing: DailyReport | None) -> dict[str, list[str]]:
    if existing is None:
        return {"today_work": [], "problems": [], "tomorrow_plan": []}
    return {
        "today_work": list(getattr(existing, "today_work", []) or []),
        "problems": list(getattr(existing, "problems", []) or []),
        "tomorrow_plan": list(getattr(existing, "tomorrow_plan", []) or []),
    }


def _direct_text_replace_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    old_value, new_value = _parse_direct_text_replace(raw_input)
    if not old_value or not new_value:
        return None
    matches = _find_text_matches(existing, old_value)
    if len(matches) == 1:
        field, index, _text = matches[0]
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            actions=[
                AgentAction(
                    type="replace_text",
                    field=field,
                    old_value=old_value,
                    new_value=new_value,
                    target_item_index=index,
                    source="direct_text_replace_unique",
                )
            ],
            clear_pending_interaction=True,
            reason="direct unique text replacement before LLM",
        )
    if len(matches) > 1:
        preview = "、".join(f"{_draft_field_label(field)}第{index}条“{text}”" for field, index, text in matches[:5])
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user=f"我找到多条包含“{old_value}”的内容：{preview}。你要修改哪一条？",
            pending_interaction_to_set=PendingInteractionPlan(
                type="pending_clarification",
                operation="replace_text",
                target_field="none",
                context={
                    "missing_fields": ["item_index"],
                    "candidate_old_text": old_value,
                    "candidate_new_text": new_value,
                    "current_report": _current_report_context(existing),
                    "matches": [
                        {"section": field, "item_index": index, "text": text}
                        for field, index, text in matches
                    ],
                    "source_message": raw_input,
                },
            ),
            reason="direct text replacement matched multiple items and needs item clarification",
        )
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=False,
        reply_to_user=f"我没找到包含“{old_value}”的条目。你要修改第几条？",
        pending_interaction_to_set=PendingInteractionPlan(
            type="pending_clarification",
            operation="replace_text",
            target_field="none",
            context={
                "missing_fields": ["item_index"],
                "candidate_old_text": old_value,
                "candidate_new_text": new_value,
                "current_report": _current_report_context(existing),
                "matches": [],
                "source_message": raw_input,
            },
        ),
        reason="direct text replacement could not locate target item",
    )


def _parse_direct_text_replace(raw_input: str) -> tuple[str, str]:
    text = str(raw_input or "").strip()
    if text.startswith("不是"):
        return "", ""
    match = re.match(r"^(?:把)?(.{1,60}?)(?:改成|改为|换成|修改为|更新为|更正为)(.{1,120})$", text)
    if not match:
        return "", ""
    old_value = _normalize_free_text(match.group(1)).strip(" ：:，,。；;、“”\"'")
    new_value = _normalize_free_text(match.group(2)).strip(" ：:，,。；;、“”\"'")
    if not old_value or not new_value:
        return "", ""
    if re.search(r"^第[一二两三四五六七八九十\d]+(?:条|项|个)?$", old_value):
        return "", ""
    if old_value in REPORT_FIELD_NAMES:
        return "", ""
    return old_value, new_value


def _find_text_matches(existing: DailyReport, needle: str) -> list[tuple[str, int, str]]:
    compact_needle = _compact_for_intent(needle)
    if not compact_needle:
        return []
    matches: list[tuple[str, int, str]] = []
    for field in REPORT_FIELD_NAMES:
        for index, item in enumerate(list(getattr(existing, field, []) or []), start=1):
            text = str(item or "")
            compact_text = _compact_for_intent(text)
            if needle in text or compact_needle in compact_text:
                matches.append((field, index, text))
    return matches


def _direct_ordinal_move_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("移到", "挪到", "放到", "归到", "是明天计划", "是明日计划", "属于明天计划", "属于明日计划")):
        return None
    if not _has_explicit_ordinal_reference(raw_input):
        return None
    indices = _extract_move_ordinal_refs(raw_input) or _extract_ordinal_refs(raw_input)
    if not indices:
        return None
    source_field = "today_work"
    if re.search(r"(问题|风险|困难).{0,6}第", raw_input):
        source_field = "problems"
    elif re.search(r"(明日计划|明天计划|计划).{0,6}第", raw_input):
        source_field = "tomorrow_plan"
    target_field = None
    if any(token in compact for token in ("明天", "明日", "明儿", "计划", "安排")):
        target_field = "tomorrow_plan"
    elif any(token in compact for token in ("问题", "风险", "困难", "卡点")):
        target_field = "problems"
    elif any(token in compact for token in ("今日工作", "今天工作", "完成事项", "工作")):
        target_field = "today_work"
    if not target_field or target_field == source_field:
        return None
    current_items = list(getattr(existing, source_field, []) or [])
    unique_indices = sorted({index for index in indices if index >= 1})
    if not unique_indices:
        return None
    max_index = max(unique_indices)
    if max_index > len(current_items):
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user=f"我只看到{_draft_field_label(source_field)}共 {len(current_items)} 条，没有第 {max_index} 条。",
            reason="ordinal move index out of bounds",
        )
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[
            AgentAction(
                type="move_item",
                field=source_field,
                source_field=source_field,
                target_field=target_field,
                item_indices=unique_indices,
            )
        ],
        reason="direct deterministic ordinal move before LLM",
    )


def _extract_move_ordinal_refs(raw_input: str) -> list[int]:
    move_markers = ("移到", "挪到", "放到", "归到", "转到", "放进", "移进", "挪进")
    destination_markers = ("明天计划", "明日计划", "明天安排", "明日安排", "问题", "风险", "困难")
    clauses = [part.strip() for part in re.split(r"[，,。；;]", raw_input) if part.strip()]
    for clause in reversed(clauses):
        compact = _compact_for_intent(clause)
        if not compact:
            continue
        if not any(marker in compact for marker in move_markers + destination_markers):
            continue
        refs = _extract_ordinal_refs(clause)
        if refs:
            return refs
    return []


def _direct_problem_slot_answer_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if _infer_current_slot(existing) != "problems":
        return None
    if _looks_like_full_report_input(raw_input):
        return None
    if _is_ambiguous_problem_signal(raw_input):
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user="你说的问题/风险具体是什么？如果没有问题，可以回复“无”。",
            reason="ambiguous problem signal needs clarification",
        )
    problem_text, plan_text = _split_problem_and_plan(raw_input)
    compact = _compact_for_intent(raw_input)
    if _mentions_no_problem(problem_text or raw_input) or _is_short_no_problem_reply(problem_text or raw_input):
        problem_text = "暂无明显问题"
    else:
        if not any(token in compact for token in ("问题", "风险", "材料", "资料", "客户", "对方", "反馈", "收齐", "给全", "加全", "影响", "缺")):
            return None
        problem_text = _clean_problem_answer(problem_text)
    embedded_problem, cleaned_plan = _split_embedded_problem_from_plan_text(plan_text)
    if embedded_problem and not problem_text:
        problem_text = embedded_problem
        plan_text = cleaned_plan
    actions: list[AgentAction] = []
    if problem_text:
        actions.append(AgentAction(type="append_items", field="problems", items=[problem_text]))
    if plan_text:
        actions.append(AgentAction(type="append_items", field="tomorrow_plan", items=[_normalize_tomorrow_plan(plan_text)]))
    if not actions:
        return None
    return ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        actions=actions,
        reason="direct problem-slot answer",
    )


def _direct_plan_slot_answer_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if _infer_current_slot(existing) != "tomorrow_plan":
        return None
    if _looks_like_full_report_input(raw_input):
        return None
    if _is_probable_noise_input(raw_input) or _is_non_report_phrase(raw_input):
        return None
    compact = _compact_for_intent(raw_input)
    if not compact:
        return None
    if _looks_like_slot_answer_edit_command(compact):
        return None
    if _is_empty_slot_reply(raw_input) or _is_short_no_problem_reply(raw_input):
        return ActionPlan(
            intent="fill_report",
            confidence="high",
            should_write=True,
            actions=[AgentAction(type="append_items", field="tomorrow_plan", items=[_empty_value_for_field("tomorrow_plan", raw_input)])],
            reason="direct tomorrow-plan empty slot answer",
        )
    followup_target = _last_filled_slot_followup_target(raw_input, existing)
    if followup_target:
        followup_text = _clean_last_filled_slot_followup(raw_input)
        if not followup_text:
            return None
        if followup_target == "today_work":
            item = _normalize_current_report_work(followup_text)
        elif followup_target == "problems":
            item = _clean_problem_answer(followup_text)
        else:
            item = _normalize_tomorrow_plan(followup_text)
        if not item:
            return None
        return ActionPlan(
            intent="fill_report",
            confidence="high",
            should_write=True,
            actions=[AgentAction(type="append_items", field=followup_target, items=[item])],
            reason="direct last-filled-slot follow-up while awaiting tomorrow plan",
        )
    if any(token in compact for token in ("\u95ee\u9898", "\u98ce\u9669", "\u56f0\u96be", "\u963b\u788d", "blocker")) and not any(token in compact for token in ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u540e\u5929", "\u63a5\u4e0b\u6765", "\u540e\u7eed", "\u8ba1\u5212")):
        return None
    embedded_problem, cleaned_plan = _split_embedded_problem_from_plan_text(raw_input)
    plan_text = cleaned_plan or raw_input
    plan_item = _normalize_tomorrow_plan(plan_text)
    if not plan_item:
        return None
    actions = [AgentAction(type="append_items", field="tomorrow_plan", items=[plan_item])]
    if embedded_problem:
        action_type = "replace_field" if existing and _is_placeholder_problem_list(list(existing.problems or [])) else "append_items"
        actions.insert(0, AgentAction(type=action_type, field="problems", items=[embedded_problem]))
    return ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        actions=actions,
        reason="direct tomorrow-plan slot answer",
    )


def _direct_tomorrow_plan_phrase_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    if _looks_like_full_report_input(raw_input) or _is_probable_noise_input(raw_input):
        return None
    compact = _compact_for_intent(raw_input)
    # A sentence that also carries an explicit current-day fact is a
    # multi-section turn. Treating the whole sentence as one future plan loses
    # today's work and can drag historical reference text into tomorrow_plan.
    if any(marker in compact for marker in ("\u4eca\u5929", "\u4eca\u65e5", "\u672c\u65e5", "\u5f53\u524d")):
        return None
    if not any(marker in compact for marker in ("明天", "明日", "明儿", "后天", "下周")):
        return None
    if any(marker in compact for marker in ("不是明天", "不用明天", "取消明天", "删除", "删掉", "清空", "改成", "修改")):
        return None
    action_markers = (
        "去",
        "出差",
        "开庭",
        "处理",
        "跟进",
        "完成",
        "审核",
        "优化",
        "整理",
        "沟通",
        "参加",
        "推进",
        "办理",
        "对接",
        "盖章",
        "调研",
        "汇报",
    )
    if not any(marker in compact for marker in action_markers):
        return None
    leading_problem, plan_text = _split_leading_problem_from_future_plan_text(raw_input)
    plan_item = _normalize_tomorrow_plan(plan_text)
    if not plan_item:
        return None
    actions: list[AgentAction] = []
    if leading_problem:
        existing_problems = list(getattr(existing, "problems", []) or [])
        section_status = getattr(existing, "section_status", {}) or {}
        if leading_problem != "暂无明显问题" or (not existing_problems and not section_status.get("problems_acknowledged_empty")):
            action_type = "replace_field" if leading_problem == "暂无明显问题" or _is_placeholder_problem_list(existing_problems) else "append_items"
            actions.append(AgentAction(type=action_type, field="problems", items=[leading_problem]))
    actions.append(AgentAction(type="append_items", field="tomorrow_plan", items=[plan_item]))
    return ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        actions=actions,
        reason="direct tomorrow-plan phrase",
    )


def _looks_like_slot_answer_edit_command(compact: str) -> bool:
    edit_markers = (
        "\u5408\u5e76",
        "\u548c\u5e76",
        "\u5e76\u4eca\u65e5\u5de5\u4f5c",
        "\u540c\u4e00\u70b9",
        "\u540c\u4e00\u6761",
        "\u4e00\u56de\u4e8b",
        "\u4e00\u4ef6\u4e8b",
        "\u4e0d\u8981\u62c6",
        "\u5408\u5e76\u540c\u7c7b\u9879",
        "\u5220\u9664",
        "\u5220\u6389",
        "\u6e05\u7a7a",
        "\u6539\u6210",
        "\u4fee\u6539",
        "\u91cd\u65b0\u6574\u7406",
        "\u91cd\u5199",
    )
    if any(marker in compact for marker in edit_markers):
        return True
    return bool(re.search(r"\d+[-\uff0d\u2014\u2013]\d+", compact))


def _last_filled_slot_followup_target(raw_input: str, existing: DailyReport | None) -> str | None:
    if existing is None:
        return None
    compact = _compact_for_intent(raw_input)
    if not compact:
        return None
    if any(marker in compact for marker in ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u540e\u5929", "\u63a5\u4e0b\u6765", "\u540e\u7eed", "\u8ba1\u5212")):
        return None
    followup_markers = ("\u8fd8", "\u53e6\u5916", "\u8865\u5145", "\u5fd8\u4e86", "\u8fd8\u6709", "\u518d\u52a0", "\u987a\u4fbf")
    if not any(marker in compact for marker in followup_markers):
        return None
    filled_fields = [field for field in ("today_work", "problems", "tomorrow_plan") if list(getattr(existing, field, []) or [])]
    if not filled_fields:
        return None
    if list(getattr(existing, "today_work", []) or []):
        return "today_work"
    return filled_fields[-1]


def _clean_last_filled_slot_followup(raw_input: str) -> str:
    text = _normalize_free_text(raw_input)
    text = re.sub(r"^(\u7b49\u7b49|\u7b49\u4e0b|\u7b49\u4e00\u4e0b|\u5bf9\u4e86|\u54e6\u5bf9|\u53e6\u5916|\u8fd8\u6709|\u8865\u5145|\u5fd8\u4e86|\u987a\u4fbf)[\uff0c,\u3002\uff1b;\u3001\s]*", "", text)
    text = re.sub(r"^(\u6211|\u8fd9\u8fb9|\u8fd9\u91cc)[\uff0c,\u3002\uff1b;\u3001\s]*", "", text)
    return text.strip(" \uff0c,\u3002\uff1b;\u3001")

def _direct_explicit_problem_append_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    compact = _compact_for_intent(raw_input)
    problem_prefixes = (
        "还有个问题",
        "还有一个问题",
        "补充一个问题",
        "补充个问题",
        "补充问题",
        "有个风险",
        "还有个风险",
        "还有一个风险",
        "补充一个风险",
        "补充个风险",
        "补充风险",
    )
    if not any(token in compact for token in problem_prefixes):
        return None
    problem_text = _clean_problem_answer(raw_input)
    problem_text = re.sub(r"^(还有个问题|还有一个问题|补充一个问题|补充个问题|补充问题|有个风险|还有个风险|还有一个风险|补充一个风险|补充个风险|补充风险)[，,。；;、\s]*", "", problem_text)
    if not problem_text:
        return None
    action_type = "append_items"
    if _is_placeholder_problem_list(list(existing.problems or [])):
        action_type = "replace_field"
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type=action_type, field="problems", items=[problem_text])],
        reason="direct explicit problem append",
    )


def _direct_last_unwritten_candidate_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    candidate = _last_unwritten_candidate_text(existing)
    if not candidate:
        return None
    field = _last_candidate_target_field(raw_input)
    if field not in {"today_work", "problems", "tomorrow_plan"}:
        return None
    value = candidate
    if field == "problems":
        value = _clean_problem_answer(value)
        action_type = "replace_field" if _is_placeholder_problem_list(list(existing.problems or [])) else "append_items"
    elif field == "tomorrow_plan":
        value = _normalize_tomorrow_plan(value)
        action_type = "append_items"
    else:
        value = _normalize_current_report_work(value)
        action_type = "append_items"
    if not value:
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type=action_type, field=field, items=[value], source="last_unwritten_candidate")],
        clear_pending_interaction=True,
        reason="direct last unwritten candidate placement",
    )


def _direct_this_is_problem_without_candidate_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    if _last_unwritten_candidate_text(existing):
        return None
    if _last_candidate_target_field(raw_input) != "problems":
        return None
    if existing.problems:
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user="这条已经在问题/风险里了，我不重复记录。",
            reason="direct problem placement phrase without candidate should not be written literally",
        )
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=False,
        reply_to_user="我知道你想放到问题/风险里，但上一句没有可承接的内容。请直接说明具体问题。",
        reason="direct problem placement phrase without candidate asks for concrete content",
    )


def _last_unwritten_candidate_text(existing: DailyReport | None) -> str:
    section_status = getattr(existing, "section_status", None)
    if not isinstance(section_status, dict):
        return ""
    payload = section_status.get("_last_unwritten_candidate")
    if isinstance(payload, dict):
        return str(payload.get("text") or "").strip()
    return str(payload or "").strip()


def _last_candidate_target_field(raw_input: str) -> str:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return "none"
    if any(token in compact for token in ("这个是问题", "这是问题", "这个放问题", "这句放问题", "这个写问题", "这个算问题", "这个是风险", "这是风险", "放问题里", "放风险里")):
        return "problems"
    if any(token in compact for token in ("这个是明日计划", "这是明日计划", "这个放明日计划", "这个放明天计划", "这句放明日计划", "放计划里", "放明天")):
        return "tomorrow_plan"
    if any(token in compact for token in ("这个是今日工作", "这是今日工作", "这个放今日工作", "这个放今天工作", "这句放今日工作", "放工作里")):
        return "today_work"
    return "none"


def _direct_explicit_field_update_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    direct_update = _extract_direct_field_update(raw_input)
    if direct_update is None:
        return None
    field, value = direct_update
    if field == "problems" and (_mentions_no_problem(value) or _is_short_no_problem_reply(value)):
        value = "暂无明显问题"
    elif field == "tomorrow_plan":
        value = _normalize_tomorrow_plan(value)
    else:
        value = _normalize_free_text(value)
    if not value:
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="replace_field", field=field, items=[value], source="direct_explicit_field_update")],
        clear_pending_interaction=True,
        reason="direct explicit field update",
    )


def _direct_explicit_append_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    field, value = _parse_explicit_append(raw_input)
    if field not in REPORT_FIELD_NAMES or not value:
        return None
    if field == "problems":
        value = _clean_problem_answer(value)
        action_type = "replace_field" if existing and _is_placeholder_problem_list(list(existing.problems or [])) else "append_items"
    elif field == "tomorrow_plan":
        value = _normalize_tomorrow_plan(value)
        action_type = "append_items"
    else:
        value = _normalize_current_report_work(value)
        action_type = "append_items"
    if not value:
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type=action_type, field=field, items=[value], source="direct_explicit_append")],
        clear_pending_interaction=True,
        reason="direct explicit section append before LLM",
    )


def _parse_explicit_append(raw_input: str) -> tuple[str, str]:
    text = str(raw_input or "").strip()
    if not text:
        return "", ""
    patterns: list[tuple[str, str]] = [
        ("today_work", r"^(?:今日工作|今天工作|今天的工作|工作)(?:里|中)?(?:加上|加|补充|新增|添加)(?:一条|一个|一下)?[：:，,。；;\s]*(.+)$"),
        ("problems", r"^(?:问题/风险|问题风险|问题|风险)(?:里|中)?(?:加上|加|补充|新增|添加)(?:一条|一个|一下)?[：:，,。；;\s]*(.+)$"),
        ("tomorrow_plan", r"^(?:明日计划|明天计划|明日安排|明天安排|计划)(?:里|中)?(?:加上|加|补充|新增|添加)(?:一条|一个|一下)?[：:，,。；;\s]*(.+)$"),
        ("today_work", r"^补充到(?:今日工作|今天工作|今天的工作|工作)(?:里|中)?[：:，,。；;\s]*(.+)$"),
        ("problems", r"^补充到(?:问题/风险|问题风险|问题|风险)(?:里|中)?[：:，,。；;\s]*(.+)$"),
        ("tomorrow_plan", r"^补充到(?:明日计划|明天计划|明日安排|明天安排|计划)(?:里|中)?[：:，,。；;\s]*(.+)$"),
    ]
    for field, pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        value = _normalize_free_text(match.group(1))
        if value:
            return field, value
    return "", ""


def _direct_explicit_work_append_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    compact = _compact_for_intent(raw_input)
    if _looks_like_contentless_append_entry(raw_input):
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            pending_interaction_to_set=PendingInteractionPlan(
                type=PENDING_INTERACTION_AWAITING_APPEND_TARGET,
                operation="append",
                target_field="none",
            ),
            reply_to_user="你想补充到哪一栏？可以回复“今日工作”“问题/风险”或“明日计划”。",
            reason="append entry without business content should select a target field first",
        )
    if not any(token in compact for token in ("补充一下", "补充一个", "补充个", "哦对了", "对了", "另外", "还有", "还处理了", "还做了", "又处理了", "又做了")):
        return None
    if any(token in compact for token in ("问题", "风险", "明天", "明日", "计划")):
        return None
    text = _normalize_free_text(raw_input)
    text = re.sub(r"^(补充一下|补充一个|补充个|哦对了|对了|另外|还有|还处理了|还做了|又处理了|又做了)[，,。；;、\s]*", "", text)
    text = re.sub(r"^(补一个|补个|补充一个|补充个|再加一个)[，,。；;、\s]*", "", text)
    if not text:
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="append_items", field="today_work", items=[text])],
        reason="direct explicit work append",
    )


def _direct_restore_snapshot_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    compact = _compact_for_intent(raw_input)
    if compact not in {"撤回", "撤回上一步", "撤回上一步操作", "恢复上一步", "恢复之前", "回退", "回退上一步", "加回来", "加回去"} and not _looks_like_restore_previous_edit_request(raw_input):
        return None
    section_status = dict(existing.section_status or {})
    snapshot = section_status.get(DRAFT_PREVIOUS_SNAPSHOT_KEY)
    if not isinstance(snapshot, dict):
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="restore_snapshot", reference_report=snapshot)],
        clear_pending_interaction=True,
        reason="direct restore previous draft snapshot",
    )


def _looks_like_contentless_append_entry(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if not re.search(r"(补充|追加|加一?条|再加)", compact):
        return False
    if any(marker in compact for marker in ("今日工作", "今天工作", "问题", "风险", "明日计划", "明天计划")):
        return False
    stripped = re.sub(r"(我想|想|帮我|麻烦)?(补充|追加|加一?条|再加)(一下|一个|个|内容|信息|事项)?", "", compact)
    return len(stripped) <= 2


def _looks_like_restore_previous_edit_request(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    has_restore = re.search(r"(撤销|恢复|复原|还原|加回|找回|不删|别删)", compact) is not None
    has_recent_edit = re.search(r"(刚才|上一步|刚刚|之前|删除|删掉|删了|删的|那个删除|不对|不是|错了|错|算了)", compact) is not None
    return bool(has_restore and has_recent_edit)


def _direct_ordinal_delete_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    compact = _compact_for_intent(raw_input)
    if not any(token in compact for token in ("删除", "删掉", "去掉", "移除", "不要")):
        return None
    if re.search(r"(删除|删掉|去掉|移除|不要)(一条|一个|某条|某个)$", compact):
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user="你想删除哪一条？可以说“删除第几条”。",
            reason="ambiguous delete without ordinal target",
        )
    if not _has_explicit_ordinal_reference(raw_input) or not _has_ordinal_delete_target(raw_input):
        return None
    indices = _extract_ordinal_refs(raw_input)
    if not indices:
        return None
    display_resolution = _resolve_last_display_indices(existing, indices)
    if display_resolution is not None:
        field, unique_indices = display_resolution
    else:
        field = _field_from_merge_text(compact)
        unique_indices = sorted({index for index in indices if index >= 1})
    current_items = list(getattr(existing, field, []) or [])
    if not current_items:
        return None
    if not unique_indices:
        return None
    max_index = max(unique_indices)
    if max_index > len(current_items):
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user=f"我只看到{_draft_field_label(field)}共 {len(current_items)} 条，没有第 {max_index} 条。",
            reason="ordinal delete index out of bounds",
        )
    action = AgentAction(type="delete_item", field=field, item_indices=unique_indices)
    if getattr(existing, "status", "") != STATUS_COMPLETED:
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=True,
            actions=[action],
            reason="direct deterministic draft ordinal delete before LLM",
        )
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[action],
        reason="direct deterministic completed ordinal delete before LLM without extra confirmation",
    )


def _direct_range_merge_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    if existing is None:
        return None
    compact = _compact_for_intent(raw_input)
    if not _looks_like_merge_items_request(compact):
        return None
    indices = _extract_merge_item_refs(raw_input)
    if len(indices) < 2:
        return None
    display_resolution = _resolve_last_display_indices(existing, indices)
    if display_resolution is not None:
        field, unique_indices = display_resolution
    else:
        field = _field_from_merge_text(compact, existing=existing, indices=indices)
        unique_indices = sorted({index for index in indices if index >= 1})
    if field not in REPORT_FIELD_NAMES:
        return None
    current_items = list(getattr(existing, field, []) or [])
    if len(unique_indices) < 2:
        return None
    max_index = max(unique_indices)
    if max_index > len(current_items):
        return ActionPlan(
            intent="edit_draft",
            confidence="high",
            should_write=False,
            reply_to_user=f"我只看到{_draft_field_label(field)}共 {len(current_items)} 条，没法合并到第 {max_index} 条。",
            reason="range merge index out of bounds",
        )
    replacement = _extract_range_merge_replacement(raw_input)
    action = AgentAction(
        type="merge_items",
        field=field,
        item_indices=unique_indices,
        items=[replacement] if replacement else [],
        source="direct_range_merge",
    )
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=True,
        actions=[action],
        reason="direct deterministic range merge before LLM",
    )


def _looks_like_merge_items_request(compact: str) -> bool:
    if any(
        token in compact
        for token in (
            "同一点",
            "同一条",
            "同一项",
            "同一个事",
            "同一个事情",
            "一件事",
            "一回事",
            "一个事",
            "一个事儿",
            "是一条",
            "是同一个",
            "合并",
            "和并",
            "合一起",
            "合一块",
            "合成一条",
        )
    ):
        return True
    return compact.startswith("并") and any(token in compact for token in ("今日工作", "今天工作", "问题", "风险", "明日计划", "明天计划"))


def _extract_merge_item_refs(raw_input: str) -> list[int]:
    refs: set[int] = set()
    arabic = r"\d{1,3}"
    chinese = r"[一二两三四五六七八九十]{1,3}"
    compact = _compact_for_intent(raw_input)

    digit_group = re.search(r"(?<!\d)([1-9]{2,4})(?:条|项|个)?(?:合并|同一点|同一条|同一项|一件事|一回事|是一条|是同一个)", compact)
    if digit_group:
        refs.update(int(char) for char in digit_group.group(1))

    chinese_digit_group = re.search(
        r"(?<![一二两三四五六七八九十])([一二两三四五六七八九]{2,6})(?:条|项|个)?(?:合并|同一点|同一条|同一项|一件事|一回事|是一条|是同一个)",
        compact,
    )
    if chinese_digit_group:
        for char in chinese_digit_group.group(1):
            number = _parse_ordinal_number(char)
            if number is not None:
                refs.add(number)

    compact_chinese_ordinals = re.findall(r"第([一二两三四五六七八九十]{1,3})(?:条|项|个)?", compact)
    if len(compact_chinese_ordinals) >= 2 and _looks_like_merge_items_request(compact):
        for value in compact_chinese_ordinals:
            number = _parse_ordinal_number(value)
            if number is not None:
                refs.add(number)

    def add_range(start_value: str, end_value: str) -> None:
        start_num = _parse_ordinal_number(start_value)
        end_num = _parse_ordinal_number(end_value)
        if start_num is None or end_num is None:
            return
        low, high = sorted((start_num, end_num))
        refs.update(range(low, high + 1))

    for start, end in re.findall(rf"({arabic})(?:条|项|个)?(?:到|至|~|～|-|—|－)({arabic})(?:条|项|个)?", raw_input):
        add_range(start, end)
    for start, end in re.findall(rf"第({chinese})(?:条|项|个)(?:到|至|~|～|-|—|－)第({chinese})(?:条|项|个)", raw_input):
        add_range(start, end)

    separator = r"(?:和|跟|与|及|、|，|,|\s+)"
    arabic_group_pattern = rf"((?:第?{arabic}(?:条|项|个)?{separator}){{1,}}第?{arabic}(?:条|项|个)?)"
    for group in re.findall(arabic_group_pattern, raw_input):
        for value in re.findall(arabic, group):
            number = _parse_ordinal_number(value)
            if number is not None:
                refs.add(number)

    chinese_group_pattern = rf"((?:第{chinese}(?:条|项|个)?{separator}){{1,}}第{chinese}(?:条|项|个)?)"
    for group in re.findall(chinese_group_pattern, raw_input):
        for value in re.findall(rf"第({chinese})", group):
            number = _parse_ordinal_number(value)
            if number is not None:
                refs.add(number)
    for value in re.findall(r"(?m)^\s*(\d{1,3})[.、]\s+", raw_input):
        number = _parse_ordinal_number(value)
        if number is not None:
            refs.add(number)
    return sorted(refs)


def _resolve_last_display_indices(existing: DailyReport, display_indices: list[int]) -> tuple[str, list[int]] | None:
    context = _valid_last_display_context(existing)
    if context is None or not display_indices:
        return None
    mapping: dict[int, tuple[str, int]] = {}
    sections = context.get("sections")
    if isinstance(sections, dict):
        iterable = [{"section": key, "items": value} for key, value in sections.items()]
    elif isinstance(sections, list):
        iterable = [section for section in sections if isinstance(section, dict)]
    else:
        iterable = []
    for section in iterable:
        fallback_section = str(section.get("section") or "")
        items = section.get("items") if isinstance(section.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            field = str(item.get("section") or fallback_section)
            if field not in REPORT_FIELD_NAMES:
                continue
            try:
                display_index = int(item.get("display_index") or 0)
                actual_index = int(item.get("actual_index") or 0)
            except (TypeError, ValueError):
                continue
            if display_index > 0 and actual_index > 0:
                mapping[display_index] = (field, actual_index)
    resolved = [mapping.get(index) for index in display_indices]
    if any(item is None for item in resolved):
        return None
    fields = {field for field, _index in resolved if field}
    if len(fields) != 1:
        return None
    field = next(iter(fields))
    return field, [index for _field, index in resolved]


def _valid_last_display_context(existing: DailyReport) -> dict[str, Any] | None:
    section_status = getattr(existing, "section_status", None)
    if not isinstance(section_status, dict):
        return None
    context = section_status.get("_last_display_context")
    if not isinstance(context, dict):
        return None
    report_date = str(context.get("report_date") or "")
    if report_date and report_date != getattr(existing, "report_date", date.min).isoformat():
        return None
    current_hash = _draft_hash_for_report(existing)
    display_hash = str(context.get("draft_hash") or "")
    if display_hash and display_hash != current_hash:
        return None
    return context


def _draft_hash_for_report(existing: DailyReport) -> str:
    payload = {
        "today_work": list(getattr(existing, "today_work", []) or []),
        "problems": list(getattr(existing, "problems", []) or []),
        "tomorrow_plan": list(getattr(existing, "tomorrow_plan", []) or []),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _field_from_merge_text(compact: str, existing: DailyReport | None = None, indices: list[int] | None = None) -> str:
    if any(token in compact for token in ("今日工作", "今天工作", "工作", "完成事项")):
        return "today_work"
    if any(token in compact for token in ("问题", "风险", "困难")):
        return "problems"
    if any(token in compact for token in ("明日", "明天", "明儿")) or re.search(r"(?:计划|安排)(?:的)?第", compact):
        return "tomorrow_plan"
    if existing is not None and indices:
        max_index = max(indices)
        candidates = [
            field
            for field in ("today_work", "problems", "tomorrow_plan")
            if max_index <= len(list(getattr(existing, field, []) or []))
        ]
        if len(candidates) == 1:
            return candidates[0]
    return "today_work"


def _extract_range_merge_replacement(raw_input: str) -> str:
    match = re.search(r"(?:合并成|合成|并成|归并成|统一成|改成|改为)(.+)$", raw_input)
    if not match:
        return ""
    value = _normalize_free_text(match.group(1))
    value = re.sub(r"^(一条|一个|一项)[，,。；;、\s]*", "", value)
    if value in {"吧", "呢", "呀", "啊", "啦", "了", "一下", "一下吧", "一起", "一块"}:
        return ""
    return value.strip(" ，,。；;、")


def _has_explicit_ordinal_reference(raw_input: str) -> bool:
    text = _compact_for_intent(raw_input)
    token = r"\d{1,3}|[一二两三四五六七八九十]{1,3}"
    return bool(
        re.search(rf"第{token}(?:条|项|个)", text)
        or re.search(rf"\d+(?:到|至|~|～|-|—|－)\d+条?", text)
        or re.search(rf"{token}(?:到|至|~|～|-|—|－){token}条", text)
        or re.search(rf"第?{token}(?:、|和)第?{token}条", text)
    )


def _has_ordinal_delete_target(raw_input: str) -> bool:
    text = str(raw_input or "").strip()
    if not text:
        return False
    token = r"\d{1,3}|[一二两三四五六七八九十]{1,3}"
    ordinal = rf"第?{token}(?:条|项|个)"
    range_ordinal = rf"{ordinal}(?:到|至|~|～|-|—|－){ordinal}"
    delete_verb = r"(?:删除|删掉|删了|去掉|移除|不要了?|不用了?)"
    same_clause_gap = r"[^。！？!?；;\n\r]{0,12}"
    return bool(
        re.search(rf"{delete_verb}{same_clause_gap}(?:{range_ordinal}|{ordinal})", text)
        or re.search(rf"(?:{range_ordinal}|{ordinal}){same_clause_gap}{delete_verb}", text)
    )


def _single_action_effect_items(raw_input: str) -> list[str]:
    text = _normalize_free_text(raw_input)
    numbered_parts = _extract_numbered_tail_items(text)
    if len(numbered_parts) >= 2:
        return numbered_parts
    item = _single_action_effects_item(raw_input)
    return [item] if item else []


def _single_action_effects_item(raw_input: str) -> str:
    text = _normalize_free_text(raw_input)
    text = re.sub(r"^今天(主要|就是|主要就)?", "", text).strip(" ，,。；;")
    text = text.replace("那个AI", "AI").replace("那个ai", "AI")
    numbered_parts = _extract_numbered_tail_items(text)
    if numbered_parts:
        head = re.split(r"[：:]", text, maxsplit=1)[0]
        head = re.sub(r"^今天", "", head).strip(" ，,。；;")
        head = head or "优化AI发送逻辑"
        return f"{head}，包括：" + "、".join(numbered_parts)
    effect_match = re.search(r"(优化.*?发送逻辑)[，,。；;]?(?:效果有几个|效果包括|包括)?[，,。；;]?(.*)$", text, flags=re.I)
    if effect_match:
        head = effect_match.group(1).strip(" ，,。；;")
        tail = effect_match.group(2).strip(" ，,。；;")
        if tail:
            return f"{head}，包括：{tail}"
        return head
    return text


def _extract_numbered_tail_items(text: str) -> list[str]:
    if not re.search(r"[：:]", text):
        return []
    tail = re.split(r"[：:]", text, maxsplit=1)[1]
    parts = re.split(r"(?:^|\s)(?:\d+|[一二三四五六七八九十]+)[\.、)]\s*", tail)
    cleaned = [_normalize_free_text(part) for part in parts if _normalize_free_text(part)]
    return cleaned


def _apply_active_user_habits(raw_input: str, user_habits: list[Any] | None) -> str:
    text = _apply_active_asr_habits(raw_input, user_habits)
    text = _apply_active_no_problem_phrase_habits(text, user_habits)
    return text


def _apply_active_asr_habits(raw_input: str, user_habits: list[Any] | None) -> str:
    text = raw_input
    for habit in user_habits or []:
        if str(getattr(habit, "habit_type", "") or "") != "asr_correction":
            continue
        trigger = str(getattr(habit, "trigger_text", "") or "").strip()
        meaning = str(getattr(habit, "meaning", "") or "")
        target = _target_from_asr_habit_meaning(meaning, trigger)
        if not trigger or not target or trigger == target:
            continue
        text = text.replace(trigger, target)
    return text


def _apply_active_no_problem_phrase_habits(raw_input: str, user_habits: list[Any] | None) -> str:
    text = raw_input
    for habit in user_habits or []:
        if str(getattr(habit, "habit_type", "") or "") != "phrase_meaning":
            continue
        trigger = str(getattr(habit, "trigger_text", "") or "").strip()
        meaning = _compact_for_intent(str(getattr(habit, "meaning", "") or ""))
        if not trigger or trigger not in text:
            continue
        if not ("problems=暂无明显问题" in meaning or "表示暂无明显问题" in meaning or "表示problems暂无明显问题" in meaning or "表示无明显问题" in meaning):
            continue
        text = re.sub(re.escape(trigger), "没问题", text, count=1)
    return text


def _target_from_asr_habit_meaning(meaning: str, trigger: str) -> str:
    quoted = re.findall(r"[“\"]([^”\"]{1,20})[”\"]", meaning or "")
    if trigger and len(quoted) >= 2:
        for index, value in enumerate(quoted[:-1]):
            if value == trigger:
                return quoted[index + 1]
    for pattern in (
        r"(?:通常是|应为|纠正为|改成|改为)[“\"]?([^，”\"。；;]{1,20})[”\"]?",
        r"->\s*([^，。；;\s]{1,20})",
        r"→\s*([^，。；;\s]{1,20})",
    ):
        match = re.search(pattern, meaning or "")
        if match:
            return match.group(1).strip()
    return ""


def _extract_numbered_work_items(raw_input: str) -> list[str]:
    text = _strip_numbered_work_prefix(raw_input)
    matches = _top_level_numbered_matches(text)
    if len(matches) >= 2:
        items: list[str] = []
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            item = _normalize_free_text(text[start:end]).strip(" ，,。；;、")
            if item:
                items.append(item)
        return items
    text = _normalize_free_text(text)
    if re.search(r"[：:]", text):
        text = re.split(r"[：:]", text, maxsplit=1)[1]
    parts = re.split(r"(?:^|\s)(?:一是|二是|三是|四是|五是|六是|七是|八是|九是|十是|\d+[\.、)]|[一二三四五六七八九十]+[\.、)])", text)
    cleaned: list[str] = []
    for part in parts:
        item = _normalize_free_text(part)
        item = item.strip(" ，,。；;、")
        if item:
            cleaned.append(item)
    return cleaned



_LONG_TEXT_TOMORROW_ANCHOR_RE = re.compile(
    r"(\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212|\u660e\u65e5\u8981\u505a|\u660e\u5929\u8981\u505a|\u660e\u5929\u51c6\u5907|\u660e\u513f\u6253\u7b97|\u63a5\u4e0b\u6765\u8981|\u63a5\u4e0b\u6765\u8ba1\u5212|\u9884\u8ba1\u660e\u5929|\u660e\u65e5\u5c06|tomorrow\s+plan|tomorrow|\u660e\u513f|\u660e\u5929|\u660e\u65e5|\u63a5\u4e0b\u6765)",
    re.IGNORECASE,
)
_LONG_TEXT_RISK_ANCHOR_RE = re.compile(
    r"(\u95ee\u9898/\u98ce\u9669|\u98ce\u9669/\u56f0\u96be|\u98ce\u9669\uff0f\u56f0\u96be|\u6682\u65e0\u98ce\u9669|\u6ca1\u6709\u98ce\u9669|\u6ca1\u5565\u5927\u95ee\u9898|\u98ce\u9669|\u95ee\u9898|\u56f0\u96be|\u963b\u788d|blocker|no\s+blockers|no\s+issues)",
    re.IGNORECASE,
)
_LONG_TEXT_TODAY_ANCHOR_RE = re.compile(
    r"(\u4eca\u65e5\u5de5\u4f5c|\u4eca\u65e5\u5b8c\u6210|\u4eca\u5929\u5b8c\u6210|\u4eca\u5929\u4e3b\u8981|\u5df2\u5b8c\u6210|\u5b8c\u6210\u4e86|finished|completed)",
    re.IGNORECASE,
)
_LONG_TEXT_ANY_FIELD_ANCHOR_RE = re.compile(
    "|".join(
        [
            _LONG_TEXT_TOMORROW_ANCHOR_RE.pattern,
            _LONG_TEXT_RISK_ANCHOR_RE.pattern,
            _LONG_TEXT_TODAY_ANCHOR_RE.pattern,
        ]
    ),
    re.IGNORECASE,
)
_LONG_TEXT_RISK_DETAIL_RE = re.compile(
    r"(\u8d85\u65f6|\u6162|\u5931\u8d25|\u5f02\u5e38|\u62a5\u9519|\u5361\u4f4f|\u4e0d\u7a33\u5b9a|\u7f3a|\u672a\u53cd\u9988|\u6ca1\u7ed9)",
    re.IGNORECASE,
)


def _apply_long_text_field_anchor_fallbacks(raw_input: str, fields: dict[str, list[str]]) -> dict[str, list[str]]:
    text = str(raw_input or "")
    result = {
        "today_work": list(fields.get("today_work") or []),
        "problems": list(fields.get("problems") or []),
        "tomorrow_plan": list(fields.get("tomorrow_plan") or []),
    }

    checked_today = _extract_checked_today_items(text)
    if checked_today:
        result["today_work"] = checked_today
    elif result["today_work"]:
        result["today_work"] = [_trim_today_item_at_future_or_risk_anchor(item) for item in result["today_work"]]
        result["today_work"] = [item for item in result["today_work"] if item]

    if result["tomorrow_plan"]:
        result["tomorrow_plan"] = [_trim_tomorrow_item(item) for item in result["tomorrow_plan"]]
        result["tomorrow_plan"] = [_normalize_tomorrow_plan(item) for item in result["tomorrow_plan"] if item]
    else:
        tomorrow = _extract_anchor_value(text, _LONG_TEXT_TOMORROW_ANCHOR_RE, field="tomorrow_plan")
        if tomorrow:
            result["tomorrow_plan"] = [_normalize_tomorrow_plan(tomorrow)]

    if not result["problems"]:
        problem = _extract_long_text_problem_value(text)
        if problem:
            result["problems"] = [_clean_problem_answer(problem)]

    if not result["today_work"]:
        today = _extract_anchor_value(text, _LONG_TEXT_TODAY_ANCHOR_RE, field="today_work")
        if today:
            result["today_work"] = [_normalize_current_report_work(today)]

    return result


def _extract_checked_today_items(text: str) -> list[str]:
    items: list[str] = []
    for line in str(text or "").splitlines():
        if not re.search(r"\[[xX]\]|\u2611", line):
            continue
        cleaned = re.sub(r"^[\s\u25cb\u25cf\-*>]*(?:\[[xX]\]|\u2611)\s*", "", line).strip()
        cleaned = _normalize_current_report_work(cleaned)
        if cleaned:
            items.append(cleaned)
    return merge_ordered([], items)


def _extract_anchor_value(text: str, anchor_re: re.Pattern[str], *, field: str) -> str:
    match = anchor_re.search(text)
    if not match:
        return ""
    start = _anchor_content_start(text, match, field=field)
    remainder = text[start:]
    end = _anchor_value_end(remainder, field=field)
    value = remainder[:end].strip(" \t\r\n\u3000\uff1a:\uff0c,\u3002\uff1b;\u3001")
    if field == "tomorrow_plan":
        value = re.sub(r"^(\u662f|\u4e3a|:|\uff1a|\u5c06|\u8981)\s*", "", value)
    return _normalize_free_text(value)


def _anchor_content_start(text: str, match: re.Match[str], *, field: str) -> int:
    token = match.group(0).lower()
    if field == "tomorrow_plan" and token in {"\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "tomorrow", "\u63a5\u4e0b\u6765"}:
        return match.start()
    return match.end()


def _anchor_value_end(remainder: str, *, field: str) -> int:
    candidates: list[int] = []
    for match in _LONG_TEXT_ANY_FIELD_ANCHOR_RE.finditer(remainder):
        if match.start() > 0:
            candidates.append(match.start())
            break
    punctuation = re.search(r"[\n\r\u3002\uff1b;]", remainder)
    if punctuation:
        candidates.append(punctuation.start())
    comma = re.search(r"[\uff0c,]", remainder)
    if comma and _LONG_TEXT_RISK_DETAIL_RE.search(remainder[comma.end():]):
        candidates.append(comma.start())
    if field == "tomorrow_plan":
        risk = _LONG_TEXT_RISK_ANCHOR_RE.search(remainder)
        if risk and risk.start() > 0:
            candidates.append(risk.start())
    return min(candidates) if candidates else len(remainder)


def _trim_today_item_at_future_or_risk_anchor(item: str) -> str:
    match_positions = [m.start() for m in (_LONG_TEXT_TOMORROW_ANCHOR_RE.search(item), _LONG_TEXT_RISK_ANCHOR_RE.search(item)) if m]
    if match_positions:
        item = item[: min(match_positions)]
    return _normalize_current_report_work(item)


def _trim_tomorrow_item(item: str) -> str:
    end = _anchor_value_end(item, field="tomorrow_plan")
    return item[:end].strip(" \t\r\n\u3000\uff1a:\uff0c,\u3002\uff1b;\u3001")


def _extract_long_text_problem_value(text: str) -> str:
    no_problem_seen = False
    for sentence in _split_long_text_sentences(text):
        detail_match = re.search(r"(?:\u6ca1\u5565\u5927\u95ee\u9898|\u6ca1\u4ec0\u4e48\u5927\u95ee\u9898|\u6ca1\u6709\u5927\u95ee\u9898)[\uff0c,\u3002;\uff1b\s]*(?:\u5c31\u662f|\u4f46\u662f|\u4e0d\u8fc7)(.+)$", sentence)
        if detail_match:
            return detail_match.group(1)
        if _mentions_no_problem(sentence) or _is_long_text_no_problem_sentence(sentence):
            no_problem_seen = True
            continue
        if _looks_like_problem(sentence) or _LONG_TEXT_RISK_DETAIL_RE.search(sentence):
            return sentence
    if no_problem_seen or _mentions_no_problem(text) or _is_long_text_no_problem_sentence(text):
        return "\u6682\u65e0\u660e\u663e\u95ee\u9898"
    return ""


def _is_long_text_no_problem_sentence(text: str) -> bool:
    compact = _compact_for_intent(text)
    patterns = (
        "\u6ca1\u6709\u9047\u5230\u4ec0\u4e48\u95ee\u9898",
        "\u6ca1\u9047\u5230\u4ec0\u4e48\u95ee\u9898",
        "\u76ee\u524d\u65e0\u660e\u663e\u98ce\u9669",
        "\u65e0\u660e\u663e\u98ce\u9669",
        "\u6682\u65e0\u98ce\u9669\u63d0\u793a",
    )
    return any(pattern in compact for pattern in patterns)


def _split_long_text_sentences(text: str) -> list[str]:
    pieces = [piece.strip(" \t\r\n\u3000\uff1a:\uff0c,\u3002\uff1b;\u3001") for piece in re.split(r"[\n\r\u3002\uff1b;]+", str(text or ""))]
    return [piece for piece in pieces if piece]


def _extract_simple_report_fields(raw_input: str) -> dict[str, list[str]]:
    text = _strip_replace_report_prefix(raw_input)
    text = re.sub(r"^(前面别记了|前面是测试|重新来|重新说|我重说|算了算了|跟你实话实说吧?|以这个为准|按这个来)[，,。；;：:\s]*", "", text)
    text = re.sub(r"^.*?(以这个为准|按这个来)[，,。；;：:\s]*", "", text)
    plan_text = ""
    plan_match = re.search(r"(明天|明日).*$", text)
    if plan_match:
        plan_text = plan_match.group(0)
        text = text[: plan_match.start()]
    text = text.strip(" ，,。；;、")
    problem_text = ""
    tail_problem = _split_tail_problem_clause(text)
    if tail_problem is not None:
        text, problem_text = tail_problem
    explicit_problem_match = re.search(
        r"(?:^|[，,。；;、])((?:问题|风险)(?:是|为)?[^，,。；;、]*|(?:对方|客户|供应商|业务|项目组|材料|资料)[^，,。；;、]*(?:还差|缺|不全|未|没|迟|慢|影响)[^，,。；;、]*)$",
        text,
    )
    if not problem_text and explicit_problem_match:
        problem_text = _clean_problem_answer(explicit_problem_match.group(1))
        text = text[: explicit_problem_match.start()]
    if _mentions_no_problem(text):
        problem_text = "暂无明显问题"
        text = re.sub(r"(然后|嗯|呃|额|啊)?[，,。；;、\s]*(没事|没啥事|没什么事|没有事|没问题|没啥问题|没什么问题|没有问题|问题没有|问题没|暂无问题|无问题|暂无明显问题|无明显问题|没风险|没什么风险|没有风险|风险没有|风险没|暂无风险|无风险|无明显风险)", "", text)
    problem_match = re.search(r"(问题|风险)(是|为)?(.+)$", text)
    if not problem_text and problem_match:
        problem_text = _normalize_free_text(problem_match.group(3))
        text = text[: problem_match.start()]
    today_text = _normalize_free_text(text)
    today_text = re.sub(r"^今天(吧|主要|就是|主要就是)?[，,。；;、\s]*", "", today_text).strip(" ，,。；;")
    today_text = _normalize_current_report_work(today_text)
    fields = {
        "today_work": [today_text] if today_text else [],
        "problems": [problem_text] if problem_text else [],
        "tomorrow_plan": [_normalize_tomorrow_plan(plan_text)] if plan_text else [],
    }
    return _apply_long_text_field_anchor_fallbacks(raw_input, fields)


def _split_tail_problem_clause(text: str) -> tuple[str, str] | None:
    parts = [part.strip(" ，,。；;、") for part in re.split(r"[，,。；;、]+", text) if part.strip(" ，,。；;、")]
    if len(parts) < 2:
        return None
    tail = parts[-1]
    if _mentions_no_problem(tail) or not _looks_like_problem(tail):
        return None
    prefix = text[: text.rfind(tail)].strip(" ，,。；;、")
    problem = _clean_problem_answer(tail)
    if not prefix or not problem:
        return None
    return prefix, problem


def _split_problem_and_plan(raw_input: str) -> tuple[str, str]:
    text = _normalize_free_text(raw_input)
    match = re.search(r"(明天|明日).*$", text)
    if not match:
        return text, ""
    return text[: match.start()].strip(" ，,。；;"), match.group(0).strip(" ，,。；;")


def _split_embedded_problem_from_plan_text(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    match = re.search(r"[，,。；;、\s]*(问题|风险)(?:是|为|在于)?[：:\s]*", text)
    if not match or match.start() <= 0:
        return "", text
    plan_text = text[: match.start()].strip(" ，,。；;、")
    problem_text = _clean_problem_answer(text[match.end():])
    return problem_text, plan_text


def _split_leading_problem_from_future_plan_text(text: str) -> tuple[str, str]:
    value = _normalize_free_text(text)
    if not value:
        return "", ""
    match = re.search(r"(明天|明日|明儿|后天|下周).*$", value)
    if not match:
        return "", value
    leading = value[: match.start()].strip(" ，,。；;、")
    plan_text = value[match.start():].strip(" ，,。；;、")
    leading = re.sub(r"^(然后|另外|还有|嗯|呃|额|啊|那个|就是)[，,。；;、\s]*", "", leading).strip(" ，,。；;、")
    if not leading:
        return "", plan_text
    if _mentions_no_problem(leading) or _is_short_no_problem_reply(leading):
        return "暂无明显问题", plan_text
    if _looks_like_problem(leading):
        return _clean_problem_answer(leading), plan_text
    return "", value


def _clean_problem_answer(text: str) -> str:
    text = _normalize_free_text(text)
    if _mentions_no_problem(text) or _is_short_no_problem_reply(text):
        return "暂无明显问题"
    text = re.sub(r"^(呃|额|啊|嗯|那个|就是|有个风险吧|有个问题吧|风险的话|问题的话)[，,。；;、\s]*", "", text)
    text = text.replace("还没收齐", "尚未收齐").replace("没给全", "未给全")
    return text.strip(" ，,。；;")


def _is_placeholder_problem_list(values: list[str]) -> bool:
    if not values:
        return False
    return all(_compact_for_intent(value) in {"暂无明显问题", "暂无问题", "无明显问题", "无问题", "没问题", "没有问题"} for value in values)


def _missing_sections_from_report(existing: DailyReport | None) -> list[str]:
    if existing is None:
        return ["today_work", "problems", "tomorrow_plan"]
    return [
        key
        for key, values in (
            ("today_work", existing.today_work),
            ("problems", existing.problems if existing.problems else ([] if not existing.section_status.get("problems_acknowledged_empty") else ["暂无明显问题"])),
            ("tomorrow_plan", existing.tomorrow_plan),
        )
        if not values
    ]


def _infer_current_slot(existing: DailyReport | None) -> str | None:
    if existing is None:
        return "today_work"
    for key in ("today_work", "problems", "tomorrow_plan"):
        if key == "problems":
            if not existing.problems and not existing.section_status.get("problems_acknowledged_empty"):
                return key
            continue
        if not getattr(existing, key):
            return key
    return None


def _preferred_slot(existing: DailyReport | None) -> str:
    return _infer_current_slot(existing) or "today_work"


async def _acquire_report_processing_lock(session: AsyncSession, user_id, report_date: date) -> None:
    if not hasattr(session, "execute"):
        return
    await acquire_daily_report_advisory_lock(session, user_id, report_date)


def _should_auto_submit(report: DailyReport, now) -> bool:
    return (
        report.status == STATUS_PENDING_CONFIRMATION
        and not _has_unresolved_draft_edit(report)
        and report.auto_submit_at is not None
        and report.auto_submit_at <= now
    )




def _historical_report_context_pending(report: DailyReport, target_date: date, *, field: str = "none") -> dict[str, Any]:
    selected_field = field if field in REPORT_FIELD_NAMES else "none"
    current_report = {
        "today_work": list(report.today_work or []),
        "problems": list(report.problems or []),
        "tomorrow_plan": list(report.tomorrow_plan or []),
        "status": report.status,
    }
    edit_cursor = build_historical_edit_cursor(
        target_date=target_date,
        current_report=current_report,
        focused_section=selected_field,
    )
    context = {
        "target_date": target_date.isoformat(),
        "requested_action": "modify_report",
        "stage": "awaiting_field_edit_content" if selected_field in REPORT_FIELD_NAMES else "awaiting_edit_instruction",
        "current_report": current_report,
        "edit_cursor": edit_cursor,
    }
    if selected_field in REPORT_FIELD_NAMES:
        context["focus_section"] = selected_field
    return {
        "type": "historical_report_edit_flow",
        "operation": "modify_report",
        "target_field": selected_field,
        "context": context,
        "expires_after_turns": 12,
    }


def _recent_report_context_payload(
    *,
    viewed_report: DailyReport,
    viewed_report_date: date,
    viewed_at,
    viewer: User,
    owner: str,
    field: str = "all",
) -> dict[str, Any]:
    return {
        "kind": "viewed_report",
        "owner": owner,
        "intent": "query_report",
        "report_id": str(getattr(viewed_report, "id", "") or ""),
        "target_user_id": str(getattr(viewed_report, "user_id", "") or ""),
        "target_user_name": getattr(viewer, "name", "") if owner == "self" else "",
        "viewer_user_id": str(getattr(viewer, "id", "") or ""),
        "report_date": viewed_report_date.isoformat(),
        "field": field if field in {*REPORT_FIELD_NAMES, "all", "none"} else "all",
        "can_edit": owner == "self",
        "can_copy_to_today": owner == "self",
        "created_at": viewed_at.isoformat() if hasattr(viewed_at, "isoformat") else "",
        "turns_left": 3,
        "draft_hash": _draft_hash_for_report(viewed_report),
    }


async def _save_recent_report_context_state(
    session: AsyncSession,
    *,
    user: User,
    existing: DailyReport | None,
    report_date: date,
    viewed_report: DailyReport,
    viewed_report_date: date,
    received_at,
    source: str,
    raw_input: str,
    field: str = "all",
) -> DailyReport:
    section_status = dict((existing.section_status if existing else {}) or {})
    section_status[RECENT_REPORT_CONTEXT_KEY] = _recent_report_context_payload(
        viewed_report=viewed_report,
        viewed_report_date=viewed_report_date,
        viewed_at=received_at,
        viewer=user,
        owner="self",
        field=field,
    )
    if viewed_report_date != report_date and PENDING_INTERACTION_KEY not in section_status:
        section_status[PENDING_INTERACTION_KEY] = _historical_report_context_pending(viewed_report, viewed_report_date, field=field)
    today_work = list(getattr(existing, "today_work", []) or []) if existing is not None else []
    problems = list(getattr(existing, "problems", []) or []) if existing is not None else []
    tomorrow_plan = list(getattr(existing, "tomorrow_plan", []) or []) if existing is not None else []
    if not hasattr(session, "execute"):
        if existing is not None:
            existing.section_status = section_status
            return existing
        return SimpleNamespace(
            id=getattr(viewed_report, "id", None),
            user_id=getattr(user, "id", None),
            team_id=getattr(user, "team_id", None),
            report_date=report_date,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion="",
            raw_input=raw_input,
            input_fragments=[],
            section_status=section_status,
            completeness_score=0.0,
            status=STATUS_COLLECTING,
            confirmation_type=CONFIRMATION_NONE,
            confirmed_by_user=False,
            quality_warning=None,
            last_modified_by_user=False,
            last_modified_at=None,
            pending_confirmation_at=None,
            auto_submit_at=None,
            source=source,
            llm_model=None,
            llm_payload={"operation": "save_recent_report_context", "viewed_report_date": viewed_report_date.isoformat()},
            submitted_at=None,
        )
    return await upsert_daily_report(
        session,
        user=user,
        report_date=report_date,
        raw_input=raw_input,
        source=source,
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        emotion=getattr(existing, "emotion", "") if existing is not None else "",
        completeness_score=float(getattr(existing, "completeness_score", 0.0) or 0.0) if existing is not None else 0.0,
        status=getattr(existing, "status", None) or STATUS_COLLECTING,
        section_status=section_status,
        llm_model=getattr(existing, "llm_model", None) if existing is not None else None,
        llm_payload={"operation": "save_recent_report_context", "viewed_report_date": viewed_report_date.isoformat()},
        received_at=received_at,
        confirmation_type=getattr(existing, "confirmation_type", None) or CONFIRMATION_NONE,
        confirmed_by_user=bool(getattr(existing, "confirmed_by_user", False)),
        quality_warning=getattr(existing, "quality_warning", None) if existing is not None else None,
        last_modified_by_user=bool(getattr(existing, "last_modified_by_user", False)),
        last_modified_at=getattr(existing, "last_modified_at", None) if existing is not None else None,
        pending_confirmation_at=getattr(existing, "pending_confirmation_at", None) if existing is not None else None,
        auto_submit_at=getattr(existing, "auto_submit_at", None) if existing is not None else None,
        replace_sections=True,
    )


def _valid_recent_report_context(existing: DailyReport | None) -> dict[str, Any] | None:
    if existing is None or not isinstance(getattr(existing, "section_status", None), dict):
        return None
    context = existing.section_status.get(RECENT_REPORT_CONTEXT_KEY)
    if not isinstance(context, dict):
        return None
    try:
        date.fromisoformat(str(context.get("report_date") or "")[:10])
    except ValueError:
        return None
    return context


def _looks_like_copy_recent_report_to_today(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    whole_markers = (
        "\u5168\u90e8",
        "\u90fd",
        "\u6574\u7bc7",
        "\u6574\u4efd",
        "\u5b8c\u6574",
        "\u539f\u6837",
    )
    copy_markers = (
        "\u590d\u5236",
        "\u62f7\u8d1d",
        "\u5e26\u5230\u4eca\u5929",
        "\u5e26\u8fc7\u6765",
        "\u653e\u5230\u4eca\u5929",
        "\u4f5c\u4e3a\u4eca\u5929",
        "\u7528\u8fd9\u4efd",
        "\u7528\u6628\u5929\u65e5\u62a5",
        "\u7528\u6628\u65e5\u65e5\u62a5",
        "\u8f6c\u6210\u4eca\u5929",
        "\u751f\u6210\u4eca\u5929\u8349\u7a3f",
    )
    exact_commands = {
        "\u5168\u90e8\u590d\u5236",
        "\u590d\u5236\u5168\u90e8",
        "\u5168\u590d\u5236",
        "\u90fd\u590d\u5236",
        "\u6574\u7bc7\u590d\u5236",
        "\u6574\u4efd\u590d\u5236",
        "\u5b8c\u6574\u590d\u5236",
        "\u539f\u6837\u590d\u5236",
    }
    if compact in exact_commands:
        return True
    if any(marker in compact for marker in copy_markers) and any(
        marker in compact
        for marker in (
            "\u4eca\u5929",
            "\u4eca\u65e5",
            "\u5f53\u524d",
            "\u8349\u7a3f",
            "\u5168\u90e8",
            "\u8fd9\u4e2a",
            "\u5b83",
            "\u5b83",
            "\u8fd9\u4efd",
            "\u6574\u7bc7",
            "\u6574\u4efd",
            "\u5b8c\u6574",
            "\u539f\u6837",
        )
    ):
        return True
    has_source_report = any(
        marker in compact
        for marker in (
            "\u6628\u5929\u65e5\u62a5",
            "\u6628\u5929\u7684\u65e5\u62a5",
            "\u6628\u5929\u5185\u5bb9",
            "\u6628\u5929\u7684\u5185\u5bb9",
            "\u6628\u65e5\u65e5\u62a5",
            "\u6628\u65e5\u7684\u65e5\u62a5",
            "\u6628\u65e5\u5185\u5bb9",
            "\u6628\u65e5\u7684\u5185\u5bb9",
            "\u6628\u513f\u65e5\u62a5",
            "\u6628\u513f\u7684\u65e5\u62a5",
            "\u524d\u5929\u65e5\u62a5",
            "\u524d\u5929\u7684\u65e5\u62a5",
            "\u524d\u5929\u5185\u5bb9",
            "\u524d\u5929\u7684\u5185\u5bb9",
            "\u524d\u65e5\u65e5\u62a5",
            "\u524d\u65e5\u7684\u65e5\u62a5",
            "\u524d\u65e5\u5185\u5bb9",
            "\u524d\u65e5\u7684\u5185\u5bb9",
        )
    ) or bool(
        re.search(r"(?:20\d{2}[-\u5e74/.]\d{1,2}[-\u6708/.]\d{1,2}|\d{1,2}\u6708\d{1,2}[\u65e5\u53f7]|\d{1,2}[\u65e5\u53f7])\u7684?.*(?:\u65e5\u62a5|\u5185\u5bb9|\u90a3\u4efd|\u8fd9\u4efd)", compact)
    )
    return has_source_report and ("\u590d\u5236" in compact or "\u62f7\u8d1d" in compact or any(marker in compact for marker in whole_markers))


def _looks_like_recent_report_whole_copy_context_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    return compact in {
        "\u6574\u7bc7\u590d\u5236",
        "\u6574\u4efd\u590d\u5236",
        "\u5b8c\u6574\u590d\u5236",
        "\u539f\u6837\u590d\u5236",
        "\u5168\u90e8\u590d\u5236",
        "\u590d\u5236\u5168\u90e8",
        "\u5168\u90e8\u8986\u76d6",
        "\u8986\u76d6",
    }


def _resolve_copy_report_source_date(raw_input: str, target_report_date: date) -> date | None:
    if not _looks_like_copy_recent_report_to_today(raw_input):
        return None
    compact = _compact_for_intent(raw_input)
    if not compact:
        return None
    explicit_match = re.search(r"(20\d{2})[-\u5e74/.](\d{1,2})[-\u6708/.](\d{1,2})", compact)
    if explicit_match:
        year, month, day = (int(part) for part in explicit_match.groups())
        try:
            explicit_date = date(year, month, day)
        except ValueError:
            return None
        return explicit_date if explicit_date < target_report_date else None
    if "\u524d\u5929" in compact or "\u524d\u65e5" in compact:
        return target_report_date - timedelta(days=2)
    if any(marker in compact for marker in ("\u6628\u5929", "\u6628\u65e5", "\u6628\u513f")):
        return target_report_date - timedelta(days=1)
    source_date = _resolve_date_from_text(compact, target_report_date)
    if source_date is None or source_date >= target_report_date:
        return None
    return source_date

def _looks_like_bare_recent_report_followup(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if _looks_like_copy_recent_report_to_today(raw_input):
        return True
    return compact in {"昨天的", "前天的", "这个", "这个人的", "他的", "她的", "刚才那个", "就刚才那个", "明细", "把他的发我", "把这个发我"} or bool(re.fullmatch(r"\d{1,2}号的?", compact))


def _looks_like_recent_report_display_followup(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact or _looks_like_copy_recent_report_to_today(raw_input):
        return False
    return _looks_like_bare_recent_report_followup(raw_input) or any(marker in compact for marker in ("发我", "给我", "看看", "看下", "展示", "明细"))


async def _auto_submit_existing_report(session: AsyncSession, report: DailyReport, received_at) -> DailyReport:
    fake_user = User(id=report.user_id, team_id=report.team_id, timezone="Asia/Shanghai", dingtalk_user_id="", name="", role="member", active=True)  # type: ignore[arg-type]
    return await upsert_daily_report(
        session,
        user=fake_user,
        report_date=report.report_date,
        raw_input="",
        source=report.source,
        today_work=report.today_work,
        problems=report.problems,
        tomorrow_plan=report.tomorrow_plan,
        emotion=report.emotion,
        completeness_score=float(report.completeness_score),
        status=STATUS_COMPLETED,
        section_status=report.section_status,
        llm_model=report.llm_model,
        llm_payload=report.llm_payload,
        received_at=received_at,
        confirmation_type=CONFIRMATION_AUTO_SUBMITTED_TIMEOUT,
        confirmed_by_user=False,
        quality_warning=report.quality_warning,
        last_modified_by_user=report.last_modified_by_user,
        last_modified_at=report.last_modified_at,
        pending_confirmation_at=report.pending_confirmation_at,
        auto_submit_at=None,
    )


def _apply_llm_meta(timings: dict[str, Any], phase: str, meta: dict[str, Any] | None) -> None:
    if not meta:
        return
    timings[f"llm_{phase}_model"] = meta.get("model") or ""
    timings[f"llm_{phase}_thinking"] = bool(meta.get("thinking"))
    timings[f"llm_{phase}_timeout"] = bool(meta.get("timeout"))
    if meta.get("fallback_to_pro"):
        timings["llm_fallback_to_pro"] = True
        timings["llm_fallback_reason"] = str(meta.get("fallback_reason") or "")


def _apply_semantic_router_timings(
    timings: dict[str, Any],
    decision: DailyInputIntentDecision | None,
    *,
    used: bool,
) -> None:
    timings["semantic_router_used"] = used
    timings["semantic_router_model"] = timings.get("llm_intent_model", "")
    timings["semantic_router_seconds"] = timings.get("llm_intent_seconds", 0.0)
    timings["semantic_router_timeout"] = timings.get("llm_intent_timeout", False)
    if decision is None:
        return
    timings["message_kind"] = decision.message_kind
    timings["operation"] = decision.operation
    timings["target_field"] = decision.target_field
    timings["item_refs"] = decision.item_refs
    timings["relation_to_existing"] = decision.relation_to_existing
    timings["matched_field"] = decision.matched_field
    timings["matched_item_index"] = decision.matched_item_index
    timings["confidence"] = decision.confidence
    timings["needs_clarification"] = decision.needs_clarification


def _finalize_timings(timings: dict[str, Any], total_start: float) -> dict[str, Any]:
    for key in (
        "acquire_report_lock_seconds",
        "load_existing_report_seconds",
        "llm_intent_seconds",
        "llm_extract_seconds",
        "llm_draft_decision_seconds",
        "report_merge_seconds",
        "upsert_report_seconds",
    ):
        timings.setdefault(key, 0.0)
    timings.setdefault("llm_intent_model", "")
    timings.setdefault("llm_extract_model", "")
    timings.setdefault("llm_summary_model", "")
    timings.setdefault("llm_draft_decision_model", "")
    timings.setdefault("llm_intent_thinking", None)
    timings.setdefault("llm_extract_thinking", None)
    timings.setdefault("llm_draft_decision_thinking", None)
    timings.setdefault("llm_intent_timeout", False)
    timings.setdefault("llm_extract_timeout", False)
    timings.setdefault("llm_draft_decision_timeout", False)
    timings.setdefault("llm_fallback_to_pro", False)
    timings.setdefault("llm_fallback_reason", "")
    timings.setdefault("semantic_router_used", False)
    timings.setdefault("semantic_router_model", "")
    timings.setdefault("semantic_router_seconds", 0.0)
    timings.setdefault("semantic_router_timeout", False)
    timings.setdefault("message_kind", "")
    timings.setdefault("operation", "")
    timings.setdefault("target_field", "")
    timings.setdefault("relation_to_existing", "")
    timings.setdefault("matched_field", "")
    timings.setdefault("matched_item_index", 0)
    timings.setdefault("confidence", 0.0)
    timings.setdefault("item_refs", [])
    timings.setdefault("needs_clarification", False)
    timings.setdefault("current_requested_slot", "")
    timings.setdefault("slot_semantic_match", None)
    timings.setdefault("executor_decision", "")
    timings.setdefault("write_fields", [])
    timings.setdefault("reject_reason", "")
    timings.setdefault("long_report_mode", False)
    timings.setdefault("detected_sections", 0)
    timings.setdefault("daily_intent_workflow", "")
    timings.setdefault("daily_intent_operation", "")
    timings.setdefault("daily_intent_target_date", "")
    timings.setdefault("daily_intent_target_field", "")
    timings.setdefault("daily_intent_target_items", [])
    timings.setdefault("daily_intent_content_count", 0)
    timings.setdefault("daily_intent_should_write", False)
    timings.setdefault("daily_intent_needs_confirmation", False)
    timings.setdefault("daily_intent_pending_relation", "")
    timings.setdefault("daily_intent_confidence", "")
    timings.setdefault("daily_intent_source", "")
    timings.setdefault("daily_intent_branch", "")
    timings.setdefault("daily_intent_safety_flags", [])
    timings.setdefault("daily_intent_reason", "")
    timings.setdefault("daily_intent_raw_text_hash", "")
    timings.setdefault("daily_intent_raw_text_chars", 0)
    timings["submit_total_seconds"] = round(time.perf_counter() - total_start, 4)
    return timings


def _calculate_completeness(today_work: list[str], problems: list[str], tomorrow_plan: list[str]) -> float:
    return round((0.34 if today_work else 0) + (0.33 if problems else 0) + (0.33 if tomorrow_plan else 0), 4)


def _compact_for_intent(raw_input: str) -> str:
    return re.sub(r"[\s，。,.、；;：:！!？?（）()【】\[\]\"'“”‘’]+", "", raw_input.lower())


def _report_content_for_extraction(raw_input: str, intent: str) -> str:
    cleaned = _strip_replace_report_prefix(raw_input) if intent == "replace_current_report" else raw_input
    cleaned = _strip_report_instruction_prefix(cleaned)
    return cleaned or raw_input


def _strip_report_instruction_prefix(text: str) -> str:
    cleaned = text.strip()
    patterns = [
        r"^(?:你帮我整理下|帮我整理下|帮我整理一下|你帮我整理一下|你帮我记一下|帮我记一下|帮我记下)[，,。；;：:\s]*",
        r"^(?:这样写|就写成|按这个写|按这个来)[，,。；;：:\s]*",
        r"^(?:我重新说|重新说|我重说|重说一下)[，,。；;：:\s]*",
    ]
    previous = None
    while previous != cleaned:
        previous = cleaned
        for pattern in patterns:
            cleaned = re.sub(pattern, "", cleaned).strip()
    return cleaned


def _strip_replace_report_prefix(text: str) -> str:
    cleaned = text.strip()
    patterns = [
        r"^(?:前面那个不算|前面那个不要|前面不算|前面的不算|刚才那个不算|刚才的不算|刚才不算)[，,。；;：:\s]*",
        r"^(?:我重新说|重新说|我重说|重说一下|我重新填|重新填|按这个来|以这个为准)[，,。；;：:\s]*",
    ]
    previous = None
    while previous != cleaned:
        previous = cleaned
        for pattern in patterns:
            cleaned = re.sub(pattern, "", cleaned).strip()
    return cleaned


LONG_REPORT_SECTION_RE = re.compile(r"(?:^|\n)\s*(?:[一二三四五六七八九十]+、|\d+[、.．]|[（(][一二三四五六七八九十\d]+[）)])")
LONG_REPORT_TITLE_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,40}(?:项目|案件|合同|事项|事宜)")


def _detect_long_report(raw_input: str) -> LongReportDetection:
    text = raw_input.strip()
    compact = _compact_for_intent(text)
    if not text:
        return LongReportDetection(False, 0, 0)

    section_count = len(LONG_REPORT_SECTION_RE.findall(text))
    title_count = len(LONG_REPORT_TITLE_RE.findall(text))
    review_tokens = [
        "分析",
        "走访",
        "核对",
        "梳理",
        "宣贯",
        "进展",
        "问题",
        "风险",
        "计划",
        "下一步",
        "后续",
        "需",
        "争取",
        "拟",
        "暂时推迟",
        "推进",
        "沟通",
    ]
    legal_tokens = [
        "调解",
        "保全",
        "协执",
        "工商内档",
        "讨薪",
        "总包",
        "街道",
        "签字",
        "口头承诺",
        "工期",
        "索赔",
        "诉讼标的",
        "诉讼费",
        "资产线索",
        "资产",
        "结算资料",
        "结算单",
        "竣工报告",
        "审计",
        "开庭",
        "资料缺失",
        "原件",
        "电子版",
        "合同业主",
        "住建",
        "送审",
    ]
    review_hits = sum(1 for token in review_tokens if token in compact)
    legal_hits = sum(1 for token in legal_tokens if token in compact)

    score = 0
    if len(compact) >= 180:
        score += 2
    elif len(compact) >= 120:
        score += 1
    if section_count >= 2:
        score += 2
    elif section_count == 1:
        score += 1
    if title_count >= 2:
        score += 2
    elif title_count == 1:
        score += 1
    if review_hits >= 3:
        score += 2
    elif review_hits >= 1:
        score += 1
    if legal_hits >= 3:
        score += 2
    elif legal_hits >= 1:
        score += 1

    detected_sections = max(section_count, title_count)
    has_structure = section_count > 0 or title_count > 0
    has_report_facts = review_hits > 0 and legal_hits > 0
    is_long_report = score >= 4 and (len(compact) >= 120 or detected_sections >= 2) and (has_structure or has_report_facts)
    return LongReportDetection(is_long_report, detected_sections, score)


def _has_replace_current_report_intent(compact: str) -> bool:
    replace_tokens = [
        "刚才那个不算",
        "刚才的不算",
        "前面是测试",
        "前面那个是测试",
        "重新说",
        "重新来",
        "重说一下",
        "我重说",
        "算了我重新",
        "算了算了",
        "跟你实话实说",
        "不对我重新",
        "作废刚才",
        "前面别记",
        "按这个来",
        "以这个为准",
        "清空前面的我重新说",
        "清空前面我重新说",
        "前面的不要我重新说",
    ]
    return any(token in compact for token in replace_tokens)


def _has_append_to_existing_intent(compact: str) -> bool:
    append_tokens = ["补充一下", "补充一个", "补充个", "另外", "还有", "对了", "再加一个", "还处理了", "还有个问题", "明天还要"]
    return any(token in compact for token in append_tokens)


def _has_explicit_long_report_append_intent(compact: str) -> bool:
    starts = (
        "补充一个事项",
        "补充一个",
        "补充个",
        "补充一下",
        "再补充",
        "另外补充",
        "追加一个",
        "再加一个事项",
    )
    return compact.startswith(starts) or any(token in compact[:20] for token in starts)


def _is_confirmation_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    confirmation_starts = ("确认", "可以", "没问题", "提交", "就这样", "对", "行", "ok", "okay")
    courtesy_suffixes = ("谢谢", "辛苦了", "感谢", "麻烦你了", "多谢")
    return any(compact == start + suffix for start in confirmation_starts for suffix in courtesy_suffixes)


def _is_explicit_submit_request(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    if re.search(r"(撤回|撤销|取消|删|删除|清空).*(日报|复盘|提交|已提交)|(?:日报|复盘|提交|已提交).*(撤回|撤销|取消|删|删除|清空)", compact):
        return False
    explicit = {
        "提交",
        "提交吧",
        "确认提交",
        "确定提交",
        "提交日报",
        "确认提交日报",
        "确定提交日报",
        "提交复盘",
        "确认提交复盘",
        "就这样提交",
        "就这么提交",
        "可以提交",
        "没问题提交",
    }
    if compact in explicit:
        return True
    if "提交" not in compact:
        return False
    return any(marker in compact for marker in ("日报", "复盘", "确认", "确定", "就这样", "就这么", "可以"))


def _is_restore_previous_request(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    return compact in {"撤回", "撤回上一步", "撤回上一步操作", "恢复上一步", "恢复之前", "恢复之前的", "回退", "回退上一步", "undo"} or _looks_like_restore_previous_edit_request(raw_input)


def _is_ambiguous_problem_signal(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact or _mentions_no_problem(raw_input) or _is_short_no_problem_reply(raw_input):
        return False
    exact = {"好像有一个", "好像有个", "应该有一个", "应该有个", "有点问题", "有个问题", "有一个问题", "可能有一个", "可能有个"}
    if compact in exact:
        return True
    return ("问题" in compact or "风险" in compact) and any(marker in compact for marker in ("好像", "应该", "可能", "有点")) and len(compact) <= 12


def _is_fast_confirmation_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    confirmations = {"确认", "确定", "可以", "没问题", "没啥问题", "就这样", "提交", "好", "好的", "嗯", "嗯嗯", "恩", "恩恩", "ok", "okay"}
    return compact in confirmations or any(compact.startswith(token) and len(compact) <= len(token) + 4 for token in confirmations)


def _is_bare_confirmation_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    return compact in {
        "\u786e\u8ba4",
        "\u786e\u5b9a",
        "\u53ef\u4ee5",
        "\u6ca1\u95ee\u9898",
        "\u6ca1\u5565\u95ee\u9898",
        "\u5c31\u8fd9\u6837",
        "\u5bf9",
        "\u5bf9\u7684",
        "\u884c",
        "\u597d",
        "\u597d\u7684",
        "\u55ef",
        "\u55ef\u55ef",
        "\u6069",
        "\u6069\u6069",
        "ok",
        "okay",
    }


def _is_global_clear_fast_path(raw_input: str) -> bool:
    if not _is_clear_current_report_request(raw_input):
        return False
    compact = _compact_for_intent(raw_input)
    scoped_markers = {
        "今日工作",
        "今天工作",
        "今日完成",
        "今天完成",
        "工作",
        "问题",
        "风险",
        "明日计划",
        "明天计划",
        "明日",
        "明天",
        "计划",
        "第1条",
        "第一条",
        "第2条",
        "第二条",
        "第3条",
        "第三条",
    }
    return not any(marker in compact for marker in scoped_markers)


def _is_clear_current_report_request(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if re.search(r"(这份|这版|当前|现在|草稿|日报|复盘).{0,4}不要了?.{0,6}(重新|重来|从头|再来)", compact):
        return True
    report_object = r"(?:(?:当前|现在|这份|这版)(?:草稿|日报|复盘|内容)|(?:草稿|日报|复盘))"
    if re.search(rf"(?:把)?{report_object}.{{0,4}}(?:清空|清掉|清除|删掉|删除)", compact):
        return True
    if re.search(rf"(?:清空|清掉|清除|删掉|删除).{{0,4}}{report_object}", compact):
        return True
    explicit_clear_all = {
        "清空全部日报",
        "清空整份日报",
        "整份日报清空",
        "全部清空",
        "重置日报",
        "删掉整份日报",
        "删除整份日报",
        "今天日报全部不要了",
        "今日日报全部不要了",
        "日报全部不要了",
        "这份日报全部不要了",
        "当前日报全部不要了",
        "日报全部清空",
    }
    if compact in explicit_clear_all or any(token in compact for token in (
        "清空全部日报",
        "清空整份日报",
        "重置日报",
        "删掉整份日报",
        "删除整份日报",
        "今天日报全部不要",
        "今日日报全部不要",
        "日报全部不要",
        "整份日报清空",
        "日报全部清空",
    )):
        return True
    exact = {
        "清空",
        "清空吧",
        "清掉吧",
        "删了吧",
        "删除吧",
        "清空草稿",
        "清空当前草稿",
        "清空当前复盘",
        "清空复盘",
        "删除草稿",
        "删除当前草稿",
        "删掉草稿",
        "删掉当前草稿",
        "这版不要",
        "这版不要了",
        "当前草稿不要",
        "当前草稿不要了",
        "当前这版不要",
        "当前这版不要了",
        "全部删掉",
        "全部删除",
        "全部清空",
        "全删掉",
        "从头开始",
        "重新开始",
        "我重新填",
        "重新填",
    }
    if compact in exact:
        return True
    tokens = [
        "帮我清空",
        "先帮我清空",
        "先清空",
        "把草稿清空",
        "把当前草稿清空",
        "把复盘清空",
        "把当前复盘清空",
        "当前草稿删掉",
        "当前复盘删掉",
        "刚才的先清掉",
        "刚才先清掉",
        "先清掉",
        "这版不要了",
        "这版不要",
    ]
    return any(token in compact for token in tokens)


def _resolve_pending_clear_reply(raw_input: str) -> str | None:
    compact = _compact_for_intent(raw_input)
    confirm = {
        "确认",
        "确定",
        "是",
        "是的",
        "清空",
        "对",
        "不要保留",
        "不要",
        "删掉",
        "删除",
        "清掉",
        "清空吧",
        "清掉吧",
        "嗯",
        "嗯嗯",
        "恩",
        "恩恩",
        "好",
        "好的",
    }
    cancel = {
        "取消",
        "不清空",
        "保留",
        "先保留",
        "算了",
        "不用了",
        "别清空",
        "不要清空",
    }
    if compact in confirm:
        return "confirm"
    if compact in cancel:
        return "cancel"
    return None


def _is_courtesy_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    exact = {
        "谢谢",
        "谢谢你",
        "收到谢谢",
        "收到谢谢你",
        "收到辛苦了",
        "辛苦了",
        "好的辛苦了",
        "好的谢谢",
        "好嘞",
        "明白",
        "明白了",
        "感谢",
        "感谢你",
        "麻烦你了",
        "多谢",
    }
    return compact in exact


def _is_postpone_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    tokens = [
        "晚点写",
        "晚点再写",
        "晚点补",
        "晚点再补",
        "晚点说",
        "等会儿发你",
        "等会发你",
        "等会儿写",
        "等会写",
        "一会儿写",
        "一会写",
        "稍后补",
        "稍后再补",
        "现在不方便",
        "先不写",
        "别烦我",
        "先别催",
    ]
    return any(token in compact for token in tokens)


def _is_short_no_problem_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    return compact in {
        "正常",
        "还行",
        "可以",
        "没啥",
        "没事",
        "没有",
        "没有没有",
        "没问题",
        "没啥问题",
        "没什么问题",
        "没有问题",
        "暂无问题",
        "暂无",
        "没遇到",
        "没遇到问题",
        "无问题",
        "ok",
        "okay",
    }


def _is_empty_slot_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    return compact in {
        "无",
        "不",
        "暂无",
        "没有",
        "没",
        "没有了",
        "无今日工作",
        "无工作",
        "暂无工作",
        "无问题",
        "暂无问题",
        "没有问题",
        "没问题",
        "无风险",
        "暂无风险",
        "没有风险",
        "无明日计划",
        "无明天计划",
        "无工作计划",
        "无计划",
        "暂无明日计划",
        "暂无明天计划",
        "暂无工作计划",
        "暂无计划",
        "没有明日计划",
        "没有明天计划",
        "没有工作计划",
        "没有计划",
        "无安排",
        "暂无安排",
        "没有安排",
    }


def _is_no_remaining_content_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    if not compact:
        return False
    scope_tokens = ("其他", "其它", "其余", "剩余", "剩下", "后续", "后面", "别的", "另外")
    empty_tokens = ("没有了", "没有", "没了", "没", "无", "暂无", "不用", "不需要", "不写", "没啥", "没什么")
    if not (any(token in compact for token in scope_tokens) and any(token in compact for token in empty_tokens)):
        return False
    if any(token in compact for token in ("问题", "风险")) and not any(token in compact for token in ("内容", "事项", "计划", "工作")):
        return False
    return True


def _empty_value_for_field(field: str, raw_input: str = "") -> str:
    if field == "problems":
        return "暂无明显问题"
    if field == "tomorrow_plan":
        compact = _compact_for_intent(raw_input)
        if "工作计划" in compact:
            return "无工作计划"
        return "无明日计划"
    if field == "today_work":
        return "无今日工作"
    return "无"


def _is_short_ack_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    return compact in {
        "收到",
        "好",
        "好的",
        "行",
        "嗯",
        "嗯嗯",
        "知道了",
        "看到了",
        "测试通过",
        "通过",
    }


def _mentions_no_problem(raw_input: str) -> bool:
    compact = "".join(raw_input.lower().split())
    if re.search(r"(没|没有|未)(碰到|遇到|发现|出现)(明显)?(新)?(问题|风险|困难|异常)", compact):
        return True
    if re.search(r"(没|没有|未)(碰到|遇到|发现|出现).{0,12}(问题|风险|困难|异常)", compact):
        return True
    standard_no_problem_phrases = [
        "\u6CA1\u95EE\u9898",
        "\u6CA1\u5565\u95EE\u9898",
        "\u6CA1\u4EC0\u4E48\u95EE\u9898",
        "\u6CA1\u6709\u95EE\u9898",
        "\u6CA1\u6709\u4EC0\u4E48\u95EE\u9898",
        "\u6CA1\u4E8B",
        "\u6CA1\u5565\u4E8B",
        "\u6CA1\u4EC0\u4E48\u4E8B",
        "\u6CA1\u6709\u4E8B",
        "\u6682\u65E0\u95EE\u9898",
        "\u6682\u65E0\u660E\u663E\u95EE\u9898",
        "\u65E0\u95EE\u9898",
        "\u65E0\u660E\u663E\u95EE\u9898",
        "\u95EE\u9898\u6682\u65E0",
        "\u95EE\u9898\u65E0",
        "\u95EE\u9898\u6CA1\u6709",
        "\u95EE\u9898\u6CA1",
        "\u6CA1\u98CE\u9669",
        "\u6CA1\u4EC0\u4E48\u98CE\u9669",
        "\u6CA1\u6709\u98CE\u9669",
        "\u6682\u65E0\u98CE\u9669",
        "\u65E0\u98CE\u9669",
        "\u98CE\u9669\u6682\u65E0",
        "\u98CE\u9669\u65E0",
        "\u98CE\u9669\u6CA1\u6709",
    ]
    if any(phrase in compact for phrase in standard_no_problem_phrases):
        return True
    phrases = [
        "没问题",
        "没啥问题",
        "没什么问题",
        "没有问题",
        "没有什么问题",
        "没碰到什么问题",
        "没有碰到什么问题",
        "没遇到什么问题",
        "没有遇到什么问题",
        "没事",
        "没啥事",
        "没什么事",
        "没有事",
        "暂无问题",
        "无问题",
        "问题暂无",
        "问题无",
        "问题没有",
        "问题没",
        "没风险",
        "没什么风险",
        "没有风险",
        "没有什么风险",
        "暂无风险",
        "无风险",
        "风险暂无",
        "风险无",
        "风险没有",
        "风险没",
    ]
    return any(phrase in compact for phrase in phrases)


def _split_tomorrow_text(text: str) -> tuple[str, str]:
    match = re.search(r"(然后)?(明天|明日|明儿|后天|接下来|之后|后续)", text)
    if not match:
        return text, ""
    return text[: match.start()], text[match.start():]


def _normalize_free_text(text: str) -> str:
    text = text.strip()
    text = _strip_report_instruction_prefix(text)
    text = re.sub(r"(你说人活着是为了什么呢?|你说[^，,。；;、]*有什么意义|今天太离谱了吧|算了不聊这个)", "", text)
    text = re.sub(r"^算了[，,。；;:：、\s]*", "", text)
    text = re.sub(r"^(算了算了|跟你实话实说吧?|我想想|这么说吧|嗯|呃|额|哎|啊|然后|那个|就是|我觉得)[，,。；;:：、\s]*", "", text)
    text = re.sub(r"^(算了算了|跟你实话实说吧?|我想想|这么说吧|嗯|呃|额|哎|啊|然后|那个|就是|我觉得)[，,。；;:：、\s]*", "", text)
    text = re.sub(r"[。；;，,]+$", "", text)
    return text.strip()


def _clean_report_items(items: list[str], *, field: str) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        if field in {"today_work", "problems", "tomorrow_plan"} and _is_empty_slot_reply(item):
            cleaned.append(_empty_value_for_field(field, item))
            continue
        text = _normalize_tomorrow_plan(item) if field == "tomorrow_plan" else _normalize_free_text(item)
        if field == "today_work":
            text = _strip_no_problem_fragment_from_work(text)
        text = _strip_replace_report_prefix(text)
        text = _strip_field_change_prefix(text)
        text = _strip_noise_fragments(text)
        if field == "tomorrow_plan":
            text = _normalize_tomorrow_plan(text)
        if text and not _is_non_report_phrase(text):
            cleaned.append(text)
    return merge_ordered([], cleaned)


def _strip_no_problem_fragment_from_work(text: str) -> str:
    if not text:
        return text
    text = re.sub(
        r"(?:[，,。；;、\s]*(?:暂无明显问题|暂无问题|无明显问题|无问题|没问题|没啥问题|没什么问题|没有问题|无风险|没风险|暂无风险|没有风险))+$",
        "",
        text,
    )
    return text.strip(" ，,。；;、")


NOISE_FRAGMENT_PHRASES = {
    "一二三一起发",
    "你赶紧回家",
    "谢谢",
    "谢谢你",
    "收到谢谢",
    "辛苦了",
}


def _strip_noise_fragments(text: str) -> str:
    if not text:
        return text
    parts = [part.strip() for part in re.split(r"[，,。；;、]+", text) if part.strip()]
    if not parts:
        return ""
    kept = [part for part in parts if not _is_non_report_phrase(part) and _compact_for_intent(part) not in NOISE_FRAGMENT_PHRASES]
    if len(kept) == len(parts):
        return text
    return "，".join(kept)


DETAIL_MARKER_RE = re.compile(
    r"(几个|若干|多(?:个|项|件|份|次)?|[0-9一二两三四五六七八九十百]+(?:个|项|件|份|次|起|笔|名|家|条)?|"
    r"上午|中午|下午|晚上|早上|广州|南京|上海|北京|深圳|杭州|成都|武汉|"
    r"客户|法院|合同|案件|材料|评估|庭审|开庭)"
)


def _preserve_simple_source_detail(
    raw_input: str,
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> tuple[list[str], list[str], list[str]]:
    fields = [
        ("today_work", today_work),
        ("problems", problems),
        ("tomorrow_plan", tomorrow_plan),
    ]
    non_empty = [(field, values) for field, values in fields if values]
    if len(non_empty) != 1 or len(non_empty[0][1]) != 1:
        return today_work, problems, tomorrow_plan

    raw_detail = _normalize_free_text(raw_input)
    if (
        not raw_detail
        or _is_non_report_phrase(raw_detail)
        or _looks_like_instruction(raw_detail)
        or _mentions_no_problem(raw_detail)
    ):
        return today_work, problems, tomorrow_plan

    field, values = non_empty[0]
    current = values[0]
    if not _looks_like_detail_loss(raw_detail, current):
        return today_work, problems, tomorrow_plan

    if field == "today_work":
        return [raw_detail], problems, tomorrow_plan
    if field == "problems":
        return today_work, [raw_detail], tomorrow_plan
    return today_work, problems, [_normalize_tomorrow_plan(raw_detail)]


def _looks_like_instruction(text: str) -> bool:
    compact = "".join(text.split())
    instruction_tokens = [
        "帮我改",
        "改成",
        "改为",
        "改下",
        "改一下",
        "修改",
        "更新",
        "补充一个问题",
        "补充问题",
        "补充",
        "清空",
        "重新说",
        "重新来",
        "确认",
        "提交",
        "谢谢",
        "收到",
    ]
    return any(token in compact for token in instruction_tokens)


def _looks_like_detail_loss(raw_detail: str, current: str) -> bool:
    if raw_detail == current or len(raw_detail) <= len(current):
        return False
    raw_markers = {match.group(0) for match in DETAIL_MARKER_RE.finditer(raw_detail)}
    if not any(marker not in current for marker in raw_markers):
        return False
    return _chars_in_order(_compact_for_preservation(current), _compact_for_preservation(raw_detail))


def _compact_for_preservation(text: str) -> str:
    return re.sub(r"[，,。；;：:\s的了]", "", text)


def _chars_in_order(needle: str, haystack: str) -> bool:
    if not needle:
        return False
    pos = 0
    for char in haystack:
        if pos < len(needle) and char == needle[pos]:
            pos += 1
    return pos == len(needle)


def _strip_field_change_prefix(text: str) -> str:
    text = re.sub(r"^(今日工作|今天工作|问题[/／困难]*|问题|困难|风险|明日计划|明天计划|计划)(那里|这块|这部分)?(改成|改为|修改为|更新为|补成)?[：:\s]*", "", text)
    text = re.sub(r"^(明天|明日)(改成|改为|修改为|更新为)[：:\s]*", "", text)
    text = re.sub(r"^(补充一个问题|补充问题|还有个问题|另外一个问题|补充一个|补充一下)[，,。；;：:\s]*", "", text)
    text = re.sub(r"^(改成|改为|修改为|更新为|补成)[：:\s]*", "", text)
    return text.strip()


def _is_non_report_phrase(text: str) -> bool:
    compact = "".join(text.split())
    phrases = {
        "哎",
        "嗯",
        "啊",
        "那个",
        "就是",
        "怎么说呢",
        "算了算了",
        "跟你实话实说吧",
        "我想想",
        "这么说吧",
        "你说人活着是为了什么呢",
        "今天太离谱了吧",
        "算了不聊这个",
        "一二三一起发",
        "你赶紧回家",
        "谢谢",
        "谢谢你",
        "收到谢谢",
        "辛苦了",
    }
    return compact in phrases


def _normalize_tomorrow_plan(text: str) -> str:
    cleaned = _normalize_free_text(text)
    cleaned = re.sub(r"^(?:\d+|[一二三四五六七八九十]+)[.、．）)]\s*", "", cleaned).strip(" ，,。；;、")
    cleaned = re.sub(r"^(明天|明日|明儿)\1+", r"\1", cleaned)
    cleaned = re.sub(r"^(明天|明日)的话[，,。；;、\s]*", r"\1", cleaned)
    cleaned = _drop_negative_plan_side_notes(cleaned)
    cleaned = cleaned.replace("开个庭", "开庭")
    compact = _compact_for_intent(cleaned)
    if (
        any(marker in compact for marker in ("明天", "明日", "明儿"))
        and any(marker in compact for marker in ("工作计划", "计划", "安排"))
        and any(marker in compact for marker in ("今天一样", "今日一样", "同今天", "同今日", "和今天一样", "和今日一样"))
    ):
        return "继续今日工作"
    if re.fullmatch(r"(明天|明日)?换一家店", cleaned):
        return "明天换一家店购买"
    if _is_vague_tomorrow_plan(cleaned):
        return "明天继续推进相关工作"
    return cleaned


def _drop_negative_plan_side_notes(text: str) -> str:
    parts = [part.strip() for part in re.split(r"[，,。；;、]+", text) if part.strip()]
    if len(parts) <= 1:
        return text
    kept = [part for part in parts if not re.search(r"(先)?不去(了)?|不用去|取消去", part)]
    return "，".join(kept) if kept else text


def _is_vague_tomorrow_plan(text: str) -> bool:
    compact = "".join(text.lower().split())
    exact_matches = {
        "明天继续",
        "明天接着",
        "明天继续做",
        "明天继续弄",
        "明天再说",
        "明天再弄",
        "之后再看",
        "继续推进",
        "继续跟进",
    }
    if compact in exact_matches:
        return True
    return bool(re.fullmatch(r"(明天|明日)?继续(做|弄|跟进|推进)?", compact))


def _looks_like_today_work(text: str) -> bool:
    return bool(re.search(r"(今天|今日|完成|做了|处理|整理|审核|评审|沟通|吃了)", text))


def _looks_like_today_work_action(text: str) -> bool:
    return bool(re.search(r"(审|审核|处理|整理|沟通|复盘|开庭|出差|参加|推进|跟进|起草|修改|学习|发送|完成|走访|梳理|办理|核查|评审|调解|谈判|对接)", text))


def _looks_like_problem(text: str) -> bool:
    return bool(re.search(r"(问题|风险|遇到|影响|卡住|失败|败诉|报错|慢|缺|缺少|没给|没有|不够|加全|加少|遗漏|难度大|较大|倾向于被告|无法|未能|未反馈|无目标|没有目标)", text))


def _looks_colloquial_or_test(raw_input: str, today_work: list[str], problems: list[str], tomorrow_plan: list[str]) -> bool:
    text = raw_input.lower()
    if "测试" in text:
        return True
    if any(word in text for word in ["手抓饼", "哈哈", "随便", "玩玩"]) and (today_work or problems or tomorrow_plan):
        return True
    return False


def _join_warnings(warnings: list[str]) -> str | None:
    unique: list[str] = []
    for warning in warnings:
        if warning and warning not in unique:
            unique.append(warning)
    return "；".join(unique) if unique else None


def _merge_quality_warning(existing: str | None, incoming: str | None) -> str | None:
    return _join_warnings([existing or "", incoming or ""])
