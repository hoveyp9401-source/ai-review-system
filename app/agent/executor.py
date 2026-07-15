from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.action_plan import ActionPlan, AgentAction, PendingInteractionPlan
from app.agent.edit_cursor import (
    build_current_edit_cursor,
    build_historical_edit_cursor,
    cursor_focused_section,
    cursor_report,
    cursor_target_date,
    normalize_edit_cursor,
    with_edit_cursor,
)
from app.models import DailyReport, User
from app.repositories import get_report, upsert_daily_report
from app.schemas import StructuredDailyReport
from app.services.state_machine import (
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


REPORT_FIELDS = ("today_work", "problems", "tomorrow_plan")
REFERENCE_REPORT_CONTEXT_KEY = "_reference_report_context"
LAST_MODIFIED_ITEM_KEY = "_last_modified_item"
CORRECTION_TARGET_KEY = "_correction_target"
LAST_UNWRITTEN_CANDIDATE_KEY = "_last_unwritten_candidate"
LAST_DISPLAY_CONTEXT_KEY = "_last_display_context"
RECENT_REPORT_CONTEXT_KEY = "_recent_report_context"
PENDING_SCOPE_KEY = "_scope"
PLACEHOLDER_VALUES = {
    "today_work": {"无今日工作", "无工作", "今天无工作", "暂无今日工作", "未填写"},
    "problems": {"暂无明显问题", "无明显问题", "暂无问题", "无问题", "正常", "没问题", "没有问题", "没啥问题", "无明显风险"},
    "tomorrow_plan": {"无明日计划", "无计划", "明天无计划", "暂无明日计划", "暂无计划", "没有计划"},
}
logger = logging.getLogger("ai_review_agent_executor")
EMPTY_ACK_FLAGS = {
    "today_work": "today_work_acknowledged_empty",
    "problems": "problems_acknowledged_empty",
    "tomorrow_plan": "tomorrow_plan_acknowledged_empty",
}
DAILY_REPORT_LOCK_HOUR = 9
DAILY_REPORT_LOCKED_ACTIONS = {
    "append_items",
    "delete_item",
    "clear_field",
    "clear_all",
    "replace_field",
    "replace_text",
    "merge_items",
    "move_item",
    "polish_items",
    "submit_report",
    "unsubmit_report",
}
DAILY_NO_CONFIRM_ACTIONS = {
    "delete_item",
    "clear_field",
    "clear_all",
    "unsubmit_report",
    "restore_snapshot",
    "update_historical_report",
}


@dataclass(frozen=True)
class AgentExecutionResult:
    report: DailyReport | None
    structured: StructuredDailyReport
    missing_sections: list[str]
    message: str
    reply_kind: str
    report_saved: bool


@dataclass(frozen=True)
class HistoricalActionResolution:
    action: AgentAction | None = None
    error: str = ""


class ReportAgentExecutor:
    async def execute(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at: datetime,
        raw_input: str,
        source: str,
        plan: ActionPlan,
        meta: dict[str, Any],
        previous_report: DailyReport | None = None,
    ) -> AgentExecutionResult:
        if _is_delegate_without_content(raw_input):
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message="这句我先不记入复盘。你可以直接说今天做了什么、遇到什么问题，或者明天计划。",
                reply_kind="agent_delegate_without_content",
            )
        if _is_casual_teaser_without_report_content(raw_input):
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message="这句我先不记入复盘。你可以直接说今天做了什么、遇到什么问题，或者明天计划。",
                reply_kind="agent_casual_teaser",
            )
        pending_append_field = _pending_append_content_field(existing)
        if pending_append_field and _should_promote_pending_append_content(raw_input, plan):
            plan = ActionPlan(
                intent="edit_draft",
                confidence="high",
                should_write=True,
                clear_pending_interaction=True,
                actions=[
                    AgentAction(
                        type="append_items",
                        field=pending_append_field,
                        items=[raw_input.strip()],
                        source="pending_append_content",
                    )
                ],
                reason="Promoted substantive reply for existing awaiting_append_content state before quality gating.",
            )
        if plan.confidence == "low":
            fallback_plan = _low_confidence_report_fragment_plan(raw_input, existing)
            if fallback_plan is not None:
                plan = fallback_plan
        if plan.confidence == "low":
            candidate_report = await self._save_last_unwritten_candidate_if_useful(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                raw_input=raw_input,
                source=source,
                plan=plan,
                meta=meta,
            )
            if candidate_report is not None:
                return self._no_write(
                    existing=candidate_report,
                    report_date=report_date,
                    message=plan.clarification_question or plan.reply_to_user or "这句我先不写入日报。如果要放到某一栏，可以说“这个放问题里”或“这个放今日工作”。",
                    reply_kind="agent_low_confidence",
                    report_saved=True,
                )
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message=plan.clarification_question or plan.reply_to_user or "我还没理解稳妥，这句先不写入日报。你可以再说具体一点。",
                reply_kind="agent_low_confidence",
            )

        query_result = await self._try_query(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            source=source,
            plan=plan,
            raw_input=raw_input,
            meta=meta,
        )
        if query_result is not None:
            return query_result

        plan = _coerce_plan_to_historical_context(plan, existing, raw_input=raw_input)
        plan = _coerce_plan_to_current_context(plan, existing, raw_input=raw_input)
        plan = _coerce_plan_to_explicit_ordinal_rewrite(plan, existing, raw_input=raw_input)
        plan = _coerce_replacement_intent_to_full_field_replacement(plan, existing, raw_input=raw_input)

        if _is_after_daily_report_lock(received_at, report_date) and _plan_would_change_report_state(plan):
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message=_daily_report_lock_message(report_date),
                reply_kind="daily_report_locked",
            )

        historical_update_result = await self._try_historical_update(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            raw_input=raw_input,
            source=source,
            plan=plan,
            meta=meta,
        )
        if historical_update_result is not None:
            return historical_update_result

        pending_field = _pending_append_content_field(existing)
        quality_pending = _pending_quality_candidate_from_no_write_plan(plan, raw_input, pending_field)
        if quality_pending is not None and existing is not None:
            report = await self._save_pending_interaction_state(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                raw_input=raw_input,
                source=source,
                pending_interaction=quality_pending,
                plan=plan,
                meta=meta,
            )
            return AgentExecutionResult(
                report=report,
                structured=StructuredDailyReport(),
                missing_sections=_missing_sections(report),
                message=plan.reply_to_user or plan.clarification_question or "这条内容有点笼统。你可以补充更具体的内容；如果就按原话记录，也可以回复“就这么写”。",
                reply_kind="agent_pending_quality_candidate",
                report_saved=True,
            )
        if not plan.should_write and not plan.clear_pending_interaction and pending_field and _can_fallback_to_pending_append(plan):
            plan = ActionPlan(
                intent="edit_draft",
                confidence=plan.confidence,
                should_write=True,
                actions=[AgentAction(type="append_items", field=pending_field, items=[raw_input.strip()])],
                reason="Fallback to existing pending_interaction awaiting_append_content.",
            )
        plan = _promote_daily_confirmation_pending_to_write(plan, existing)
        if not plan.should_write and _can_execute_structured_no_write_actions(plan, existing, raw_input):
            payload = plan.model_dump()
            payload["should_write"] = True
            payload["reason"] = (plan.reason or "") + " Executor promoted safe structured actions to write."
            plan = ActionPlan.model_validate(payload)

        no_write_mismatch_message = _structured_no_write_mismatch_message(plan, existing, raw_input)
        if no_write_mismatch_message:
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message=no_write_mismatch_message,
                reply_kind="agent_write_intent_clarification",
            )

        if not plan.should_write:
            if plan.clear_pending_interaction and existing is not None:
                report = await self._save_pending_interaction_state(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    raw_input=raw_input,
                    source=source,
                    pending_interaction=None,
                    plan=plan,
                    meta=meta,
                )
                return AgentExecutionResult(
                    report=report,
                    structured=StructuredDailyReport(),
                    missing_sections=_missing_sections(report),
                    message=plan.reply_to_user or "好的，已取消本次操作。",
                    reply_kind="agent_clear_pending_interaction",
                    report_saved=True,
                )
            if plan.intent == "fill_report":
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message="可以，请告诉我今天主要做了什么、有没有问题或风险，以及明天计划。",
                    reply_kind="agent_fill_report_prompt",
                )
            pending_action = _pending_action_from_no_write_plan(plan, existing)
            if pending_action is not None:
                pending, action = pending_action
                report = await self._save_pending_interaction_state(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    raw_input=raw_input,
                    source=source,
                    pending_interaction=pending,
                    plan=plan,
                    meta=meta,
                )
                return AgentExecutionResult(
                    report=report,
                    structured=StructuredDailyReport(),
                    missing_sections=_missing_sections(report),
                    message=plan.reply_to_user or plan.clarification_question or _confirmation_message_for_action(action, existing),
                    reply_kind="agent_pending_action_confirmation",
                    report_saved=True,
                )
            clarification_pending = _pending_clarification_from_no_write_plan(plan, existing, raw_input)
            if clarification_pending is not None and existing is not None:
                report = await self._save_pending_interaction_state(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    raw_input=raw_input,
                    source=source,
                    pending_interaction=clarification_pending,
                    plan=plan,
                    meta=meta,
                )
                return AgentExecutionResult(
                    report=report,
                    structured=StructuredDailyReport(),
                    missing_sections=_missing_sections(report),
                    message=plan.reply_to_user or plan.clarification_question or "我还需要你补充具体要处理哪一条。",
                    reply_kind="agent_pending_clarification",
                    report_saved=True,
                )
            if plan.pending_interaction_to_set and _should_persist_pending_interaction(plan):
                report = await self._save_pending_interaction(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    raw_input=raw_input,
                    source=source,
                    plan=plan,
                    meta=meta,
                )
                return AgentExecutionResult(
                    report=report,
                    structured=StructuredDailyReport(),
                    missing_sections=_missing_sections(report),
                    message=plan.reply_to_user or plan.clarification_question or "好的，请继续。",
                    reply_kind="agent_pending_interaction",
                    report_saved=True,
                )
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message=plan.reply_to_user or plan.clarification_question or "好的，这句我先不写入日报。",
                reply_kind=f"agent_{plan.intent}",
            )

        today_work, problems, tomorrow_plan = _current_lists(existing)
        initial_values = (list(today_work), list(problems), list(tomorrow_plan))
        section_status = _current_section_status(existing)
        plan_actions = _rewrite_previous_plan_reference_placeholders(
            plan.actions,
            raw_input,
            section_status,
            previous_report=previous_report,
        )
        active_current_pending = _current_context_pending(existing)
        active_current_cursor = normalize_edit_cursor(active_current_pending)
        active_current_field = cursor_focused_section(active_current_cursor)
        empty_ack = _empty_ack_from_existing(existing)
        initial_empty_ack = dict(empty_ack)
        cleared_all_this_turn = False
        destructive = False
        changed = False
        content_changed = False
        reference_only_change = False
        reference_rollover_attempted = False
        last_changed_field = active_current_field if active_current_field in REPORT_FIELDS else "none"
        action_messages: list[str] = []
        withdraw_in_plan = any(action.type == "unsubmit_report" for action in plan_actions)
        last_modified_item: dict[str, Any] | None = None
        replaced_fields_this_turn: set[str] = set()
        touched_fields_this_turn: set[str] = set()
        batch_pending = _pending_batch_action_from_plan(plan_actions, existing, clear_pending=plan.clear_pending_interaction)
        if batch_pending is not None:
            pending, message = batch_pending
            report = await self._save_pending_interaction_state(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                raw_input=raw_input,
                source=source,
                pending_interaction=pending,
                plan=plan,
                meta=meta,
            )
            return AgentExecutionResult(
                report=report,
                structured=StructuredDailyReport(),
                missing_sections=_missing_sections(report),
                message=message,
                reply_kind="agent_pending_action_confirmation",
                report_saved=True,
            )
        for original_action in plan_actions:
            if original_action.type == "load_reference_report":
                reference = _reference_report_from_action(original_action, report_date)
                if reference:
                    section_status[REFERENCE_REPORT_CONTEXT_KEY] = reference
                    changed = True
                    reference_only_change = True
                    action_messages.append("收到，我已把这份内容作为昨天日报参考。你可以继续说哪些已完成、哪些明天继续。")
                continue
            expanded_actions = _expand_reference_rollover_action(
                original_action,
                section_status,
                raw_input=raw_input,
                previous_report=previous_report,
            )
            if expanded_actions and original_action.type in {
                "complete_previous_plan_item",
                "complete_all_previous_plan_items",
                "rollover_previous_plan_items",
            }:
                reference_rollover_attempted = True
            if not expanded_actions and original_action.type in {
                "complete_previous_plan_item",
                "complete_all_previous_plan_items",
                "rollover_previous_plan_items",
            }:
                continue
            if not expanded_actions:
                expanded_actions = [original_action]
            for action in expanded_actions:
                if action.type == "delete_item" and action.field in replaced_fields_this_turn:
                    action = AgentAction(type="no_op")
                merge_mismatch = _coerce_delete_merge_mismatch(action, existing, raw_input)
                if merge_mismatch[1]:
                    return self._no_write(
                        existing=existing,
                        report_date=report_date,
                        message=merge_mismatch[1],
                        reply_kind="agent_merge_clarification",
                    )
                action = merge_mismatch[0]
                action = _coerce_merge_field_mismatch(action, existing, raw_input)
                action = _coerce_previous_plan_placeholder_action(
                    action,
                    section_status,
                    raw_input,
                    previous_report=previous_report,
                )
                if not plan.clear_pending_interaction and _delete_action_without_delete_request(action, raw_input):
                    action = AgentAction(type="no_op")
                if not plan.clear_pending_interaction and _text_replace_without_replace_request(action, raw_input):
                    action = AgentAction(type="no_op")
                ambiguous_delete_message = _ambiguous_delete_clarification(action, existing, raw_input)
                if ambiguous_delete_message:
                    return self._no_write(
                        existing=existing,
                        report_date=report_date,
                        message=ambiguous_delete_message,
                        reply_kind="agent_delete_ambiguous",
                    )
                action = _enforce_draft_multi_delete_confirmation(action, existing)
                action = _allow_confirmed_pending_action(action, clear_pending=plan.clear_pending_interaction)
                action = _relax_draft_single_item_confirmation(action, existing)
                action = _relax_daily_report_action_confirmation(action)
                action = _restore_snapshot_from_saved_state(action, section_status)
                action = _drop_unrequested_previous_plan_rollover(action, section_status, raw_input, previous_report=previous_report)
                if action.field in REPORT_FIELDS:
                    content_changed = True
                touched_fields_this_turn.update(_action_touched_fields(action))
                needs_confirmation = action.requires_confirmation or (
                    _requires_completed_confirmation(existing, action) and not plan.clear_pending_interaction and not withdraw_in_plan
                )
                if needs_confirmation:
                    pending = _pending_interaction_from_action(action, existing)
                    if existing is not None and pending is not None:
                        report = await self._save_pending_interaction_state(
                            session,
                            user=user,
                            existing=existing,
                            report_date=report_date,
                            received_at=received_at,
                            raw_input=raw_input,
                            source=source,
                            pending_interaction=pending,
                            plan=plan,
                            meta=meta,
                        )
                        return AgentExecutionResult(
                            report=report,
                            structured=StructuredDailyReport(),
                            missing_sections=_missing_sections(report),
                            message=action.confirmation_message or _confirmation_message_for_action(action, existing),
                            reply_kind="agent_pending_action_confirmation",
                            report_saved=True,
                        )
                    return self._no_write(
                        existing=existing,
                        report_date=report_date,
                        message="这个操作会影响已有日报内容，但我没有拿到可执行的确认信息。请重新说明要操作哪一项。",
                        reply_kind="agent_requires_confirmation_without_pending",
                    )
                if action.field in REPORT_FIELDS:
                    if _is_empty_placeholder_items(action.field, action.items):
                        empty_ack[action.field] = True
                    elif action.items and action.type in {"append_items", "replace_field", "polish_items"}:
                        empty_ack[action.field] = False
                before = (list(today_work), list(problems), list(tomorrow_plan))
                action = _normalize_action_for_existing(action, before, raw_input=raw_input)
                touched_fields_this_turn.update(_action_touched_fields(action))
                unsafe_message = _unsafe_report_content_write_message(action, raw_input)
                if unsafe_message:
                    return self._no_write(
                        existing=existing,
                        report_date=report_date,
                        message=unsafe_message,
                        reply_kind="agent_unsafe_operation_phrase",
                    )
                message = _action_success_message(action, before)
                today_work, problems, tomorrow_plan, action_destructive = _apply_action(
                    action,
                    today_work=today_work,
                    problems=problems,
                    tomorrow_plan=tomorrow_plan,
                )
                if action.type == "clear_all":
                    empty_ack = {field: False for field in REPORT_FIELDS}
                    cleared_all_this_turn = True
                destructive = destructive or action_destructive
                if action.type == "replace_field" and action.field in REPORT_FIELDS:
                    replaced_fields_this_turn.add(action.field)
                action_changed = before != (today_work, problems, tomorrow_plan)
                changed = changed or action_changed
                if action_changed and _action_field(action) in REPORT_FIELDS:
                    last_changed_field = _action_field(action)
                    last_modified_item = _last_modified_item_payload(
                        action,
                        before=before,
                        after=(today_work, problems, tomorrow_plan),
                        user=user,
                        existing=existing,
                        report_date=report_date,
                        raw_input=raw_input,
                        received_at=received_at,
                    )
                if action_changed and message:
                    action_messages.append(message)

        preserve_existing_item_boundaries = _preserve_existing_item_boundaries(plan, existing)
        today_work, problems, tomorrow_plan, empty_ack = _normalize_report_fields_after_actions(
            today_work,
            problems,
            tomorrow_plan,
            empty_ack,
            split_today_work=not preserve_existing_item_boundaries,
        )
        if not preserve_existing_item_boundaries:
            today_work, problems = _redistribute_misfiled_problem_items(today_work, problems)
        today_work, problems, tomorrow_plan = _restore_untouched_existing_fields(
            existing,
            touched_fields=touched_fields_this_turn,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
        )
        final_values_after_normalization = (list(today_work), list(problems), list(tomorrow_plan))
        empty_ack_changed = empty_ack != initial_empty_ack
        if (
            final_values_after_normalization == initial_values
            and not empty_ack_changed
            and action_messages
            and any(_is_mutating_report_action(action) for action in plan_actions)
        ):
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message=_no_effect_action_message(plan, existing),
                reply_kind="agent_edit_no_effect",
            )
        changed = (changed and final_values_after_normalization != initial_values) or reference_only_change or empty_ack_changed

        if any(action.type == "unsubmit_report" for action in plan_actions):
            if existing is None:
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message="当前还没有可撤回的日报。",
                    reply_kind="agent_unsubmit_without_report",
                )
            if existing.status != STATUS_COMPLETED:
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message="当前日报还不是已提交状态，不需要撤回。",
                    reply_kind="agent_unsubmit_not_completed",
                )
            report = await self._save_report(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                raw_input=raw_input,
                source=source,
                today_work=today_work,
                problems=problems,
                tomorrow_plan=tomorrow_plan,
                section_status=_with_empty_ack(_clear_agent_pending(section_status), empty_ack),
                status=STATUS_COLLECTING,
                completeness_score=float(getattr(existing, "completeness_score", 1.0) or 1.0),
                confirmation_type=CONFIRMATION_NONE,
                confirmed_by_user=False,
                meta=meta,
                plan=plan,
            )
            return AgentExecutionResult(
                report=report,
                structured=StructuredDailyReport(today_work=today_work, problems=problems, tomorrow_plan=tomorrow_plan, completeness=float(getattr(report, "completeness_score", 1.0) or 1.0)),
                missing_sections=_missing_sections(report),
                message=_build_unsubmit_success_reply(report, action_messages),
                reply_kind="agent_unsubmit_report",
                report_saved=True,
            )

        if plan.intent == "confirm_submit" or any(action.type == "submit_report" for action in plan.actions):
            if existing is None:
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message="当前还没有可提交的日报内容，请先填写今日工作、问题/风险和明日计划。",
                    reply_kind="agent_confirm_without_report",
                )
            if (
                section_status.get("_cleared_all_current_report")
                and not today_work
                and not problems
                and not tomorrow_plan
            ):
                empty_ack["problems"] = True
                problems = ["暂无明显问题"]
                status = STATUS_COLLECTING
                final_section_status = _with_empty_ack(_clear_agent_pending(section_status), empty_ack)
                final_section_status.pop("_cleared_all_current_report", None)
                report = await self._save_report(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    raw_input=raw_input,
                    source=source,
                    today_work=today_work,
                    problems=problems,
                    tomorrow_plan=tomorrow_plan,
                    section_status=final_section_status,
                    status=status,
                    completeness_score=_completeness_from_missing(["today_work", "tomorrow_plan"]),
                    confirmation_type=CONFIRMATION_NONE,
                    confirmed_by_user=False,
                    meta=meta,
                    plan=plan,
                )
                return AgentExecutionResult(
                    report=report,
                    structured=StructuredDailyReport(today_work=today_work, problems=problems, tomorrow_plan=tomorrow_plan),
                    missing_sections=_missing_sections(report),
                    message=build_followup_message(
                        ["today_work", "tomorrow_plan"],
                        today_work=today_work,
                        problems=problems,
                        tomorrow_plan=tomorrow_plan,
                    ),
                    reply_kind="agent_confirm_incomplete_report",
                    report_saved=True,
                )
            confirm_missing = _missing_sections_for_values(
                today_work=today_work,
                problems=problems,
                tomorrow_plan=tomorrow_plan,
                empty_ack=empty_ack,
            )
            if confirm_missing:
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message=build_followup_message(
                        confirm_missing,
                        today_work=today_work,
                        problems=problems,
                        tomorrow_plan=tomorrow_plan,
                    ),
                    reply_kind="agent_confirm_incomplete_report",
                )
            report = await self._save_report(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                raw_input=raw_input,
                source=source,
                today_work=today_work,
                problems=problems,
                tomorrow_plan=tomorrow_plan,
                section_status=_with_empty_ack(_clear_agent_pending(section_status), empty_ack),
                status=STATUS_COMPLETED,
                completeness_score=1.0,
                confirmation_type=CONFIRMATION_USER_CONFIRMED,
                confirmed_by_user=True,
                meta=meta,
                plan=plan,
            )
            return AgentExecutionResult(
                report=report,
                structured=StructuredDailyReport(today_work=today_work, problems=problems, tomorrow_plan=tomorrow_plan, completeness=1.0),
                missing_sections=[],
                message="已确认，日报将提交。",
                reply_kind="agent_confirm_submit",
                report_saved=True,
            )

        if not changed:
            candidate_report = await self._save_last_unwritten_candidate_if_useful(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                raw_input=raw_input,
                source=source,
                plan=plan,
                meta=meta,
            )
            if candidate_report is not None:
                return self._no_write(
                    existing=candidate_report,
                    report_date=report_date,
                    message=_no_change_message(plan, existing),
                    reply_kind="agent_no_change",
                    report_saved=True,
                )
            if reference_rollover_attempted and existing is not None:
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message="相关事项已在当前日报草稿中，我没有重复添加。\n\n" + _build_current_report_preview(existing),
                    reply_kind="agent_no_duplicate_rollover",
                )
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message=_no_change_message(plan, existing),
                reply_kind="agent_no_change",
            )

        structured = StructuredDailyReport(today_work=today_work, problems=problems, tomorrow_plan=tomorrow_plan)
        base_section_status = _with_empty_ack(_clear_agent_pending(section_status), empty_ack)
        state = infer_report_state(
            existing_section_status=base_section_status,
            merged_today_work=today_work,
            merged_problems=problems,
            merged_tomorrow_plan=tomorrow_plan,
            structured=structured,
            raw_input=raw_input,
        )
        missing_sections = [field for field in state.missing_sections if not empty_ack.get(field)]
        completeness_score = _completeness_from_missing(missing_sections)
        ready_for_confirmation = not missing_sections
        final_status = STATUS_PENDING_CONFIRMATION if ready_for_confirmation else STATUS_COLLECTING
        confirmation_type = CONFIRMATION_NONE
        confirmed_by_user = False
        content_empty = not any((today_work, problems, tomorrow_plan))
        if existing and existing.status == STATUS_COMPLETED and not content_empty:
            final_status = STATUS_COMPLETED
            confirmation_type = existing.confirmation_type or CONFIRMATION_USER_CONFIRMED
            confirmed_by_user = bool(existing.confirmed_by_user)

        final_section_status = _with_empty_ack(dict(state.section_status), empty_ack)
        if cleared_all_this_turn:
            final_section_status["_cleared_all_current_report"] = True
        _preserve_agent_section_status(section_status, final_section_status)
        if any(action.source == "last_unwritten_candidate" for action in plan.actions):
            final_section_status.pop(LAST_UNWRITTEN_CANDIDATE_KEY, None)
        for field, acknowledged in empty_ack.items():
            if acknowledged:
                final_section_status[field] = True
        if destructive:
            final_section_status["_previous_draft_snapshot"] = _snapshot(existing, user=user, report_date=report_date)
        if last_modified_item is not None:
            final_section_status[LAST_MODIFIED_ITEM_KEY] = last_modified_item
            final_section_status[CORRECTION_TARGET_KEY] = last_modified_item
        if plan.pending_interaction_to_set:
            pending_to_store = plan.pending_interaction_to_set.model_dump()
            if pending_to_store.get("type") == "current_report_edit_flow":
                pending_to_store = _current_pending_after_update(
                    today_work=today_work,
                    problems=problems,
                    tomorrow_plan=tomorrow_plan,
                    status=final_status,
                    report_date=report_date,
                    base_pending=pending_to_store,
                    selected_field=last_changed_field,
                )
            pending_to_store = _with_pending_scope(pending_to_store, user=user, existing=existing, report_date=report_date)
            final_section_status["_pending_interaction"] = pending_to_store
        elif active_current_pending is not None and content_changed and not plan.clear_pending_interaction:
            pending_to_store = _current_pending_after_update(
                today_work=today_work,
                problems=problems,
                tomorrow_plan=tomorrow_plan,
                status=final_status,
                report_date=report_date,
                base_pending=active_current_pending,
                selected_field=last_changed_field,
            )
            final_section_status["_pending_interaction"] = _with_pending_scope(pending_to_store, user=user, existing=existing, report_date=report_date)
        final_section_status[LAST_DISPLAY_CONTEXT_KEY] = _last_display_context_payload(
            existing=existing,
            report_date=report_date,
            displayed_at=received_at,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
        )
        report = await self._save_report(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            raw_input=raw_input,
            source=source,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            section_status=final_section_status,
            status=final_status,
            completeness_score=completeness_score,
            confirmation_type=confirmation_type,
            confirmed_by_user=confirmed_by_user,
            meta=meta,
            plan=plan,
        )
        if reference_only_change and not content_changed:
            message = "\n".join(action_messages) if action_messages else "收到，我已保存参考内容。"
        else:
            message = _build_action_success_reply(report, action_messages)
        return AgentExecutionResult(
            report=report,
            structured=structured,
            missing_sections=missing_sections,
            message=message,
            reply_kind="agent_report_update",
            report_saved=True,
        )

    async def _try_query(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at: datetime,
        source: str,
        plan: ActionPlan,
        raw_input: str,
        meta: dict[str, Any],
    ) -> AgentExecutionResult | None:
        if plan.intent == "query_current":
            if (
                not _looks_like_current_report_query(raw_input)
                and "pending current report query" not in plan.reason
                and "direct current report draft query" not in plan.reason
            ):
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message=plan.reply_to_user
                    or "我主要负责日报填写、修改和查询；这类信息我暂时查不了。你可以继续告诉我今天做了什么、问题风险或明天计划。",
                    reply_kind="agent_non_report_query",
                )
            display_report = existing
            report_saved = False
            if existing is not None:
                display_report = await self._save_display_context_state(
                    session,
                    user=user,
                    existing=existing,
                    report_date=report_date,
                    received_at=received_at,
                    raw_input=raw_input,
                    source=source,
                    plan=plan,
                    meta=meta,
                )
                report_saved = True
            return self._no_write(
                existing=display_report,
                report_date=report_date,
                message=_format_report(display_report, report_date) if display_report else _format_empty_report(report_date),
                reply_kind="agent_query_current",
                report_saved=report_saved,
            )
        history_action = next((action for action in plan.actions if action.type == "query_history"), None)
        if plan.intent != "query_history" and history_action is None:
            return None
        target_date = _resolve_target_date(history_action.target_date if history_action else None, report_date)
        field = history_action.field if history_action and history_action.field in REPORT_FIELDS else "none"
        report = await get_report(session, user.id, target_date)
        state_report = existing
        report_saved = False
        if report is not None and target_date != report_date and existing is not None and _historical_context_pending(existing) is None:
            pending_interaction = _historical_report_context_pending(report, target_date, field=field)
            state_report = await self._save_pending_interaction_state(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                raw_input=raw_input,
                source=source,
                pending_interaction=pending_interaction,
                plan=plan,
                meta=meta,
                recent_report_context=_recent_report_context_payload(
                    viewed_report=report,
                    viewed_report_date=target_date,
                    viewed_at=received_at,
                    viewer=user,
                    owner="self",
                    field=field,
                ),
            )
            report_saved = True
        return self._no_write(
            existing=state_report,
            report_date=report_date,
            message=_format_report_field(report, target_date, field) if report and field in REPORT_FIELDS else (_format_report(report, target_date) if report else f"没有查到 {target_date.isoformat()} 的日报记录。"),
            reply_kind="agent_query_history",
            report_saved=report_saved,
        )

    async def _try_historical_update(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at: datetime,
        raw_input: str,
        source: str,
        plan: ActionPlan,
        meta: dict[str, Any],
    ) -> AgentExecutionResult | None:
        action = next((item for item in plan.actions if item.type == "update_historical_report"), None)
        if action is None:
            return None
        if action.requires_confirmation:
            return None
        if action.source in {"delete_items", "clear_field"}:
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message="历史日报不能删除。我可以帮你查看该日报，或基于历史日报复制一份作为今日草稿。",
                reply_kind="agent_historical_delete_blocked",
            )
        field = _action_field(action)
        plan_pending = plan.pending_interaction_to_set.model_dump() if plan.pending_interaction_to_set else None
        cursor = normalize_edit_cursor(plan_pending) or normalize_edit_cursor(_historical_context_pending(existing))
        cursor_date = cursor_target_date(cursor)
        target_date = _resolve_target_date(cursor_date or action.target_date, report_date)
        target_report = await get_report(session, user.id, target_date)
        if target_report is None:
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message=f"我没有查到 {target_date.isoformat()} 的日报记录，不能修改。",
                reply_kind="agent_historical_update_not_found",
            )
        working_report = cursor_report(cursor) if cursor_date == target_date.isoformat() else {}
        today_work = list(working_report.get("today_work") or target_report.today_work or [])
        problems = list(working_report.get("problems") or target_report.problems or [])
        tomorrow_plan = list(working_report.get("tomorrow_plan") or target_report.tomorrow_plan or [])
        values_by_field = {
            "today_work": today_work,
            "problems": problems,
            "tomorrow_plan": tomorrow_plan,
        }
        if action.source == "replace_report":
            replacement_report = _report_from_action_reference(action)
            if replacement_report is None:
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message=f"我没能解析出要替换到 {target_date.isoformat()} 日报的完整内容，先不改。",
                    reply_kind="agent_historical_replace_report_missing_content",
                )
            today_work = replacement_report["today_work"]
            problems = replacement_report["problems"]
            tomorrow_plan = replacement_report["tomorrow_plan"]
        else:
            if field not in REPORT_FIELDS:
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message="没有定位到要修改的历史日报栏目，请重新说明是今日工作、问题/风险还是明日计划。",
                    reply_kind="agent_historical_update_missing_field",
                )
            unsafe_message = _unsafe_report_content_write_message(action, raw_input)
            if unsafe_message:
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message=unsafe_message,
                    reply_kind="agent_unsafe_operation_phrase",
                )
            updated_values = _apply_historical_update_action(values_by_field[field], action)
            if updated_values is None:
                return self._no_write(
                    existing=existing,
                    report_date=report_date,
                    message=f"我没能在 {target_date.isoformat()} 日报的“{_field_label(field)}”里定位到要修改的内容，先不改。请重新说明具体内容。",
                    reply_kind="agent_historical_update_not_found_in_field",
                )
            if field == "today_work":
                today_work = updated_values
            elif field == "problems":
                problems = updated_values
            elif field == "tomorrow_plan":
                tomorrow_plan = updated_values
        if not (today_work or problems or tomorrow_plan):
            return self._no_write(
                existing=existing,
                report_date=report_date,
                message=f"替换后的 {target_date.isoformat()} 日报为空，我先不改。",
                reply_kind="agent_historical_replace_report_empty",
            )
        updated_target = await upsert_daily_report(
            session,
            user=user,
            report_date=target_date,
            raw_input=raw_input,
            source=source,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion=getattr(target_report, "emotion", "") or "",
            completeness_score=float(getattr(target_report, "completeness_score", 0.0) or 0.0),
            status=getattr(target_report, "status", None) or STATUS_COLLECTING,
            section_status=dict(getattr(target_report, "section_status", {}) or {}),
            llm_model=str(meta.get("model") or "report-agent"),
            llm_payload={"report_agent": plan.model_dump(), "meta": meta},
            received_at=received_at,
            confirmation_type=getattr(target_report, "confirmation_type", None) or CONFIRMATION_NONE,
            confirmed_by_user=bool(getattr(target_report, "confirmed_by_user", False)),
            quality_warning=getattr(target_report, "quality_warning", None),
            last_modified_by_user=True,
            last_modified_at=received_at,
            pending_confirmation_at=getattr(target_report, "pending_confirmation_at", None),
            auto_submit_at=getattr(target_report, "auto_submit_at", None),
            replace_sections=True,
        )
        state_report = existing
        if existing is not None:
            next_pending = _historical_pending_after_update(
                action,
                updated_target,
                target_date,
                base_pending=plan_pending,
            )
            state_report = await self._save_pending_interaction_state(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                raw_input=raw_input,
                source=source,
                pending_interaction=next_pending,
                plan=plan,
                meta=meta,
            )
        if action.source == "replace_report":
            message = (
                f"已整体替换 {target_date.isoformat()} 日报。\n\n"
                "修改后的日报：\n"
                f"{_format_report(updated_target, target_date)}"
            )
        else:
            label = _field_label(field)
            message = (
                f"已更新 {target_date.isoformat()} 日报的“{label}”。\n\n"
                "修改后的日报：\n"
                f"{_format_report(updated_target, target_date)}"
            )
        return AgentExecutionResult(
            report=state_report,
            structured=StructuredDailyReport(),
            missing_sections=_missing_sections(state_report),
            message=message,
            reply_kind="agent_historical_update",
            report_saved=True,
        )

    async def _save_pending_interaction(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at: datetime,
        raw_input: str,
        source: str,
        plan: ActionPlan,
        meta: dict[str, Any],
    ) -> DailyReport:
        pending_interaction = plan.pending_interaction_to_set.model_dump() if plan.pending_interaction_to_set else {}
        return await self._save_pending_interaction_state(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            raw_input=raw_input,
            source=source,
            pending_interaction=pending_interaction,
            plan=plan,
            meta=meta,
        )

    async def _save_pending_interaction_state(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at: datetime,
        raw_input: str,
        source: str,
        pending_interaction: dict[str, Any] | None,
        plan: ActionPlan,
        meta: dict[str, Any],
        recent_report_context: dict[str, Any] | None = None,
    ) -> DailyReport:
        section_status = _current_section_status(existing)
        if recent_report_context:
            section_status[RECENT_REPORT_CONTEXT_KEY] = recent_report_context
        if pending_interaction:
            pending_interaction = _with_pending_scope(pending_interaction, user=user, existing=existing, report_date=report_date)
            section_status["_pending_interaction"] = _normalize_pending_interaction_for_existing(pending_interaction, existing)
        else:
            section_status.pop("_pending_interaction", None)
        return await self._save_report(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            raw_input=raw_input,
            source=source,
            today_work=list(getattr(existing, "today_work", []) or []),
            problems=list(getattr(existing, "problems", []) or []),
            tomorrow_plan=list(getattr(existing, "tomorrow_plan", []) or []),
            section_status=section_status,
            status=getattr(existing, "status", None) or STATUS_COLLECTING,
            completeness_score=float(getattr(existing, "completeness_score", 0.0) or 0.0),
            confirmation_type=(getattr(existing, "confirmation_type", None) or CONFIRMATION_NONE),
            confirmed_by_user=bool(getattr(existing, "confirmed_by_user", False)),
            meta=meta,
            plan=plan,
        )

    async def _save_display_context_state(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport,
        report_date: date,
        received_at: datetime,
        raw_input: str,
        source: str,
        plan: ActionPlan,
        meta: dict[str, Any],
    ) -> DailyReport:
        section_status = _current_section_status(existing)
        section_status[LAST_DISPLAY_CONTEXT_KEY] = _last_display_context_payload(
            existing=existing,
            report_date=report_date,
            displayed_at=received_at,
            today_work=list(getattr(existing, "today_work", []) or []),
            problems=list(getattr(existing, "problems", []) or []),
            tomorrow_plan=list(getattr(existing, "tomorrow_plan", []) or []),
        )
        section_status[RECENT_REPORT_CONTEXT_KEY] = _recent_report_context_payload(
            viewed_report=existing,
            viewed_report_date=report_date,
            viewed_at=received_at,
            viewer=user,
            owner="self",
            field="all",
        )
        return await self._save_report(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            raw_input=raw_input,
            source=source,
            today_work=list(getattr(existing, "today_work", []) or []),
            problems=list(getattr(existing, "problems", []) or []),
            tomorrow_plan=list(getattr(existing, "tomorrow_plan", []) or []),
            section_status=section_status,
            status=getattr(existing, "status", None) or STATUS_COLLECTING,
            completeness_score=float(getattr(existing, "completeness_score", 0.0) or 0.0),
            confirmation_type=(getattr(existing, "confirmation_type", None) or CONFIRMATION_NONE),
            confirmed_by_user=bool(getattr(existing, "confirmed_by_user", False)),
            meta=meta,
            plan=plan,
        )

    async def _save_report(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at: datetime,
        raw_input: str,
        source: str,
        today_work: list[str],
        problems: list[str],
        tomorrow_plan: list[str],
        section_status: dict[str, Any],
        status: str,
        completeness_score: float,
        confirmation_type: str,
        confirmed_by_user: bool,
        meta: dict[str, Any],
        plan: ActionPlan,
    ) -> DailyReport:
        report = await upsert_daily_report(
            session,
            user=user,
            report_date=report_date,
            raw_input=raw_input,
            source=source,
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion="",
            completeness_score=completeness_score,
            status=status,
            section_status=section_status,
            llm_model=str(meta.get("model") or "report-agent"),
            llm_payload={
                "report_agent": plan.model_dump(),
                "meta": meta,
            },
            received_at=received_at,
            confirmation_type=confirmation_type,
            confirmed_by_user=confirmed_by_user,
            quality_warning=getattr(existing, "quality_warning", None) if existing else None,
            last_modified_by_user=existing is not None,
            last_modified_at=received_at if existing is not None else None,
            pending_confirmation_at=received_at if status == STATUS_PENDING_CONFIRMATION else None,
            auto_submit_at=getattr(existing, "auto_submit_at", None) if existing else None,
            replace_sections=True,
        )
        if status != STATUS_COMPLETED and hasattr(report, "submitted_at"):
            report.submitted_at = None
        return report

    async def _save_last_unwritten_candidate_if_useful(
        self,
        session: AsyncSession,
        *,
        user: User,
        existing: DailyReport | None,
        report_date: date,
        received_at: datetime,
        raw_input: str,
        source: str,
        plan: ActionPlan,
        meta: dict[str, Any],
    ) -> DailyReport | None:
        if existing is None or not _should_store_last_unwritten_candidate(plan, raw_input):
            return None
        section_status = _current_section_status(existing)
        section_status[LAST_UNWRITTEN_CANDIDATE_KEY] = {
            "text": str(raw_input or "").strip(),
            "created_at": received_at.isoformat() if hasattr(received_at, "isoformat") else "",
            "intent": plan.intent,
        }
        return await self._save_report(
            session,
            user=user,
            existing=existing,
            report_date=report_date,
            received_at=received_at,
            raw_input=raw_input,
            source=source,
            today_work=list(getattr(existing, "today_work", []) or []),
            problems=list(getattr(existing, "problems", []) or []),
            tomorrow_plan=list(getattr(existing, "tomorrow_plan", []) or []),
            section_status=section_status,
            status=getattr(existing, "status", None) or STATUS_COLLECTING,
            completeness_score=float(getattr(existing, "completeness_score", 0.0) or 0.0),
            confirmation_type=(getattr(existing, "confirmation_type", None) or CONFIRMATION_NONE),
            confirmed_by_user=bool(getattr(existing, "confirmed_by_user", False)),
            meta=meta,
            plan=plan,
        )

    def _no_write(
        self,
        *,
        existing: DailyReport | None,
        report_date: date,
        message: str,
        reply_kind: str,
        report_saved: bool = False,
    ) -> AgentExecutionResult:
        return AgentExecutionResult(
            report=existing,
            structured=StructuredDailyReport(),
            missing_sections=_missing_sections(existing),
            message=_safe_no_write_message(message, reply_kind=reply_kind),
            reply_kind=reply_kind,
            report_saved=report_saved,
        )


def _is_after_daily_report_lock(received_at: datetime, report_date: date) -> bool:
    lock_date = report_date + timedelta(days=1)
    return (
        getattr(received_at, "date", lambda: report_date)(),
        int(getattr(received_at, "hour", 0) or 0),
        int(getattr(received_at, "minute", 0) or 0),
        int(getattr(received_at, "second", 0) or 0),
        int(getattr(received_at, "microsecond", 0) or 0),
    ) >= (lock_date, DAILY_REPORT_LOCK_HOUR, 0, 0, 0)


def _daily_report_lock_message(report_date: date) -> str:
    lock_date = report_date + timedelta(days=1)
    return (
        f"{report_date.isoformat()} 的日报已在 {lock_date.isoformat()} 09:00 形成，之后不能修改、删除、撤回或提交。"
        "你可以继续查看日报。"
    )


def _plan_would_change_report_state(plan: ActionPlan) -> bool:
    if plan.should_write or plan.clear_pending_interaction or plan.pending_interaction_to_set is not None:
        return True
    if plan.intent == "confirm_submit":
        return True
    return any(action.type in DAILY_REPORT_LOCKED_ACTIONS for action in plan.actions)


def _current_lists(existing: DailyReport | None) -> tuple[list[str], list[str], list[str]]:
    if existing is None:
        return [], [], []
    return list(existing.today_work or []), list(existing.problems or []), list(existing.tomorrow_plan or [])


def _current_section_status(existing: DailyReport | None) -> dict[str, Any]:
    return dict(getattr(existing, "section_status", {}) or {})


def _coerce_plan_to_historical_context(plan: ActionPlan, existing: DailyReport | None, *, raw_input: str) -> ActionPlan:
    pending = _historical_context_pending(existing)
    if pending is None or _mentions_current_day_report(raw_input):
        return plan
    context = pending.get("context") if isinstance(pending.get("context"), dict) else {}
    edit_cursor = normalize_edit_cursor(pending)
    target_date = cursor_target_date(edit_cursor) or str(context.get("target_date") or pending.get("target_date") or "")
    if not target_date:
        return plan
    focus_section = cursor_focused_section(edit_cursor)
    if focus_section not in REPORT_FIELDS:
        focus_section = str(context.get("focus_section") or pending.get("target_field") or "none")
    if edit_cursor is not None:
        context = dict(context)
        context["edit_cursor"] = edit_cursor

    if plan.pending_interaction_to_set and not plan.should_write and not plan.clear_pending_interaction:
        selected_field = plan.pending_interaction_to_set.target_field
        if selected_field in REPORT_FIELDS:
            return _historical_focus_plan(
                plan,
                target_date=target_date,
                field=selected_field,
                context=context,
                reason="Executor redirected pending field selection to historical report context.",
            )

    converted_actions: list[AgentAction] = []
    for action in plan.actions:
        resolution = _resolve_historical_cursor_action(
            action,
            target_date=target_date,
            fallback_field=focus_section,
            context=context,
        )
        if resolution.error:
            return _historical_cursor_blocked_plan(
                plan,
                target_date=target_date,
                field=focus_section,
                context=context,
                message=resolution.error,
                reason="Executor rejected a historical edit action that conflicted with the active edit cursor.",
            )
        if resolution.action is not None:
            converted_actions.append(resolution.action)
        elif action.type not in {"ask_clarification", "no_op"}:
            converted_actions.append(action)
    if converted_actions and any(action.type == "update_historical_report" for action in converted_actions):
        payload = plan.model_dump()
        payload["intent"] = "edit_draft"
        payload["should_write"] = True
        payload["actions"] = [action.model_dump() for action in converted_actions]
        payload["pending_interaction_to_set"] = None
        payload["reason"] = (plan.reason or "") + " Executor resolved relative edit actions against the active edit cursor."
        return ActionPlan.model_validate(payload)

    if not plan.should_write and focus_section in REPORT_FIELDS and _asks_current_or_historical_again(plan):
        return _historical_focus_plan(
            plan,
            target_date=target_date,
            field=focus_section,
            context=context,
            reason="Executor kept existing historical report focus instead of asking current-vs-history again.",
        )
    return plan


def _coerce_plan_to_current_context(plan: ActionPlan, existing: DailyReport | None, *, raw_input: str) -> ActionPlan:
    pending = _current_context_pending(existing)
    if pending is None or _mentions_previous_day_report(raw_input):
        return plan
    context = pending.get("context") if isinstance(pending.get("context"), dict) else {}
    edit_cursor = normalize_edit_cursor(pending)
    focus_section = cursor_focused_section(edit_cursor)
    if focus_section not in REPORT_FIELDS:
        focus_section = str(context.get("focus_section") or pending.get("target_field") or "none")
    if edit_cursor is not None:
        context = dict(context)
        context["edit_cursor"] = edit_cursor

    if plan.pending_interaction_to_set and not plan.should_write and not plan.clear_pending_interaction:
        selected_field = plan.pending_interaction_to_set.target_field
        if selected_field in REPORT_FIELDS:
            return _current_focus_plan(
                plan,
                field=selected_field,
                context=context,
                reason="Executor redirected pending field selection to current report context.",
            )

    converted_actions: list[AgentAction] = []
    for action in plan.actions:
        resolution = _resolve_current_cursor_action(
            action,
            fallback_field=focus_section,
            context=context,
        )
        if resolution.error:
            return _current_cursor_blocked_plan(
                plan,
                field=focus_section,
                context=context,
                message=resolution.error,
                reason="Executor rejected a current edit action that conflicted with the active edit cursor.",
            )
        if resolution.action is not None:
            converted_actions.append(resolution.action)
        elif action.type not in {"ask_clarification", "no_op"}:
            converted_actions.append(action)
    if converted_actions and any(action.type in _CURRENT_MUTATION_TYPES for action in converted_actions):
        payload = plan.model_dump()
        payload["intent"] = "edit_draft"
        payload["should_write"] = True
        payload["actions"] = [action.model_dump() for action in converted_actions]
        payload["pending_interaction_to_set"] = None
        payload["reason"] = (plan.reason or "") + " Executor resolved relative edit actions against the active current edit cursor."
        return ActionPlan.model_validate(payload)

    if not plan.should_write and focus_section in REPORT_FIELDS and _asks_current_or_historical_again(plan):
        return _current_focus_plan(
            plan,
            field=focus_section,
            context=context,
            reason="Executor kept existing current report focus instead of asking current-vs-history again.",
        )
    return plan




def _coerce_plan_to_explicit_ordinal_rewrite(plan: ActionPlan, existing: DailyReport | None, *, raw_input: str) -> ActionPlan:
    if existing is None or _historical_context_pending(existing) is not None:
        return plan
    parsed = _parse_explicit_ordinal_rewrite_request(raw_input)
    if parsed is None:
        return plan
    item_index, new_value = parsed
    if not new_value:
        return plan
    if any(action.type in {"update_historical_report", "query_history", "submit_report", "unsubmit_report", "delete_item", "merge_items", "move_item", "clear_field", "clear_all"} for action in plan.actions):
        return plan
    field = _ordinal_rewrite_target_field(raw_input, plan, existing, item_index)
    if field not in REPORT_FIELDS:
        return plan
    values = _values_for_existing(existing, field)
    if not (1 <= item_index <= len(values)):
        return plan
    old_value = values[item_index - 1]
    payload = plan.model_dump()
    payload.update(
        {
            "intent": "edit_draft",
            "should_write": True,
            "actions": [
                AgentAction(
                    type="replace_text",
                    field=field,
                    old_value=old_value,
                    new_value=new_value,
                    target_item_index=item_index,
                    source="executor_explicit_ordinal_rewrite_guard",
                ).model_dump()
            ],
            "reason": ((plan.reason or "") + " Executor constrained explicit ordinal rewrite to a single item.").strip(),
        }
    )
    return ActionPlan.model_validate(payload)


def _coerce_replacement_intent_to_full_field_replacement(plan: ActionPlan, existing: DailyReport | None, *, raw_input: str) -> ActionPlan:
    if existing is None or not _has_report_replacement_intent(raw_input):
        return plan
    if any(action.type in {"query_history", "submit_report", "unsubmit_report", "delete_item", "merge_items", "move_item", "clear_field", "clear_all", "restore_snapshot", "update_historical_report"} for action in plan.actions):
        return plan
    writable_actions = [
        action
        for action in plan.actions
        if action.type in {"append_items", "replace_field", "polish_items"} and action.field in REPORT_FIELDS
    ]
    if not writable_actions:
        return plan
    raw_fields = _extract_replacement_fields_from_raw(raw_input)
    actions: list[AgentAction] = []
    touched: set[str] = set()
    for action in plan.actions:
        if action.type in {"append_items", "replace_field", "polish_items"} and action.field in REPORT_FIELDS:
            payload = action.model_dump()
            payload["type"] = "replace_field"
            if raw_fields.get(action.field):
                payload["items"] = raw_fields[action.field]
            payload["source"] = payload.get("source") or "executor_replacement_intent_guard"
            actions.append(AgentAction.model_validate(payload))
            touched.add(action.field)
        else:
            actions.append(action)
    if "problems" not in touched and (raw_fields.get("problems") or _raw_mentions_no_problem(raw_input)):
        actions.append(
            AgentAction(
                type="replace_field",
                field="problems",
                items=raw_fields.get("problems") or ["暂无明显问题"],
                source="executor_replacement_intent_guard",
            )
        )
        touched.add("problems")
    payload = plan.model_dump()
    payload.update(
        {
            "intent": "edit_draft" if plan.intent in {"fill_report", "unclear"} else plan.intent,
            "should_write": True,
            "actions": [action.model_dump() for action in actions],
            "clear_pending_interaction": True,
            "reason": ((plan.reason or "") + " Executor treated explicit replacement wording as field replacement, not append.").strip(),
        }
    )
    return ActionPlan.model_validate(payload)


def _extract_replacement_fields_from_raw(raw_input: str) -> dict[str, list[str]]:
    text = _strip_replacement_prefix(raw_input)
    plan_text = ""
    plan_match = re.search(r"(明天|明日|明儿).*$", text)
    if plan_match:
        plan_text = plan_match.group(0)
        text = text[: plan_match.start()]
    problem_text = ""
    if _raw_mentions_no_problem(text):
        problem_text = "暂无明显问题"
        text = re.sub(
            r"(然后|嗯|呃|额|啊)?[，,。；;、\s]*(没事|没啥事|没什么事|没有事|没问题|没啥问题|没什么问题|没有问题|问题没有|问题没|暂无问题|无问题|暂无明显问题|无明显问题|没风险|没什么风险|没有风险|风险没有|风险没|暂无风险|无风险|无明显风险)",
            "",
            text,
        )
    today_text = _clean_report_text_shell(text)
    today_text = re.sub(r"^今天(吧|主要|就是|主要就是)?[，,。；;、\s]*", "", today_text).strip(" ，,。；;")
    fields = {"today_work": [], "problems": [], "tomorrow_plan": []}
    if today_text:
        fields["today_work"] = _clean_business_values("today_work", [today_text])
    if problem_text:
        fields["problems"] = [problem_text]
    if plan_text:
        fields["tomorrow_plan"] = _clean_business_values("tomorrow_plan", [plan_text])
    return fields


def _strip_replacement_prefix(raw_input: str) -> str:
    cleaned = str(raw_input or "").strip()
    patterns = (
        r"^(?:前面那个不算|前面那个不要|前面不算|前面的不算|刚才那个不算|刚才的不算|刚才不算|前面别记了|前面别记|前面是测试)[，,。；;：:\s]*",
        r"^(?:我重新说|重新说|我重说|重说一下|我重新填|重新填|按这个来|以这个为准|算了算了|跟你实话实说吧?|不对我重新说?)[，,。；;：:\s]*",
    )
    previous = None
    while previous != cleaned:
        previous = cleaned
        for pattern in patterns:
            cleaned = re.sub(pattern, "", cleaned).strip()
    cleaned = re.sub(r"^.*?(以这个为准|按这个来)[，,。；;：:\s]*", "", cleaned)
    return cleaned.strip()


def _has_report_replacement_intent(raw_input: str) -> bool:
    compact = _compact(raw_input)
    return any(
        token in compact
        for token in (
            "刚才那个不算",
            "刚才的不算",
            "前面是测试",
            "前面那个是测试",
            "前面不算",
            "前面的不算",
            "重新说",
            "重新来",
            "重说一下",
            "我重说",
            "我重新说",
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
        )
    )


def _raw_mentions_no_problem(raw_input: str) -> bool:
    compact = _compact(raw_input)
    return any(
        token in compact
        for token in (
            "没问题",
            "没啥问题",
            "没什么问题",
            "没有问题",
            "问题没有",
            "暂无问题",
            "无问题",
            "暂无明显问题",
            "无明显问题",
            "没风险",
            "没有风险",
            "暂无风险",
            "无风险",
        )
    )


def _parse_explicit_ordinal_rewrite_request(raw_input: str) -> tuple[int, str] | None:
    text = str(raw_input or "").strip()
    if not text:
        return None
    token = r"\d{1,3}|[一二两三四五六七八九十]{1,3}"
    match = re.search(rf"第?\s*({token})\s*(?:条|项|个)?\s*.*?(?:改成|改为|修改为|更新为|换成|更正为)\s*(.+)$", text)
    if not match:
        return None
    item_index = _parse_report_item_number(match.group(1))
    new_value = str(match.group(2) or "").strip(" ：:，,。；;、\n\r\t")
    if item_index is None or not new_value:
        return None
    return item_index, new_value


def _parse_report_item_number(value: str) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    numerals = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if raw in numerals:
        return numerals[raw]
    if raw.startswith("十") and len(raw) == 2 and raw[1] in numerals:
        return 10 + numerals[raw[1]]
    if raw.endswith("十") and len(raw) == 2 and raw[0] in numerals:
        return numerals[raw[0]] * 10
    if "十" in raw:
        left, right = raw.split("十", 1)
        left_value = numerals.get(left, 1 if not left else 0)
        right_value = numerals.get(right, 0) if right else 0
        value = left_value * 10 + right_value
        return value if value > 0 else None
    return None


def _ordinal_rewrite_target_field(raw_input: str, plan: ActionPlan, existing: DailyReport, item_index: int) -> str | None:
    explicit_field = _field_from_text(raw_input)
    if explicit_field in REPORT_FIELDS and item_index <= len(_values_for_existing(existing, explicit_field)):
        return explicit_field

    action_fields = {
        _action_field(action)
        for action in plan.actions
        if _action_field(action) in REPORT_FIELDS and item_index <= len(_values_for_existing(existing, _action_field(action)))
    }
    if len(action_fields) == 1:
        return next(iter(action_fields))

    if item_index <= len(_values_for_existing(existing, "today_work")):
        return "today_work"

    candidates = [
        field
        for field in REPORT_FIELDS
        if item_index <= len(_values_for_existing(existing, field))
    ]
    return candidates[0] if len(candidates) == 1 else None

def _historical_context_pending(existing: DailyReport | None) -> dict[str, Any] | None:
    pending = _current_section_status(existing).get("_pending_interaction")
    if not isinstance(pending, dict):
        return None
    if pending.get("type") not in {"historical_report_edit_flow", "awaiting_dated_report_action"}:
        return None
    return with_edit_cursor(pending)


def _current_context_pending(existing: DailyReport | None) -> dict[str, Any] | None:
    pending = _current_section_status(existing).get("_pending_interaction")
    if not isinstance(pending, dict):
        return None
    if pending.get("type") != "current_report_edit_flow":
        return None
    return with_edit_cursor(pending)


def _historical_focus_plan(
    plan: ActionPlan,
    *,
    target_date: str,
    field: str,
    context: dict[str, Any],
    reason: str,
) -> ActionPlan:
    next_context = dict(context)
    next_context.update(
        {
            "target_date": target_date,
            "requested_action": next_context.get("requested_action") or "modify_report",
            "stage": "awaiting_field_edit_content",
            "focus_section": field,
            "edit_cursor": build_historical_edit_cursor(
                target_date=target_date,
                current_report=next_context.get("current_report") if isinstance(next_context.get("current_report"), dict) else cursor_report(normalize_edit_cursor({"type": "historical_report_edit_flow", "context": next_context})),
                focused_section=field,
            ),
        }
    )
    payload = plan.model_dump()
    payload.update(
        {
            "intent": "edit_draft",
            "should_write": False,
            "clear_pending_interaction": False,
            "reply_to_user": _historical_focus_reply(field, next_context),
            "clarification_question": "",
            "pending_interaction_to_set": PendingInteractionPlan(
                type="historical_report_edit_flow",
                operation="modify_report",
                target_field=field,
                context=next_context,
            ).model_dump(),
            "actions": [],
            "reason": (plan.reason or "") + f" {reason}",
        }
    )
    return ActionPlan.model_validate(payload)


def _current_focus_plan(
    plan: ActionPlan,
    *,
    field: str,
    context: dict[str, Any],
    reason: str,
) -> ActionPlan:
    target_date = str(context.get("target_date") or "")
    next_context = dict(context)
    next_context.update(
        {
            "target_date": target_date,
            "requested_action": next_context.get("requested_action") or "modify_report",
            "stage": "awaiting_field_edit_content",
            "focus_section": field,
            "edit_cursor": build_current_edit_cursor(
                target_date=target_date,
                current_report=next_context.get("current_report") if isinstance(next_context.get("current_report"), dict) else cursor_report(normalize_edit_cursor({"type": "current_report_edit_flow", "context": next_context})),
                focused_section=field,
            ),
        }
    )
    payload = plan.model_dump()
    payload.update(
        {
            "intent": "edit_draft",
            "should_write": False,
            "clear_pending_interaction": False,
            "reply_to_user": _current_focus_reply(field, next_context),
            "clarification_question": "",
            "pending_interaction_to_set": PendingInteractionPlan(
                type="current_report_edit_flow",
                operation="modify_report",
                target_field=field,
                context=next_context,
            ).model_dump(),
            "actions": [],
            "reason": (plan.reason or "") + f" {reason}",
        }
    )
    return ActionPlan.model_validate(payload)


def _historical_cursor_blocked_plan(
    plan: ActionPlan,
    *,
    target_date: str,
    field: str,
    context: dict[str, Any],
    message: str,
    reason: str,
) -> ActionPlan:
    next_context = dict(context)
    cursor = normalize_edit_cursor({"type": "historical_report_edit_flow", "context": next_context})
    if cursor is not None:
        next_context["edit_cursor"] = cursor
        next_context["target_date"] = cursor_target_date(cursor)
        next_context["current_report"] = cursor.get("active_draft_snapshot") or next_context.get("current_report") or {}
        cursor_field = cursor_focused_section(cursor)
        if cursor_field in REPORT_FIELDS:
            next_context["focus_section"] = cursor_field
            field = cursor_field

    payload = plan.model_dump()
    payload.update(
        {
            "intent": "edit_draft",
            "should_write": False,
            "clear_pending_interaction": False,
            "reply_to_user": message,
            "clarification_question": "",
            "pending_interaction_to_set": PendingInteractionPlan(
                type="historical_report_edit_flow",
                operation="modify_report",
                target_field=field if field in REPORT_FIELDS else "none",
                context=next_context,
            ).model_dump(),
            "actions": [],
            "reason": (plan.reason or "") + f" {reason}",
        }
    )
    return ActionPlan.model_validate(payload)


def _current_cursor_blocked_plan(
    plan: ActionPlan,
    *,
    field: str,
    context: dict[str, Any],
    message: str,
    reason: str,
) -> ActionPlan:
    next_context = dict(context)
    cursor = normalize_edit_cursor({"type": "current_report_edit_flow", "context": next_context})
    if cursor is not None:
        next_context["edit_cursor"] = cursor
        next_context["target_date"] = cursor_target_date(cursor)
        next_context["current_report"] = cursor.get("active_draft_snapshot") or next_context.get("current_report") or {}
        cursor_field = cursor_focused_section(cursor)
        if cursor_field in REPORT_FIELDS:
            next_context["focus_section"] = cursor_field
            field = cursor_field

    payload = plan.model_dump()
    payload.update(
        {
            "intent": "edit_draft",
            "should_write": False,
            "clear_pending_interaction": False,
            "reply_to_user": message,
            "clarification_question": "",
            "pending_interaction_to_set": PendingInteractionPlan(
                type="current_report_edit_flow",
                operation="modify_report",
                target_field=field if field in REPORT_FIELDS else "none",
                context=next_context,
            ).model_dump(),
            "actions": [],
            "reason": (plan.reason or "") + f" {reason}",
        }
    )
    return ActionPlan.model_validate(payload)


def _historical_focus_reply(field: str, context: dict[str, Any]) -> str:
    label = _field_label(field)
    current = _historical_field_text_from_context(field, context)
    return (
        f"已经定位到 {context.get('target_date')} 日报的“{label}”。当前内容：\n\n"
        f"{current}\n\n"
        "请直接告诉我要怎么改。"
    )


def _current_focus_reply(field: str, context: dict[str, Any]) -> str:
    label = _field_label(field)
    current = _historical_field_text_from_context(field, context)
    return (
        f"已经定位到今天日报的“{label}”。当前内容：\n\n"
        f"{current}\n\n"
        "请直接告诉我要怎么改。"
    )


def _historical_field_text_from_context(field: str, context: dict[str, Any]) -> str:
    report = context.get("current_report") if isinstance(context.get("current_report"), dict) else {}
    values = report.get(field)
    if not isinstance(values, list):
        values = []
    cleaned = [str(item).strip() for item in values if str(item or "").strip()]
    if not cleaned:
        return "暂无"
    if len(cleaned) == 1:
        return cleaned[0]
    return "\n".join(f"{index}. {item}" for index, item in enumerate(cleaned, start=1))


def _resolve_historical_cursor_action(
    action: AgentAction,
    *,
    target_date: str,
    fallback_field: str,
    context: dict[str, Any],
) -> HistoricalActionResolution:
    if action.type in {"ask_clarification", "no_op"}:
        return HistoricalActionResolution()

    target_conflict = _historical_target_conflict(action, cursor_target_date=target_date)
    if target_conflict:
        return HistoricalActionResolution(error=target_conflict)

    field_resolution = _resolve_historical_cursor_field(action, fallback_field=fallback_field, context=context)
    if field_resolution.error:
        return HistoricalActionResolution(error=field_resolution.error)
    field = field_resolution.field
    if action.type == "update_historical_report" and action.source == "replace_report":
        payload = action.model_dump()
        payload.update(
            {
                "type": "update_historical_report",
                "field": "none",
                "target_date": target_date,
                "source": "replace_report",
                "requires_confirmation": True,
                "confirmation_message": action.confirmation_message or _confirmation_message_for_action(action, None),
            }
        )
        return HistoricalActionResolution(action=AgentAction.model_validate(payload))
    if field not in REPORT_FIELDS:
        if action.type in _HISTORICAL_MUTATION_TYPES:
            return HistoricalActionResolution(
                error=f"我还在修改 {target_date} 的日报，但没有定位到具体栏目。请先说要改今日工作、问题/风险还是明日计划。"
            )
        return HistoricalActionResolution()

    source_by_type = {
        "append_items": "append_items",
        "replace_field": "replace_field",
        "polish_items": "replace_field",
        "replace_text": "replace_text",
        "merge_items": "merge_items",
        "delete_item": "delete_items",
        "clear_field": "clear_field",
        "update_historical_report": action.source,
    }
    source = source_by_type.get(action.type)
    if source is None:
        return HistoricalActionResolution()
    payload = action.model_dump()
    message_action = action
    if field_resolution.item_indices is not None:
        message_payload = action.model_dump()
        message_payload["item_indices"] = field_resolution.item_indices
        message_action = AgentAction.model_validate(message_payload)
    payload.update(
        {
            "type": "update_historical_report",
            "field": field,
            "item_indices": field_resolution.item_indices if field_resolution.item_indices is not None else action.item_indices,
            "target_date": target_date,
            "source": source,
            "requires_confirmation": True,
            "confirmation_message": action.confirmation_message
            or _historical_confirmation_message(field, target_date=target_date, source=source, action=message_action),
        }
    )
    return HistoricalActionResolution(action=AgentAction.model_validate(payload))


def _resolve_current_cursor_action(
    action: AgentAction,
    *,
    fallback_field: str,
    context: dict[str, Any],
) -> HistoricalActionResolution:
    if action.type in {"ask_clarification", "no_op"}:
        return HistoricalActionResolution()
    if action.type not in _CURRENT_MUTATION_TYPES:
        return HistoricalActionResolution()
    if action.type == "clear_all":
        payload = action.model_dump()
        payload["requires_confirmation"] = True
        payload["confirmation_message"] = action.confirmation_message or _current_confirmation_message("none", action=AgentAction.model_validate(payload))
        return HistoricalActionResolution(action=AgentAction.model_validate(payload))

    field_resolution = _resolve_current_cursor_field(action, fallback_field=fallback_field, context=context)
    if field_resolution.error:
        return HistoricalActionResolution(error=field_resolution.error)
    field = field_resolution.field
    if field not in REPORT_FIELDS:
        return HistoricalActionResolution(
            error="我还在修改今天的日报，但没有定位到具体栏目。请先说要改今日工作、问题/风险还是明日计划。"
        )

    payload = action.model_dump()
    payload["field"] = field
    if field_resolution.item_indices is not None:
        payload["item_indices"] = field_resolution.item_indices
    if action.type != "clear_all":
        payload["requires_confirmation"] = False
        payload["confirmation_message"] = ""
    return HistoricalActionResolution(action=AgentAction.model_validate(payload))


_HISTORICAL_MUTATION_TYPES = {
    "append_items",
    "replace_field",
    "polish_items",
    "replace_text",
    "merge_items",
    "delete_item",
    "clear_field",
    "update_historical_report",
}

_CURRENT_MUTATION_TYPES = {
    "append_items",
    "replace_field",
    "polish_items",
    "replace_text",
    "merge_items",
    "delete_item",
    "clear_field",
    "clear_all",
    "move_item",
}

_CURRENT_DESTRUCTIVE_TYPES = {
    "replace_field",
    "polish_items",
    "replace_text",
    "merge_items",
    "delete_item",
    "clear_field",
    "clear_all",
    "move_item",
}


@dataclass(frozen=True)
class HistoricalFieldResolution:
    field: str = "none"
    item_indices: list[int] | None = None
    error: str = ""


def _resolve_historical_cursor_field(action: AgentAction, *, fallback_field: str, context: dict[str, Any]) -> HistoricalFieldResolution:
    field = _action_field(action)
    if fallback_field in REPORT_FIELDS:
        if field in REPORT_FIELDS and field != fallback_field:
            return HistoricalFieldResolution(
                error=(
                    f"我现在锁定的是历史日报的“{_field_label(fallback_field)}”。"
                    f"这次动作却指向“{_field_label(field)}”，为避免改错，我先不执行。"
                    "如果要切换栏目，请先说“改今日工作/改问题/改明日计划”。"
                )
            )
        return HistoricalFieldResolution(field=fallback_field)
    if field in REPORT_FIELDS:
        return HistoricalFieldResolution(field=field)

    cursor = context.get("edit_cursor") if isinstance(context.get("edit_cursor"), dict) else None
    report = cursor_report(cursor)
    if action.type in {"delete_item", "replace_field", "polish_items", "merge_items"} and action.item_indices:
        resolved = _flat_index_resolution(report, action.item_indices)
        if resolved:
            return HistoricalFieldResolution(field=resolved[0], item_indices=resolved[1])
    if action.type == "replace_text" and action.old_value:
        resolved = _field_containing_text_in_report(report, action.old_value)
        if resolved:
            return HistoricalFieldResolution(field=resolved)
    if action.source_item_text:
        resolved = _field_containing_text_in_report(report, action.source_item_text)
        if resolved:
            return HistoricalFieldResolution(field=resolved)
    return HistoricalFieldResolution()


def _resolve_current_cursor_field(action: AgentAction, *, fallback_field: str, context: dict[str, Any]) -> HistoricalFieldResolution:
    field = _action_field(action)
    if fallback_field in REPORT_FIELDS:
        if field in REPORT_FIELDS and field != fallback_field:
            return HistoricalFieldResolution(
                error=(
                    f"我现在锁定的是今天日报的“{_field_label(fallback_field)}”。"
                    f"这次动作却指向“{_field_label(field)}”，为避免改错，我先不执行。"
                    "如果要切换栏目，请先说“改今日工作/改问题/改明日计划”。"
                )
            )
        return HistoricalFieldResolution(field=fallback_field)
    if field in REPORT_FIELDS:
        return HistoricalFieldResolution(field=field)

    cursor = context.get("edit_cursor") if isinstance(context.get("edit_cursor"), dict) else None
    report = cursor_report(cursor)
    if action.type in {"delete_item", "replace_field", "polish_items", "merge_items"} and action.item_indices:
        resolved = _flat_index_resolution(report, action.item_indices)
        if resolved:
            return HistoricalFieldResolution(field=resolved[0], item_indices=resolved[1])
    if action.type == "replace_text" and action.old_value:
        resolved = _field_containing_text_in_report(report, action.old_value)
        if resolved:
            return HistoricalFieldResolution(field=resolved)
    if action.source_item_text:
        resolved = _field_containing_text_in_report(report, action.source_item_text)
        if resolved:
            return HistoricalFieldResolution(field=resolved)
    return HistoricalFieldResolution()


def _historical_target_conflict(action: AgentAction, *, cursor_target_date: str) -> str:
    target = str(action.target_date or "").strip()
    if not target:
        return ""
    allowed = {cursor_target_date, "cursor.target_date", "target_date", "pending target_date", "pending_target_date"}
    if target in allowed:
        return ""
    return f"当前编辑游标锁定的是 {cursor_target_date} 日报，但动作目标日期是 {target}。为避免改错，我先不执行。"


def _field_for_flat_indices(report: dict[str, list[str]], item_indices: list[int]) -> str | None:
    resolved = _flat_index_resolution(report, item_indices)
    return resolved[0] if resolved is not None else None


def _flat_index_resolution(report: dict[str, list[str]], item_indices: list[int]) -> tuple[str, list[int]] | None:
    if not item_indices or any(index <= 0 for index in item_indices):
        return None
    flat: list[tuple[str, int]] = []
    for field in REPORT_FIELDS:
        for section_index, _item in enumerate(report.get(field, []), start=1):
            flat.append((field, section_index))
    if not flat or max(item_indices) > len(flat):
        return None
    selected = [flat[index - 1] for index in item_indices]
    selected_fields = {field for field, _index in selected}
    if len(selected_fields) != 1:
        return None
    return selected[0][0], [index for _field, index in selected]


def _field_containing_text_in_report(report: dict[str, list[str]], text: str) -> str | None:
    needle = _compact(text)
    if not needle:
        return None
    matches: list[str] = []
    for field in REPORT_FIELDS:
        for item in report.get(field, []):
            item_key = _compact(item)
            if item_key and (needle in item_key or item_key in needle):
                matches.append(field)
                break
    return matches[0] if len(matches) == 1 else None


def _historical_confirmation_message(field: str, *, target_date: str, source: str, action: AgentAction) -> str:
    label = _field_label(field)
    if source == "clear_field":
        return f"确认清空 {target_date} 日报的“{label}”吗？"
    if source == "delete_items" and action.item_indices:
        joined = "、".join(str(index) for index in action.item_indices)
        return f"你确认要删除 {target_date} 日报“{label}”第{joined}条吗？回复“确认”执行，回复“取消”保留。"
    if source == "merge_items" and action.item_indices:
        joined = "、".join(str(index) for index in action.item_indices)
        return f"确认把 {target_date} 日报“{label}”第{joined}条合并为一条吗？回复“确认”执行，回复“取消”保留。"
    if source == "replace_text" and action.old_value and action.new_value:
        return f"确认把 {target_date} 日报“{label}”里的“{action.old_value}”改成“{action.new_value}”吗？"
    if action.items:
        verb = "补充" if source == "append_items" else "改成"
        return f"确认把 {target_date} 日报的“{label}”{verb}“{'；'.join(action.items)}”吗？"
    return f"确认修改 {target_date} 日报的“{label}”吗？"


def _current_confirmation_message(field: str, *, action: AgentAction) -> str:
    label = _field_label(field)
    if action.type == "clear_all":
        return "确认清空当前日报内容吗？"
    if action.type == "clear_field":
        return f"确认清空今天日报的“{label}”吗？"
    if action.type == "delete_item":
        if action.item_indices:
            joined = "、".join(str(index) for index in action.item_indices)
            return f"你确认要删除今天日报“{label}”第{joined}条吗？回复“确认”执行，回复“取消”保留。"
        if action.source_item_text:
            return f"你确认要删除今天日报“{label}”里的“{action.source_item_text}”这条吗？回复“确认”执行，回复“取消”保留。"
        return f"你确认要删除今天日报“{label}”里的这项内容吗？回复“确认”执行，回复“取消”保留。"
    if action.type == "merge_items" and action.item_indices:
        joined = "、".join(str(index) for index in action.item_indices)
        return f"确认把今天日报“{label}”第{joined}条合并为一条吗？回复“确认”执行，回复“取消”保留。"
    if action.type == "replace_text" and action.old_value and action.new_value:
        return f"确认把今天日报“{label}”里的“{action.old_value}”改成“{action.new_value}”吗？"
    if action.items:
        return f"确认把今天日报的“{label}”改成“{'；'.join(action.items)}”吗？"
    return f"确认修改今天日报的“{label}”吗？"


def _asks_current_or_historical_again(plan: ActionPlan) -> bool:
    text = f"{plan.reply_to_user}\n{plan.clarification_question}"
    return ("今天" in text or "当前" in text) and ("昨天" in text or "历史" in text or "哪天" in text)


def _mentions_current_day_report(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    if any(marker in compact for marker in ("改成", "改为", "换成", "修改为", "更新为", "更正为")):
        return any(marker in compact for marker in ("今天的日报", "今日日报", "当前日报", "现在的日报", "当前草稿"))
    if any(marker in compact for marker in ("今天", "今日", "当前", "现在")) and any(
        marker in compact for marker in ("日报", "日志", "复盘", "草稿", "今日工作", "问题", "风险", "明日计划", "计划")
    ):
        return True
    return any(marker in compact for marker in ("今天", "今日", "刚才", "上午", "下午", "晚上")) and any(
        marker in compact for marker in ("做了", "完成", "处理", "审核", "发送", "沟通", "整理", "推进", "跟进", "记录")
    )


def _mentions_previous_day_report(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    return any(marker in compact for marker in ("昨天", "昨日", "前一天")) and any(
        marker in compact for marker in ("日报", "日志", "复盘", "草稿", "今日工作", "问题", "风险", "明日计划", "计划")
    )


def _empty_ack_from_existing(existing: DailyReport | None) -> dict[str, bool]:
    section_status = _current_section_status(existing)
    result = {field: bool(section_status.get(flag)) for field, flag in EMPTY_ACK_FLAGS.items()}
    if existing is not None:
        for field in REPORT_FIELDS:
            if _is_placeholder_list(field, list(getattr(existing, field, []) or [])):
                result[field] = True
    return result


def _with_empty_ack(section_status: dict[str, Any], empty_ack: dict[str, bool]) -> dict[str, Any]:
    updated = dict(section_status)
    for field, flag in EMPTY_ACK_FLAGS.items():
        if empty_ack.get(field):
            updated[flag] = True
        else:
            updated.pop(flag, None)
    return updated


def _is_empty_placeholder_items(field: str, items: list[str]) -> bool:
    cleaned = [item.strip() for item in items if item and item.strip()]
    if not cleaned:
        return False
    placeholders = {_compact(item) for item in PLACEHOLDER_VALUES.get(field, set())}
    return all(_compact(item) in placeholders for item in cleaned)


def _clean_business_values(field: str, values: list[str], *, split_today_work: bool = True) -> list[str]:
    placeholders = {_compact(item) for item in PLACEHOLDER_VALUES.get(field, set())}
    cleaned: list[str] = []
    for value in values:
        text = _clean_report_text_shell(str(value or ""))
        if field == "problems":
            text = _normalize_problem_text(text)
        elif field == "tomorrow_plan":
            text = _normalize_plan_text(text)
        if field == "today_work" and split_today_work:
            expanded = _split_today_work_multi_action(text)
            if len(expanded) > 1:
                for candidate in expanded:
                    if candidate and _compact(candidate) not in placeholders:
                        cleaned.append(candidate)
                continue
        if field == "problems" and text == "暂无明显问题":
            cleaned.append(text)
        elif text and _compact(text) not in placeholders:
            cleaned.append(text)
    return _dedupe_preserve_order(cleaned)


def _split_today_work_multi_action(text: str) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    compact = _compact(value)
    if _looks_like_integrated_plan_item(value):
        return [value]
    if any(
        token in compact
        for token in (
            "明天",
            "明日",
            "明儿",
            "问题",
            "风险",
            "困难",
            "暂无",
            "没问题",
            "没有问题",
            "优化",
            "包括",
            "效果",
            "发送逻辑",
            "更清楚",
            "收紧",
            "更保守",
            "更稳",
            "影子记忆",
            "补一个",
            "补充一个",
            "再加一个",
            "加一个",
        )
    ):
        return [value]
    if not (
        "、" in value
        or "；" in value
        or ";" in value
        or re.search(r"(?:^|[，,])\s*(?:一是|二是|三是|四是|\d+[.、])", value)
    ):
        return [value]
    parts = [
        part.strip(" ，,、。；;")
        for part in re.split(r"[、；;]+|(?<!\d)[，,](?!\d)(?=\s*(?:一是|二是|三是|四是|\d+[.、]))", value)
    ]
    parts = [part for part in parts if len(_compact(part)) >= 2]
    return parts if len(parts) >= 2 else [value]


def _looks_like_integrated_plan_item(text: str) -> bool:
    value = str(text or "").strip()
    if not value or not re.search(r"[，,、；;]", value):
        return False
    compact = _compact(value).lower()
    if any(
        token in compact
        for token in (
            "下一步计划",
            "后续计划",
            "整体方案",
            "系统方案",
            "原告底表",
            "outbox",
            "案件进展系统",
            "自动关联",
            "补充进展",
            "询问进展",
            "固定时间",
            "固定节点",
            "时间节点",
        )
    ):
        return True
    if "系统" in compact and any(token in compact for token in ("结合", "打通", "关联", "构建", "建设", "进展", "节点")):
        return True
    if "计划" in compact and any(token in compact for token in ("结合", "系统", "自动", "固定", "节点", "进展")):
        return True
    return False


def _clean_report_text_shell(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^(呃就是吧|嗯就是吧|啊就是吧|就是吧)[，,。；;、\s]*", "", text)
    text = re.sub(r"^(哎|唉|呃+|嗯+|啊+|那个|就是|怎么说呢|我真服了|真服了|服了)[，,。；;、\s]*", "", text)
    text = re.sub(r"^(另外)?(补一个|补充一个|再加一个|加一个)[，,。；;、\s]*", "", text)
    text = re.sub(r"^(还有个风险|还有一个风险|有个风险|另外有个风险|另外还有个风险)[，,。；;、\s]*", "", text)
    text = re.sub(r"^(还有个问题|还有一个问题|有个问题|另外有个问题|另外还有个问题)[，,。；;、\s]*", "", text)
    return text.strip(" ，,。；;、")


def _normalize_problem_text(value: str) -> str:
    text = _clean_report_text_shell(value)
    if not text:
        return ""
    compact = _compact(text)
    if _is_no_problem_text(compact):
        return "暂无明显问题"
    text = re.sub(r"^(问题|风险)(是|为)?[：:\s]*", "", text).strip(" ，,。；;、")
    return text


def _normalize_plan_text(value: str) -> str:
    text = _clean_report_text_shell(value)
    if not text:
        return ""
    text = re.sub(r"^(?:\d+|[一二三四五六七八九十]+)[.、．）)]\s*", "", text).strip(" ，,。；;、")
    text = text.replace("开个庭", "开庭")
    compact = _compact(text)
    if (
        any(marker in compact for marker in ("明天", "明日", "明儿"))
        and any(marker in compact for marker in ("工作计划", "计划", "安排"))
        and any(marker in compact for marker in ("今天一样", "今日一样", "同今天", "同今日", "和今天一样", "和今日一样"))
    ):
        return "继续今日工作"
    clauses = [part.strip(" ，,。；;、") for part in re.split(r"[，,。；;、]\s*", text) if part.strip(" ，,。；;、")]
    if len(clauses) > 1:
        kept = [part for part in clauses if not _looks_like_deleted_clause(part)]
        if kept:
            text = "，".join(kept)
    return text.strip(" ，,。；;、")


def _looks_like_deleted_clause(value: str) -> bool:
    compact = _compact(value)
    return any(token in compact for token in ("删掉", "删除", "先删", "去掉", "先不去", "不去了", "不用去了", "取消"))


def _is_no_problem_text(compact: str) -> bool:
    if not compact:
        return False
    if re.fullmatch(r"(没|没有|未)(碰到|遇到|发现|出现)(明显)?(新)?(问题|风险|困难|异常)", compact):
        return True
    no_problem_values = {
        _compact("暂无明显问题"),
        _compact("暂无问题"),
        _compact("问题暂无"),
        _compact("暂无风险"),
        _compact("风险暂无"),
        _compact("无明显问题"),
        _compact("无问题"),
        _compact("问题无"),
        _compact("无风险"),
        _compact("风险无"),
        _compact("没有问题"),
        _compact("问题没有"),
        _compact("没有风险"),
        _compact("风险没有"),
        _compact("没有新风险"),
        _compact("没问题"),
        _compact("问题没"),
        _compact("没啥问题"),
        _compact("没什么问题"),
        _compact("没遇到什么"),
        _compact("没遇到什么问题"),
        _compact("没遇到什么风险"),
        _compact("没事"),
        _compact("正常"),
        _compact("未发现明显问题"),
        _compact("未发现问题"),
        _compact("未发现风险"),
    }
    if compact in no_problem_values:
        return True
    return bool(re.fullmatch(r"(暂?无|没有|没啥|没什么|没遇到什么|没)(明显)?(新)?(问题|风险)?", compact))


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = _compact(value)
        if key and key not in seen:
            result.append(value)
            seen.add(key)
    return result


def _normalize_report_fields_after_actions(
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    empty_ack: dict[str, bool],
    *,
    split_today_work: bool = True,
) -> tuple[list[str], list[str], list[str], dict[str, bool]]:
    today_work = _clean_business_values("today_work", today_work, split_today_work=split_today_work)
    problems = _clean_business_values("problems", problems)
    tomorrow_plan = _clean_business_values("tomorrow_plan", tomorrow_plan)
    if split_today_work:
        today_work, extracted_problems = _extract_problem_tail_from_work(today_work)
        if extracted_problems:
            problems = _dedupe_preserve_order([*problems, *extracted_problems])
    problems, extracted_plans = _extract_plan_items_from_problems(problems)
    if extracted_plans:
        tomorrow_plan = _dedupe_preserve_order([*tomorrow_plan, *extracted_plans])
    if empty_ack.get("problems") and not problems:
        problems = ["暂无明显问题"]
    if _is_placeholder_list("problems", problems):
        problems = ["暂无明显问题"]
    return today_work, problems, tomorrow_plan, empty_ack


def _preserve_existing_item_boundaries(plan: ActionPlan, existing: DailyReport | None) -> bool:
    if existing is None:
        return any(action.source == "direct_numbered_work_items" for action in plan.actions)
    item_level_actions = {"replace_text", "merge_items", "delete_item", "move_item"}
    if any(action.type in item_level_actions for action in plan.actions):
        return True
    if any(action.source == "direct_numbered_work_items" for action in plan.actions):
        return True
    return False


def _extract_problem_tail_from_work(values: list[str]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    problems: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        parts = [part.strip(" ，,。；;、") for part in re.split(r"[，,。；;、]\s*", text) if part.strip(" ，,。；;、")]
        if len(parts) <= 1:
            if _is_no_problem_text(_compact(text)):
                problems.append("暂无明显问题")
            else:
                kept.append(text)
            continue
        work_parts: list[str] = []
        for part in parts:
            normalized_problem = _normalize_problem_text(part)
            if normalized_problem == "暂无明显问题" or _looks_like_problem_statement(part):
                problems.append(normalized_problem)
            else:
                work_parts.append(part)
        if work_parts:
            kept.append("，".join(work_parts))
    return _dedupe_preserve_order(kept), _dedupe_preserve_order(problems)


def _extract_plan_items_from_problems(values: list[str]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    plans: list[str] = []
    for value in values:
        text = str(value or "").strip()
        compact = _compact(text)
        if any(token in compact for token in ("明天", "明日", "明儿", "明儿继续", "明天继续", "明日继续")) and not any(
            token in compact for token in ("问题", "风险", "困难", "卡点")
        ):
            plans.append(_normalize_plan_text(text))
        else:
            kept.append(text)
    return _dedupe_preserve_order(kept), _dedupe_preserve_order(plans)


def _redistribute_misfiled_problem_items(today_work: list[str], problems: list[str]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    moved: list[str] = []
    for item in today_work:
        if _looks_like_problem_statement(item):
            moved.append(_normalize_problem_statement(item))
        else:
            kept.append(item)
    if not moved:
        return today_work, problems
    existing_keys = {_compact(item) for item in problems}
    merged_problems = list(problems)
    for item in moved:
        key = _compact(item)
        if key and key not in existing_keys:
            merged_problems.append(item)
            existing_keys.add(key)
    return kept or today_work, merged_problems


def _looks_like_problem_statement(value: str) -> bool:
    text = _compact(value)
    if not text:
        return False
    work_prefixes = (
        "处理",
        "解决",
        "修复",
        "优化",
        "完成",
        "补齐",
        "整理",
        "审核",
        "沟通",
        "复盘",
        "推进",
        "跟进",
        "发送",
        "下载",
        "签署",
        "开庭",
        "记录",
    )
    if text.startswith(work_prefixes) and not any(marker in text for marker in ("仍", "未", "无法", "不能", "卡住", "报错", "异常")):
        return False
    work_action_markers = (
        "处理",
        "解决",
        "修复",
        "优化",
        "完成",
        "补齐",
        "整理",
        "审核",
        "沟通",
        "推进",
        "跟进",
        "发送",
        "下载",
        "签署",
        "开庭",
        "记录",
        "收集",
        "对接",
    )
    strong_problem_markers = (
        "风险",
        "问题",
        "困难",
        "阻塞",
        "卡住",
        "延期",
        "延迟",
        "报错",
        "失败",
        "无法",
        "不能",
        "不全",
        "不完整",
        "缺少",
        "缺失",
        "还缺",
        "没给",
        "未给",
        "争议",
        "逾期",
        "投诉",
        "退回",
    )
    if any(marker in text for marker in work_action_markers) and not any(marker in text for marker in strong_problem_markers):
        return False
    problem_markers = strong_problem_markers + ("异常",)
    return any(marker in text for marker in problem_markers)


def _normalize_problem_statement(value: str) -> str:
    text = str(value or "").strip()
    return re.sub(r"[，,。；;]?\s*已记录\s*$", "", text).strip() or text


def _completeness_from_missing(missing_sections: list[str]) -> float:
    if not missing_sections:
        return 1.0
    return round((3 - len(missing_sections)) / 3, 4)


def _pending_append_content_field(existing: DailyReport | None) -> str | None:
    pending = _current_section_status(existing).get("_pending_interaction")
    if not isinstance(pending, dict):
        return None
    if pending.get("type") != "awaiting_append_content":
        return None
    target = pending.get("target_field")
    return target if target in REPORT_FIELDS else None


def _should_promote_pending_append_content(raw_input: str, plan: ActionPlan) -> bool:
    text = str(raw_input or "").strip()
    if not text:
        return False
    if plan.clear_pending_interaction:
        return False
    if _looks_like_current_report_query(text) or _looks_like_control_only_reply(text):
        return False
    compact = _compact(text)
    if compact in {"\u786e\u8ba4", "\u597d", "ok", "OK", "\u53ef\u4ee5", "\u7b97\u4e86", "\u53d6\u6d88", "\u4e0d\u8865\u4e86"}:
        return False
    if re.search(r"[?\uff1f]$", text):
        return False
    return True


def _pending_quality_candidate_from_no_write_plan(
    plan: ActionPlan,
    raw_input: str,
    pending_field: str | None,
) -> dict[str, Any] | None:
    if not pending_field or pending_field not in REPORT_FIELDS:
        return None
    if plan.should_write or plan.clear_pending_interaction:
        return None
    if plan.pending_interaction_to_set and plan.pending_interaction_to_set.type not in {
        "awaiting_append_target",
        "awaiting_append_target_confirmation",
    }:
        return None
    if not _plan_asks_for_more_detail(plan):
        return None
    candidate = (raw_input or "").strip()
    if not candidate or _looks_like_current_report_query(candidate) or _looks_like_control_only_reply(candidate):
        return None
    return {
        "type": "awaiting_content_quality_confirmation",
        "operation": "append",
        "target_field": pending_field,
        "context": {"candidate_items": [candidate]},
    }


def _pending_clarification_from_no_write_plan(
    plan: ActionPlan,
    existing: DailyReport | None,
    raw_input: str,
) -> dict[str, Any] | None:
    if existing is None or plan.should_write or plan.clear_pending_interaction or plan.pending_interaction_to_set:
        return None
    if not any(action.type == "ask_clarification" for action in plan.actions):
        return None
    question = (plan.reply_to_user or plan.clarification_question or "").strip()
    if not question:
        return None
    context: dict[str, Any] = {
        "source_message": str(raw_input or "").strip(),
        "question": question[:300],
        "current_report": _snapshot(existing) or {},
    }
    combined = f"{raw_input}\n{question}"
    target_field = _field_from_text(combined)

    if _has_merge_intent(_compact(combined)):
        context["operation_intent"] = "merge_items"
        indices = _extract_loose_item_indices(combined)
        if indices:
            context["candidate_indices"] = indices
        return {
            "type": "pending_clarification",
            "operation": "clarification_only",
            "target_field": target_field,
            "context": context,
        }

    old_value, new_value = _parse_loose_replace_request(raw_input)
    if old_value and new_value:
        context.update(
            {
                "operation_intent": "replace_text",
                "candidate_old_text": old_value,
                "candidate_new_text": new_value,
            }
        )
        indices = _extract_loose_item_indices(question)
        if len(indices) == 1:
            context["candidate_item_index"] = indices[0]
        return {
            "type": "pending_clarification",
            "operation": "clarification_only",
            "target_field": target_field,
            "context": context,
        }

    return {
        "type": "pending_clarification",
        "operation": "clarification_only",
        "target_field": target_field,
        "context": context,
    }


def _field_from_text(value: str) -> str:
    compact = _compact(value)
    if any(token in compact for token in ("问题", "风险")):
        return "problems"
    if any(token in compact for token in ("明日计划", "明天计划", "明日", "明天", "计划")):
        return "tomorrow_plan"
    if any(token in compact for token in ("今日工作", "今天工作", "工作", "完成")):
        return "today_work"
    return "none"


def _parse_loose_replace_request(raw_input: str) -> tuple[str, str]:
    text = str(raw_input or "").strip()
    match = re.search(r"(.+?)(?:改成|改为|换成|替换成|修正为)(.+)$", text)
    if not match:
        return "", ""
    old_value = re.sub(r"^把", "", match.group(1)).strip(" ：:，,。；;、")
    old_value = re.sub(r"(那条|这条|那个|这个|这一条|那一条)$", "", old_value).strip(" ：:，,。；;、")
    new_value = match.group(2).strip(" ：:，,。；;、")
    return old_value, new_value


def _extract_loose_item_indices(value: str) -> list[int]:
    text = str(value or "")
    refs: set[int] = set()

    def parse_number(raw: str) -> int | None:
        raw = str(raw or "").strip()
        if raw.isdigit():
            return int(raw)
        numerals = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        if raw in numerals:
            return numerals[raw]
        if raw.startswith("十") and len(raw) == 2 and raw[1] in numerals:
            return 10 + numerals[raw[1]]
        if raw.endswith("十") and len(raw) == 2 and raw[0] in numerals:
            return numerals[raw[0]] * 10
        if "十" in raw:
            left, right = raw.split("十", 1)
            left_value = numerals.get(left, 1 if not left else 0)
            right_value = numerals.get(right, 0) if right else 0
            return left_value * 10 + right_value
        return None

    number = r"\d{1,3}|[一二两三四五六七八九十]{1,3}"
    for start, end in re.findall(rf"第?({number})(?:条|项|个)?(?:到|至|~|～|-|—|－)第?({number})(?:条|项|个)?", text):
        start_num = parse_number(start)
        end_num = parse_number(end)
        if start_num is None or end_num is None:
            continue
        low, high = sorted((start_num, end_num))
        refs.update(range(low, high + 1))
    for raw in re.findall(rf"第?({number})(?:条|项|个)", text):
        parsed = parse_number(raw)
        if parsed is not None:
            refs.add(parsed)
    compact = _compact(text)
    digit_group = re.search(r"(?<!\d)([1-9]{2,6})(?:条|项|个)?", compact)
    if digit_group and any(token in compact for token in ("合并", "同一条", "同一项", "一回事", "一件事")):
        refs.update(int(char) for char in digit_group.group(1))
    chinese_group = re.search(r"(?<![一二两三四五六七八九十])([一二两三四五六七八九]{2,6})(?:条|项|个)?", compact)
    if chinese_group and any(token in compact for token in ("合并", "同一条", "同一项", "一回事", "一件事")):
        for char in chinese_group.group(1):
            parsed = parse_number(char)
            if parsed is not None:
                refs.add(parsed)
    return sorted(refs)


def _plan_asks_for_more_detail(plan: ActionPlan) -> bool:
    if any(action.type == "ask_clarification" for action in plan.actions):
        return True
    text = f"{plan.reply_to_user}\n{plan.clarification_question}"
    markers = ("具体", "笼统", "补充", "描述", "哪", "什么")
    return any(marker in text for marker in markers)


def _looks_like_control_only_reply(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return True
    return compact in {
        "额",
        "啊",
        "嗯",
        "嗯嗯",
        "对",
        "是",
        "确认",
        "确定",
        "可以",
        "取消",
        "算了",
        "不用",
        "不用了",
        "谢谢",
        "收到",
    }


def _can_fallback_to_pending_append(plan: ActionPlan) -> bool:
    if plan.intent not in {"fill_report", "edit_draft"}:
        return False
    if plan.pending_interaction_to_set:
        return False
    if any(action.type in {"ask_clarification", "query_history", "submit_report"} for action in plan.actions):
        return False
    return True


def _promote_daily_confirmation_pending_to_write(plan: ActionPlan, existing: DailyReport | None) -> ActionPlan:
    if plan.should_write or plan.clear_pending_interaction or plan.pending_interaction_to_set is None:
        return plan
    pending = plan.pending_interaction_to_set
    if pending.type not in {"awaiting_action_confirmation", "pending_batch_action"}:
        return plan
    actions = _actions_from_pending_confirmation(pending)
    if not actions:
        return plan
    relaxed_actions = [_relax_daily_report_action_confirmation(action) for action in actions]
    if not all(_daily_no_confirm_action_is_executable(action, existing) for action in relaxed_actions):
        return plan
    payload = plan.model_dump()
    payload["should_write"] = True
    payload["actions"] = [action.model_dump() for action in relaxed_actions]
    payload["pending_interaction_to_set"] = None
    payload["clear_pending_interaction"] = True
    payload["reply_to_user"] = ""
    payload["clarification_question"] = ""
    payload["reason"] = (plan.reason or "") + " Executor promoted daily confirmation pending to direct write."
    return ActionPlan.model_validate(payload)


def _actions_from_pending_confirmation(pending: PendingInteractionPlan) -> list[AgentAction]:
    context = pending.context if isinstance(pending.context, dict) else {}
    raw_actions = context.get("actions")
    candidates: list[Any]
    if isinstance(raw_actions, list) and raw_actions:
        candidates = raw_actions
    elif isinstance(context.get("action"), dict):
        candidates = [context.get("action")]
    else:
        candidates = []
    actions: list[AgentAction] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        action = AgentAction.model_validate(candidate)
        action = _fill_pending_action_target(action, pending, context)
        actions.append(action)
    if actions:
        return actions
    operation = str(pending.operation or "")
    if operation in {"delete_report_item", "delete_item"}:
        indices = _clean_indices(context.get("item_indices"))
        field = pending.target_field if pending.target_field in REPORT_FIELDS else "none"
        if field in REPORT_FIELDS and indices:
            return [AgentAction(type="delete_item", field=field, item_indices=indices)]
    if operation == "unsubmit_report":
        return [AgentAction(type="unsubmit_report")]
    return []


def _clean_indices(value: Any) -> list[int]:
    """Normalize persisted pending indices without guessing missing targets."""

    candidates = value if isinstance(value, list) else [value]
    result: list[int] = []
    for item in candidates:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            result.append(number)
    return result


def _fill_pending_action_target(action: AgentAction, pending: PendingInteractionPlan, context: dict[str, Any]) -> AgentAction:
    payload = action.model_dump()
    if action.type == "delete_item":
        if payload.get("field") not in REPORT_FIELDS and pending.target_field in REPORT_FIELDS:
            payload["field"] = pending.target_field
        if not payload.get("item_indices"):
            payload["item_indices"] = _clean_indices(context.get("item_indices"))
    if action.type == "clear_field" and payload.get("field") not in REPORT_FIELDS and pending.target_field in REPORT_FIELDS:
        payload["field"] = pending.target_field
    return AgentAction.model_validate(payload)


def _daily_no_confirm_action_is_executable(action: AgentAction, existing: DailyReport | None) -> bool:
    if action.type not in DAILY_NO_CONFIRM_ACTIONS:
        return False
    if action.type == "unsubmit_report":
        return existing is not None and existing.status == STATUS_COMPLETED
    return _structured_no_write_action_is_safe(action, existing)


def _can_execute_structured_no_write_actions(plan: ActionPlan, existing: DailyReport | None, raw_input: str) -> bool:
    if plan.intent not in {"edit_draft", "fill_report"}:
        return False
    if plan.pending_interaction_to_set or plan.clear_pending_interaction:
        return False
    if not plan.actions:
        return False
    if not _has_write_intent(raw_input):
        return False
    for action in plan.actions:
        if action.requires_confirmation and action.type not in DAILY_NO_CONFIRM_ACTIONS:
            return False
        if not _is_write_action(action):
            return False
        if not _structured_no_write_action_is_safe(action, existing):
            return False
    return True


def _structured_no_write_mismatch_message(plan: ActionPlan, existing: DailyReport | None, raw_input: str) -> str:
    if plan.should_write or not plan.actions or not _has_write_intent(raw_input):
        return ""
    write_actions = [action for action in plan.actions if _is_write_action(action)]
    if not write_actions:
        return ""
    if _can_execute_structured_no_write_actions(plan, existing, raw_input):
        return ""
    if any(action.type == "merge_items" for action in write_actions):
        return (
            plan.clarification_question
            or "\u6211\u7406\u89e3\u4f60\u662f\u60f3\u5408\u5e76\u8fd9\u4e9b\u6761\u76ee\uff0c\u8bf7\u786e\u8ba4\u8981\u5408\u5e76\u54ea\u4e2a\u680f\u76ee\u548c\u54ea\u4e9b\u7f16\u53f7\u3002"
        )
    return (
        plan.clarification_question
        or "\u6211\u7406\u89e3\u4f60\u662f\u60f3\u4fee\u6539\u65e5\u62a5\uff0c\u4f46\u8fd8\u6ca1\u5b9a\u4f4d\u6e05\u695a\u8981\u64cd\u4f5c\u7684\u680f\u76ee\u6216\u7f16\u53f7\u3002\u8bf7\u786e\u8ba4\u8981\u4fee\u6539\u54ea\u4e00\u90e8\u5206\u548c\u54ea\u4e9b\u7f16\u53f7\u3002"
    )


def _is_write_action(action: AgentAction) -> bool:
    return action.type in {
        "append_items",
        "replace_field",
        "replace_text",
        "merge_items",
        "move_item",
        "delete_item",
        "clear_field",
        "clear_all",
        "polish_items",
        "restore_snapshot",
        "complete_previous_plan_item",
        "complete_all_previous_plan_items",
        "rollover_previous_plan_items",
        "update_historical_report",
        "unsubmit_report",
    }


def _is_mutating_report_action(action: AgentAction) -> bool:
    return action.type in {
        "append_items",
        "replace_field",
        "replace_text",
        "merge_items",
        "move_item",
        "delete_item",
        "clear_field",
        "clear_all",
        "polish_items",
        "restore_snapshot",
    }


def _no_effect_action_message(plan: ActionPlan, existing: DailyReport | None) -> str:
    for action in plan.actions:
        field = _action_field(action)
        if action.type == "merge_items" and field in REPORT_FIELDS:
            return _no_change_message(ActionPlan(intent=plan.intent, confidence=plan.confidence, should_write=True, actions=[action]), existing)
        if action.type in {"replace_text", "replace_field", "polish_items"} and field in REPORT_FIELDS:
            return f"我刚才没有实际改动{_field_label(field)}，所以先不回复已修改。请重新说明要改哪一条，例如“第7条改成……”。"
        if action.type == "delete_item" and field in REPORT_FIELDS:
            return _no_change_message(ActionPlan(intent=plan.intent, confidence=plan.confidence, should_write=True, actions=[action]), existing)
    return "我刚才没有实际改动草稿，先不回复已完成。请重新说明要修改、合并或删除的具体栏目和编号。"


def _should_store_last_unwritten_candidate(plan: ActionPlan, raw_input: str) -> bool:
    text = str(raw_input or "").strip()
    compact = _compact(text)
    if not _is_meaningful_low_confidence_report_fragment(text):
        return False
    if plan.intent not in {"fill_report", "edit_draft", "answer_current_slot", "supplement"}:
        return False
    if any(
        action.type
        in {
            "query_history",
            "submit_report",
            "unsubmit_report",
            "complete_previous_plan_item",
            "complete_all_previous_plan_items",
            "rollover_previous_plan_items",
        }
        for action in plan.actions
    ):
        return False
    if _looks_like_control_only_reply(text) or _looks_like_current_report_query(text):
        return False
    if any(token in compact for token in ("改成", "改为", "删除", "撤回", "合并", "确认", "提交")):
        return False
    return True


def _structured_no_write_action_is_safe(action: AgentAction, existing: DailyReport | None) -> bool:
    if action.type == "append_items":
        return action.field in REPORT_FIELDS and bool(action.items)
    if action.type in {"replace_field", "polish_items"}:
        return action.field in REPORT_FIELDS and bool(action.items)
    if action.type == "replace_text":
        return action.field in REPORT_FIELDS and bool(action.old_value and action.new_value)
    if action.type == "merge_items":
        return action.field in REPORT_FIELDS and _safe_merge_indices(existing, action.field, action.item_indices)
    if action.type == "delete_item":
        return action.field in REPORT_FIELDS and _safe_delete_indices(existing, action.field, action.item_indices)
    if action.type == "move_item":
        return (
            action.source_field in REPORT_FIELDS
            and action.target_field in REPORT_FIELDS
            and (bool(action.source_item_text) or _safe_delete_indices(existing, action.source_field, action.item_indices))
        )
    if action.type == "clear_field":
        return action.field in REPORT_FIELDS
    if action.type == "clear_all":
        return True
    if action.type == "restore_snapshot":
        return isinstance(action.reference_report, dict) and any(action.reference_report.get(field) for field in REPORT_FIELDS)
    if action.type in {"complete_previous_plan_item", "complete_all_previous_plan_items", "rollover_previous_plan_items"}:
        return bool(action.completed_items or action.unfinished_items or action.item_indices or action.source_item_text)
    if action.type == "update_historical_report":
        return action.field in REPORT_FIELDS or action.source == "replace_report"
    if action.type == "unsubmit_report":
        return existing is not None and existing.status == STATUS_COMPLETED
    return False


def _safe_delete_indices(existing: DailyReport | None, field: str, item_indices: list[int]) -> bool:
    if field not in REPORT_FIELDS:
        return False
    unique_indices = sorted({index for index in item_indices if index >= 1})
    if not unique_indices:
        return False
    values = _values_for_existing(existing, field)
    return bool(values) and max(unique_indices) <= len(values)


def _has_write_intent(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    tokens = (
        "\u5408\u5e76",
        "\u548c\u5e76",
        "\u5e76\u4eca\u65e5\u5de5\u4f5c",
        "\u5e76\u4eca\u5929\u5de5\u4f5c",
        "\u540c\u4e00\u70b9",
        "\u540c\u4e00\u6761",
        "\u4e00\u56de\u4e8b",
        "\u4e0d\u8981\u62c6\u8fd9\u4e48\u788e",
        "\u5408\u5e76\u540c\u7c7b\u9879",
        "\u4fee\u6539",
        "\u6539\u6210",
        "\u6539\u4e3a",
        "\u5220\u9664",
        "\u5220\u6389",
        "\u5220",
        "\u79fb\u9664",
        "\u53bb\u6389",
        "\u8865\u5145",
        "\u52a0\u4e0a",
        "\u6dfb\u52a0",
        "\u8c03\u6574",
        "\u6da6\u8272",
        "\u91cd\u5199",
        "\u91cd\u65b0\u5199",
    )
    return any(token in compact for token in tokens)

def _should_persist_pending_interaction(plan: ActionPlan) -> bool:
    if plan.intent == "fill_report":
        return False
    return True


def _reference_report_from_action(action: AgentAction, report_date: date) -> dict[str, Any]:
    reference = dict(action.reference_report or {})
    source = action.source or str(reference.get("source") or "pasted_previous_report")
    return {
        "source": source,
        "loaded_at_report_date": report_date.isoformat(),
        "report_date": str(reference.get("report_date") or reference.get("date") or ""),
        "today_work": _clean_reference_items(reference.get("today_work")),
        "problems": _clean_reference_items(reference.get("problems")),
        "tomorrow_plan": _clean_reference_items(reference.get("tomorrow_plan")),
    }


def _clean_reference_items(value: Any) -> list[str]:
    candidates = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    result: list[str] = []
    for item in candidates:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _historical_report_context_pending(report: DailyReport, target_date: date, *, field: str = "none") -> dict[str, Any]:
    selected_field = field if field in REPORT_FIELDS else "none"
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
        "stage": "awaiting_field_edit_content" if selected_field in REPORT_FIELDS else "awaiting_edit_instruction",
        "current_report": current_report,
        "edit_cursor": edit_cursor,
    }
    if selected_field in REPORT_FIELDS:
        context["focus_section"] = selected_field
    return {
        "type": "historical_report_edit_flow",
        "operation": "modify_report",
        "target_field": selected_field,
        "context": context,
        "expires_after_turns": 12,
    }


def _historical_pending_after_update(
    action: AgentAction,
    updated_report: DailyReport,
    target_date: date,
    *,
    base_pending: dict[str, Any] | None,
) -> dict[str, Any]:
    field = _action_field(action)
    selected_field = field if field in REPORT_FIELDS else "none"
    if isinstance(base_pending, dict):
        pending = with_edit_cursor(base_pending)
        context = dict(pending.get("context") or {})
    else:
        pending = {
            "type": "historical_report_edit_flow",
            "operation": "modify_report",
            "target_field": selected_field,
            "context": {},
            "expires_after_turns": 12,
        }
        context = {}
    current_report = {
        "today_work": list(updated_report.today_work or []),
        "problems": list(updated_report.problems or []),
        "tomorrow_plan": list(updated_report.tomorrow_plan or []),
        "status": updated_report.status,
    }
    context.update(
        {
            "target_date": target_date.isoformat(),
            "requested_action": "modify_report",
            "stage": "awaiting_field_edit_content" if selected_field in REPORT_FIELDS else "awaiting_edit_instruction",
            "current_report": current_report,
            "edit_cursor": build_historical_edit_cursor(
                target_date=target_date,
                current_report=current_report,
                focused_section=selected_field,
            ),
        }
    )
    if selected_field in REPORT_FIELDS:
        context["focus_section"] = selected_field
    pending.update(
        {
            "type": "historical_report_edit_flow",
            "operation": "modify_report",
            "target_field": selected_field,
            "context": context,
            "expires_after_turns": 12,
            "turns_seen": 0,
        }
    )
    return pending


def _current_pending_after_update(
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    status: str,
    report_date: date,
    base_pending: dict[str, Any] | None,
    selected_field: str,
) -> dict[str, Any]:
    field = selected_field if selected_field in REPORT_FIELDS else "none"
    if isinstance(base_pending, dict):
        pending = with_edit_cursor(base_pending)
        context = dict(pending.get("context") or {})
    else:
        pending = {
            "type": "current_report_edit_flow",
            "operation": "modify_report",
            "target_field": field,
            "context": {},
            "expires_after_turns": 12,
        }
        context = {}
    current_report = {
        "today_work": list(today_work or []),
        "problems": list(problems or []),
        "tomorrow_plan": list(tomorrow_plan or []),
        "status": status,
    }
    context.update(
        {
            "target_date": report_date.isoformat(),
            "requested_action": "modify_report",
            "stage": "awaiting_field_edit_content" if field in REPORT_FIELDS else "awaiting_edit_instruction",
            "current_report": current_report,
            "edit_cursor": build_current_edit_cursor(
                target_date=report_date,
                current_report=current_report,
                focused_section=field,
            ),
        }
    )
    if field in REPORT_FIELDS:
        context["focus_section"] = field
    pending.update(
        {
            "type": "current_report_edit_flow",
            "operation": "modify_report",
            "target_field": field,
            "context": context,
            "expires_after_turns": 12,
            "turns_seen": 0,
        }
    )
    return pending


def _expand_reference_rollover_action(
    action: AgentAction,
    section_status: dict[str, Any],
    *,
    raw_input: str = "",
    previous_report: DailyReport | None = None,
) -> list[AgentAction]:
    if action.type not in {
        "complete_previous_plan_item",
        "complete_all_previous_plan_items",
        "rollover_previous_plan_items",
    }:
        return []
    reference_items = _reference_tomorrow_plan(section_status, previous_report=previous_report)
    completed = list(action.completed_items or [])
    unfinished = list(action.unfinished_items or [])
    cancelled = list(action.cancelled_items or [])
    selected_by_index = _select_reference_items(reference_items, action.item_indices)
    completed = _resolve_reference_values_from_action_items(completed, reference_items)
    unfinished = _resolve_reference_values_from_action_items(unfinished, reference_items)
    cancelled = _resolve_reference_values_from_action_items(cancelled, reference_items)
    deterministic_resolution = _previous_plan_reference_resolution(raw_input, len(reference_items), raw_input=raw_input)
    if deterministic_resolution is not None:
        completed_indices, unfinished_indices, _rollover_hint = deterministic_resolution
        deterministic_completed = _select_reference_items(reference_items, completed_indices)
        deterministic_unfinished = _select_reference_items(reference_items, unfinished_indices)
        if deterministic_completed:
            completed = deterministic_completed
        if deterministic_unfinished:
            unfinished = deterministic_unfinished
    inferred_unfinished = _infer_unfinished_reference_items(reference_items, raw_input)
    if inferred_unfinished:
        unfinished = _merge_reference_values(unfinished, inferred_unfinished)
    rollover_unfinished = _mentions_unfinished_rollover(raw_input)
    reuse_previous_for_tomorrow = action.source == "reuse_previous_plan_for_tomorrow" or _mentions_reuse_previous_plan_for_tomorrow(raw_input)

    if action.type == "complete_all_previous_plan_items":
        completed = completed or reference_items
    elif action.type == "complete_previous_plan_item":
        completed = completed or selected_by_index
        if not completed and action.source_item_text:
            completed = _match_reference_items(reference_items, action.source_item_text)
    elif action.type == "rollover_previous_plan_items":
        if not rollover_unfinished:
            return []
        unfinished = unfinished or selected_by_index
        if not unfinished and action.source_item_text:
            unfinished = _match_reference_items(reference_items, action.source_item_text)
        if not unfinished and reference_items:
            completed_keys = {_semantic_key(item) for item in completed}
            cancelled_keys = {_semantic_key(item) for item in cancelled}
            unfinished = [
                item
                for item in reference_items
                if _semantic_key(item) not in completed_keys and _semantic_key(item) not in cancelled_keys
            ]

    excluded_keys = {_semantic_key(item) for item in [*unfinished, *cancelled] if item and item.strip()}
    if excluded_keys:
        completed = [item for item in completed if _semantic_key(item) not in excluded_keys]

    expanded: list[AgentAction] = []
    completed_items = [_completion_text(item) for item in completed if item and item.strip()]
    if completed_items:
        expanded.append(AgentAction(type="append_items", field="today_work", items=completed_items))
    unfinished_items = [item.strip() for item in unfinished if item and item.strip()]
    if reuse_previous_for_tomorrow and reference_items:
        expanded.append(
            AgentAction(
                type="append_items",
                field="tomorrow_plan",
                items=[item.strip() for item in reference_items if item and item.strip()],
                source="previous_plan_reuse_for_tomorrow",
            )
        )
    elif rollover_unfinished and unfinished_items:
        expanded.append(AgentAction(type="append_items", field="tomorrow_plan", items=unfinished_items))
    return expanded


def _merge_reference_values(existing: list[str], incoming: list[str]) -> list[str]:
    result = [item for item in existing if item and item.strip()]
    seen = {_semantic_key(item) for item in result}
    for item in incoming:
        key = _semantic_key(item)
        if key and key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _resolve_reference_values_from_action_items(values: list[str], reference_items: list[str]) -> list[str]:
    if not values or not reference_items:
        return values
    resolved: list[str] = []
    changed = False
    for value in values:
        text = str(value or "").strip()
        index = _parse_reference_index(text)
        if index is not None and 1 <= index <= len(reference_items):
            resolved.append(reference_items[index - 1])
            changed = True
            continue
        matched = _match_reference_items(reference_items, text)
        if matched and _is_previous_plan_reference_placeholder_item(text, range_context=True):
            resolved.extend(matched)
            changed = True
            continue
        resolved.append(text)
    return _merge_reference_values([], resolved) if changed else values


def _infer_unfinished_reference_items(reference_items: list[str], raw_input: str) -> list[str]:
    if not reference_items or not raw_input:
        return []
    compact = _semantic_key(raw_input)
    inferred = _select_reference_items(reference_items, _previous_plan_unfinished_indices(compact, len(reference_items)))
    for item in reference_items:
        key = _semantic_key(item)
        if not key:
            continue
        position = compact.find(key)
        if position < 0:
            continue
        window = compact[max(0, position - 8): position + len(key) + 8]
        if re.search(r"(除|除了|除开|没|未|没有|来不及)", window) and re.search(r"(没做|未做|没完成|未完成|没有完成|没来得及|来不及|没处理|未处理)", window):
            inferred.append(item)
    return _merge_reference_values([], inferred)


def _drop_unrequested_previous_plan_rollover(
    action: AgentAction,
    section_status: dict[str, Any],
    raw_input: str,
    *,
    previous_report: DailyReport | None = None,
) -> AgentAction:
    if action.field != "tomorrow_plan" or action.type not in {"append_items", "replace_field", "polish_items"}:
        return action
    if _mentions_unfinished_rollover(raw_input):
        return action
    reference_items = _reference_tomorrow_plan(section_status, previous_report=previous_report)
    unfinished = _infer_unfinished_reference_items(reference_items, raw_input)
    if not unfinished or not action.items:
        return action
    unfinished_keys = {_semantic_key(item) for item in unfinished}
    item_keys = {_semantic_key(item) for item in action.items if item and item.strip()}
    if item_keys and item_keys.issubset(unfinished_keys):
        return AgentAction(type="no_op")
    return action


def _coerce_previous_plan_placeholder_action(
    action: AgentAction,
    section_status: dict[str, Any],
    raw_input: str,
    *,
    previous_report: DailyReport | None = None,
) -> AgentAction:
    if action.field != "today_work" or action.type not in {"append_items", "replace_field", "polish_items"}:
        return action
    if not action.items or not any(_is_previous_plan_reference_placeholder_item(item, range_context=True) for item in action.items):
        return action
    reference_items = _reference_tomorrow_plan(section_status, previous_report=previous_report)
    if not reference_items:
        return action
    action_text = "\n".join(item for item in action.items if item and item.strip())
    resolution = _previous_plan_reference_resolution(
        "\n".join(part for part in (raw_input, action_text) if part),
        len(reference_items),
        raw_input=raw_input,
    )
    if resolution is None and isinstance(section_status.get(REFERENCE_REPORT_CONTEXT_KEY), dict):
        resolution = _contextual_previous_plan_reference_resolution(raw_input, len(reference_items))
    if resolution is None:
        return action
    completed_indices, _unfinished_indices, _rollover_unfinished = resolution
    completed_items = [
        _completion_text(reference_items[index - 1])
        for index in completed_indices
        if 1 <= index <= len(reference_items)
    ]
    completed_items = [item for item in completed_items if item]
    if not completed_items:
        return action
    return AgentAction(
        type="append_items",
        field="today_work",
        items=completed_items,
        source="previous_plan_placeholder_guard",
    )


def _rewrite_previous_plan_reference_placeholders(
    actions: list[AgentAction],
    raw_input: str,
    section_status: dict[str, Any],
    *,
    previous_report: DailyReport | None = None,
) -> list[AgentAction]:
    if not actions or any(
        action.type in {"complete_previous_plan_item", "complete_all_previous_plan_items", "rollover_previous_plan_items"}
        for action in actions
    ):
        return actions
    reference_items = _reference_tomorrow_plan(section_status, previous_report=previous_report)
    if not reference_items:
        return actions
    action_text = "\n".join(item for action in actions for item in (action.items or []))
    resolution = _previous_plan_reference_resolution(
        "\n".join(part for part in (raw_input, action_text) if part),
        len(reference_items),
        raw_input=raw_input,
    )
    if resolution is None and isinstance(section_status.get(REFERENCE_REPORT_CONTEXT_KEY), dict):
        resolution = _contextual_previous_plan_reference_resolution(raw_input, len(reference_items))
    if resolution is None:
        return actions
    completed_indices, unfinished_indices, rollover_unfinished = resolution

    completed_items = [_completion_text(reference_items[index - 1]) for index in completed_indices if 1 <= index <= len(reference_items)]
    completed_items = [item for item in completed_items if item]
    if not completed_items:
        return actions
    unfinished_items = [reference_items[index - 1].strip() for index in unfinished_indices if 1 <= index <= len(reference_items)]
    unfinished_items = [item for item in unfinished_items if item]

    rewritten: list[AgentAction] = []
    inserted_completion = _actions_cover_reference_items(actions, completed_items, field="today_work")
    inserted_rollover = _actions_cover_reference_items(actions, unfinished_items, field="tomorrow_plan") if rollover_unfinished else True
    for action in actions:
        if action.type in {"append_items", "replace_field", "polish_items"} and action.field in REPORT_FIELDS:
            cleaned_items = [
                item
                for item in action.items
                if not _is_previous_plan_reference_placeholder_item(item, range_context=True)
            ]
            had_placeholder = len(cleaned_items) != len(action.items)
            if action.field == "today_work" and not inserted_completion:
                if not _action_items_cover_reference_items(cleaned_items, completed_items):
                    extras = _non_reference_action_items(cleaned_items, completed_items)
                    rewritten.append(
                        action.model_copy(
                            update={
                                "items": _merge_by_reference_key(completed_items, extras),
                                "source": action.source or "previous_plan_reference_resolution",
                            }
                        )
                    )
                    inserted_completion = True
                    continue
            if action.field == "today_work" and had_placeholder and not inserted_completion:
                rewritten.append(
                    AgentAction(
                        type="append_items",
                        field="today_work",
                        items=completed_items,
                        source="previous_plan_reference_resolution",
                    )
                )
                inserted_completion = True
            if action.field == "tomorrow_plan" and had_placeholder and rollover_unfinished and unfinished_items and not inserted_rollover:
                rewritten.append(
                    AgentAction(
                        type="append_items",
                        field="tomorrow_plan",
                        items=unfinished_items,
                        source="previous_plan_reference_rollover",
                    )
                )
                inserted_rollover = True
            if not cleaned_items:
                continue
            if cleaned_items != action.items:
                rewritten.append(action.model_copy(update={"items": cleaned_items}))
                continue
        rewritten.append(action)
    if not inserted_completion:
        rewritten.insert(
            0,
            AgentAction(
                type="append_items",
                field="today_work",
                items=completed_items,
                source="previous_plan_reference_resolution",
            ),
        )
    if rollover_unfinished and unfinished_items and not inserted_rollover:
        rewritten.append(
            AgentAction(
                type="append_items",
                field="tomorrow_plan",
                items=unfinished_items,
                source="previous_plan_reference_rollover",
            )
        )
    return rewritten


def _mentions_previous_plan_reference(text: str) -> bool:
    compact = _semantic_key(text)
    previous_markers = (
        "\u6628\u5929",
        "\u6628\u65e5",
        "\u6628\u513f",
        "\u524d\u4e00\u5929",
        "\u4e0a\u4e00\u5929",
        "\u4e0a\u4e00\u65e5",
        "\u4e0a\u4e2a\u5de5\u4f5c\u65e5",
        "\u4e0a\u4e00\u4e2a\u5de5\u4f5c\u65e5",
    )
    plan_markers = (
        "\u8ba1\u5212",
        "\u5f85\u529e",
        "\u5b89\u6392",
        "\u4efb\u52a1",
        "\u4e8b\u9879",
        "\u4e8b\u513f",
        "\u5de5\u4f5c",
        "\u660e\u65e5",
        "\u660e\u5929",
        "\u540e\u7eed",
    )
    return any(marker in compact for marker in previous_markers) and any(marker in compact for marker in plan_markers)


def _is_previous_plan_reference_placeholder_item(text: str, *, range_context: bool) -> bool:
    compact = _semantic_key(text)
    if not compact:
        return False
    if _mentions_previous_plan_reference(compact):
        return True
    if not range_context:
        return False
    number = r"[0-9\uff10-\uff19\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3}"
    if re.fullmatch(rf"(?:\u8fd9\u4e9b|\u8fd9\u51e0\u4e2a|\u5176\u4e2d)?(?:\u5b8c\u6210|\u5df2\u5b8c\u6210)?(?:\u524d|\u5934){number}(?:\u9879|\u6761|\u4e2a|\u4ef6)?(?:\u90fd|\u5168\u90e8|\u5df2|\u5df2\u7ecf|\u6b63\u5e38|\u4eca\u5929|\u4eca\u65e5)?(?:\u5b8c\u6210|\u505a\u5b8c|\u641e\u5b9a)", compact):
        return True
    if re.fullmatch(rf"(?:\u7b2c)?{number}(?:\u9879|\u6761|\u4e2a|\u4ef6)?(?:\u672a\u5b8c\u6210|\u6ca1\u5b8c\u6210|\u6ca1\u6709\u5b8c\u6210|\u6ca1\u505a|\u672a\u505a|\u6ca1\u6709\u505a|\u672a\u5904\u7406|\u6ca1\u5904\u7406|\u6ca1\u52a8|\u660e\u5929\u7ee7\u7eed|\u660e\u65e5\u7ee7\u7eed|\u540e\u7eed\u7ee7\u7eed)", compact):
        return True
    if "\u6700\u540e" in compact and any(marker in compact for marker in ("\u9664", "\u6ca1", "\u672a", "\u5916")):
        return True
    return False


def _parse_reference_index(value: str) -> int | None:
    compact = _semantic_key(value).lstrip("\u7b2c")
    if not compact:
        return None
    compact = compact.translate(str.maketrans("\uff10\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19", "0123456789"))
    try:
        parsed = int(compact)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed if parsed > 0 else None

    digit_values = {
        "\u96f6": 0,
        "\u4e00": 1,
        "\u4e8c": 2,
        "\u4e24": 2,
        "\u4e09": 3,
        "\u56db": 4,
        "\u4e94": 5,
        "\u516d": 6,
        "\u4e03": 7,
        "\u516b": 8,
        "\u4e5d": 9,
    }
    if compact in digit_values:
        parsed = digit_values[compact]
        return parsed if parsed > 0 else None
    ten = "\u5341"
    if ten in compact:
        left, right = compact.split(ten, 1)
        tens = 1 if not left else digit_values.get(left)
        ones = 0 if not right else digit_values.get(right)
        if tens is not None and ones is not None:
            parsed = tens * 10 + ones
            return parsed if parsed > 0 else None
    return None


def _previous_plan_reference_resolution(
    text: str,
    max_index: int,
    *,
    raw_input: str,
) -> tuple[list[int], list[int], bool] | None:
    if not text or max_index <= 0:
        return None
    compact = _semantic_key(text)
    if not _mentions_previous_plan_reference(compact):
        return None
    completed = set(_previous_plan_completed_indices(compact, max_index))
    unfinished = set(_previous_plan_unfinished_indices(compact, max_index))
    if not completed and unfinished and _mentions_remaining_previous_plan_completed(compact):
        completed = {index for index in range(1, max_index + 1) if index not in unfinished}
    completed = {index for index in completed if 1 <= index <= max_index and index not in unfinished}
    unfinished = {index for index in unfinished if 1 <= index <= max_index}
    if not completed:
        return None
    return sorted(completed), sorted(unfinished), _mentions_unfinished_rollover(raw_input)


def _contextual_previous_plan_reference_resolution(text: str, max_index: int) -> tuple[list[int], list[int], bool] | None:
    if not text or max_index <= 0:
        return None
    compact = _semantic_key(text)
    completed = set(_previous_plan_completed_indices(compact, max_index))
    unfinished = set(_previous_plan_unfinished_indices(compact, max_index))
    if not completed and unfinished and _mentions_remaining_previous_plan_completed(compact):
        completed = {index for index in range(1, max_index + 1) if index not in unfinished}
    completed = {index for index in completed if 1 <= index <= max_index and index not in unfinished}
    unfinished = {index for index in unfinished if 1 <= index <= max_index}
    if not completed:
        return None
    return sorted(completed), sorted(unfinished), _mentions_unfinished_rollover(text)


def _previous_plan_completed_indices(compact: str, max_index: int) -> list[int]:
    indices: set[int] = set()
    number = r"[0-9\uff10-\uff19\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3}"
    range_pattern = re.compile(
        rf"(?:\u7b2c)?({number})(?:\u9879|\u6761|\u4e2a|\u4ef6)?(?:~|\uff5e|-|\u5230|\u81f3)(?:\u7b2c)?({number})(?:\u9879|\u6761|\u4e2a|\u4ef6)"
    )
    for start_text, end_text in range_pattern.findall(compact):
        start = _parse_reference_index(start_text)
        end = _parse_reference_index(end_text)
        if start is None or end is None:
            continue
        lo, hi = sorted((start, end))
        indices.update(index for index in range(lo, hi + 1) if 1 <= index <= max_index)

    front_pattern = re.compile(rf"(?:\u524d|\u5934)({number})(?:\u9879|\u6761|\u4e2a|\u4ef6)")
    for value in front_pattern.findall(compact):
        count = _parse_reference_index(value)
        if count is not None:
            indices.update(index for index in range(1, min(count, max_index) + 1))
    return sorted(indices)


def _previous_plan_unfinished_indices(compact: str, max_index: int) -> list[int]:
    indices: set[int] = set()
    number = r"[0-9\uff10-\uff19\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]{1,3}"
    numbered_unfinished = re.compile(
        rf"(?:\u7b2c)?({number})(?:\u9879|\u6761|\u4e2a|\u4ef6)?(?:\u6ca1\u6709\u5b8c\u6210|\u6ca1\u5b8c\u6210|\u672a\u5b8c\u6210|\u6ca1\u6709\u505a|\u6ca1\u505a|\u672a\u505a|\u6ca1\u5904\u7406|\u672a\u5904\u7406|\u6ca1\u52a8|\u660e\u5929\u7ee7\u7eed|\u660e\u65e5\u7ee7\u7eed|\u540e\u7eed\u7ee7\u7eed)"
    )
    for value in numbered_unfinished.findall(compact):
        index = _parse_reference_index(value)
        if index is not None:
            indices.add(index)

    unfinished_numbered = re.compile(
        rf"(?:\u6ca1\u6709\u5b8c\u6210|\u6ca1\u5b8c\u6210|\u672a\u5b8c\u6210|\u6ca1\u6709\u505a|\u6ca1\u505a|\u672a\u505a|\u6ca1\u5904\u7406|\u672a\u5904\u7406|\u6ca1\u52a8)(?:\u7684)?(?:\u7b2c)?({number})(?:\u9879|\u6761|\u4e2a|\u4ef6)?"
    )
    for value in unfinished_numbered.findall(compact):
        index = _parse_reference_index(value)
        if index is not None:
            indices.add(index)

    exclude_numbered = re.compile(rf"(?:\u9664\u4e86|\u9664)(?:\u7b2c)?({number})(?:\u9879|\u6761|\u4e2a|\u4ef6)?.{{0,4}}(?:\u4e4b\u5916|\u4ee5\u5916|\u5916)")
    for value in exclude_numbered.findall(compact):
        index = _parse_reference_index(value)
        if index is not None:
            indices.add(index)

    exclude_remaining_completed = re.compile(
        rf"(?:\u9664\u4e86|\u9664)(?:\u7b2c)?({number})(?:\u9879|\u6761|\u4e2a|\u4ef6)?(?:\u4e4b\u5916|\u4ee5\u5916|\u5916)?.{{0,8}}(?:\u5176\u4ed6|\u5176\u5b83|\u5176\u4f59|\u5269\u4e0b|\u5269\u4f59|\u4f59\u4e0b).{{0,12}}(?:\u5b8c\u6210|\u505a\u5b8c|\u641e\u5b9a|\u5904\u7406\u5b8c|\u529e\u5b8c|\u5f04\u5b8c|\u843d\u5b9e)"
    )
    for value in exclude_remaining_completed.findall(compact):
        index = _parse_reference_index(value)
        if index is not None:
            indices.add(index)

    if re.search(r"(?:\u9664\u4e86|\u9664)?(?:\u6700\u540e\u4e00|\u6700\u540e|\u672b)(?:\u9879|\u6761|\u4e2a|\u4ef6).{0,4}(?:\u4e4b\u5916|\u4ee5\u5916|\u5916|\u6ca1\u505a|\u672a\u505a|\u6ca1\u5b8c\u6210|\u672a\u5b8c\u6210)", compact):
        indices.add(max_index)
    return sorted(index for index in indices if 1 <= index <= max_index)


def _mentions_remaining_previous_plan_completed(compact: str) -> bool:
    remaining_markers = (
        "\u5176\u4ed6",
        "\u5176\u5b83",
        "\u5176\u4f59",
        "\u5269\u4e0b",
        "\u5269\u4f59",
        "\u4f59\u4e0b",
    )
    exclusion_markers = ("\u5916", "\u4ee5\u5916", "\u4e4b\u5916")
    completion_markers = (
        "\u5b8c\u6210",
        "\u505a\u5b8c",
        "\u641e\u5b9a",
        "\u5904\u7406\u5b8c",
        "\u529e\u5b8c",
        "\u5f04\u5b8c",
        "\u843d\u5b9e",
    )
    mentions_remaining = any(marker in compact for marker in remaining_markers)
    mentions_exclusion = any(marker in compact for marker in exclusion_markers)
    mentions_completion = any(marker in compact for marker in completion_markers)
    return mentions_completion and (mentions_remaining or mentions_exclusion)


def _mentions_unfinished_rollover(text: str) -> bool:
    compact = _semantic_key(text)
    future_markers = (
        "\u660e\u5929",
        "\u660e\u65e5",
        "\u540e\u7eed",
        "\u4e4b\u540e",
        "\u540e\u9762",
        "\u4e0b\u4e00\u6b65",
    )
    continue_markers = (
        "\u7ee7\u7eed",
        "\u518d\u5904\u7406",
        "\u63a5\u7740\u5904\u7406",
        "\u8ddf\u8fdb",
        "\u63a8\u8fdb",
        "\u5904\u7406",
        "\u8865\u505a",
        "\u518d\u529e",
    )
    return any(marker in compact for marker in future_markers) and any(marker in compact for marker in continue_markers)


def _mentions_reuse_previous_plan_for_tomorrow(text: str) -> bool:
    compact = _semantic_key(text)
    if not compact:
        return False
    if not any(marker in compact for marker in ("\u660e\u5929", "\u660e\u65e5", "\u540e\u7eed")):
        return False
    if not any(marker in compact for marker in ("\u6628\u5929", "\u6628\u65e5", "\u524d\u4e00\u5929", "\u4e0a\u4e00\u5929")):
        return False
    if not any(marker in compact for marker in ("\u8ba1\u5212", "\u5b89\u6392", "\u5f85\u529e", "\u4e8b\u9879")):
        return False
    return bool(
        re.search(r"(\u660e\u5929|\u660e\u65e5|\u540e\u7eed).{0,12}(\u8ba1\u5212|\u5b89\u6392|\u5f85\u529e).{0,12}(\u7167|\u6309|\u540c|\u8ddf|\u548c|\u4e00\u6837|\u4e0d\u53d8).{0,12}(\u6628\u5929|\u6628\u65e5|\u524d\u4e00\u5929|\u4e0a\u4e00\u5929)",
            compact)
        or re.search(r"(\u660e\u5929|\u660e\u65e5|\u540e\u7eed).{0,12}(\u6628\u5929|\u6628\u65e5|\u524d\u4e00\u5929|\u4e0a\u4e00\u5929).{0,12}(\u8ba1\u5212|\u5b89\u6392|\u5f85\u529e).{0,12}(\u7167|\u6309|\u540c|\u8ddf|\u548c|\u4e00\u6837|\u4e0d\u53d8)",
            compact)
        or re.search(r"(\u7167|\u6309|\u540c|\u8ddf|\u548c).{0,12}(\u6628\u5929|\u6628\u65e5|\u524d\u4e00\u5929|\u4e0a\u4e00\u5929).{0,12}(\u8ba1\u5212|\u5b89\u6392|\u5f85\u529e).{0,12}(\u660e\u5929|\u660e\u65e5|\u540e\u7eed)",
            compact)
    )


def _actions_cover_reference_items(actions: list[AgentAction], expected_items: list[str], *, field: str) -> bool:
    if not expected_items:
        return True
    candidates = [
        _reference_match_key(item)
        for action in actions
        if action.field == field and action.type in {"append_items", "replace_field", "polish_items"}
        for item in action.items
    ]
    if not candidates:
        return False
    for expected in expected_items:
        expected_key = _reference_match_key(expected)
        if not any(expected_key and (expected_key == candidate or expected_key in candidate or candidate in expected_key) for candidate in candidates):
            return False
    return True


def _action_items_cover_reference_items(items: list[str], expected_items: list[str]) -> bool:
    if not expected_items:
        return True
    item_keys = [_reference_match_key(item) for item in items]
    if not item_keys:
        return False
    for expected in expected_items:
        expected_key = _reference_match_key(expected)
        if not any(expected_key and (expected_key == item_key or expected_key in item_key or item_key in expected_key) for item_key in item_keys):
            return False
    return True


def _non_reference_action_items(items: list[str], reference_items: list[str]) -> list[str]:
    reference_keys = [_reference_match_key(item) for item in reference_items if _reference_match_key(item)]
    result: list[str] = []
    for item in items:
        key = _reference_match_key(item)
        if key and any(key == ref_key or key in ref_key or ref_key in key for ref_key in reference_keys):
            continue
        if _is_previous_plan_reference_placeholder_item(item, range_context=True):
            continue
        result.append(item)
    return result


def _merge_by_reference_key(primary: list[str], extras: list[str]) -> list[str]:
    result = [item for item in primary if item and item.strip()]
    seen = {_reference_match_key(item) for item in result if _reference_match_key(item)}
    for item in extras:
        key = _reference_match_key(item)
        if key and key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _previous_plan_reference_indices(text: str, max_index: int) -> list[int]:
    resolution = _previous_plan_reference_resolution(text, max_index, raw_input=text)
    return list(resolution[0]) if resolution is not None else []



def _reference_tomorrow_plan(section_status: dict[str, Any], *, previous_report: DailyReport | None = None) -> list[str]:
    reference = section_status.get(REFERENCE_REPORT_CONTEXT_KEY)
    if not isinstance(reference, dict):
        return _clean_reference_items(getattr(previous_report, "tomorrow_plan", None))
    reference_items = _clean_reference_items(reference.get("tomorrow_plan"))
    if reference_items:
        return reference_items
    return _clean_reference_items(getattr(previous_report, "tomorrow_plan", None))


def _select_reference_items(values: list[str], indices: list[int]) -> list[str]:
    selected: list[str] = []
    for index in indices:
        normalized = 1 if index == 0 else index
        if 1 <= normalized <= len(values):
            selected.append(values[normalized - 1])
    return selected


def _match_reference_items(values: list[str], text: str) -> list[str]:
    key = _semantic_key(text)
    return [value for value in values if key and (key in _semantic_key(value) or _semantic_key(value) in key)]


def _completion_text(item: str) -> str:
    text = _normalize_completed_previous_plan_item(item.strip())
    if not text:
        return ""
    if text.startswith(("完成", "已完成")):
        return text
    return f"完成{text}"


def _normalize_completed_previous_plan_item(text: str) -> str:
    text = str(text or "").strip(" \t\r\n，,。；;、")
    if not text:
        return ""
    text = re.sub(r"^(明天|明日|明儿|后续|接下来|之后)[，,。；;、\s]*", "", text)
    text = re.sub(r"^(计划|准备|打算)[去做]*[，,。；;、\s]*", "", text)
    text = re.sub(r"^(继续|接着|开始|再|持续)[，,。；;、\s]*", "", text)
    text = re.sub(r"^(做)[，,。；;、\s]*", "", text)
    return text.strip(" \t\r\n，,。；;、")


def _semantic_key(value: str) -> str:
    return "".join(str(value or "").split()).replace("，", "").replace("。", "").replace(",", "")


def _reference_match_key(value: str) -> str:
    key = _semantic_key(_normalize_completed_previous_plan_item(value))
    for prefix in ("已经完成", "已完成", "完成"):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    return key


def _pending_action_from_no_write_plan(
    plan: ActionPlan,
    existing: DailyReport | None,
) -> tuple[dict[str, Any], AgentAction] | None:
    for action in plan.actions:
        if action.type in {"delete_item", "clear_field", "clear_all", "replace_field", "replace_text", "merge_items", "move_item", "unsubmit_report"}:
            pending = _pending_interaction_from_action(action, existing)
            if pending is not None:
                return pending, action
    return None


def _pending_batch_action_from_plan(
    actions: list[AgentAction],
    existing: DailyReport | None,
    *,
    clear_pending: bool,
) -> tuple[dict[str, Any], str] | None:
    if existing is None or clear_pending or not actions:
        return None
    if any(action.type == "unsubmit_report" for action in actions):
        return None
    relaxed_actions = [_relax_daily_report_action_confirmation(_relax_draft_single_item_confirmation(action, existing)) for action in actions]
    if existing.status == STATUS_COMPLETED and any(_requires_completed_confirmation(existing, action) for action in relaxed_actions):
        pending_actions = [AgentAction(type="unsubmit_report"), *relaxed_actions]
        pending = _pending_interaction_from_actions(
            pending_actions,
            existing,
            operation="withdraw_and_modify",
            target_field=next((_action_field(action) for action in relaxed_actions if _action_field(action) in REPORT_FIELDS), "none"),
        )
        return pending, "这份日报已提交，是否撤回并修改？"
    if len(relaxed_actions) > 1 and any(action.requires_confirmation for action in relaxed_actions):
        pending = _pending_interaction_from_actions(
            relaxed_actions,
            existing,
            operation="batch_action",
            target_field=next((_action_field(action) for action in relaxed_actions if _action_field(action) in REPORT_FIELDS), "none"),
        )
        return pending, _batch_confirmation_message(relaxed_actions, existing)
    return None


def _pending_interaction_from_actions(
    actions: list[AgentAction],
    existing: DailyReport | None,
    *,
    operation: str,
    target_field: str,
) -> dict[str, Any]:
    resume_pending = _current_context_pending(existing)
    edit_cursor = normalize_edit_cursor(resume_pending) if resume_pending is not None else None
    context: dict[str, Any] = {
        "actions": [action.model_dump() for action in actions],
    }
    if actions:
        context["action"] = actions[0].model_dump()
    if resume_pending is not None:
        context["resume_pending_interaction"] = resume_pending
    if edit_cursor is not None:
        context["edit_cursor"] = edit_cursor
    pending = {
        "type": "pending_batch_action",
        "operation": operation,
        "target_field": target_field if target_field in REPORT_FIELDS else "none",
        "context": context,
    }
    return _normalize_pending_interaction_for_existing(pending, existing)


def _relax_draft_single_item_confirmation(action: AgentAction, existing: DailyReport | None) -> AgentAction:
    if existing is not None and existing.status == STATUS_COMPLETED:
        return action
    direct = False
    if action.type == "append_items" and action.field in REPORT_FIELDS and action.items:
        direct = True
    elif action.type == "replace_text" and action.field in REPORT_FIELDS and action.old_value and action.new_value:
        direct = True
    elif action.type == "merge_items" and action.field in REPORT_FIELDS and len(action.item_indices) >= 2:
        direct = True
    elif action.type == "delete_item" and action.field in REPORT_FIELDS:
        direct = len(action.item_indices) == 1 or bool(action.source_item_text)
    if not direct or not action.requires_confirmation:
        return action
    payload = action.model_dump()
    payload["requires_confirmation"] = False
    payload["confirmation_message"] = ""
    return AgentAction.model_validate(payload)


def _relax_daily_report_action_confirmation(action: AgentAction) -> AgentAction:
    if action.type not in {
        "delete_item",
        "clear_field",
        "clear_all",
        "unsubmit_report",
        "update_historical_report",
    }:
        return action
    if not action.requires_confirmation and not action.confirmation_message:
        return action
    payload = action.model_dump()
    payload["requires_confirmation"] = False
    payload["confirmation_message"] = ""
    return AgentAction.model_validate(payload)


def _enforce_draft_multi_delete_confirmation(action: AgentAction, existing: DailyReport | None) -> AgentAction:
    return action


def _allow_confirmed_pending_action(action: AgentAction, *, clear_pending: bool) -> AgentAction:
    if not clear_pending or not action.requires_confirmation:
        return action
    payload = action.model_dump()
    payload["requires_confirmation"] = False
    payload["confirmation_message"] = ""
    return AgentAction.model_validate(payload)


def _ambiguous_delete_clarification(action: AgentAction, existing: DailyReport | None, raw_input: str) -> str:
    if existing is not None and existing.status == STATUS_COMPLETED:
        return ""
    if action.type != "delete_item" or action.field not in REPORT_FIELDS:
        return ""
    if action.source_item_text:
        return ""
    text = _normalized_command_text(raw_input)
    if not text or not _looks_like_vague_relative_delete(text):
        return ""
    if _has_explicit_delete_reference(text):
        return ""
    label = _field_label(action.field)
    return f"我还没定位清楚要删除{label}里的哪几条。请说具体序号，比如“删除第9到第12条”。"


def _normalized_command_text(raw_input: str) -> str:
    return re.sub(r"[\s，。；;：:、,.!?！？（）()\[\]【】\"'“”‘’]+", "", str(raw_input or ""))


def _is_delegate_without_content(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if compact in {
        "随便",
        "随便吧",
        "随便写",
        "你看着写",
        "随便吧你看着写",
        "随便你看着写",
        "你随便写",
    }:
        return True
    return "随便" in compact and any(token in compact for token in ("凑一下", "凑一份", "编一下", "编一份", "看着写"))


def _is_casual_teaser_without_report_content(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    if compact in {"你猜我今天干嘛了", "猜猜我今天干嘛了", "你猜我干嘛了", "猜猜我干嘛了"}:
        return True
    if not compact.startswith(("你猜", "猜猜")) or len(compact) > 18:
        return False
    report_markers = ("审核", "整理", "处理", "沟通", "完成", "问题", "风险", "明天", "明日", "计划")
    return not any(marker in compact for marker in report_markers)


def _coerce_delete_merge_mismatch(action: AgentAction, existing: DailyReport | None, raw_input: str) -> tuple[AgentAction, str]:
    if action.type != "delete_item":
        return action, ""
    compact = _compact(raw_input)
    if _has_explicit_delete_intent(compact) or not _has_merge_intent(compact):
        return action, ""
    if action.field in REPORT_FIELDS and _safe_merge_indices(existing, action.field, action.item_indices):
        payload = action.model_dump()
        payload["type"] = "merge_items"
        payload["requires_confirmation"] = False
        payload["confirmation_message"] = ""
        payload["reason"] = (payload.get("reason") or "") + " Coerced from delete_item because the user asked to merge items."
        return AgentAction.model_validate(payload), ""
    return action, "我理解你是想合并这些条目，请确认要合并哪个栏目和哪些编号。"


def _coerce_merge_field_mismatch(action: AgentAction, existing: DailyReport | None, raw_input: str) -> AgentAction:
    if action.type != "merge_items" or _safe_merge_indices(existing, action.field, action.item_indices):
        return action
    compact = _compact(raw_input)
    if not _has_merge_intent(compact):
        return action
    if any(token in compact for token in ("问题", "风险", "困难", "明日", "明天", "计划")):
        return action
    if not _safe_merge_indices(existing, "today_work", action.item_indices):
        return action
    payload = action.model_dump()
    payload["field"] = "today_work"
    payload["source"] = payload.get("source") or "executor_merge_field_guard"
    payload["reason"] = (payload.get("reason") or "") + " Executor redirected merge to today_work because only that field has the referenced indices."
    return AgentAction.model_validate(payload)


def _safe_merge_indices(existing: DailyReport | None, field: str, item_indices: list[int]) -> bool:
    if field not in REPORT_FIELDS:
        return False
    unique_indices = sorted({index for index in item_indices if index >= 1})
    if len(unique_indices) < 2:
        return False
    values = _values_for_existing(existing, field)
    return bool(values) and max(unique_indices) <= len(values)


def _has_merge_intent(compact: str) -> bool:
    if not compact:
        return False
    if re.search(r"(?:合成|并成|归并|归为|合到|并到).{0,4}(?:一条|一项|一个|一起|同一)", compact):
        return True
    merge_tokens = (
        "合并",
        "和并",
        "并今日工作",
        "并今天工作",
        "并当前工作",
        "同一点",
        "同一条",
        "同一项",
        "一回事",
        "一件事",
        "不要拆这么碎",
        "别拆这么碎",
        "拆太碎",
        "合并同类项",
        "同类项",
    )
    return any(token in compact for token in merge_tokens)


def _has_explicit_delete_intent(compact: str) -> bool:
    if not compact:
        return False
    if _has_merge_intent(compact):
        return False
    return any(token in compact for token in ("删除", "删掉", "删了", "删", "去掉", "移除", "不要", "后面重复"))


def _delete_action_without_delete_request(action: AgentAction, raw_input: str) -> bool:
    if action.type != "delete_item":
        return False
    compact = _compact(raw_input)
    if _has_explicit_delete_intent(compact):
        return False
    return True

def _text_replace_without_replace_request(action: AgentAction, raw_input: str) -> bool:
    if action.type != "replace_text":
        return False
    compact = _compact(raw_input)
    has_range = bool(re.search(r"\d{1,2}(到|至|~|～|-|—|－)\d{1,2}", compact))
    if not has_range:
        return False
    return not any(token in compact for token in ("改成", "改为", "替换", "换成", "修正", "不是"))


def _looks_like_vague_relative_delete(text: str) -> bool:
    if not any(marker in text for marker in ("删", "不要", "去掉", "移除")):
        return False
    if any(marker in text for marker in ("后面重复", "后边重复", "后面的重复", "下面重复", "重复的", "重复项")):
        return True
    if any(marker in text for marker in ("一条", "1条", "一个", "一项", "某条", "任意一条", "随便一条")):
        return True
    return any(
        marker in text
        for marker in (
            "后面几条",
            "后面那几条",
            "后面的几条",
            "后边几条",
            "后边那几条",
            "下面几条",
            "下面那几条",
            "前面几条",
            "前面那几条",
            "上面几条",
            "上面那几条",
            "那几条",
            "这几条",
            "这些",
            "那些",
        )
    )


def _has_explicit_delete_reference(text: str) -> bool:
    number = r"\d+|[一二三四五六七八九十两俩〇零]+"
    if re.search(rf"第?({number})(到|至|-|~|—)第?({number})条?", text):
        return True
    if re.search(rf"第({number})条", text):
        return True
    return False


def _batch_confirmation_message(actions: list[AgentAction], existing: DailyReport | None) -> str:
    confirming = [action for action in actions if action.requires_confirmation]
    if len(confirming) == 1:
        return confirming[0].confirmation_message or _confirmation_message_for_action(confirming[0], existing)
    if not confirming:
        return "这些操作需要确认，是否执行？"
    lines = [
        action.confirmation_message or _confirmation_message_for_action(action, existing)
        for action in confirming
    ]
    return "这些操作需要确认，是否执行？\n" + "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))


def _pending_interaction_from_action(action: AgentAction, existing: DailyReport | None) -> dict[str, Any] | None:
    target_field = _action_field(action)
    operation = _pending_operation_for_action(action)
    if not operation:
        return None
    resume_pending = None
    if action.type == "update_historical_report":
        resume_pending = _historical_context_pending(existing)
    elif action.type in {"delete_item", "clear_field", "clear_all", "replace_field", "replace_text", "merge_items", "move_item", "append_items", "polish_items"}:
        resume_pending = _current_context_pending(existing)
    edit_cursor = normalize_edit_cursor(resume_pending) if resume_pending is not None else None
    context: dict[str, Any] = {
        "action": action.model_dump(),
        "item_indices": list(action.item_indices),
        "target_item_index": action.target_item_index,
        "source_item_text": action.source_item_text,
        "old_value": action.old_value,
        "new_value": action.new_value,
        "items": list(action.items),
    }
    if resume_pending is not None:
        context["resume_pending_interaction"] = resume_pending
    if edit_cursor is not None:
        context["edit_cursor"] = edit_cursor
    pending = {
        "type": "awaiting_action_confirmation",
        "operation": operation,
        "target_field": target_field,
        "context": context,
    }
    return _normalize_pending_interaction_for_existing(pending, existing)


def _with_pending_scope(
    pending_interaction: dict[str, Any],
    *,
    user: User,
    existing: DailyReport | None,
    report_date: date,
) -> dict[str, Any]:
    pending = dict(pending_interaction or {})
    context = dict(pending.get("context") or {})
    context[PENDING_SCOPE_KEY] = {
        "user_id": str(getattr(user, "id", "") or ""),
        "team_id": str(getattr(user, "team_id", "") or ""),
        "report_date": report_date.isoformat(),
        "report_id": str(getattr(existing, "id", "") or ""),
    }
    pending["context"] = context
    return pending


def _confirmation_message_for_action(action: AgentAction, existing: DailyReport | None) -> str:
    if action.confirmation_message:
        return action.confirmation_message
    label = _field_label(_action_field(action))
    values = _values_for_existing(existing, _action_field(action))
    normalized_indices, _ = _normalize_indices(action.item_indices, len(values))
    if action.type == "delete_item":
        if len(normalized_indices) == 1 and 1 <= normalized_indices[0] <= len(values):
            item = values[normalized_indices[0] - 1]
            return f"你确认要删除{label}第{normalized_indices[0]}条“{item}”这条吗？回复“确认”执行，回复“取消”保留。"
        if normalized_indices:
            return _delete_items_confirmation_message(label, normalized_indices, values)
        if action.source_item_text:
            return f"你确认要删除{label}里的“{action.source_item_text}”这条吗？回复“确认”执行，回复“取消”保留。"
        return f"你确认要删除{label}中的这项内容吗？回复“确认”执行，回复“取消”保留。"
    if action.type == "merge_items" and normalized_indices:
        joined = "、".join(str(index) for index in normalized_indices)
        return f"确认把{label}第{joined}条合并为一条吗？回复“确认”执行，回复“取消”保留。"
    if action.type in {"clear_field", "clear_all"}:
        return "确认清空当前日报内容吗？" if action.type == "clear_all" else f"确认清空{label}吗？"
    if action.type == "update_historical_report":
        target_date = action.target_date or "目标日期"
        if action.source == "replace_report":
            return f"确认用这段内容整体替换 {target_date} 日报吗？"
        if action.source == "delete_items" and action.item_indices:
            joined = "、".join(str(index) for index in action.item_indices)
            return f"你确认要删除 {target_date} 日报“{label}”第{joined}条吗？回复“确认”执行，回复“取消”保留。"
        if action.source == "merge_items" and action.item_indices:
            joined = "、".join(str(index) for index in action.item_indices)
            return f"确认把 {target_date} 日报“{label}”第{joined}条合并为一条吗？回复“确认”执行，回复“取消”保留。"
        if action.source == "clear_field":
            return f"确认清空 {target_date} 日报的“{label}”吗？"
        if action.source == "replace_text" and action.old_value and action.new_value:
            return f"确认把 {target_date} 日报“{label}”里的“{action.old_value}”改成“{action.new_value}”吗？"
        if action.source == "append_items" and action.items:
            return f"确认向 {target_date} 日报的“{label}”补充“{'；'.join(action.items)}”吗？"
        replacement = "；".join(action.items) if action.items else action.new_value
        if replacement:
            return f"确认把 {target_date} 日报的“{label}”改成“{replacement}”吗？"
        return f"确认修改 {target_date} 日报的“{label}”吗？"
    if action.type == "unsubmit_report":
        return "确认撤回已提交日报吗？"
    return action.confirmation_message or "该操作会影响已有日报内容，请确认是否执行。"


def _delete_items_confirmation_message(label: str, indices: list[int], values: list[str]) -> str:
    valid = [index for index in indices if 1 <= index <= len(values)]
    if valid:
        lines = [f"你确认要删除{label}以下 {len(valid)} 条吗？"]
        lines.extend(f"- 第{index}条“{values[index - 1]}”" for index in valid[:8])
        if len(valid) > 8:
            lines.append(f"- 另有 {len(valid) - 8} 条")
        lines.append("回复“确认”执行，回复“取消”保留。")
        return "\n".join(lines)
    joined = "、".join(str(index) for index in indices)
    return f"你确认要删除{label}第{joined}条吗？回复“确认”执行，回复“取消”保留。"


def _pending_operation_for_action(action: AgentAction) -> str:
    if action.type == "delete_item":
        return "delete_report_item"
    if action.type in {"replace_text", "replace_field", "polish_items", "merge_items", "move_item"}:
        return "modify_report_item"
    if action.type == "clear_field":
        return "clear_section"
    if action.type == "clear_all":
        return "high_risk_clear_all"
    if action.type == "submit_report":
        return "submit_report"
    if action.type == "unsubmit_report":
        return "unsubmit_report"
    if action.type == "update_historical_report":
        return "update_historical_report"
    return ""


def _action_field(action: AgentAction) -> str:
    if action.field in REPORT_FIELDS:
        return action.field
    if action.target_field in REPORT_FIELDS:
        return action.target_field
    if action.source_field in REPORT_FIELDS:
        return action.source_field
    return "none"


def _normalize_pending_interaction_for_existing(
    pending_interaction: dict[str, Any],
    existing: DailyReport | None,
) -> dict[str, Any]:
    pending = dict(pending_interaction)
    context = dict(pending.get("context") or {})
    target_field = str(pending.get("target_field") or context.get("target_field") or "none")
    if (
        pending.get("type") == "awaiting_append_target_confirmation"
        and target_field in REPORT_FIELDS
        and (context.get("candidate_items") or context.get("items") or context.get("candidate"))
    ):
        pending["type"] = "awaiting_content_quality_confirmation"
    values = _values_for_existing(existing, target_field)
    item_indices, normalized = _normalize_indices(context.get("item_indices"), len(values))
    if item_indices:
        context["item_indices"] = item_indices
    action_payload = context.get("action")
    if isinstance(action_payload, dict):
        action_payload = dict(action_payload)
        if item_indices:
            action_payload["item_indices"] = item_indices
        if action_payload.get("target_item_index") == 0 and values:
            action_payload["target_item_index"] = 1
            normalized = True
        context["action"] = action_payload
    if normalized:
        logger.warning(
            "agent_index_normalized before=%s after=%s",
            (pending_interaction.get("context") or {}).get("item_indices"),
            item_indices,
        )
    pending["context"] = context
    if pending.get("type") in {"historical_report_edit_flow", "awaiting_dated_report_action", "current_report_edit_flow"}:
        pending = with_edit_cursor(pending)
        try:
            current_expiry = int(pending.get("expires_after_turns", 0))
        except (TypeError, ValueError):
            current_expiry = 0
        pending["expires_after_turns"] = max(current_expiry, 12)
    return pending


def _normalize_action_for_existing(
    action: AgentAction,
    before: tuple[list[str], list[str], list[str]],
    *,
    raw_input: str = "",
) -> AgentAction:
    action = _infer_missing_move_fields(action, raw_input=raw_input)
    action = _coerce_move_source_field_from_target(action, before)
    action = _coerce_negative_target_move_to_noop(action, raw_input=raw_input)
    action = _coerce_non_problem_move_to_noop(action, before)
    action = _coerce_resolved_problem_action_to_work(action)
    action = _coerce_text_patch_action(action, before, raw_input=raw_input)
    action = _redirect_replace_text_field_if_unique(action, before)
    values = _values_for_field(before, action.field)
    if action.type == "delete_item" and action.field in REPORT_FIELDS and _refers_to_recent_added_item(raw_input):
        if values and action.item_indices and all(index > len(values) for index in action.item_indices):
            payload = action.model_dump()
            payload["item_indices"] = [len(values)]
            return AgentAction.model_validate(payload)
    item_indices, normalized = _normalize_indices(action.item_indices, len(values))
    payload = action.model_dump()
    payload["item_indices"] = item_indices
    if action.target_item_index == 0 and values:
        payload["target_item_index"] = 1
        normalized = True
    if normalized:
        logger.warning("agent_index_normalized before=%s after=%s", action.item_indices, item_indices)
    return AgentAction.model_validate(payload)


def _infer_missing_move_fields(action: AgentAction, *, raw_input: str) -> AgentAction:
    if action.type != "move_item":
        return action
    payload = action.model_dump()
    compact = _compact(raw_input)
    if payload.get("source_field") in (None, "", "none"):
        payload["source_field"] = "today_work"
    if payload.get("target_field") in (None, "", "none"):
        if any(token in compact for token in ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u8ba1\u5212", "\u5b89\u6392")):
            payload["target_field"] = "tomorrow_plan"
        elif any(token in compact for token in ("\u95ee\u9898", "\u98ce\u9669", "\u56f0\u96be", "\u5361\u70b9")):
            payload["target_field"] = "problems"
    if payload.get("field") in (None, "", "none") or payload.get("field") == payload.get("target_field"):
        payload["field"] = payload.get("source_field") or "today_work"
    return AgentAction.model_validate(payload)


def _coerce_move_source_field_from_target(
    action: AgentAction,
    before: tuple[list[str], list[str], list[str]],
) -> AgentAction:
    if action.type != "move_item" or action.target_field not in REPORT_FIELDS:
        return action
    target_values = _values_for_field(before, action.target_field)
    indices, _ = _normalize_indices(action.item_indices, len(target_values))
    if not indices or any(1 <= index <= len(target_values) for index in indices):
        return action
    source_field = action.source_field if action.source_field in REPORT_FIELDS else action.field
    if source_field != action.target_field and action.field != action.target_field:
        return action
    candidates = [
        field
        for field in REPORT_FIELDS
        if field != action.target_field and all(1 <= index <= len(_values_for_field(before, field)) for index in indices)
    ]
    if len(candidates) != 1:
        return action
    payload = action.model_dump()
    payload["field"] = candidates[0]
    payload["source_field"] = candidates[0]
    return AgentAction.model_validate(payload)


def _coerce_negative_target_move_to_noop(action: AgentAction, *, raw_input: str) -> AgentAction:
    if action.type != "move_item" or action.target_field != "tomorrow_plan":
        return action
    compact = _compact(raw_input)
    if not any(token in compact for token in ("不是明天计划", "不是明日计划", "不是计划", "不属于明天计划", "不属于明日计划")):
        return action
    payload = action.model_dump()
    payload.update({"type": "no_op", "field": "none", "source_field": "none", "target_field": "none", "item_indices": []})
    return AgentAction.model_validate(payload)


def _coerce_non_problem_move_to_noop(action: AgentAction, before: tuple[list[str], list[str], list[str]]) -> AgentAction:
    if action.type != "move_item" or action.target_field != "problems":
        return action
    values = _values_for_field(before, action.source_field)
    if not values:
        return action
    indices, _ = _normalize_indices(action.item_indices, len(values))
    moved = [values[index - 1] for index in indices if 1 <= index <= len(values)]
    if not moved:
        return action
    if any(_looks_like_problem_item(item) for item in moved):
        return action
    if not all(_looks_like_work_or_effect_item(item) for item in moved):
        return action
    payload = action.model_dump()
    payload.update({"type": "no_op", "field": "none", "source_field": "none", "target_field": "none", "item_indices": []})
    return AgentAction.model_validate(payload)


def _looks_like_problem_item(value: str) -> bool:
    compact = _compact(value)
    if not compact:
        return False
    return any(
        token in compact
        for token in (
            "\u95ee\u9898",
            "\u98ce\u9669",
            "\u56f0\u96be",
            "\u5361\u70b9",
            "\u5f02\u5e38",
            "\u5931\u8d25",
            "\u62a5\u9519",
            "\u672a\u5b8c\u6210",
            "\u6ca1\u5b8c\u6210",
            "\u9700\u8981\u652f\u6301",
        )
    )


def _looks_like_work_or_effect_item(value: str) -> bool:
    compact = _compact(value)
    if not compact:
        return False
    return any(
        token in compact
        for token in (
            "\u4f18\u5316",
            "\u4fee\u590d",
            "\u7a33\u5b9a",
            "\u6536\u7d27",
            "\u66f4\u6e05\u695a",
            "\u66f4\u4fdd\u5b88",
            "\u66f4\u7a33",
            "\u5b8c\u6210",
            "\u5904\u7406",
            "\u6c9f\u901a",
            "\u4e0b\u8f7d",
            "\u7b7e\u7f72",
            "LLM",
            "\u5f71\u5b50\u8bb0\u5fc6",
            "\u903b\u8f91",
            "\u7cfb\u7edf",
        )
    )


def _coerce_resolved_problem_action_to_work(action: AgentAction) -> AgentAction:
    if action.type not in {"append_items", "replace_field"} or action.field != "problems":
        return action
    if not action.items or not all(_looks_like_resolved_work_item(item) for item in action.items):
        return action
    payload = action.model_dump()
    payload["field"] = "today_work"
    return AgentAction.model_validate(payload)


def _looks_like_resolved_work_item(value: str) -> bool:
    compact = _compact(value)
    if not compact:
        return False
    if any(token in compact for token in ("风险", "问题", "缺", "未", "没", "慢", "影响")):
        return False
    return any(token in compact for token in ("补齐", "补全", "完成", "已处理", "已解决", "用印材料"))


def _refers_to_recent_added_item(raw_input: str) -> bool:
    compact = _compact(raw_input)
    return any(token in compact for token in ("刚补的那条", "刚补那条", "刚加的那条", "刚加那条", "刚才那条", "刚才这个", "刚新增的"))


def _coerce_text_patch_action(
    action: AgentAction,
    before: tuple[list[str], list[str], list[str]],
    *,
    raw_input: str,
) -> AgentAction:
    if action.type not in {"replace_field", "polish_items"} or action.field not in REPORT_FIELDS:
        return action
    old_value, new_value = _parse_inline_replace_text(raw_input)
    if not old_value or not new_value:
        return action
    target_field = _field_containing_text(before, old_value)
    if target_field is None:
        return action
    payload = action.model_dump()
    payload.update(
        {
            "type": "replace_text",
            "field": target_field,
            "old_value": old_value,
            "new_value": new_value,
            "items": [],
        }
    )
    logger.warning(
        "agent_patch_action_coerced raw_input=%s original_type=%s original_field=%s target_field=%s",
        raw_input,
        action.type,
        action.field,
        target_field,
    )
    return AgentAction.model_validate(payload)


def _redirect_replace_text_field_if_unique(action: AgentAction, before: tuple[list[str], list[str], list[str]]) -> AgentAction:
    if action.type != "replace_text" or not action.old_value:
        return action
    current_values = _values_for_field(before, action.field)
    if any(action.old_value in value for value in current_values):
        return action
    target_field = _field_containing_text(before, action.old_value)
    if target_field is None or target_field == action.field:
        return action
    payload = action.model_dump()
    payload["field"] = target_field
    logger.warning(
        "agent_patch_field_redirected old_value=%s before_field=%s after_field=%s",
        action.old_value,
        action.field,
        target_field,
    )
    return AgentAction.model_validate(payload)


def _field_containing_text(before: tuple[list[str], list[str], list[str]], text: str) -> str | None:
    matches: list[str] = []
    for field in REPORT_FIELDS:
        values = _values_for_field(before, field)
        if any(text in value for value in values):
            matches.append(field)
    return matches[0] if len(matches) == 1 else None


def _parse_inline_replace_text(raw_input: str) -> tuple[str, str]:
    text = (raw_input or "").strip()
    match = re.match(r"^(.{1,40}?)(?:改成|改为|换成|更正为)(.{1,80})$", text)
    if not match:
        return "", ""
    old_value = _strip_patch_side(match.group(1))
    new_value = _strip_patch_side(match.group(2))
    if not old_value or not new_value or old_value in REPORT_FIELDS:
        return "", ""
    return old_value, new_value


def _strip_patch_side(value: str) -> str:
    return str(value or "").strip(" ：:，,。；;“”\"'")


def _normalize_indices(value: Any, item_count: int) -> tuple[list[int], bool]:
    candidates = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    raw: list[int] = []
    for item in candidates:
        try:
            raw.append(int(item))
        except (TypeError, ValueError):
            continue
    if not raw:
        return [], False
    if any(index == 0 for index in raw):
        normalized = [index + 1 for index in raw if index >= 0]
        return normalized, normalized != raw
    return [index for index in raw if index > 0], False


def _values_for_existing(existing: DailyReport | None, field: str) -> list[str]:
    if existing is None:
        return []
    if field == "today_work":
        return list(existing.today_work or [])
    if field == "problems":
        return list(existing.problems or [])
    if field == "tomorrow_plan":
        return list(existing.tomorrow_plan or [])
    return []


def _no_change_message(plan: ActionPlan, existing: DailyReport | None) -> str:
    for action in plan.actions:
        field = _action_field(action)
        if action.type in {
            "complete_previous_plan_item",
            "complete_all_previous_plan_items",
            "rollover_previous_plan_items",
        }:
            if action.completed_items or action.unfinished_items or action.item_indices or action.source_item_text:
                if existing is not None:
                    return "相关事项已在当前日报草稿中，我没有重复添加。\n\n" + _build_current_report_preview(existing)
                return "相关事项已记录过，我没有重复添加。"
            return "我没有找到昨天的待办内容。你可以把昨天的明日计划发我，我来帮你转成今天的完成事项。"
        if action.type == "delete_item" and field in REPORT_FIELDS:
            values = _values_for_existing(existing, field)
            if values:
                invalid_indices = [index for index in action.item_indices if index > len(values)]
                if invalid_indices:
                    requested = "、".join(str(index) for index in invalid_indices)
                    return f"没有第{requested}条。当前{_field_label(field)}只有 {len(values)} 条，请重新说明要删除哪一条。"
                available = "\n".join(f"{index}. {item}" for index, item in enumerate(values, start=1))
                return f"没有找到要删除的条目。当前{_field_label(field)}只有：\n\n{available}\n\n你可以回复要删除的编号，或者说“取消”。"
            return f"当前{_field_label(field)}没有可删除的内容。"
        if action.type == "merge_items" and field in REPORT_FIELDS:
            values = _values_for_existing(existing, field)
            if values:
                available = "\n".join(f"{index}. {item}" for index, item in enumerate(values, start=1))
                return f"没有定位到要合并的条目。当前{_field_label(field)}是：\n\n{available}\n\n你可以回复要合并的编号，例如“合并第2、3、4条”。"
            return f"当前{_field_label(field)}没有可合并的内容。"
        if action.type == "move_item":
            source_field = action.source_field if action.source_field in REPORT_FIELDS else action.field
            target_field = action.target_field if action.target_field in REPORT_FIELDS else "none"
            values = _values_for_existing(existing, source_field)
            if source_field in REPORT_FIELDS and target_field in REPORT_FIELDS:
                available = "\n".join(f"{index}. {item}" for index, item in enumerate(values, start=1))
                if available:
                    return f"没有定位到要移动的条目。当前{_field_label(source_field)}是：\n\n{available}\n\n请重新说明要移动哪一条。"
                return f"当前{_field_label(source_field)}没有可移动的内容。"
    return plan.reply_to_user or "没有找到可修改的日报内容，请重新说明要操作哪一项。"


def _clear_agent_pending(section_status: dict[str, Any]) -> dict[str, Any]:
    updated = dict(section_status)
    updated.pop("_pending_interaction", None)
    updated.pop("_pending_quality_clarification", None)
    updated.pop("_pending_draft_edit", None)
    return updated


def _preserve_agent_section_status(previous: dict[str, Any], current: dict[str, Any]) -> None:
    for key in (REFERENCE_REPORT_CONTEXT_KEY, LAST_MODIFIED_ITEM_KEY, CORRECTION_TARGET_KEY):
        if key in previous and key not in current:
            current[key] = previous[key]


def _low_confidence_report_fragment_plan(raw_input: str, existing: DailyReport | None) -> ActionPlan | None:
    text = str(raw_input or "").strip()
    if not _is_meaningful_low_confidence_report_fragment(text):
        return None
    if existing is not None and any((existing.today_work, existing.problems, existing.tomorrow_plan)):
        return None
    return ActionPlan(
        intent="fill_report",
        confidence="high",
        should_write=True,
        actions=[AgentAction(type="append_items", field="today_work", items=[text])],
        reason="Accepted meaningful low-confidence fragment as initial today_work.",
    )


def _is_meaningful_low_confidence_report_fragment(text: str) -> bool:
    compact = _compact(text)
    if len(compact) < 2:
        return False
    if _looks_like_operation_or_repair_phrase_content(text, text):
        return False
    if re.fullmatch(r"(哈|哈哈|哈哈哈|呵|呵呵|嗯|恩|哦|噢|额|呃|啊|呀|行|好|可以|随便|不知道|你猜)+", compact):
        return False
    if re.fullmatch(r"[\W_]+", compact, flags=re.UNICODE):
        return False
    blocked_tokens = (
        "你是谁",
        "怎么用",
        "能干嘛",
        "昨天日报",
        "昨日日报",
        "前天日报",
        "发我看",
        "给我看",
        "查一下",
        "查下",
        "撤回",
        "删除",
        "清空",
    )
    if any(token in compact for token in blocked_tokens):
        return False
    return True


def _unsafe_report_content_write_message(action: AgentAction, raw_input: str) -> str:
    if action.type not in {"append_items", "replace_field", "replace_text", "polish_items", "update_historical_report"}:
        return ""
    if _action_field(action) not in REPORT_FIELDS:
        return ""
    candidates: list[str] = []
    if action.type in {"append_items", "replace_field", "polish_items"}:
        candidates.extend(str(item or "") for item in (action.items or []))
    elif action.type == "replace_text":
        candidates.append(str(action.new_value or ""))
    elif action.type == "update_historical_report":
        candidates.extend(_clean_action_items(action))
        if action.new_value:
            candidates.append(str(action.new_value or ""))
    if not candidates:
        return ""
    for candidate in candidates:
        if _looks_like_operation_or_repair_phrase_content(candidate, raw_input):
            return (
                "\u8fd9\u53e5\u66f4\u50cf\u786e\u8ba4\u3001\u7ea0\u9519\u6216\u65e5\u671f\u8bf4\u660e\uff0c\u6211\u5148\u4e0d\u5199\u8fdb\u65e5\u62a5\u6b63\u6587\u3002"
                "\u5982\u679c\u662f\u8981\u63d0\u4ea4\uff0c\u8bf7\u56de\u590d\u201c\u786e\u8ba4\u63d0\u4ea4\u201d\uff1b"
                "\u5982\u679c\u662f\u8981\u6539\u67d0\u5929\u65e5\u62a5\uff0c\u8bf7\u8bf4\u660e\u65e5\u671f\u548c\u8981\u6539\u7684\u5185\u5bb9\u3002"
            )
    return ""


def _looks_like_operation_or_repair_phrase_content(text: str, raw_input: str = "") -> bool:
    compact = _compact(text)
    if not compact:
        return False
    exact_phrases = {
        "\u786e\u8ba4",
        "\u786e\u8ba4\u63d0\u4ea4",
        "\u786e\u5b9a\u63d0\u4ea4",
        "\u63d0\u4ea4",
        "\u63d0\u4ea4\u65e5\u62a5",
        "\u786e\u8ba4\u63d0\u4ea4\u65e5\u62a5",
        "\u662f\u7684",
        "\u5bf9",
        "\u5bf9\u7684",
        "\u55ef",
        "\u6069",
        "\u53ef\u4ee5",
        "\u597d",
        "\u597d\u7684",
        "ok",
        "okay",
        "\u9519\u4e86",
        "\u4e0d\u5bf9",
        "\u4e0d\u662f",
        "\u4e0d\u662f\u8fd9\u4e2a",
        "\u5df2\u7ecf\u8bf4\u4e86",
        "\u65e0\u8bed",
        "\u5b7a\u5b50\u4e0d\u53ef\u6559",
        "\u6309\u4e0a\u4e00\u6761\u8bed\u97f3",
        "\u6309\u6211\u4e0a\u4e00\u6761\u8bed\u97f3",
        "\u4e0a\u4e00\u6761\u8bed\u97f3",
        "\u8fd9\u662f24\u53f7\u7684",
        "\u8fd9\u662f24\u65e5\u7684",
        "\u8fd9\u662f\u6628\u5929\u7684",
        "\u8fd9\u4e2a\u662f24\u53f7\u7684",
        "\u8fd9\u4e2a\u8bb0\u4e3a24\u53f7",
        "\u4e0d\u662f\u4eca\u5929\u7684",
    }
    if compact in exact_phrases:
        return True
    if len(compact) <= 14 and any(marker in compact for marker in ("\u786e\u8ba4\u63d0\u4ea4", "\u5b7a\u5b50\u4e0d\u53ef\u6559", "\u5df2\u7ecf\u8bf4\u4e86", "\u4e0a\u4e00\u6761\u8bed\u97f3")):
        return True
    if len(compact) <= 18 and any(marker in compact for marker in ("24\u53f7", "24\u65e5", "\u6628\u5929")) and any(marker in compact for marker in ("\u8fd9\u662f", "\u8fd9\u4e2a\u662f", "\u8bb0\u4e3a", "\u4e0d\u662f\u4eca\u5929")):
        return True
    if len(compact) <= 10 and any(marker in compact for marker in ("\u9519\u4e86", "\u4e0d\u5bf9", "\u4e0d\u662f\u8fd9\u4e2a", "\u65e0\u8bed")):
        return True
    return False


def _action_touched_fields(action: AgentAction) -> set[str]:
    if action.type in {"clear_all", "restore_snapshot"}:
        return set(REPORT_FIELDS)
    fields: set[str] = set()
    if action.field in REPORT_FIELDS:
        fields.add(action.field)
    if action.source_field in REPORT_FIELDS:
        fields.add(action.source_field)
    if action.target_field in REPORT_FIELDS:
        fields.add(action.target_field)
    return fields


def _restore_untouched_existing_fields(
    existing: DailyReport | None,
    *,
    touched_fields: set[str],
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> tuple[list[str], list[str], list[str]]:
    if existing is None:
        return today_work, problems, tomorrow_plan
    values = {
        "today_work": list(today_work),
        "problems": list(problems),
        "tomorrow_plan": list(tomorrow_plan),
    }
    previous = {
        "today_work": list(existing.today_work or []),
        "problems": list(existing.problems or []),
        "tomorrow_plan": list(existing.tomorrow_plan or []),
    }
    for field in REPORT_FIELDS:
        if field not in touched_fields and previous[field] and len(values[field]) < len(previous[field]):
            values[field] = previous[field]
    return values["today_work"], values["problems"], values["tomorrow_plan"]


def _apply_action(
    action: AgentAction,
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> tuple[list[str], list[str], list[str], bool]:
    lists = {
        "today_work": list(today_work),
        "problems": list(problems),
        "tomorrow_plan": list(tomorrow_plan),
    }
    destructive = False
    if action.type == "append_items" and action.field in REPORT_FIELDS:
        target = list(lists[action.field])
        if _is_placeholder_list(action.field, target) and action.items:
            target = []
        if action.source == "direct_explicit_append":
            lists[action.field] = _append_items_exact(target, action.items)
        else:
            lists[action.field] = _merge_items(target, action.items)
    elif action.type == "replace_field" and action.field in REPORT_FIELDS:
        lists[action.field] = list(action.items)
        destructive = True
    elif action.type == "replace_text" and action.field in REPORT_FIELDS:
        replaced = _replace_text_in_items(lists[action.field], action)
        if replaced is not None:
            lists[action.field] = replaced
            destructive = True
    elif action.type == "clear_field" and action.field in REPORT_FIELDS:
        lists[action.field] = []
        destructive = True
    elif action.type == "clear_all":
        lists = {"today_work": [], "problems": [], "tomorrow_plan": []}
        destructive = True
    elif action.type == "delete_item" and action.field in REPORT_FIELDS:
        lists[action.field] = _delete_items(lists[action.field], action)
        destructive = True
    elif action.type == "merge_items" and action.field in REPORT_FIELDS:
        merged = _merge_items_by_indices(lists[action.field], action)
        if merged is not None:
            lists[action.field] = merged
            destructive = True
    elif action.type == "restore_snapshot" and isinstance(action.reference_report, dict):
        lists = {
            "today_work": _clean_report_reference_items(action.reference_report.get("today_work")),
            "problems": _clean_report_reference_items(action.reference_report.get("problems")),
            "tomorrow_plan": _clean_report_reference_items(action.reference_report.get("tomorrow_plan")),
        }
        destructive = True
    elif action.type == "move_item" and action.source_field in REPORT_FIELDS and action.target_field in REPORT_FIELDS:
        source = list(lists[action.source_field])
        selected = _select_items(source, action)
        if selected:
            for item in selected:
                source = [value for value in source if value != item]
            target = list(lists[action.target_field])
            if _is_placeholder_list(action.target_field, target):
                target = []
            lists[action.source_field] = source
            lists[action.target_field] = _merge_items(target, selected)
            destructive = True
    elif action.type == "polish_items":
        if action.field in REPORT_FIELDS and action.items:
            lists[action.field] = list(action.items)
            destructive = True
    return lists["today_work"], lists["problems"], lists["tomorrow_plan"], destructive


def _apply_historical_update_action(values: list[str], action: AgentAction) -> list[str] | None:
    mode = (action.source or "").strip()
    replacement = _clean_action_items(action)
    if mode == "clear_field":
        return []
    if mode == "delete_items":
        return _delete_items(values, AgentAction(type="delete_item", field=action.field, item_indices=action.item_indices, source_item_text=action.source_item_text))
    if mode == "merge_items":
        return _merge_items_by_indices(values, action)
    if mode == "replace_text" or (action.old_value and action.new_value):
        target_item_index = action.target_item_index
        if target_item_index is None and len(action.item_indices) == 1:
            target_item_index = action.item_indices[0]
        return _replace_text_in_items(
            values,
            AgentAction(
                type="replace_text",
                field=action.field,
                old_value=action.old_value,
                new_value=action.new_value,
                target_item_index=target_item_index,
            ),
        )
    if mode == "replace_items" and action.item_indices:
        if not replacement:
            return None
        return _replace_items_by_indices(values, action.item_indices, replacement)
    if mode == "append_items":
        return _merge_items(values, replacement)
    if not replacement:
        return None
    return replacement


def _report_from_action_reference(action: AgentAction) -> dict[str, list[str]] | None:
    report = action.reference_report if isinstance(action.reference_report, dict) else {}
    parsed = {
        "today_work": _clean_report_reference_items(report.get("today_work")),
        "problems": _clean_report_reference_items(report.get("problems")),
        "tomorrow_plan": _clean_report_reference_items(report.get("tomorrow_plan")),
    }
    if not any(parsed.values()):
        return None
    return parsed


def _restore_snapshot_from_saved_state(action: AgentAction, section_status: dict[str, Any]) -> AgentAction:
    if action.type != "restore_snapshot":
        return action
    if _report_reference_has_content(action.reference_report):
        return action
    snapshot = section_status.get("_previous_draft_snapshot")
    if not _report_reference_has_content(snapshot):
        return action
    return action.model_copy(update={"reference_report": snapshot})


def _report_reference_has_content(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return any(_clean_report_reference_items(value.get(field)) for field in REPORT_FIELDS)


def _clean_report_reference_items(value: Any) -> list[str]:
    candidates = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    return [text for item in candidates if (text := str(item or "").strip())]


def _clean_action_items(action: AgentAction) -> list[str]:
    candidates = list(action.items or [])
    if not candidates and action.new_value:
        candidates = [action.new_value]
    return [item.strip() for item in candidates if item and item.strip()]


def _replace_items_by_indices(values: list[str], item_indices: list[int], replacement: list[str]) -> list[str] | None:
    if not item_indices:
        return None
    normalized = sorted({index for index in item_indices if index > 0})
    if any(index > len(values) for index in normalized):
        return None
    updated = list(values)
    first = normalized[0]
    for index in sorted(normalized, reverse=True):
        updated.pop(index - 1)
    for offset, item in enumerate(replacement):
        updated.insert(first - 1 + offset, item)
    return updated


def _merge_items_by_indices(values: list[str], action: AgentAction) -> list[str] | None:
    normalized = sorted({index for index in action.item_indices if 1 <= index <= len(values)})
    if len(normalized) < 2:
        return None
    replacement = _clean_action_items(action)
    if replacement:
        merged_value = "；".join(replacement)
    else:
        merged_value = "，".join(str(values[index - 1]).strip() for index in normalized if str(values[index - 1]).strip())
    merged_value = merged_value.strip(" ，,。；;、")
    if not merged_value:
        return None
    updated: list[str] = []
    selected = set(normalized)
    first = normalized[0]
    for position, item in enumerate(values, start=1):
        if position == first:
            updated.append(merged_value)
        if position in selected:
            continue
        updated.append(item)
    return updated


def _merge_items(existing: list[str], incoming: list[str]) -> list[str]:
    result = [item.strip() for item in existing if item and item.strip()]
    for item in incoming:
        text = item.strip()
        if text and not _contains_equivalent(result, text):
            result.append(text)
    return result


def _append_items_exact(existing: list[str], incoming: list[str]) -> list[str]:
    result = [item.strip() for item in existing if item and item.strip()]
    seen = {_compact(item) for item in result}
    for item in incoming:
        text = str(item or "").strip()
        key = _compact(text)
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _contains_equivalent(values: list[str], incoming: str) -> bool:
    compact_incoming = _compact(incoming)
    for value in values:
        compact_value = _compact(value)
        if compact_value == compact_incoming:
            return True
        if compact_incoming and compact_value and (compact_incoming in compact_value or compact_value in compact_incoming):
            return True
    return False


def _action_success_message(action: AgentAction, before: tuple[list[str], list[str], list[str]]) -> str:
    if action.type == "restore_snapshot":
        return "已恢复刚才的日报内容。"
    if action.field not in REPORT_FIELDS:
        return ""
    values = _values_for_field(before, action.field)
    label = _field_label(action.field)
    if action.type == "delete_item":
        indices = sorted({index for index in action.item_indices if 1 <= index <= len(values)})
        if not indices:
            return ""
        if len(indices) == 1:
            item = values[indices[0] - 1]
            return f"已删除{label}第{indices[0]}条“{item}”。如需恢复，可回复“撤销”。"
        joined = "、".join(str(index) for index in indices)
        return f"已删除{label}第{joined}条。"
    if action.type == "append_items":
        if action.reason == "restore_overwritten_new" and action.items:
            return f"已补充“{'；'.join(action.items)}”。"
        return f"已记录到{label}。"
    if action.type == "merge_items":
        indices = sorted({index for index in action.item_indices if 1 <= index <= len(values)})
        if len(indices) < 2:
            return ""
        joined = "、".join(str(index) for index in indices)
        return f"已合并{label}第{joined}条。"
    if action.type in {"replace_field", "replace_text", "polish_items"}:
        if action.reason == "restore_overwritten_old" and action.new_value:
            index = action.target_item_index or (action.item_indices[0] if action.item_indices else None)
            suffix = f"第{index}条" if index else ""
            return f"抱歉，刚才把内容错误覆盖了。已恢复{label}{suffix}“{action.new_value}”。"
        return f"已更新{label}。"
    if action.type == "clear_field":
        return f"已清空{label}。"
    return ""


def _last_modified_item_payload(
    action: AgentAction,
    *,
    before: tuple[list[str], list[str], list[str]],
    after: tuple[list[str], list[str], list[str]],
    user: User,
    existing: DailyReport | None,
    report_date: date,
    raw_input: str,
    received_at: datetime,
) -> dict[str, Any] | None:
    field = _action_field(action)
    if field not in REPORT_FIELDS:
        return None
    before_values = _values_for_field(before, field)
    after_values = _values_for_field(after, field)
    item_index: int | None = None
    old_content = ""
    new_content = ""
    if action.type == "delete_item" and len(action.item_indices) == 1:
        index = action.item_indices[0]
        if 1 <= index <= len(before_values):
            item_index = index
            old_content = before_values[index - 1]
            new_content = ""
    elif action.type == "replace_text":
        index = action.target_item_index
        if index is None and len(action.item_indices) == 1:
            index = action.item_indices[0]
        if index is not None and 1 <= index <= len(before_values) and 1 <= index <= len(after_values):
            item_index = index
            old_content = before_values[index - 1]
            new_content = after_values[index - 1]
    elif action.type == "append_items" and action.items:
        for index, value in enumerate(after_values, start=1):
            if index > len(before_values) or value not in before_values:
                item_index = index
                old_content = ""
                new_content = value
                break
    elif action.type == "merge_items":
        indices = sorted({index for index in action.item_indices if 1 <= index <= len(before_values)})
        if len(indices) >= 2:
            item_index = indices[0]
            old_content = "；".join(before_values[index - 1] for index in indices)
            if 1 <= item_index <= len(after_values):
                new_content = after_values[item_index - 1]
    elif action.type in {"replace_field", "polish_items"} and after_values:
        item_index = 1
        old_content = "；".join(before_values)
        new_content = after_values[0] if len(after_values) == 1 else "；".join(after_values)
    if item_index is None:
        return None
    return {
        "report_id": str(getattr(existing, "id", "") or ""),
        "report_date": report_date.isoformat(),
        "user_id": str(getattr(user, "id", "") or ""),
        "team_id": str(getattr(user, "team_id", "") or ""),
        "section": field,
        "item_index": item_index,
        "old_content": old_content,
        "new_content": new_content,
        "source_user_message": raw_input,
        "timestamp": received_at.isoformat(),
    }


def _values_for_field(values: tuple[list[str], list[str], list[str]], field: str) -> list[str]:
    mapping = {
        "today_work": values[0],
        "problems": values[1],
        "tomorrow_plan": values[2],
    }
    return mapping.get(field, [])


def _field_label(field: str) -> str:
    return {
        "today_work": "今日工作",
        "problems": "问题/风险",
        "tomorrow_plan": "明日计划",
    }.get(field, "日报")


def _delete_items(values: list[str], action: AgentAction) -> list[str]:
    indices = {index for index in action.item_indices if 1 <= index <= len(values)}
    if indices:
        return [item for position, item in enumerate(values, start=1) if position not in indices]
    selected = set(_select_items(values, action))
    if not selected:
        return values
    removed: set[str] = set()
    result: list[str] = []
    for item in values:
        if item in selected and item not in removed:
            removed.add(item)
            continue
        result.append(item)
    return result


def _select_items(values: list[str], action: AgentAction) -> list[str]:
    selected: list[str] = []
    for index in action.item_indices:
        if 1 <= index <= len(values):
            selected.append(values[index - 1])
    if action.source_item_text:
        source = _compact(action.source_item_text)
        for value in values:
            compact_value = _compact(value)
            if source and (source == compact_value or source in compact_value or compact_value in source):
                selected.append(value)
    return list(dict.fromkeys(selected))


def _replace_text_in_items(values: list[str], action: AgentAction) -> list[str] | None:
    if not values:
        return None
    old_value = action.old_value.strip()
    new_value = action.new_value.strip()
    if not old_value or not new_value:
        return None
    index = action.target_item_index
    if index is not None:
        if not (1 <= index <= len(values)):
            return None
        value = values[index - 1]
        if old_value not in value:
            return None
        updated = list(values)
        replacement_items = _split_replace_text_replacement_items(value, old_value, action)
        if replacement_items is not None:
            updated[index - 1 : index] = replacement_items
            return updated
        updated[index - 1] = value.replace(old_value, new_value, 1)
        return updated
    matches = [idx for idx, value in enumerate(values) if old_value in value]
    if len(matches) != 1:
        return None
    updated = list(values)
    matched_value = updated[matches[0]]
    replacement_items = _split_replace_text_replacement_items(matched_value, old_value, action)
    if replacement_items is not None:
        updated[matches[0] : matches[0] + 1] = replacement_items
        return updated
    updated[matches[0]] = matched_value.replace(old_value, new_value, 1)
    return updated


def _split_replace_text_replacement_items(value: str, old_value: str, action: AgentAction) -> list[str] | None:
    if action.source != "pending_split_confirmation":
        return None
    if _compact(value) != _compact(old_value):
        return None
    parts = [part.strip(" \t\r\n\u3000，,。；;、") for part in str(action.new_value or "").splitlines()]
    parts = [part for part in parts if part]
    return parts if len(parts) >= 2 else None


def _is_placeholder_list(field: str, values: list[str]) -> bool:
    placeholders = {_compact(item) for item in PLACEHOLDER_VALUES.get(field, set())}
    return bool(values) and all(_compact(value) in placeholders for value in values)


def _requires_completed_confirmation(existing: DailyReport | None, action: AgentAction) -> bool:
    return False


def _snapshot(existing: DailyReport | None, *, user: User | None = None, report_date: date | None = None) -> dict[str, Any] | None:
    if existing is None:
        return None
    snapshot = {
        "today_work": list(existing.today_work or []),
        "problems": list(existing.problems or []),
        "tomorrow_plan": list(existing.tomorrow_plan or []),
        "status": existing.status,
    }
    if user is not None and report_date is not None:
        snapshot["_scope"] = {
            "user_id": str(getattr(user, "id", "") or ""),
            "team_id": str(getattr(user, "team_id", "") or ""),
            "report_date": report_date.isoformat(),
            "report_id": str(getattr(existing, "id", "") or ""),
        }
    return snapshot


def _recent_report_context_payload(
    *,
    viewed_report: DailyReport,
    viewed_report_date: date,
    viewed_at: datetime,
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
        "field": field if field in {*REPORT_FIELDS, "all", "none"} else "all",
        "can_edit": owner == "self",
        "can_copy_to_today": owner == "self",
        "created_at": viewed_at.isoformat() if hasattr(viewed_at, "isoformat") else "",
        "turns_left": 3,
        "draft_hash": _draft_hash(
            today_work=list(getattr(viewed_report, "today_work", []) or []),
            problems=list(getattr(viewed_report, "problems", []) or []),
            tomorrow_plan=list(getattr(viewed_report, "tomorrow_plan", []) or []),
        ),
    }


def _last_display_context_payload(
    *,
    existing: DailyReport | None,
    report_date: date,
    displayed_at: datetime,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
) -> dict[str, Any]:
    values_by_field = {
        "today_work": list(today_work or []),
        "problems": list(problems or []),
        "tomorrow_plan": list(tomorrow_plan or []),
    }
    sections: list[dict[str, Any]] = []
    for field in REPORT_FIELDS:
        items: list[dict[str, Any]] = []
        for index, text in enumerate(values_by_field[field], start=1):
            items.append(
                {
                    "section": field,
                    "display_index": index,
                    "actual_index": index,
                    "text": str(text or ""),
                }
            )
        sections.append({"section": field, "items": items})
    return {
        "report_date": report_date.isoformat(),
        "report_id": str(getattr(existing, "id", "") or ""),
        "draft_hash": _draft_hash(today_work=today_work, problems=problems, tomorrow_plan=tomorrow_plan),
        "displayed_at": displayed_at.isoformat() if hasattr(displayed_at, "isoformat") else "",
        "sections": sections,
    }


def _draft_hash(*, today_work: list[str], problems: list[str], tomorrow_plan: list[str]) -> str:
    payload = {
        "today_work": list(today_work or []),
        "problems": list(problems or []),
        "tomorrow_plan": list(tomorrow_plan or []),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _missing_sections(existing: DailyReport | None) -> list[str]:
    if existing is None:
        return ["today_work", "problems", "tomorrow_plan"]
    section_status = existing.section_status or {}
    missing: list[str] = []
    if not existing.today_work and not section_status.get("today_work_acknowledged_empty"):
        missing.append("today_work")
    if not existing.problems and not section_status.get("problems_acknowledged_empty"):
        missing.append("problems")
    if not existing.tomorrow_plan and not section_status.get("tomorrow_plan_acknowledged_empty"):
        missing.append("tomorrow_plan")
    return missing


def _missing_sections_for_values(
    *,
    today_work: list[str],
    problems: list[str],
    tomorrow_plan: list[str],
    empty_ack: dict[str, bool],
) -> list[str]:
    missing: list[str] = []
    if not today_work and not empty_ack.get("today_work"):
        missing.append("today_work")
    if not problems and not empty_ack.get("problems"):
        missing.append("problems")
    if not tomorrow_plan and not empty_ack.get("tomorrow_plan"):
        missing.append("tomorrow_plan")
    return missing


def _build_saved_message(report: DailyReport, missing_sections: list[str], *, updated: bool) -> str:
    today_work = _display_values_for_message(report, "today_work")
    problems = _display_values_for_message(report, "problems")
    tomorrow_plan = _display_values_for_message(report, "tomorrow_plan")
    if report.status == STATUS_COMPLETED:
        return build_completed_message(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            updated=updated,
        )
    if report.status == STATUS_PENDING_CONFIRMATION:
        return build_confirmation_message(
            today_work=today_work,
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            quality_warning=report.quality_warning,
            updated=updated,
        )
    return build_followup_message(
        missing_sections,
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
    )


def _build_action_success_reply(report: DailyReport, action_messages: list[str]) -> str:
    operation = "；".join(_strip_final_punctuation(message) for message in action_messages if message.strip())
    if not operation:
        operation = "已更新日报内容"
    return (
        f"已完成本次操作：{operation}。\n\n"
        "当前日报草稿：\n\n"
        f"今日工作：\n{_format_preview_section(_display_values_for_message(report, 'today_work'))}\n\n"
        f"问题/风险：\n{_format_preview_section(_display_values_for_message(report, 'problems'), empty_fallback='暂无')}\n\n"
        f"明日计划：\n{_format_preview_section(_display_values_for_message(report, 'tomorrow_plan'), empty_fallback='暂无')}\n\n"
        "需要调整可以继续说；没问题可以回复“确认提交”。"
    )


def _build_current_report_preview(report: DailyReport) -> str:
    return (
        "当前日报草稿：\n\n"
        f"今日工作：\n{_format_preview_section(_display_values_for_message(report, 'today_work'))}\n\n"
        f"问题/风险：\n{_format_preview_section(_display_values_for_message(report, 'problems'), empty_fallback='暂无')}\n\n"
        f"明日计划：\n{_format_preview_section(_display_values_for_message(report, 'tomorrow_plan'), empty_fallback='暂无')}"
    )


def _build_unsubmit_success_reply(report: DailyReport, action_messages: list[str]) -> str:
    operations = [_strip_final_punctuation(message) for message in action_messages if message.strip()]
    if not operations:
        return "已撤回日报，当前状态已变为草稿。你可以继续修改；没问题可以回复“确认提交”。"
    return (
        f"已撤回日报，并完成修改：{'；'.join(operations)}。当前日报已变为草稿状态。\n\n"
        f"{_build_current_report_preview(report)}\n\n"
        "需要调整可以继续说；没问题可以回复“确认提交”。"
    )


def _format_preview_section(values: list[str], *, empty_fallback: str = "暂无") -> str:
    cleaned = [value.strip() for value in values if value and value.strip()]
    if not cleaned:
        return empty_fallback
    if len(cleaned) == 1:
        return f"1. {cleaned[0]}"
    return "\n".join(f"{index}. {value}" for index, value in enumerate(cleaned, start=1))


def _strip_final_punctuation(text: str) -> str:
    return text.strip().rstrip("。；;")


def _format_report(report: DailyReport | None, target_date: date) -> str:
    if report is None:
        return _format_empty_report(target_date)
    return (
        f"**【日期】** {target_date.isoformat()}\n"
        f"**【今日工作】** {_format_section(_display_values_for_message(report, 'today_work'))}\n"
        f"**【问题/风险】** {_format_section(_display_values_for_message(report, 'problems'), empty_fallback='暂无明显问题')}\n"
        f"**【明日计划】** {_format_section(_display_values_for_message(report, 'tomorrow_plan'))}\n"
        f"**【状态】** {_status_label(report.status)}"
    )


def _format_report_field(report: DailyReport, target_date: date, field: str) -> str:
    label = _field_label(field)
    values = _display_values_for_message(report, field)
    fallback = "暂无明显问题" if field == "problems" else "未填写"
    return (
        f"**【日期】** {target_date.isoformat()}\n"
        f"**【{label}】** {_format_section(values, empty_fallback=fallback)}\n"
        f"**【状态】** {_status_label(report.status)}"
    )


def _format_empty_report(target_date: date) -> str:
    return (
        f"**【日期】** {target_date.isoformat()}\n"
        "**【今日工作】** 未填写\n"
        "**【问题/风险】** 未填写\n"
        "**【明日计划】** 未填写\n"
        "**【状态】** 未开始"
    )


def _format_section(values: list[str], *, empty_fallback: str = "未填写") -> str:
    cleaned = [value.strip() for value in values if value and value.strip()]
    if not cleaned:
        return empty_fallback
    if len(cleaned) == 1:
        return cleaned[0]
    return "\n" + "\n".join(f"{index}. {value}" for index, value in enumerate(cleaned, start=1))


def _display_values_for_message(report: DailyReport, field: str) -> list[str]:
    values = list(getattr(report, field, []) or [])
    if values:
        return values
    section_status = report.section_status or {}
    if field == "today_work" and section_status.get("today_work_acknowledged_empty"):
        return ["无今日工作"]
    if field == "problems" and section_status.get("problems_acknowledged_empty"):
        return ["暂无明显问题"]
    if field == "tomorrow_plan" and section_status.get("tomorrow_plan_acknowledged_empty"):
        return ["无明日计划"]
    return []


def _status_label(status: str) -> str:
    return {
        STATUS_COLLECTING: "填写中",
        STATUS_PENDING_CONFIRMATION: "待确认",
        STATUS_COMPLETED: "已提交",
    }.get(status or "", status or "未知")


def _safe_no_write_message(message: str, *, reply_kind: str = "") -> str:
    text = (message or "").strip()
    if not text:
        return "好的，这句我先不写入日报。"
    misleading_markers = (
        "已记录",
        "已更新",
        "已保存",
        "已加入",
        "已添加",
        "已修改",
        "已合并",
        "已删除",
        "已清空",
        "已完成",
    )
    if any(marker in text for marker in misleading_markers):
        return "我没有实际改动草稿，先不按已修改处理。请重新说明要修改、合并或删除的具体栏目和编号。"
    if reply_kind in {"agent_emotional_feedback", "agent_small_talk", "agent_chat"}:
        compact = _compact(text)
        if not ("不" in compact and ("日报" in compact or "复盘" in compact)):
            return f"{text}\n\n这句我先不写入日报或复盘；如果要记录到日报，直接说具体工作、问题或明日计划。"
    return text


def _looks_like_current_report_query(raw_input: str) -> bool:
    compact = _compact(raw_input)
    if not compact:
        return False
    non_report_terms = ("天气", "新闻", "股票", "股价", "电影", "外卖", "打车")
    if any(term in compact for term in non_report_terms):
        return False
    short_commands = {"展示", "展示一下", "展示给我看看", "发我下", "发我看下", "发我一下", "看看", "当前内容", "当前草稿"}
    if compact in short_commands:
        return True
    report_terms = ("日报", "复盘", "草稿", "报告", "当前内容", "填写内容")
    query_terms = ("看", "展示", "发", "查", "什么样", "哪些", "内容")
    return any(term in compact for term in report_terms) and any(term in compact for term in query_terms)


def _resolve_target_date(value: str | None, report_date: date) -> date:
    text = (value or "").strip().lower()
    if text in {"today", "current_day", "今天"}:
        return report_date
    if text in {"yesterday", "previous_day", "last_day", "昨天"}:
        return report_date - timedelta(days=1)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return report_date - timedelta(days=1)


def _compact(value: str) -> str:
    return "".join(str(value or "").split()).strip()
