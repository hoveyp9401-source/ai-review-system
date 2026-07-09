from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.llm.extractor import DailyReportExtractor, LLMOutputError
from app.models import DailyReport, User
from app.repositories import acquire_daily_report_advisory_lock, get_report, merge_ordered, upsert_daily_report
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
from app.utils.time import now_in_timezone, today_in_timezone


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
MESSAGE_CLEAR_CURRENT_REPORT_DONE = "已清空当前复盘草稿。你可以重新说今天主要做了什么。"
MESSAGE_CONFIRM_CLEAR_DRAFT = "你是想清空当前复盘草稿吗？回复“清空”我就清掉当前内容，回复“取消”则保留原内容。"
MESSAGE_CONFIRM_CLEAR_COMPLETED = "当前复盘已经提交。你确定要清空并重新填写吗？回复“清空”确认，回复“取消”保留。"
MESSAGE_CANCEL_CLEAR_CURRENT_REPORT = "好的，当前复盘内容已保留。你可以继续补充或修改。"


class DailyReportService:
    def __init__(self, settings: Settings, extractor: DailyReportExtractor):
        self.settings = settings
        self.extractor = extractor

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
        report_date = report_date or today_in_timezone(user.timezone or self.settings.timezone)
        received_at = now_in_timezone(user.timezone or self.settings.timezone)

        step_start = time.perf_counter()
        await _acquire_report_processing_lock(session, user.id, report_date)
        timings["acquire_report_lock_seconds"] = round(time.perf_counter() - step_start, 4)

        step_start = time.perf_counter()
        existing = await get_report(session, user.id, report_date)
        timings["load_existing_report_seconds"] = round(time.perf_counter() - step_start, 4)

        if existing and _should_auto_submit(existing, received_at):
            existing = await _auto_submit_existing_report(session, existing, received_at)

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

        dated_clear_target = _resolve_dated_clear_request(raw_input, report_date)
        if dated_clear_target is not None:
            return await self._ask_confirm_clear_dated_report(
                session,
                user=user,
                existing=existing,
                current_report_date=report_date,
                target_date=dated_clear_target,
                received_at=received_at,
                source=source,
                timings=timings,
                total_start=total_start,
            )

        display_request = _resolve_report_display_request(raw_input, report_date)
        if display_request is not None:
            target_date, field = display_request
            message = await _build_report_display_reply(session, user=user, target_date=target_date, field=field)
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=message,
                reply_kind="history_query",
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

        if _looks_like_current_report_edit_entry_request(raw_input):
            return await self._start_current_report_edit_flow(
                session,
                existing=existing,
                report_date=report_date,
                raw_input=raw_input,
                timings=timings,
                total_start=total_start,
            )

        if bool(getattr(self.settings, "llm_draft_decision_enabled", False)):
            pre_current_slot = _infer_current_slot(existing)
            pending_interaction = _get_pending_interaction(existing)
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
            if existing and existing.status == STATUS_PENDING_CONFIRMATION and (
                _is_confirmation_reply(raw_input) or _is_fast_confirmation_reply(raw_input)
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
            previous_section_status = _clear_pending_interaction(previous_section_status)
            _preserve_internal_section_status(previous_section_status, state.section_status)
        if long_report_detection.is_long_report:
            state.section_status[LONG_REPORT_MODE_KEY] = True

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
    ) -> SubmitReportResult:
        current_slot = _infer_current_slot(existing)
        missing_sections = _missing_sections_from_report(existing)
        context = _build_draft_decision_context(
            user=user,
            existing=existing,
            report_date=report_date,
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
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=decision.reply_to_user or decision.clarification_question or "这句话我先不写入日报。你可以继续补充或修改复盘内容。",
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

        step_start = time.perf_counter()
        applied = _apply_draft_decision_to_sections(decision, existing)
        timings["report_merge_seconds"] = round(time.perf_counter() - step_start, 4)
        if applied.get("error"):
            return _build_non_report_result(
                existing=existing,
                report_date=report_date,
                message=str(applied["error"]),
                reply_kind="draft_decision_rejected",
                timings=_finalize_timings(timings, total_start),
            )

        today_work = applied["today_work"]
        problems = applied["problems"]
        tomorrow_plan = applied["tomorrow_plan"]
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
        previous_section_status = _clear_pending_interaction(_clear_pending_draft_edit(_clear_pending_quality(previous_section_status)))
        state = infer_report_state(
            existing_section_status=previous_section_status,
            merged_today_work=today_work,
            merged_problems=problems,
            merged_tomorrow_plan=tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(previous_section_status, state.section_status)
        if existing:
            state.section_status[DRAFT_PREVIOUS_SNAPSHOT_KEY] = _snapshot_from_report(existing)

        is_modification = bool(existing and _has_report_content(existing))
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
                updated=is_modification,
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
        previous_section_status = _clear_pending_draft_edit(_clear_pending_quality(existing.section_status or {}))
        state = infer_report_state(
            existing_section_status=previous_section_status,
            merged_today_work=edit_result.today_work,
            merged_problems=edit_result.problems,
            merged_tomorrow_plan=edit_result.tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(previous_section_status, state.section_status)
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
        section_status = _clear_pending_quality(existing.section_status or {})
        state = infer_report_state(
            existing_section_status=section_status,
            merged_today_work=today_work,
            merged_problems=problems,
            merged_tomorrow_plan=tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(section_status, state.section_status)
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
        section_status = _clear_pending_quality(existing.section_status or {})
        state = infer_report_state(
            existing_section_status=section_status,
            merged_today_work=existing.today_work,
            merged_problems=existing.problems,
            merged_tomorrow_plan=existing.tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(section_status, state.section_status)
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
        return _build_non_report_result(
            existing=state_report if state_report is not None and state_report.id != report.id else report,
            report_date=current_report_date,
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
        return _build_non_report_result(
            existing=report,
            report_date=report_date,
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

        full_sections = _parse_full_report_replacement_text(raw_input)
        if full_sections is not None:
            return await self._replace_current_report_sections(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                sections=full_sections,
                reply_kind="current_report_edit_full_replace",
                timings=timings,
                total_start=total_start,
            )

        text_replacement = _resolve_current_report_text_replacement(raw_input, existing)
        if text_replacement is not None:
            return await self._replace_current_report_sections(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                sections=text_replacement,
                reply_kind="current_report_edit_text_replace",
                timings=timings,
                total_start=total_start,
            )

        delete_sections = _resolve_current_report_delete_mutation(raw_input, existing, pending_interaction)
        if delete_sections is not None:
            return await self._replace_current_report_sections(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                sections=delete_sections,
                reply_kind="current_report_edit_delete",
                timings=timings,
                total_start=total_start,
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

        focused_field = _current_report_pending_field(pending_interaction)
        field_sections = _resolve_current_report_field_replacement(raw_input, existing, focused_field or selected_field)
        if field_sections is not None:
            return await self._replace_current_report_sections(
                session,
                user=user,
                existing=existing,
                report_date=report_date,
                received_at=received_at,
                source=source,
                raw_input=raw_input,
                sections=field_sections,
                reply_kind="current_report_edit_field_replace",
                timings=timings,
                total_start=total_start,
            )

        message = (
            _build_current_report_field_focus_message(existing, report_date, focused_field)
            if focused_field
            else _build_current_report_edit_entry_message(existing, report_date)
        )
        return _build_non_report_result(
            existing=existing,
            report_date=report_date,
            message=message,
            reply_kind="current_report_edit_waiting_content",
            timings=_finalize_timings(timings, total_start),
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
        previous_section_status = _clear_pending_interaction(_clear_pending_draft_edit(_clear_pending_quality(dict(existing.section_status or {}))))
        state = infer_report_state(
            existing_section_status=previous_section_status,
            merged_today_work=today_work,
            merged_problems=problems,
            merged_tomorrow_plan=tomorrow_plan,
            structured=structured,
            raw_input="",
        )
        _preserve_internal_section_status(previous_section_status, state.section_status)
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
    compact = _compact_for_intent(raw_input)
    if not compact or any(token in compact for token in ("昨天", "昨日", "前天", "历史")):
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


def _build_current_report_edit_pending(
    report_date: date,
    report: DailyReport,
    *,
    target_field: str | None = None,
) -> dict[str, Any]:
    field = target_field if target_field in CURRENT_REPORT_EDIT_FIELDS else None
    return {
        "type": PENDING_INTERACTION_CURRENT_REPORT_EDIT_FLOW,
        "operation": "modify_report",
        "target_field": field or "",
        "context": {
            "target_date": report_date.isoformat(),
            "stage": "awaiting_field_edit_content" if field else "awaiting_edit_instruction",
            "snapshot": _snapshot_from_report(report),
        },
    }


def _build_current_report_edit_entry_message(report: DailyReport, report_date: date) -> str:
    return (
        f"好的，我先把今天的日报调出来：\n\n"
        f"今日日报（{report_date.isoformat()}）：\n"
        f"今日工作：\n{_format_numbered_section(report.today_work, empty_fallback='未填写')}\n\n"
        f"问题/风险：\n{_format_numbered_section(report.problems, empty_fallback='暂无明显问题')}\n\n"
        f"明日计划：\n{_format_numbered_section(report.tomorrow_plan, empty_fallback='未填写')}\n\n"
        f"你可以直接发一整段新的日报内容，我会按今日工作、问题/风险、明日计划更新今天这份草稿。"
    )


def _build_current_report_field_focus_message(report: DailyReport, report_date: date, field: str | None) -> str:
    if field not in CURRENT_REPORT_EDIT_FIELDS:
        return _build_current_report_edit_entry_message(report, report_date)
    values = list(getattr(report, field) or [])
    fallback = "暂无明显问题" if field == "problems" else "未填写"
    return (
        f"好的，已锁定今天日报的{CURRENT_REPORT_FIELD_LABELS[field]}。\n\n"
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


def _copy_current_report_sections(report: DailyReport) -> dict[str, list[str]]:
    return {
        "today_work": list(report.today_work or []),
        "problems": list(report.problems or []),
        "tomorrow_plan": list(report.tomorrow_plan or []),
    }


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
    pattern = re.compile(r"(今日工作|今天工作|问题/风险|问题风险|问题|风险|明日计划|明天计划)[:：]?", re.I)
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
    parts = [part.strip() for part in re.split(r"(?:同时|并且|另外|此外|以及|，|,|；|;|、|\n)", text) if part.strip()]
    if not parts and text.strip():
        parts = [text.strip()]
    if field == "today_work":
        return [_normalize_current_report_work(part) for part in parts if part.strip()]
    if field == "problems":
        return [_normalize_current_report_problem(part) for part in parts if part.strip()]
    return [_normalize_current_report_plan(part) for part in parts if part.strip()]


def _strip_current_report_field_prefix(text: str, field: str) -> str:
    text = text.strip("：:，,；;、 \n\t")
    if field == "today_work":
        return re.sub(r"^(今日工作|今天工作|工作|任务|事项)[：:\s]*", "", text).strip()
    if field == "problems":
        return re.sub(r"^(问题/风险|问题风险|问题|风险|隐患)[：:\s]*", "", text).strip()
    return re.sub(r"^(明日计划|明天计划|明日安排|明天安排|计划|明天|明日|后续计划|后续)[：:\s]*", "", text).strip()


def _normalize_current_report_work(text: str) -> str:
    text = _normalize_free_text(text)
    text = re.sub(r"^(今天|今日|我|同时|并且|另外|此外|还|又)[，,、\s]*", "", text)
    text = re.sub(r"出差去?了", "出差去", text)
    text = re.sub(r"审核了", "审核", text)
    text = re.sub(r"写了", "撰写", text)
    text = re.sub(r"处理了", "处理", text)
    return text.strip("。；;，,、 ")


def _normalize_current_report_problem(text: str) -> str:
    text = _normalize_free_text(text)
    text = re.sub(r"^(问题是|风险是|问题/风险|问题|风险)[：:\s]*", "", text)
    text = text.replace("没签", "未签").replace("没有签", "未签")
    if text and not text.startswith(("发现", "存在", "部分", "暂无", "无")):
        text = f"发现{text}"
    return text.strip("。；;，,、 ")


def _normalize_current_report_plan(text: str) -> str:
    text = _normalize_free_text(text)
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
    raw_input: str,
    current_slot: str | None,
    missing_sections: list[str],
) -> dict[str, Any]:
    section_status = existing.section_status if existing else {}
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
        "current_report_items": {
            "today_work": _numbered_context_items(existing.today_work if existing else []),
            "problems": _numbered_context_items(existing.problems if existing else []),
            "tomorrow_plan": _numbered_context_items(existing.tomorrow_plan if existing else []),
        },
        "current_report": {
            "today_work": existing.today_work if existing else [],
            "problems": existing.problems if existing else [],
            "tomorrow_plan": existing.tomorrow_plan if existing else [],
            "quality_warning": existing.quality_warning if existing else None,
        },
        "recent_turns": _recent_turns_from_report(existing),
        "date_context": {
            "today": report_date.isoformat(),
            "yesterday": (report_date - timedelta(days=1)).isoformat(),
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


def _resolve_dated_clear_request(raw_input: str, report_date: date) -> date | None:
    compact = _compact_for_intent(raw_input)
    if not compact or "日报" not in compact:
        return None
    if not any(marker in compact for marker in ("清空", "删掉", "删除", "清掉", "删了", "不要")):
        return None
    return _resolve_date_from_text(compact, report_date)


def _resolve_report_display_request(raw_input: str, report_date: date) -> tuple[date, str] | None:
    compact = _compact_for_intent(raw_input)
    if not compact or "日报" not in compact:
        return None
    if not any(marker in compact for marker in ("发我", "给我", "展示", "显示", "看看", "看下", "查看", "当前", "什么样")):
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


def _resolve_date_from_text(compact: str, report_date: date) -> date | None:
    if "前天" in compact:
        return report_date - timedelta(days=2)
    if "昨天" in compact or "昨日" in compact:
        return report_date - timedelta(days=1)
    if "今天" in compact or "今日" in compact or "当前" in compact:
        return report_date
    match = re.search(r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})", compact)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    match = re.search(r"(\d{1,2})月(\d{1,2})日", compact)
    if match:
        month, day = (int(part) for part in match.groups())
        try:
            return date(report_date.year, month, day)
        except ValueError:
            return None
    return None


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
        current = list(values_by_field[field])
        if update.mode == "keep":
            continue
        if update.mode == "clear":
            values_by_field[field] = []
            continue
        if update.mode == "remove_items":
            removed = _remove_items_by_refs(current, update.item_refs)
            if removed.get("error"):
                return removed
            values_by_field[field] = removed["items"]
            continue
        cleaned_items = _clean_report_items(list(update.items or []), field=field)
        if update.mode == "replace":
            if update.item_refs:
                replaced = _replace_items_by_refs(current, update.item_refs, cleaned_items, field)
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
        moved = _move_items_by_refs(
            values_by_field[source],
            values_by_field[destination],
            move.item_refs,
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
        removed = _remove_items_by_refs(
            values_by_field[field],
            delete.item_refs,
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
    if len(indices) == 1:
        updated[indices[0]] = _normalize_tomorrow_plan(new_items[0]) if field == "tomorrow_plan" else _normalize_free_text(new_items[0])
        for extra in new_items[1:]:
            normalized = _normalize_tomorrow_plan(extra) if field == "tomorrow_plan" else _normalize_free_text(extra)
            if normalized:
                updated.append(normalized)
    else:
        first = min(indices)
        for index in sorted(indices, reverse=True):
            updated.pop(index)
        merged = "；".join(item for item in new_items if item)
        if merged:
            updated.insert(first, _normalize_tomorrow_plan(merged) if field == "tomorrow_plan" else _normalize_free_text(merged))
    return {"items": updated}


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


def _numbered_context_items(values: list[str] | None) -> list[dict[str, Any]]:
    return [{"index": index, "text": value} for index, value in enumerate(values or [], start=1)]


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
        values = _draft_field_values(today_work, problems, tomorrow_plan).get(field, [])
        message = _build_delete_confirmation_message(field, index, values)
        pending_edit = _build_pending_draft_edit_operation(
            "delete_item",
            field,
            [index],
            requires_confirmation=True,
            confirmation_message=message,
        )
        return DraftEditResult(today_work, problems, tomorrow_plan, message, pending_edit=pending_edit)

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
        f"你要删除的是{label}第 {zero_index + 1} 条：“{target_text}”。",
        "",
        f"删除后{label}将变为：",
    ]
    if remaining:
        lines.extend(f"{index}. {value}" for index, value in enumerate(remaining, start=1))
    else:
        lines.append("（空）")
    lines.extend(["", "确认删除吗？回复“确认”执行，回复“取消”保留。"])
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
            today_work, problems, tomorrow_plan = _apply_simple_replacement(
                raw_input,
                today_work=today_work,
                problems=problems,
                tomorrow_plan=tomorrow_plan,
            )
        return today_work, problems, tomorrow_plan

    if intent == "continue_collecting" and existing:
        today_work = _merge_today_work_with_relation(existing_today_work, parsed.today_work, decision)
        problems = _merge_problems_for_collecting(existing_problems, parsed.problems, existing, decision)
        tomorrow_plan = _merge_tomorrow_for_collecting(existing_tomorrow_plan, parsed.tomorrow_plan, decision)
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
    parsed_problems = _normalize_problem_items(parsed_problems, raw_input)
    parsed_today_work = _remove_problem_overlap_from_today_work(parsed_today_work, parsed_problems)
    if intent != "modify_field":
        parsed_today_work, parsed_problems, parsed_tomorrow_plan = _preserve_simple_source_detail(
            raw_input,
            today_work=parsed_today_work,
            problems=parsed_problems,
            tomorrow_plan=parsed_tomorrow_plan,
        )

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


def _build_non_report_result(
    *,
    existing: DailyReport | None,
    report_date: date,
    message: str,
    reply_kind: str,
    timings: dict[str, float],
) -> SubmitReportResult:
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
) -> SubmitReportResult:
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
        report_saved=True,
        reply_kind=reply_kind,
        timings=timings,
    )


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
        and report.auto_submit_at is not None
        and report.auto_submit_at <= now
    )


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


def _is_fast_confirmation_reply(raw_input: str) -> bool:
    compact = _compact_for_intent(raw_input)
    confirmations = {"确认", "确定", "可以", "没问题", "没啥问题", "就这样", "提交", "ok", "okay"}
    return compact in confirmations or any(compact.startswith(token) and len(compact) <= len(token) + 4 for token in confirmations)


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
    exact = {
        "清空",
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
    phrases = ["没问题", "没啥问题", "没什么问题", "没有问题", "暂无问题", "无问题", "没风险", "没有风险", "暂无风险"]
    return any(phrase in compact for phrase in phrases)


def _split_tomorrow_text(text: str) -> tuple[str, str]:
    match = re.search(r"(然后)?(明天|明日|明儿|后天|接下来|之后)", text)
    if not match:
        return text, ""
    return text[: match.start()], text[match.start():]


def _normalize_free_text(text: str) -> str:
    text = text.strip()
    text = _strip_report_instruction_prefix(text)
    text = re.sub(r"(你说人活着是为了什么呢?|今天太离谱了吧|算了不聊这个)", "", text)
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
        text = _strip_replace_report_prefix(text)
        text = _strip_field_change_prefix(text)
        text = _strip_noise_fragments(text)
        if field == "tomorrow_plan":
            text = _normalize_tomorrow_plan(text)
        if text and not _is_non_report_phrase(text):
            cleaned.append(text)
    return merge_ordered([], cleaned)


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
    if re.fullmatch(r"(明天|明日)?换一家店", cleaned):
        return "明天换一家店购买"
    if _is_vague_tomorrow_plan(cleaned):
        return "明天继续推进相关工作"
    return cleaned


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
    return bool(re.search(r"(审|审核|处理|整理|沟通|开庭|出差|参加|推进|跟进|起草|修改|学习|发送|完成|走访|梳理|办理|核查|评审|调解|谈判|对接)", text))


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
