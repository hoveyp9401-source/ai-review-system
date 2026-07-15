from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import hashlib
import re
from typing import Any, Literal

from app.agent2.coordination_plan import ACTION_DAILY_ENTRY, CoordinationPlan
from app.agent2.daily_edit_intent import looks_like_contextual_daily_edit, looks_like_local_delete
from app.workflows.problem_evidence import extract_problem_evidence
from app.workflows.relative_dates import has_previous_to_current_repeat_reference
from app.workflows.intake import (
    EFFECT_ADD_DAILY_REPORT_ITEM,
    EFFECT_CONFIRM_DAILY_REPORT,
    EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
    WORKFLOW_DAILY_REPORT,
    IncomingMessageEnvelope,
    RoutingPlan,
    WorkflowEffect,
)


DailyCommandOperation = Literal[
    "fill",
    "edit",
    "confirm",
    "query_current",
    "query_history",
    "begin_edit",
    "copy_previous",
    "copy_current_to_tomorrow",
    "complete_previous_plan",
    "clear",
    "revoke",
    "no_write",
    "unknown",
]
DailyCommandTargetField = Literal["today_work", "problems", "tomorrow_plan", "all", "none", "unknown"]
DailyCommandConfidence = Literal["high", "medium", "low"]

DAILY_COMMAND_SUPPORTED_EFFECTS = {
    EFFECT_ADD_DAILY_REPORT_ITEM,
    EFFECT_CONFIRM_DAILY_REPORT,
    EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
}


@dataclass(frozen=True)
class DailyCommand:
    """Agent2 daily-report command.

    This is a dry-run command contract. It describes what the daily workflow
    should do, but it never mutates reports by itself.
    """

    workflow: str = WORKFLOW_DAILY_REPORT
    operation: DailyCommandOperation = "unknown"
    target_date: str = ""
    active_report_date: str = ""
    target_field: DailyCommandTargetField = "none"
    content: list[str] = field(default_factory=list)
    should_write: bool = False
    requires_confirmation: bool = False
    confidence: DailyCommandConfidence = "low"
    task_id: str = ""
    source_effect_type: str = ""
    execution_policy: str = "dry_run"
    safety_flags: list[str] = field(default_factory=list)
    reason: str = ""
    raw_text_hash: str = ""
    raw_text_chars: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "operation": self.operation,
            "target_date": self.target_date,
            "active_report_date": self.active_report_date,
            "target_field": self.target_field,
            "content_count": len(self.content),
            "should_write": self.should_write,
            "requires_confirmation": self.requires_confirmation,
            "confidence": self.confidence,
            "task_id": self.task_id,
            "source_effect_type": self.source_effect_type,
            "execution_policy": self.execution_policy,
            "safety_flags": list(self.safety_flags),
            "reason": self.reason,
            "raw_text_hash": self.raw_text_hash,
            "raw_text_chars": self.raw_text_chars,
        }

    def timing_payload(self) -> dict[str, Any]:
        return self.as_dict()


def compile_daily_commands(
    plan: RoutingPlan,
    envelope: IncomingMessageEnvelope | None = None,
    *,
    coordination_plan: CoordinationPlan | None = None,
) -> list[DailyCommand]:
    commands: list[DailyCommand] = []
    if _monthly_reply_blocks_daily_commands(plan):
        return []
    for effect in plan.effects:
        if effect.target_system != WORKFLOW_DAILY_REPORT:
            continue
        if effect.effect_type not in DAILY_COMMAND_SUPPORTED_EFFECTS:
            continue
        commands.append(_command_from_effect(plan, effect, envelope))
    coordination_commands = _commands_from_coordination_plan(plan, coordination_plan)
    if (
        coordination_commands
        and _can_replace_with_coordination_commands(commands)
        and len(coordination_commands) >= len(commands)
    ):
        return coordination_commands
    if not commands and plan.primary_workflow == WORKFLOW_DAILY_REPORT:
        commands.append(
            DailyCommand(
                operation="no_write",
                target_field="none",
                should_write=False,
                confidence=_confidence_from_plan(plan),
                reason="daily workflow selected but no supported daily effect was planned",
                safety_flags=["missing_daily_effect"],
                raw_text_hash=_hash_text(envelope.raw_text if envelope else ""),
                raw_text_chars=len(str(envelope.raw_text if envelope else "")),
            )
        )
    return commands


def _monthly_reply_blocks_daily_commands(plan: RoutingPlan) -> bool:
    if plan.primary_workflow != "monthly_report":
        return False
    if plan.safety_decision.commit_policy != "needs_confirmation":
        return False
    return any(
        effect.target_system == WORKFLOW_DAILY_REPORT
        and effect.effect_type in DAILY_COMMAND_SUPPORTED_EFFECTS
        for effect in plan.effects
    )


def _commands_from_coordination_plan(
    plan: RoutingPlan,
    coordination_plan: CoordinationPlan | None,
) -> list[DailyCommand]:
    if coordination_plan is None:
        return []
    if not _coordination_daily_allowed(plan):
        return []
    commands: list[DailyCommand] = []
    for action in coordination_plan.actions:
        if action.action_type != ACTION_DAILY_ENTRY:
            continue
        content = str(action.payload.get("content") or "").strip()
        target_field = str(action.target.get("field") or "today_work")
        if target_field not in {"today_work", "problems", "tomorrow_plan"}:
            target_field = "today_work"
        cleaned_content = _eligible_daily_content([content], target_field)  # type: ignore[arg-type]
        commands.append(
            DailyCommand(
                operation="fill" if cleaned_content else "no_write",
                target_date=_target_date_from_text(content),
                target_field=target_field,  # type: ignore[arg-type]
                content=cleaned_content,
                should_write=bool(cleaned_content),
                requires_confirmation=False,
                confidence=_confidence_from_plan(plan),
                source_effect_type=ACTION_DAILY_ENTRY,
                execution_policy="dry_run",
                reason=action.reason or "coordination daily entry",
                raw_text_hash=action.source_text_hash,
                raw_text_chars=action.source_text_chars,
            )
        )
    return commands


def _coordination_daily_allowed(plan: RoutingPlan) -> bool:
    if any(
        effect.target_system == WORKFLOW_DAILY_REPORT and effect.effect_type in DAILY_COMMAND_SUPPORTED_EFFECTS
        for effect in plan.effects
    ):
        return True
    return WORKFLOW_DAILY_REPORT in plan.matched_workflows and plan.safety_decision.commit_policy != "blocked"


def _can_replace_with_coordination_commands(commands: list[DailyCommand]) -> bool:
    if not commands:
        return True
    if all(command.operation == "no_write" for command in commands):
        return True
    if any(command.source_effect_type == EFFECT_LEGACY_DAILY_CONTEXT_ACTION for command in commands):
        return False
    return all(command.operation == "fill" for command in commands)


def _command_from_effect(
    plan: RoutingPlan,
    effect: WorkflowEffect,
    envelope: IncomingMessageEnvelope | None,
) -> DailyCommand:
    raw_text = _raw_text(effect, envelope)
    received_at = getattr(envelope, "received_at", None) if envelope else None
    operation = _operation_from_effect(effect, raw_text)
    target_field = _target_field(effect, operation, raw_text)
    payload_safety_flags = list(effect.payload.get("safety_flags") or [])
    is_prevalidated_daily_content = bool(
        {
            "conditional_daily_supplement",
            "active_daily_concrete_reminder",
            "active_daily_explicit_work",
        }.intersection(payload_safety_flags)
    )
    if effect.effect_type == EFFECT_LEGACY_DAILY_CONTEXT_ACTION and not is_prevalidated_daily_content and not _legacy_daily_context_command_eligible(
        operation,
        target_field,
        raw_text,
    ):
        operation = "no_write"
        target_field = "none"
    historical_cutoff_blocked = _historical_mutation_blocked_after_cutoff(
        operation,
        raw_text,
        effect,
        received_at=received_at,
    )
    if historical_cutoff_blocked:
        operation = "no_write"
        target_field = "none"
    requires_confirmation = _requires_confirmation(plan, effect, operation, raw_text)
    should_write = _should_write(operation, requires_confirmation)
    safety_flags = _safety_flags(plan, effect, operation, target_field, requires_confirmation, raw_text)
    if historical_cutoff_blocked:
        safety_flags = _dedupe([*safety_flags, "historical_daily_mutation_blocked_after_cutoff"])
    if effect.effect_type == EFFECT_LEGACY_DAILY_CONTEXT_ACTION and operation == "no_write":
        safety_flags = _dedupe([*safety_flags, "daily_write_eligibility_blocked"])
    content = _content(effect, operation, raw_text)
    if operation == "complete_previous_plan" and target_field in {"today_work", "problems", "tomorrow_plan"}:
        content = _eligible_daily_content(content, target_field)
    elif operation == "fill" and target_field in {"today_work", "problems", "tomorrow_plan"}:
        payload_safety_flags = list(effect.payload.get("safety_flags") or [])
        bypass_content_filter = bool(
            {"conditional_daily_supplement", "active_daily_explicit_work"}.intersection(
                payload_safety_flags
            )
        )
        if not bypass_content_filter:
            content = _eligible_daily_content(content, target_field)
        if not content:
            operation = "no_write"
            target_field = "none"
            should_write = False
            safety_flags = _dedupe([*safety_flags, "daily_write_eligibility_blocked"])
    return DailyCommand(
        operation=operation,
        target_date=_target_date(
            raw_text,
            effect,
            operation=operation,
            received_at=received_at,
        ),
        active_report_date=str(effect.target.get("report_date") or ""),
        target_field=target_field,
        content=content,
        should_write=should_write,
        requires_confirmation=requires_confirmation,
        confidence=_confidence_from_plan(plan),
        task_id=str(effect.target.get("task_id") or ""),
        source_effect_type=effect.effect_type,
        safety_flags=safety_flags,
        reason=(
            "historical daily reports are read-only after the 09:00 cutoff; use query or copy instead"
            if historical_cutoff_blocked
            else effect.reason or plan.reason
        ),
        raw_text_hash=_hash_text(raw_text),
        raw_text_chars=len(raw_text),
    )


def _operation_from_effect(effect: WorkflowEffect, raw_text: str) -> DailyCommandOperation:
    compact = _compact(raw_text)
    if effect.effect_type == EFFECT_CONFIRM_DAILY_REPORT:
        return "confirm"
    structured_operation = _structured_operation_from_effect(effect)
    if structured_operation in {"query_current", "query_history", "begin_edit", "no_write", "clear", "revoke", "copy_current_to_tomorrow"}:
        return structured_operation
    if _looks_like_daily_confirmation_text(raw_text):
        return "confirm"
    if (
        not _looks_like_quoted_previous_item_copy(raw_text)
        and (_contains_any(compact, _COPY_PREVIOUS_MARKERS) or _looks_like_repeat_previous_work(raw_text))
    ):
        return "copy_previous"
    if _looks_like_affirm_same_as_previous_daily(raw_text):
        return "copy_previous"
    if (
        _looks_like_report_revoke(raw_text)
        or _looks_like_bare_completed_report_revoke(raw_text, effect)
        or _looks_like_completed_report_revoke_edit(raw_text, effect)
    ):
        return "revoke"
    if _looks_like_current_daily_item_retraction(raw_text):
        return "edit"
    if _looks_like_risk_field_delete_request(raw_text):
        return "clear"
    if _looks_like_bare_revoke(raw_text) or _looks_like_report_revoke_edit(raw_text):
        return "no_write"
    if _looks_like_quoted_previous_item_copy(raw_text):
        return "fill"
    if _looks_like_current_daily_status_query(raw_text):
        return "query_current"
    if _looks_like_copy_current_work_to_tomorrow(raw_text):
        return "copy_current_to_tomorrow"
    if _looks_like_daily_meta_or_date_question(raw_text):
        return "no_write"
    if _looks_like_historical_daily_risk_lookup(raw_text):
        return "query_history"
    if _looks_like_case_progress_record_request(raw_text):
        return "no_write"
    if _looks_like_completed_previous_plan_without_new_content(raw_text):
        return "no_write"
    if _looks_like_completed_previous_plan(raw_text):
        if _completed_previous_plan_has_explicit_today_work(raw_text):
            return "fill"
        return "complete_previous_plan"
    if structured_operation == "fill" and str(effect.target.get("action_type") or "") == "daily_write":
        return "fill"
    if _looks_like_negative_replacement(raw_text) and str(effect.target.get("task_id") or "").strip():
        return "edit"
    if _looks_like_replacement_followup(raw_text):
        return "edit"
    if _looks_like_item_edit(raw_text):
        return "edit"
    if _looks_like_quoted_delete_edit(raw_text):
        return "edit"
    if looks_like_contextual_daily_edit(raw_text):
        return "edit"
    if _looks_like_short_delete_reference(raw_text):
        return "edit"
    if _looks_like_legal_document_work(raw_text):
        return "fill"
    if _looks_like_field_level_delete(raw_text):
        return "clear"
    if str(effect.target.get("action_type") or "") == "daily_edit" or structured_operation == "edit":
        return "edit"
    if _contains_any(compact, _CLEAR_MARKERS):
        return "clear"
    if (
        effect.effect_type == EFFECT_LEGACY_DAILY_CONTEXT_ACTION
        and not _looks_like_context_edit(raw_text)
        and not _contains_any(compact, ("\u590d\u5236", "\u62f7\u8d1d", "\u5e26\u5230", "\u5e26\u8fc7\u6765"))
        and _eligible_contextual_daily_fill(raw_text)
    ):
        return "fill"
    if _looks_like_history_query(compact):
        return "query_history"
    if _contains_any(compact, _QUERY_CURRENT_MARKERS) and not _eligible_contextual_daily_fill(raw_text):
        return "query_current"
    if structured_operation:
        return structured_operation
    if effect.effect_type == EFFECT_LEGACY_DAILY_CONTEXT_ACTION:
        return "edit" if _looks_like_context_edit(raw_text) else "fill"
    if effect.effect_type == EFFECT_ADD_DAILY_REPORT_ITEM:
        return "fill"
    return "unknown"


def _structured_operation_from_effect(effect: WorkflowEffect) -> DailyCommandOperation | None:
    operation = str(effect.target.get("operation") or "")
    if operation == "start_collection":
        return "no_write"
    if operation in {
        "fill",
        "edit",
        "no_write",
        "query_current",
        "query_history",
        "begin_edit",
        "clear",
        "revoke",
        "copy_previous",
        "copy_current_to_tomorrow",
        "complete_previous_plan",
    }:
        return operation  # type: ignore[return-value]
    return None


def _target_field(
    effect: WorkflowEffect,
    operation: DailyCommandOperation,
    raw_text: str,
) -> DailyCommandTargetField:
    if operation == "clear":
        if _looks_like_risk_field_delete_request(raw_text):
            return "problems"
        explicit_field = _field_from_text(raw_text)
        return explicit_field if explicit_field else "all"
    if operation == "copy_previous":
        if _copy_previous_targets_today_work(raw_text):
            return "today_work"
        explicit_field = _field_from_text(raw_text)
        if explicit_field in {"today_work", "problems", "tomorrow_plan"}:
            return explicit_field
        if _looks_like_affirm_same_as_previous_daily(raw_text):
            return "today_work"
        return "all"
    if operation == "copy_current_to_tomorrow":
        return "tomorrow_plan"
    if operation in {"confirm", "revoke"}:
        return "all"
    if operation == "complete_previous_plan":
        return "today_work"
    if operation in {"query_current", "query_history", "begin_edit", "no_write"}:
        return "none"
    compact = _compact(raw_text)
    if operation in {"fill", "edit"} and _looks_like_resolved_risk_update(raw_text):
        return "today_work"
    if _looks_like_historical_previous_plan_completion(raw_text):
        return "today_work"
    target = str(effect.target.get("field") or "")
    if (
        operation == "fill"
        and str(effect.target.get("action_type") or "") == "daily_write"
        and target in {"today_work", "problems", "tomorrow_plan"}
    ):
        return target  # type: ignore[return-value]
    if _contains_any(compact, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u4e2a")) and _contains_any(compact, ("\u63a5\u7740", "\u7ee7\u7eed", "\u518d")):
        return "tomorrow_plan"
    if _contains_any(compact, _TOMORROW_MARKERS) and _contains_any(compact, _BUSINESS_ACTION_MARKERS) and _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return "tomorrow_plan"
    if _looks_like_contextual_case_strategy_plan(raw_text):
        return "tomorrow_plan"
    if _looks_like_completed_previous_plan(raw_text) or _looks_like_previous_plan_content_reference(raw_text):
        return "today_work"
    if _looks_like_document_correction_problem(raw_text):
        return "problems"
    if _looks_like_contextual_problem_followup(raw_text):
        return "problems"
    if _contains_any(compact, _PROBLEM_MARKERS):
        return "problems"
    if _contains_any(compact, _TOMORROW_MARKERS):
        return "tomorrow_plan"
    if target in {"today_work", "problems", "tomorrow_plan"}:
        return target  # type: ignore[return-value]
    if operation in {"fill", "edit"}:
        return "today_work"
    return "none"


def _target_date(
    raw_text: str,
    effect: WorkflowEffect,
    *,
    operation: DailyCommandOperation = "unknown",
    received_at: Any = None,
) -> str:
    target_date = effect.target.get("date") or effect.target.get("target_date")
    if target_date:
        return str(target_date)
    if operation in {"fill", "edit"} and _looks_like_resolved_risk_update(raw_text):
        return "today"
    text_hint = _target_date_from_text(raw_text)
    if operation in {"fill", "edit"} and _contains_any(_compact(raw_text), _TODAY_MARKERS):
        return "today"
    if text_hint in {"yesterday", "day_before_yesterday", "three_days_ago"}:
        return text_hint
    if (
        _before_morning_cutoff(received_at)
        and operation in {"fill", "edit", "confirm", "query_current", "begin_edit", "clear", "revoke"}
        and not str(effect.target.get("report_date") or "").strip()
        and not _explicitly_targets_today_report(raw_text)
    ):
        return "yesterday"
    return text_hint


def _target_date_from_text(raw_text: str) -> str:
    if _looks_like_completed_previous_plan(raw_text) or _looks_like_completed_yesterday_referenced_task_today(raw_text):
        return "today"
    compact = _compact(raw_text)
    if _contains_any(compact, _THREE_DAYS_AGO_MARKERS):
        return "three_days_ago"
    if _contains_any(compact, _DAY_BEFORE_YESTERDAY_MARKERS):
        return "day_before_yesterday"
    if _contains_any(compact, _YESTERDAY_MARKERS):
        return "yesterday"
    if _contains_any(compact, _TOMORROW_MARKERS):
        return "tomorrow"
    if _contains_any(compact, _TODAY_MARKERS):
        return "today"
    return ""


def _before_morning_cutoff(received_at: Any) -> bool:
    if received_at is None:
        return False
    try:
        return (
            int(getattr(received_at, "hour", 0) or 0),
            int(getattr(received_at, "minute", 0) or 0),
            int(getattr(received_at, "second", 0) or 0),
            int(getattr(received_at, "microsecond", 0) or 0),
        ) < (9, 0, 0, 0)
    except Exception:
        return False


def _after_or_at_morning_cutoff(received_at: Any) -> bool:
    if received_at is None:
        return False
    try:
        return (
            int(getattr(received_at, "hour", 0) or 0),
            int(getattr(received_at, "minute", 0) or 0),
            int(getattr(received_at, "second", 0) or 0),
            int(getattr(received_at, "microsecond", 0) or 0),
        ) >= (9, 0, 0, 0)
    except Exception:
        return False


def _historical_mutation_blocked_after_cutoff(
    operation: DailyCommandOperation,
    raw_text: str,
    effect: WorkflowEffect,
    *,
    received_at: Any,
) -> bool:
    if operation not in {"fill", "edit", "begin_edit", "clear", "revoke", "confirm"}:
        return False
    if operation in {"fill", "edit"} and _looks_like_resolved_risk_update(raw_text):
        return False
    if not _after_or_at_morning_cutoff(received_at):
        return False
    today = _received_date(received_at)
    if today is None:
        return False
    target_date = effect.target.get("date") or effect.target.get("target_date")
    if _is_historical_target(target_date, today=today):
        return True
    text_hint = _target_date_from_text(raw_text)
    if text_hint in {"yesterday", "day_before_yesterday", "three_days_ago"}:
        return True
    active_report_date = effect.target.get("report_date")
    if _is_historical_target(active_report_date, today=today):
        if text_hint in {"today", "tomorrow"} or _explicitly_targets_today_report(raw_text):
            return False
        return True
    return False


def _received_date(received_at: Any) -> Any:
    value = getattr(received_at, "date", None)
    if callable(value):
        try:
            return value()
        except Exception:
            return None
    return None


def _is_historical_target(value: Any, *, today: Any) -> bool:
    if not value:
        return False
    if value in {"yesterday", "day_before_yesterday", "three_days_ago"}:
        return True
    try:
        return date.fromisoformat(str(value)) < today
    except (TypeError, ValueError):
        return False


def _explicitly_targets_today_report(raw_text: str) -> bool:
    compact = _compact(raw_text)
    return _contains_any(
        compact,
        (
            "\u4eca\u5929\u65e5\u62a5",
            "\u4eca\u65e5\u65e5\u62a5",
            "\u4eca\u5929\u7684\u65e5\u62a5",
            "\u4eca\u65e5\u7684\u65e5\u62a5",
            "\u4eca\u5929\u65e5\u5fd7",
            "\u4eca\u65e5\u65e5\u5fd7",
            "\u4eca\u5929\u7684\u65e5\u5fd7",
            "\u4eca\u65e5\u7684\u65e5\u5fd7",
        ),
    )


def _requires_confirmation(
    plan: RoutingPlan,
    effect: WorkflowEffect,
    operation: DailyCommandOperation,
    raw_text: str,
) -> bool:
    if effect.requires_confirmation:
        return True
    if _can_write_daily_with_sidecar_effects(plan, effect):
        return False
    return plan.safety_decision.commit_policy == "needs_confirmation" and operation not in {
        "query_current",
        "query_history",
        "begin_edit",
    }


def _should_write(operation: DailyCommandOperation, requires_confirmation: bool) -> bool:
    if requires_confirmation:
        return False
    return operation in {"fill", "edit", "confirm", "clear", "revoke", "copy_previous", "copy_current_to_tomorrow", "complete_previous_plan"}


def _safety_flags(
    plan: RoutingPlan,
    effect: WorkflowEffect,
    operation: DailyCommandOperation,
    target_field: DailyCommandTargetField,
    requires_confirmation: bool,
    raw_text: str,
) -> list[str]:
    flags: list[str] = []
    if target_field in {"none", "unknown"} and operation in {"fill", "edit"}:
        flags.append("missing_target_field")
    if requires_confirmation:
        flags.append("requires_confirmation")
    if (
        operation in {"clear", "revoke", "copy_previous", "copy_current_to_tomorrow"}
        or (_contains_any(_compact(raw_text), _CLEAR_MARKERS) and not looks_like_local_delete(raw_text))
        or _looks_like_report_revoke(raw_text)
    ):
        flags.append("destructive_or_overwrite")
    if plan.safety_decision.flags:
        flags.extend(f"plan:{flag}" for flag in plan.safety_decision.flags)
    if effect.effect_type == EFFECT_LEGACY_DAILY_CONTEXT_ACTION:
        flags.append("legacy_context_command")
    return _dedupe(flags)


def _content(effect: WorkflowEffect, operation: DailyCommandOperation, raw_text: str) -> list[str]:
    if operation == "complete_previous_plan":
        content = str(effect.payload.get("content") or raw_text or "").strip()
        return [content] if content else []
    if operation in {
        "confirm",
        "query_current",
        "query_history",
        "begin_edit",
        "clear",
        "revoke",
        "copy_previous",
        "copy_current_to_tomorrow",
        "no_write",
    }:
        return []
    content = str(effect.payload.get("content") or raw_text or "").strip()
    if effect.effect_type == EFFECT_LEGACY_DAILY_CONTEXT_ACTION and operation == "fill" and _looks_like_contextual_problem_followup(raw_text):
        content = _contextual_problem_followup_content(raw_text)
    return [content] if content else []


def _raw_text(effect: WorkflowEffect, envelope: IncomingMessageEnvelope | None) -> str:
    payload_text = effect.payload.get("content")
    if payload_text:
        return str(payload_text)
    if envelope is not None:
        return str(envelope.raw_text or "")
    return ""


def _confidence_from_plan(plan: RoutingPlan) -> DailyCommandConfidence:
    if plan.confidence >= 0.75:
        return "high"
    if plan.confidence >= 0.45:
        return "medium"
    return "low"


def _contains_any(compact_text: str, markers: tuple[str, ...]) -> bool:
    return any(_compact(marker) in compact_text for marker in markers)


def _compact(value: str) -> str:
    return re.sub(r"[\s\u3000:：,，.。;；!！?？()（）\\[\\]【】\"'“”‘’]+", "", str(value or "")).lower()


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _can_write_daily_with_sidecar_effects(plan: RoutingPlan, effect: WorkflowEffect) -> bool:
    if effect.target_system != WORKFLOW_DAILY_REPORT:
        return False
    sidecar_effects = [
        planned.effect_type
        for planned in plan.effects
        if planned.target_system != WORKFLOW_DAILY_REPORT
    ]
    allowed_sidecars = {
        "upsert_travel_plan",
        "append_case_progress",
        "run_legal_research",
        "draft_weekly_report",
    }
    return bool(sidecar_effects) and all(effect_type in allowed_sidecars for effect_type in sidecar_effects)


def _legacy_daily_context_command_eligible(
    operation: DailyCommandOperation,
    target_field: DailyCommandTargetField,
    raw_text: str,
) -> bool:
    """Final gate before active daily context can turn a loose reply into a write.

    The action-first layer owns workflow intent. This guard only preserves
    clearly structured daily operations that were intentionally left to the
    daily command compiler, such as shorthand copies and local edits.
    """

    if operation in {"query_current", "query_history", "begin_edit", "copy_previous", "copy_current_to_tomorrow", "complete_previous_plan", "clear", "revoke"}:
        return True
    if operation == "edit":
        return _eligible_contextual_daily_edit(raw_text)
    if operation != "fill":
        return False
    if target_field not in {"today_work", "problems", "tomorrow_plan"}:
        return False
    return _eligible_contextual_daily_fill(raw_text, target_field=target_field)


def _eligible_contextual_daily_edit(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _looks_like_meta_test_probe(raw_text):
        return False
    if _looks_like_ambiguous_that_day_daily_reference(raw_text):
        return False
    if _looks_like_contextual_retract_or_revoke(raw_text):
        return True
    if _looks_like_named_meeting_delete(raw_text):
        return True
    if _looks_like_quantity_correction(raw_text):
        return True
    if _looks_like_current_daily_item_retraction(raw_text):
        return True
    if _looks_like_quoted_delete_edit(raw_text):
        return True
    if _looks_like_office_device_correction_edit(raw_text):
        return True
    if _looks_like_symbolic_replacement_edit(raw_text):
        return True
    if _looks_like_correction_with_business_content(raw_text):
        return True
    if _looks_like_item_edit(raw_text) or _looks_like_short_delete_reference(raw_text) or looks_like_local_delete(raw_text):
        return True
    if _looks_like_context_edit(raw_text):
        return True
    if _looks_like_negative_replacement(raw_text):
        return True
    if _looks_like_replacement_followup(raw_text):
        return True
    if _looks_like_field_level_delete(raw_text):
        return True
    if not looks_like_contextual_daily_edit(raw_text):
        return False
    if _contains_any(compact, _DAILY_FIELD_MARKERS):
        return True
    if _contains_any(compact, _ORDINAL_MARKERS) and _contains_any(compact, _EDIT_ACTION_MARKERS):
        return True
    if _contains_any(compact, _REPLACEMENT_EDIT_MARKERS):
        return True
    return False


def _looks_like_correction_with_business_content(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not _contains_any(compact, ("\u8bf4\u9519\u4e86", "\u5199\u9519\u4e86", "\u521a\u624d\u5199\u9519", "\u4e0d\u5bf9", "\u6211\u662f\u8bf4", "\u5176\u5b9e\u662f")):
        return False
    has_time = _contains_any(compact, _TODAY_MARKERS + _TOMORROW_MARKERS)
    has_work = _contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)
    return has_work and (has_time or _contains_any(compact, ("\u5176\u5b9e\u662f", "\u4e0d\u662f")))


def _eligible_contextual_daily_fill(raw_text: str, *, target_field: DailyCommandTargetField | None = None) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _looks_like_external_report_edit_request(raw_text) or (_looks_like_office_device_chatter(raw_text) and not _looks_like_office_device_correction_edit(raw_text)):
        return False
    if _looks_like_monthly_meta_request(raw_text) or _looks_like_monthly_metric_fragment(raw_text) or _looks_like_daily_bot_feedback(raw_text):
        return False
    if _looks_like_personal_workplace_rant(raw_text) or _looks_like_generic_busy_chatter(raw_text) or _looks_like_lifestyle_plan_question(raw_text):
        return False
    if _looks_like_self_prompted_forgotten_item(raw_text):
        return False
    if _looks_like_do_not_write_daily(raw_text):
        return False
    if _looks_like_daily_submitted_status(raw_text):
        return False
    if _looks_like_submission_status_question(raw_text):
        return False
    if _looks_like_process_or_policy_question(raw_text):
        return False
    if _looks_like_ambiguous_that_day_daily_reference(raw_text):
        return False
    if _looks_like_emotional_customer_phone_only(raw_text):
        return False
    if _looks_like_tentative_case_resolution_chatter(raw_text):
        return False
    if _looks_like_travel_logistics_only(raw_text) or _looks_like_agent_task_instruction(raw_text):
        return False
    if _looks_like_daily_date_classification_question(raw_text):
        return False
    if _looks_like_past_result_only_update(raw_text):
        return False
    if _looks_like_meeting_work(raw_text):
        return True
    if _looks_like_system_rant_only(raw_text) or _looks_like_standalone_past_work_without_current_context(raw_text):
        return False
    if _contains_any(compact, ("\u6848\u4ef6\u8fdb\u5c55", "\u8bb0\u5230\u6848\u4ef6\u8fdb\u5c55", "\u8bb0\u8fdb\u6848\u4ef6\u8fdb\u5c55")) and _contains_any(compact, ("\u8bb0\u5230", "\u8bb0\u8fdb", "\u5199\u5230", "\u5199\u8fdb")):
        return False
    if _looks_like_travel_application_only(raw_text) or _looks_like_reminder_request_only(raw_text) or _looks_like_weather_question(raw_text) or _looks_like_calendar_event_only(raw_text):
        return False
    if _looks_like_food_or_rest_plan(raw_text) or _looks_like_bare_emotional_travel(raw_text) or _looks_like_non_tomorrow_future_deadline(raw_text) or _looks_like_future_daily_makeup_notice(raw_text) or _looks_like_moyu_self_deprecation(raw_text) or _looks_like_dream_or_fantasy_chatter(raw_text):
        return False
    if _looks_like_generic_tomorrow_continue(raw_text):
        return False
    if _looks_like_no_change_statement(raw_text):
        return False
    if _looks_like_current_daily_status_query(raw_text):
        return False
    if _looks_like_case_commentary_without_action(raw_text):
        return False
    if _looks_like_daily_meta_or_date_question(raw_text):
        return False
    if _looks_like_case_progress_record_request(raw_text):
        return False
    if _looks_like_historical_daily_risk_lookup(raw_text):
        return False
    if _looks_like_creative_assistant_request(raw_text):
        return False
    if _looks_like_meta_test_probe(raw_text):
        return False
    if _looks_like_history_info_delivery_request(raw_text):
        return False
    if _looks_like_assistant_service_request(raw_text):
        return False
    if _looks_like_conditional_feedback_request(raw_text):
        return False
    problem_evidence = extract_problem_evidence(raw_text)
    has_concrete_problem_detail = _has_concrete_problem_detail(raw_text)
    if _looks_like_referential_write_reminder(raw_text) and not has_concrete_problem_detail:
        return False
    if _looks_like_contextual_problem_followup(raw_text):
        return True
    if _looks_like_office_device_correction_edit(raw_text):
        return True
    if _looks_like_completed_yesterday_referenced_task_today(raw_text):
        return True
    if problem_evidence.is_problem or problem_evidence.is_no_problem:
        return True
    if _looks_like_document_correction_problem(raw_text):
        return True
    if target_field == "tomorrow_plan" and _looks_like_contextual_case_strategy_plan(raw_text):
        return True
    if target_field == "problems" and _contains_any(_compact(raw_text), ("\u98ce\u9669", "\u5ef6\u671f", "\u63a8\u8fdf", "\u4e34\u65f6\u6709\u4e8b")):
        if _looks_like_resolved_risk_update(raw_text):
            return False
        return True
    if _looks_like_absurd_content(raw_text) or _looks_like_empty_daily_content(raw_text):
        return False
    if _looks_like_non_substantive_daily_request(raw_text):
        return False
    if _looks_like_daily_meta_commit_request(raw_text):
        return False
    if _looks_like_referential_write_reminder(raw_text) and not has_concrete_problem_detail:
        return False
    if _looks_like_summary_or_analysis_request(raw_text):
        return False
    if _looks_like_weak_travel_destination_fragment(raw_text):
        return False
    if _looks_like_vague_repeat_or_workload_statement(raw_text):
        if target_field == "tomorrow_plan" and _looks_like_short_contextual_plan(raw_text):
            return True
        return False
    if _looks_like_emotional_deferral_without_business(raw_text):
        return False
    if (_looks_like_question(raw_text) and not _looks_like_business_plan_statement(raw_text)) or _looks_like_non_daily_feedback(raw_text):
        return False
    if _looks_like_personal_state_only(raw_text):
        return False
    if _looks_like_subjective_business_chatter(raw_text):
        return False
    if _contains_any(compact, _DAILY_FIELD_MARKERS):
        return True
    if _looks_like_short_contextual_plan(raw_text):
        return True
    if _looks_like_contextual_plan_reference(raw_text):
        return True
    if _looks_like_contextual_business_detail(raw_text):
        return True
    if _looks_like_meeting_work(raw_text):
        return True
    if _looks_like_business_plan_statement(raw_text):
        return True
    if _looks_like_system_failure_problem(raw_text):
        return True
    if _contains_any(compact, ("\u613f\u610f\u548c\u89e3", "\u540c\u610f\u548c\u89e3", "\u8fbe\u6210\u548c\u89e3", "\u548c\u89e3\u610f\u5411")) and _contains_any(
        compact,
        ("\u5bf9\u65b9", "\u5ba2\u6237", "\u7532\u65b9", "\u5f8b\u5e08"),
    ):
        return True
    has_action = _contains_any(compact, _BUSINESS_ACTION_MARKERS)
    has_object = _contains_any(compact, _BUSINESS_OBJECT_MARKERS)
    has_time = _contains_any(compact, _TODAY_MARKERS + _TOMORROW_MARKERS)
    has_clock_time = bool(re.search(r"(?:\u4e0a\u5348|\u4e0b\u5348|\u4e2d\u5348|\u665a\u4e0a|\u65e9\u4e0a)?\d{1,2}(?:\u70b9|\u70b9\u534a|:\d{2})", str(raw_text or "")))
    if _looks_like_legal_document_work(raw_text):
        return True
    if _looks_like_travel_work_task(raw_text):
        return True
    if target_field in {"today_work", "tomorrow_plan"} and has_object and (has_time or has_clock_time):
        return True
    if has_action and has_object:
        return True
    if has_time and (has_action or has_object) and _contains_any(compact, _STRONG_BUSINESS_EVENT_MARKERS):
        return True
    return False


def _has_concrete_problem_detail(raw_text: str) -> bool:
    stripped = re.sub(
        r"[\s，,。.!！?？:：；;]+|刚才说的|前面说的|这个|那个|风险点|风险|问题|困难|卡点|存在|记得|帮我|给我|写上|记上|哈",
        "",
        str(raw_text or ""),
    )
    return _contains_any(_compact(stripped), _BUSINESS_OBJECT_MARKERS)


def _eligible_daily_content(content: list[str], target_field: DailyCommandTargetField) -> list[str]:
    result: list[str] = []
    for item in content:
        cleaned = _clean_daily_content_item(item, target_field)
        if cleaned:
            result.append(cleaned)
    return result


def _looks_like_travel_work_task(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if _looks_like_absurd_content(text) or _looks_like_weak_travel_destination_fragment(text):
        return False
    if not _contains_any(compact, _TODAY_MARKERS + _TOMORROW_MARKERS):
        return False
    if _looks_like_question(text) or _looks_like_lifestyle_only(text):
        return False
    if re.search(
        r"(?:\u8981|\u8ba1\u5212|\u51c6\u5907|\u4f30\u8ba1|\u53ef\u80fd)?(?:\u53bb|\u8d74|\u5230|\u524d\u5f80)[\u4e00-\u9fa5]{2,12}$",
        text,
    ):
        return True
    return bool(
        re.search(
            r"(?:\u51fa\u5dee|\u53bb|\u8d74|\u5230|\u524d\u5f80).{1,18}(?:\u529e\u7406|\u5904\u7406|\u6c9f\u901a|\u5f00\u5ead|\u76d6\u7ae0|\u7528\u5370|\u8d70\u8bbf|\u8ba8\u85aa)",
            text,
        )
    )


def _clean_daily_content_item(item: str, target_field: DailyCommandTargetField) -> str:
    text = str(item or "").strip()
    if not text:
        return ""
    if _looks_like_current_daily_item_retraction(text):
        return text
    if target_field == "today_work" and _looks_like_completed_yesterday_referenced_task_today(text):
        return re.sub(r"^(?:\u6628\u5929|\u6628\u65e5)(?:\u8bf4\u7684|\u63d0\u7684|\u63d0\u5230\u7684)", "", text).strip()
    if (
        _looks_like_weather_question(text)
        or _looks_like_calendar_event_only(text)
        or _looks_like_food_or_rest_plan(text)
        or _looks_like_report_later_deferral(text)
        or _looks_like_travel_logistics_only(text)
        or _looks_like_travel_booking_request_only(text)
        or _looks_like_bare_city_trip_without_purpose(text)
        or _looks_like_agent_task_instruction(text)
        or _looks_like_daily_date_classification_question(text)
        or _looks_like_reminder_request_only(text)
        or _looks_like_travel_application_only(text)
        or _looks_like_bare_emotional_travel(text)
        or _looks_like_non_tomorrow_future_deadline(text)
        or _looks_like_future_daily_makeup_notice(text)
        or _looks_like_moyu_self_deprecation(text)
        or _looks_like_dream_or_fantasy_chatter(text)
        or _looks_like_generic_tomorrow_continue(text)
        or _looks_like_no_change_statement(text)
        or _looks_like_emotional_customer_phone_only(text)
        or _looks_like_current_daily_status_query(text)
        or _looks_like_case_commentary_without_action(text)
        or _looks_like_daily_meta_or_date_question(text)
        or _looks_like_case_progress_record_request(text)
        or _looks_like_history_info_delivery_request(text)
        or _looks_like_historical_daily_risk_lookup(text)
        or _looks_like_monthly_meta_request(text)
        or _looks_like_monthly_metric_fragment(text)
        or _looks_like_unfinished_daily_shell(text)
        or _looks_like_daily_submitted_status(text)
        or _looks_like_submission_status_question(text)
        or _looks_like_do_not_write_daily(text)
        or _looks_like_non_substantive_daily_request(text)
        or _looks_like_process_or_policy_question(text)
        or _looks_like_tentative_case_resolution_chatter(text)
        or _looks_like_weekly_report_routing_note(text)
        or _looks_like_clothing_question(text)
    ):
        return ""
    if target_field == "problems" and _looks_like_no_significant_problem_phrase(text):
        return ""
    if _looks_like_system_rant_only(text) or _looks_like_standalone_past_work_without_current_context(text):
        return ""
    if target_field == "problems" and _looks_like_resolved_risk_update(text):
        return ""
    if target_field == "today_work" and _looks_like_unreportable_today_content(text):
        return ""
    if target_field == "today_work" and _looks_like_subjective_business_chatter(text):
        return ""
    if target_field != "today_work":
        return _polish_daily_content_item(text, target_field)
    if _contains_lifestyle_and_business(text):
        business_fragments = [
            fragment
            for fragment in _split_mixed_content_fragments(text)
            if _eligible_contextual_daily_fill(fragment) and not _looks_like_lifestyle_only(fragment)
        ]
        if business_fragments:
            polished = [
                _polish_daily_content_item(_strip_leading_daily_filler(fragment), target_field)
                for fragment in business_fragments
                if fragment
            ]
            return "\uff0c".join(fragment for fragment in polished if fragment)
    return _polish_daily_content_item(text, target_field)


def _looks_like_unreportable_today_content(text: str) -> bool:
    compact = _compact(text)
    if not compact:
        return True
    if _looks_like_creative_assistant_request(text):
        return True
    if _looks_like_meta_test_probe(text):
        return True
    if _looks_like_history_info_delivery_request(text):
        return True
    if _looks_like_assistant_service_request(text):
        return True
    if _looks_like_report_later_deferral(text):
        return True
    if _looks_like_daily_submitted_status(text):
        return True
    if _looks_like_process_or_policy_question(text):
        return True
    if _looks_like_emotional_customer_phone_only(text):
        return True
    if _looks_like_tentative_case_resolution_chatter(text):
        return True
    if _looks_like_personal_workplace_rant(text) or _looks_like_generic_busy_chatter(text) or _looks_like_lifestyle_plan_question(text):
        return True
    if _looks_like_monthly_meta_request(text) or _looks_like_monthly_metric_fragment(text) or _looks_like_travel_application_only(text):
        return True
    if _looks_like_conditional_feedback_request(text):
        return True
    if _looks_like_vague_repeat_or_workload_statement(text):
        return True
    if _looks_like_case_commentary_without_action(text):
        return True
    if _contains_any(compact, ("\u522b\u5199", "\u4e0d\u8981\u5199", "\u522b\u8bb0", "\u4e0d\u8981\u8bb0", "\u522b\u5199\u4e0a\u53bb", "\u4e0d\u5199\u4e0a\u53bb")):
        return True
    if _contains_any(compact, ("\u6478\u9c7c", "\u6ca1\u5565\u4e8b", "\u6ca1\u4ec0\u4e48\u4e8b", "\u6ca1\u4e8b", "\u5565\u4e5f\u6ca1\u5e72", "\u4ec0\u4e48\u4e5f\u6ca1\u5e72")) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return True
    if _contains_any(compact, ("\u597d\u51b7", "\u592a\u51b7", "\u51b7\u554a", "\u597d\u70ed", "\u592a\u70ed", "\u70ed\u6b7b")) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return True
    if _looks_like_empty_daily_content(text) or _looks_like_lifestyle_only(text) or _looks_like_personal_state_only(text) or _looks_like_unfinished_daily_shell(text):
        return True
    return False


def _looks_like_conditional_feedback_request(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u7684\u8bdd\u7ed9\u4e2a\u53cd\u9988", "\u7684\u8bdd\u53cd\u9988", "\u6536\u5230\u7684\u8bdd", "\u6709\u6d88\u606f\u7684\u8bdd"))


def _looks_like_contextual_problem_followup(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u60c5\u7eea\u6fc0\u52a8", "\u60c5\u7eea\u633a\u6fc0\u52a8", "\u4e00\u5ba1\u4e0d\u516c\u5e73", "\u4e0d\u516c\u5e73")) and _contains_any(
        compact,
        ("\u4ed6\u4eec", "\u5bf9\u65b9", "\u5ba2\u6237", "\u5458\u5de5", "\u5de5\u4eba", "\u8bf4"),
    ):
        return True
    if not _contains_any(compact, ("\u4e0d\u884c", "\u4e0d\u80fd\u7528", "\u5931\u8d25", "\u62a5\u9519", "\u5361\u4f4f", "\u5361\u4e86", "\u62d6\u540e\u817f")):
        return False
    return _contains_any(compact, ("\u7cfb\u7edf", "\u63a5\u53e3", "\u6d41\u7a0b", "\u5ba1\u6279", "\u4f1a\u8bae\u5ba4", "\u9884\u8ba2"))


def _contextual_problem_followup_content(raw_text: str) -> str:
    pieces = [piece.strip() for piece in re.split(r"[\r\n，,。；;！？]+", str(raw_text or "")) if piece.strip()]
    context = ""
    problem = ""
    for piece in pieces:
        compact = _compact(piece)
        if _contains_any(compact, ("\u7cfb\u7edf", "\u63a5\u53e3", "\u6d41\u7a0b", "\u5ba1\u6279", "\u4f1a\u8bae\u5ba4", "\u9884\u8ba2")):
            context = re.sub(r"^(?:\u5bf9\u4e86)?(?:\u6628\u5929|\u6628\u65e5|\u6628\u513f)?(?:\u4f60\u8bf4\u7684)?(?:\u90a3\u4e2a)?", "", piece).strip()
        if _contains_any(compact, ("\u4e0d\u884c", "\u4e0d\u80fd\u7528", "\u5931\u8d25", "\u62a5\u9519", "\u5361\u4f4f", "\u5361\u4e86", "\u62d6\u540e\u817f")):
            problem = piece
            break
    if context and problem and context not in problem:
        return f"{context}\uff0c{problem}"
    return problem or str(raw_text or "").strip()


def _polish_daily_content_item(item: str, target_field: DailyCommandTargetField) -> str:
    value = str(item or "").strip(" \t\r\n\u3000\uff1b;\uff0c,\u3002.")
    if not value:
        return ""
    if target_field == "tomorrow_plan" and _looks_like_tentative_tomorrow_travel_plan(value):
        return value
    value = re.sub(r"^\u4eca\u586b\s*", "", value)
    value = re.sub(r"\s*(?:\uff0c|,)?\s*\u6ca1\u5e72\u5565\u522b\u7684$", "", value)
    value = re.sub(r"\s*(?:\uff0c|,)?\s*\u6ca1\u5e72\u4ec0\u4e48\u522b\u7684$", "", value)
    quoted_copy = re.search(r"\u6628\u5929\u7684?\u65e5\u62a5\u91cc[“\"](.+?)[”\"].{0,12}?\u590d\u5236\u8fc7\u6765", value)
    if quoted_copy:
        value = quoted_copy.group(1)
    quoted_add = re.search(
        r"(?:\u65e5\u62a5|\u65e5\u5fd7|\u4eca\u65e5\u5de5\u4f5c|\u4eca\u5929\u5de5\u4f5c|\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212).{0,16}?"
        r"(?:\u52a0\u4e0a|\u52a0\u5165|\u52a0\u8fdb|\u8865\u4e0a|\u5199\u4e0a|\u8bb0\u4e0a)[\u201c\u201d\u2018\u2019\"'\u300e\u300f\u300c\u300d](?P<content>.+?)[\u201c\u201d\u2018\u2019\"'\u300e\u300f\u300c\u300d]",
        value,
    )
    if quoted_add:
        value = quoted_add.group("content")
    bare_quoted_add = re.search(
        r"^(?:\u518d)?(?:\u52a0\u4e0a|\u52a0\u5165|\u8865\u4e0a|\u8ffd\u52a0)(?:\u4e00\u4e2a|\u4e00\u6761)?[\u201c\u201d\u2018\u2019\"'\u300e\u300f\u300c\u300d](?P<content>.+?)[\u201c\u201d\u2018\u2019\"'\u300e\u300f\u300c\u300d]$",
        value,
    )
    if bare_quoted_add:
        value = bare_quoted_add.group("content")
    value = _strip_leading_daily_filler(value)
    if target_field == "problems":
        value = re.sub(r"^(?:\u8fd8\u6709)?(?:\u4e2a)?(?:\u9057\u7559\u95ee\u9898|遗留问题)\s*", "", value)
        value = re.sub(r"^(?:\u54e6\u5bf9|\u54e6\u5bf9\u4e86|\u5bf9\u4e86|\u55ef|\u90a3|\u8fd8\u6709|also)?\s*(?:\uff0c|,)?\s*(?:\u8fd8\u6709)?\s*(?:\u4e00\u4e2a)?\s*", "", value)
        value = re.sub(r"^(?:\u628a)?(?P<content>.+?)(?:\u4f5c\u4e3a|\u5f53\u6210)(?:\u95ee\u9898|\u98ce\u9669)(?:\u8bb0\u4e0a|\u5199\u4e0a)?$", r"\g<content>", value)
        value = re.sub(r"^(?:\u9047\u5230|\u78b0\u5230)(?:\u4e00\u4e2a)?(?:\u98ce\u9669|\u95ee\u9898)\s*(?:\uff0c|,|:|\uff1a)?\s*", "", value)
        value = re.sub(r"^(?:\u6709\u4e2a)?(?:\u98ce\u9669|\u95ee\u9898)(?:\u5c31\u662f|\u662f)?\s*", "", value)
        value = re.sub(r"^(?:\u7684\u8bdd|\u7684?\u8bdd)\s*(?:\uff0c|,|:|\uff1a)?\s*", "", value)
        value = re.sub(r"^(?:\u5ba2\u6237|\u7532\u65b9|\u5bf9\u65b9)(?P<object>.+?)\u6709\u7591\u8651$", r"客户\g<object>存在疑虑", value)
        value = re.sub(r"^(?:\u95ee\u9898|\u98ce\u9669|\u95ee\u9898/\u98ce\u9669|\u95ee\u9898\u98ce\u9669)\s*(?:\u7684?\u8bdd)?\s*(?:[:：]|\u662f|，|,)?\s*", "", value)
        value = re.sub(r"^(?:\u8fd8\u6709|另外|此外)?\s*(?:，|,)?\s*(?:\u6628\u5929|\u6628\u65e5|之前|此前)(?:\u8bf4\u7684)?(?:\u90a3\u4e2a|\u8fd9\u4e2a)?", "\u6b64\u524d", value)
        value = re.sub(r"\u5df2\u7ecf\u89e3\u9664\u4e86?$", "\u5df2\u89e3\u9664", value)
    if target_field == "today_work":
        value = value.replace("\u6495\u9700\u6c42", "\u6c9f\u901a\u9700\u6c42")
        if _contains_any(_compact(value), ("\u4e0d\u8fc7\u5408\u540c\u603b\u7b97\u7b7e\u5b8c", "\u5408\u540c\u603b\u7b97\u7b7e\u5b8c")):
            value = "\u5408\u540c\u7b7e\u5b8c"
        if _contains_any(_compact(value), ("\u5408\u540c\u603b\u7b97\u7b7e\u5b8c", "\u5408\u540c\u7b7e\u5b8c")) and _contains_any(_compact(value), ("\u5012\u9709", "\u88ab\u5ba2\u6237\u9a82")):
            value = "\u5408\u540c\u7b7e\u5b8c"
        value = re.sub(r"(?:\u90a3\u4e2a)?\u5934\u75bc\u7684", "", value)
        value = value.replace("\u5934\u75bc", "")
        value = re.sub(
            r"^(?:\u6628\u5929|\u6628\u65e5)\u5199\u7684(?:\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212)(?:\u662f)?[^\uff0c,\u3002]*[\uff0c,]\s*",
            "",
            value,
        )
        value = re.sub(r"^(?:\u4eca\u5929|\u4eca\u65e5)(?:\u7684)?(?:\u5de5\u4f5c|今日工作|今天工作)\s*[:\uff1a]\s*", "", value)
        value = re.sub(r"^(?:\u8fd8\u6709\u5462|\u8fd8\u6709|\u53e6\u5916|also)\s*(?:\uff0c|,)?\s*(?:\u90a3\u4e2a|\u8fd9\u4e2a)?", "", value)
        value = re.sub(r"^(?:\u4eca\u5929|\u4eca\u65e5)?\s*(?:\u4e0a\u5348|\u4e0b\u5348|\u4e2d\u5348|\u665a\u4e0a|\u65e9\u4e0a|\d{1,2}\s*\u70b9)?\s*\u628a", "", value)
        if re.fullmatch(r"(?:\u6628\u5929|\u6628\u65e5)(?:\u7684)?(?:\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212|\u8ba1\u5212|\u5f85\u529e|\u5b89\u6392)(?:\u5df2)?(?:\u5b8c\u6210|\u641e\u5b9a|\u505a\u5b8c)(?:\u4e86)?", value):
            return ""
        value = re.sub(r"^(?:\u8865\u4e00\u4e0b|\u8865\u4e0b|\u8865\u5199|\u8865\u4e0a)(?:\u6628\u5929|\u6628\u65e5)(?:\u7684)?(?:\u65e5\u62a5|\u65e5\u5fd7)\s*[\uff0c,]?\s*(?:\u6628\u5929|\u6628\u65e5)?(?:\u4e3b\u8981)?(?:\u5de5\u4f5c)?(?:\u662f)?", "", value)
        prior_intent_match = re.search(
            r"(?:\u6628\u5929|\u6628\u65e5).{0,12}?(?:\u8bf4|\u63d0\u5230|\u8bb2\u8fc7).{0,6}?"
            r"(?:\u4eca\u5929|\u4eca\u65e5)\s*(?:\u8981|\u8ba1\u5212|\u51c6\u5907)?(?P<task>[^\uff0c,\u3002]+)",
            value,
        )
        if prior_intent_match and _looks_like_completed_previous_plan(value):
            value = prior_intent_match.group("task").strip()
            value = re.sub(r"^(?:\u8981|\u8ba1\u5212|\u51c6\u5907)?(?:\u53bb|\u8d74|\u5230|\u524d\u5f80)?", "", value)
        value = re.sub(r"^(?:\u5bf9\u4e86|\u5bf9|哦对了)?\s*(?:\uff0c|,)?\s*(?:\u6628\u5929|\u6628\u65e5)(?:\u7684)?(?:\u65e5\u62a5(?:\u91cc)?(?:\u7684)?|)(?:\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212|\u8ba1\u5212|\u5f85\u529e|\u5b89\u6392)\s*(?:\u91cc)?\s*(?:\u90a3\u4e2a|\u8fd9\u4e2a)?\s*(?:\u5f85\u529e|\u4e8b\u9879)?\s*[:\uff1a\uff0c,、]?\s*", "", value)
        value = re.sub(r"^\u628a?(?:\u4eca\u5929|\u4eca\u65e5)(?:\u7684)?(?:\u5de5\u4f5c\u4e8b\u9879|\u5de5\u4f5c|\u4eca\u65e5\u5de5\u4f5c|\u4eca\u5929\u5de5\u4f5c)\s*(?:\u518d)?(?:\u8865\u5145|\u52a0)(?:\u4e00\u4e2a|\u4e00\u4e0b)?\s*[:\uff1a]?\s*", "", value)
        value = re.sub(r"^(?:\u54e6\u5bf9\u4e86|\u5bf9\u4e86)?\s*(?:\uff0c|,)?\s*(?:\u521a\u624d\u90a3\u4e2a|\u90a3\u4e2a|\u8fd9\u4e2a).{0,20}?(?:\u8fdb\u5ea6|\u4e8b\u9879|\u5185\u5bb9).{0,8}?(?:\u518d\u52a0\u4e00\u70b9|\u8865\u5145\u4e00\u4e0b|\u8865\u5145\u4e00\u4e2a)\s*(?:\uff0c|,|:|\uff1a)?\s*", "", value)
        value = re.sub(r"^(?:\u6211\u4eec)?(?:\u5df2\u7ecf)?\s*", "", value)
        value = re.sub(r"^(?:\u52a0\u4e0a|\u8865\u4e0a|\u8ffd\u52a0)\s*(?:\u4eca\u5929|\u4eca\u65e5)?(?:\u7684)?(?:\u5b8c\u6210\u5de5\u4f5c|\u5de5\u4f5c|\u4eca\u65e5\u5de5\u4f5c|\u4eca\u5929\u5de5\u4f5c)\s*[:\uff1a]?\s*", "", value)
        value = re.sub(r"^(?:\u6628\u5929|\u6628\u65e5)(?:\u7684)?(?:\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212|\u8ba1\u5212|\u5f85\u529e|\u5b89\u6392)\s*(?:\u662f|:|：)?\s*", "", value)
        value = re.sub(r"^(?:\u6628\u5929|\u6628\u65e5)(?:\u7684)?(?:\u65e5\u62a5(?:\u91cc)?(?:\u5199\u7684)?|)(?:\u90a3\u4e2a|\u8fd9\u4e2a)?(?P<task>.+?)(?:\u4eca\u5929|\u4eca\u65e5)(?:\u641e\u5b8c\u4e86|\u641e\u5b8c|\u641e\u5b9a\u4e86|\u641e\u5b9a|\u5b8c\u6210\u4e86|\u5b8c\u6210)$", r"\g<task>", value)
        value = re.sub(r"^(?:\u6628\u5929|\u6628\u65e5)(?:\u4e3b\u8981)?(?:\u5de5\u4f5c)?(?:\u662f)?", "", value)
        value = re.sub(r"^(?:\u6628\u5929|\u6628\u65e5)(?:\u8bf4\u7684|\u63d0\u7684|\u63d0\u5230\u7684)", "", value)
        value = re.sub(r"^(?:\u5df2\u5b8c\u6210|\u5b8c\u6210\u4e86|\u5b8c\u6210)\s*(?:(?:\uff0c|,)\s*|(?:\u90a3\u4e2a|\u8fd9\u4e2a))", "", value)
        value = re.sub(r"^(?:\u521a\u624d\u8bf4\u7684\u90a3\u4e9b|\u521a\u624d\u90a3\u4e9b|\u524d\u9762\u8bf4\u7684\u90a3\u4e9b|\u4e0a\u9762\u90a3\u4e9b|\u90a3\u4e9b)\s*(?:\u518d)?(?:\u52a0\u4e00\u4e2a|\u52a0\u4e00\u6761|\u8865\u4e00\u4e2a|\u8865\u4e00\u6761)\s*", "", value)
        value = re.sub(r"^(?:\u518d)?(?:\u52a0\u4e0a|\u52a0\u5165|\u8865\u4e0a|\u8ffd\u52a0)(?:\u4e00\u4e2a|\u4e00\u6761)?\s*", "", value)
        value = re.sub(r"(?:\u7684)?\u4e8b(?:\u6211)?(?:\u5df2\u7ecf)?(?:\u641e\u5b8c\u4e86|\u641e\u5b8c|\u641e\u5b9a\u4e86|\u641e\u5b9a|\u505a\u5b8c\u4e86|\u505a\u5b8c)$", "", value)
        value = re.sub(r"(?:\uff0c|,)?\s*(?:\u5df2\u7ecf)?(?:\u5df2\u5b8c\u6210|\u5b8c\u6210\u4e86|\u5b8c\u6210|\u505a\u5b8c|\u641e\u5b8c\u4e86|\u641e\u5b8c|\u641e\u5b9a|\u5ba1\u5b8c|\u5904\u7406\u5b8c|\u6c47\u62a5\u5b8c\u4e86|\u6c47\u62a5\u5b8c)$", "", value)
        value = re.sub(r"(?:[，,；;。]?\s*(?:\u7d2f\u6b7b\u4e86?|\u597d\u7d2f|太累|有点累))", "", value)
        value = re.sub(r"(?:[，,；;。]?\s*(?:\u8fd8\u5f97|\u8fd8\u8981|\u5f97|\u8981)?\u5199\u65e5\u62a5)", "", value)
    if target_field == "today_work":
        value = re.sub(r"(?:\u603b\u7b97)?\u5598\u53e3\u6c14", "", value)
        value = re.sub(r"(?:\uff0c|,)?\s*(?:\u54c8\u54c8|\u5934\u5927|\u665a\u4e0a\u5f97?\u5403\u987f\u597d\u7684(?:\u72b8\u52b3\u4e00\u4e0b|\u72b8\u52b3\u4e0b|\u5e86\u795d\u4e0b)?|\u665a\u4e0a\u5403\u987f\u597d\u7684\u5e86\u795d\u4e0b|\u72b8\u52b3\u4e00\u4e0b|\u72b8\u52b3\u4e0b|\u5e86\u795d\u4e0b)$", "", value)
    value = re.sub(r"^(?:\u7136\u540e|\u987a\u4fbf|\u8fd8|also|\u4e5f|\u540c\u65f6|\u5e76\u4e14|\u53e6\u5916|\u6b64\u5916|\u4e3b\u8981|\u4e3b\u8981\u662f|\u5c31\u662f|\u8fd8\u6709\u5c31\u662f|\u4ee5\u53ca)", "", value)
    value = re.sub(r"^(?:\u6211|本人)\s*", "", value).strip()
    value = re.sub(r"^(?:\u54e6\u5bf9|\u54e6\u5bf9\u4e86|\u5bf9\u4e86|\u55ef|\u90a3)\s*(?:\uff0c|,)?\s*(?:\u8fd8\u6709)?\s*(?:\u90a3\u4e2a|\u8fd9\u4e2a)?\s*", "", value)
    value = re.sub(r"^\u8fd8\u6709\s*(?:\uff0c|,)?\s*(?:\u90a3\u4e2a|\u8fd9\u4e2a)?\s*", "", value)
    if target_field == "tomorrow_plan":
        value = re.sub(r"^(?:\u518d)?(?:\u628a)?(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f)?(?:\u7684)?(?:\u8ba1\u5212|\u660e\u5929\u8ba1\u5212|\u660e\u65e5\u8ba1\u5212)?(?:\u52a0\u4e0a|\u52a0\u5165|\u52a0\u8fdb|\u8865\u4e0a)\s*(?:[:\uff1a])?\s*", "", value)
        value = re.sub(r"^\u4e86(?=\u660e\u5929|\u660e\u65e5|\u660e\u513f)", "", value)
        match = re.search(r"^(?:\u5bf9|嗯|恩)?[，,]?\s*([\u4e00-\u9fa5A-Za-z0-9]{2,20})\u7684[，,]?\s*(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f)?(?:\u8fd8)?(?:\u8981|\u5f97)?\u8ddf\u8fdb$", value)
        if match:
            value = f"\u8ddf\u8fdb{match.group(1)}\u4e8b\u9879"
        if _contains_any(_compact(value), ("\u8fd9\u4e2a\u4e8b\u660e\u5929\u8fd8\u5f97\u7ee7\u7eed", "\u8fd9\u4ef6\u4e8b\u660e\u5929\u8fd8\u5f97\u7ee7\u7eed", "\u8fd9\u4e8b\u660e\u5929\u8fd8\u5f97\u7ee7\u7eed")):
            value = "\u7ee7\u7eed\u8ddf\u8fdb\u524d\u8ff0\u4e8b\u9879"
        if re.fullmatch(r"(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f)?(?:\u7ee7\u7eed|\u63a5\u7740|\u518d)?(?:\u5f04|\u641e|\u6574|\u5904\u7406|\u63a8\u8fdb)(?:\u8fd9\u4e2a|\u8fd9\u4ef6\u4e8b|\u8fd9\u9879)?", value):
            value = "\u7ee7\u7eed\u8ddf\u8fdb\u524d\u8ff0\u4e8b\u9879"
        value = re.sub(r"^(?:\u9884\u8ba1|\u4f30\u8ba1)?\s*(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f)(?:\u4e0a\u5348|\u4e0b\u5348|\u4e2d\u5348|\u665a\u4e0a|\u65e9\u4e0a)?\s*(?:\u7684)?\s*(?:\u8ba1\u5212|\u51c6\u5907|\u6253\u7b97|\u8981|\u8fd8\u8981|\u8fd8\u5f97|\u5f97)?\s*", "", value)
        value = re.sub(r"^(?:\u90a3|\u90a3\u4e2a)?\s*(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f)\s*(?:\u7684)?\s*(?:\u8ba1\u5212|\u51c6\u5907|\u6253\u7b97|\u8981|\u8fd8\u8981|\u8fd8\u5f97|\u5f97)?\s*(?:\u8fd8\u662f|\u5c31\u662f|\u662f)?\s*", "", value)
        value = re.sub(r"^(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f)\s*(?:\u7684)?\s*(?:\u8ba1\u5212|\u51c6\u5907|\u6253\u7b97|\u8981|\u8fd8\u8981|\u8fd8\u5f97|\u5f97)?\s*(?:\u5c31\u662f|\u662f)?\s*", "", value)
        value = re.sub(r"^(?:\u8ba1\u5212|\u51c6\u5907|\u6253\u7b97)\s*", "", value)
        value = re.sub(r"^(?:\u7ee7\u7eed)?\u8fd8\u662f\s*", "", value)
        value = re.sub(r"\s*(?:\uff0c|,)?\s*(?:\u522b\u5fd8\u4e86|\u8bb0\u5f97)$", "", value)
    if target_field in {"today_work", "tomorrow_plan"}:
        value = re.sub(r"(?<=[\u4e00-\u9fa5])\u4e86(?=[\u4e00-\u9fa5A-Za-z0-9])", "", value)
        value = re.sub(r"(?<=[\u4e00-\u9fa5])\u4e86$", "", value)
    return value.strip(" \t\r\n\u3000\uff1b;\uff0c,\u3002.:\uff1a")


def _looks_like_tentative_tomorrow_travel_plan(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if not _contains_any(compact, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u4e2a")):
        return False
    if not _contains_any(compact, ("\u4f30\u8ba1", "\u9884\u8ba1", "\u53ef\u80fd", "\u5927\u6982", "\u5e94\u8be5")):
        return False
    return bool(
        re.search(
            r"(?:\u4f30\u8ba1|\u9884\u8ba1|\u53ef\u80fd|\u5927\u6982|\u5e94\u8be5)?"
            r"(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f|\u660e\u4e2a)"
            r"(?:\u4e0a\u5348|\u4e0b\u5348|\u65e9\u4e0a)?"
            r"(?:\u8981|\u5f97|\u8fd8\u8981|\u8fd8\u5f97)?"
            r"(?:\u53bb|\u8d74|\u5230|\u524d\u5f80)[\u4e00-\u9fa5]{2,12}$",
            text,
        )
    )


def _looks_like_natural_tomorrow_plan_sentence(raw_text: str) -> bool:
    text = str(raw_text or "").strip(" \t\r\n\u3000\uff1b;\uff0c,\u3002.")
    compact = _compact(text)
    if not compact:
        return False
    if not _contains_any(compact, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u65e9")):
        return False
    if re.search(r"^(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f)(?:\u7684)?(?:\u8ba1\u5212|\u5b89\u6392)(?:\u662f|:|\uff1a)", text):
        return False
    return (
        _contains_any(compact, _BUSINESS_ACTION_MARKERS)
        or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)
        or _contains_any(compact, ("\u51fa\u5dee", "\u5f00\u5ead", "\u76d6\u7ae0", "\u7528\u5370", "\u8ddf\u8fdb", "\u63a8\u8fdb"))
    )


def _contains_lifestyle_and_business(text: str) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    return _contains_any(compact, _LIFESTYLE_OBJECT_MARKERS) and (
        _contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)
    )


def _split_mixed_content_fragments(text: str) -> list[str]:
    normalized = re.sub(r"\s+", "\uff0c", str(text or "").strip())
    protected = re.sub(r"(?<=[\u4e00-\u9fa5])\u6211(?=[\u4e00-\u9fa5]{0,8}(?:\u5403|\u559d|\u770b|\u73a9|\u4e70|\u7761))", "\uff0c\u6211", normalized)
    protected = re.sub(r"(?<=[\u4e00-\u9fa5])(?=(?:\u8bc4\u5ba1|\u5ba1\u6838|\u5904\u7406|\u5b8c\u6210|\u641e\u5b8c|\u6c9f\u901a|\u6574\u7406|\u68b3\u7406|\u8d77\u8349|\u8ddf\u8fdb|\u5f00\u5ead|\u7528\u5370|\u76d6\u7ae0|\u5199|\u4fee\u6539|\u6539))", "\uff0c", protected)
    return [fragment.strip(" \t\r\n\uff0c,;\uff1b\u3002") for fragment in re.split(r"[\uff0c,;\uff1b\u3002]+", protected) if fragment.strip(" \t\r\n\uff0c,;\uff1b\u3002")]


def _strip_leading_daily_filler(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^(?:\u90a3|\u5bf9\u4e86|\u54e6\u5bf9\u4e86)?\s*(?:\uff0c|,)?\s*\u5e2e\u6211(?:\u8bb0|\u8bb0\u5f55)(?:\u4e00\u4e0b|\u4e00\u6761)?\s*(?:\uff0c|,|:|\uff1a)?\s*", "", value)
    value = re.sub(r"^(?:\u7ee7\u7eed\u62a5|\u63a5\u7740\u62a5|\u518d\u62a5(?:\u4e00\u4e0b)?|\u8865\u5145\u4e00\u4e0b)\s*(?:[:\uff1a\uff0c,，])?\s*", "", value)
    value = re.sub(r"^(?:\u4eca\u5929|\u4eca\u65e5)\s*(?:\u6211)?\s*", "", value)
    value = re.sub(r"^(?:\u65e5\u62a5|\u65e5\u5fd7)\s*[:：]\s*", "", value)
    value = re.sub(r"^(?:\u4eca\u5929|\u4eca\u65e5)\s*(?:\u6211)?\s*", "", value)
    value = re.sub(r"^(?:\u54e6\u5bf9|对了|另外|还有|那)?\s*(?:你)?\s*(?:帮我)?\s*(?:在)?\s*(?:\u65e5\u62a5|\u65e5\u5fd7)\s*(?:里)?\s*(?:\u5c31)?\s*(?:\u5199|记|记录|记一下)\s*[:：]?\s*", "", value)
    value = re.sub(r"^(?:\u90a3)?\s*(?:\u4eca\u5929|\u4eca\u65e5)?\s*(?:\u7684)?\s*(?:\u65e5\u62a5|\u65e5\u5fd7)\s*(?:\u5c31)?\s*(?:\u5199|记|记录|记一下)\s*[:：]?\s*", "", value)
    return value.strip()


def _looks_like_lifestyle_only(text: str) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    if _looks_like_absurd_content(text):
        return True
    return _contains_any(compact, _LIFESTYLE_OBJECT_MARKERS) and not (
        _contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)
    )


def _looks_like_emotional_deferral_without_business(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return False
    return _contains_any(compact, ("\u7d2f\u6b7b", "\u597d\u7d2f", "\u70e6\u6b7b", "\u4e0d\u60f3\u5e72\u6d3b", "\u4e0d\u60f3\u52a8")) and _contains_any(
        compact,
        ("\u660e\u5929\u518d\u8bf4", "\u660e\u65e5\u518d\u8bf4", "\u56de\u5934\u518d\u8bf4", "\u518d\u8bf4\u5427"),
    )


def _looks_like_personal_workplace_rant(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return False
    return _contains_any(compact, ("\u8001\u677f", "\u9886\u5bfc")) and _contains_any(compact, ("\u603c", "\u9a82", "\u51f6", "\u70e6", "\u751f\u6c14"))


def _looks_like_generic_busy_chatter(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if compact in {"\u5168\u5728\u5f00\u4f1a", "\u4e00\u76f4\u5f00\u4f1a", "\u5168\u662f\u4f1a", "\u4eca\u5929\u53c8\u662f\u5fd9\u788c\u7684\u4e00\u5929\u554a", "\u4eca\u5929\u662f\u5fd9\u788c\u7684\u4e00\u5929", "\u5fd9\u788c\u7684\u4e00\u5929"}:
        return True
    if _contains_any(compact, ("\u5565\u4e5f\u6ca1\u5e72\u6210", "\u4ec0\u4e48\u4e5f\u6ca1\u5e72\u6210", "\u6ca1\u5e72\u6210")) and _contains_any(compact, ("\u5168\u5728\u5f00\u4f1a", "\u4e00\u76f4\u5f00\u4f1a", "\u5168\u662f\u4f1a")):
        return True
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS) and not _contains_any(compact, ("\u7535\u8bdd", "\u63a5\u7535\u8bdd")):
        return False
    return _contains_any(compact, ("\u5fd9\u5230\u98de\u8d77", "\u5fd9\u6b7b", "\u5149\u63a5\u7535\u8bdd", "\u63a5\u4e8620\u4e2a", "\u4e8b\u60c5\u633a\u591a", "\u4e8b\u633a\u591a", "\u4eca\u5929\u53c8\u52a0\u73ed", "\u53c8\u52a0\u73ed\u7d2f\u6b7b", "\u52a0\u73ed\u7d2f\u6b7b"))


def _looks_like_lifestyle_plan_question(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u53bb\u54ea\u55e8", "\u53bb\u54ea\u91cc\u55e8", "\u665a\u4e0a\u53bb\u54ea", "\u5468\u4e94\u5566", "\u5468\u4e94\u4e86")) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS)


def _looks_like_travel_application_only(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u7533\u8bf7\u4e2a\u51fa\u5dee", "\u7533\u8bf7\u51fa\u5dee", "\u5148\u7533\u8bf7\u4e2a\u51fa\u5dee")) and not _contains_any(
        compact,
        ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u5df2\u7ecf"),
    )


def _looks_like_reminder_request_only(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u5e2e\u6211\u63d0\u9192", "\u63d0\u9192\u4e0b", "\u63d0\u9192\u6211", "\u5e2e\u6211\u8bb0\u5f97", "\u8bb0\u5f97\u63d0\u9192\u6211")) and _contains_any(
        compact,
        ("\u4f1a\u8bae", "\u5f00\u4f1a", "\u65e5\u7a0b", "\u65f6\u95f4", "\u5468\u62a5", "\u65e5\u62a5", "\u63d0\u4ea4", "\u4ea4"),
    )


def _looks_like_weather_question(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u5929\u6c14", "\u4e0b\u96e8", "\u964d\u6e29", "\u51e0\u5ea6")) and _contains_any(
        compact,
        ("\u600e\u6837", "\u548b\u6837", "\u600e\u4e48\u6837", "\u5417", "\u4e0d\u4e0b", "\u51e0\u5ea6"),
    )


def _looks_like_calendar_event_only(raw_text: str) -> bool:
    text = str(raw_text or "")
    compact = _compact(text)
    if not compact:
        return False
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return False
    return bool(re.search(r"(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f).{0,6}\d{1,2}\s*\u70b9.{0,4}(?:\u6709\u4e2a\u4f1a|\u5f00\u4f1a|\u4f1a\u8bae)", text))


def _looks_like_food_or_rest_plan(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u5403\u996d", "\u5403\u4e86\u4e2a\u996d", "\u9ec4\u7116\u9e21", "\u65e5\u6599")) and not _contains_any(compact, ("\u8c08", "\u6c9f\u901a", "\u4f1a\u8bae", "\u5408\u540c", "\u9879\u76ee", "\u6848")):
        return True
    return _contains_any(compact, ("\u60f3\u5403", "\u53bb\u5403", "\u5403\u996d", "\u5403\u4e86\u4e2a\u996d", "\u9ec4\u7116\u9e21", "\u65e5\u6599", "\u5403\u5c0f\u9f99\u867e", "\u5c0f\u9f99\u867e", "\u6708\u4eae", "\u6708\u997c", "\u7761\u89c9", "\u7761\u61d2\u89c9", "\u4f11\u606f")) and not _contains_any(
        compact,
        _BUSINESS_OBJECT_MARKERS,
    )


def _looks_like_bare_emotional_travel(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if not _contains_any(compact, ("\u51fa\u5dee", "\u53bb\u51fa\u5dee")):
        return False
    has_destination_or_work = _known_travel_destination(str(raw_text or "")) or _loose_travel_destination(str(raw_text or "")) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)
    return not has_destination_or_work and _contains_any(compact, ("\u70e6", "\u53c8\u8981", "\u597d\u7d2f", "\u7d2f"))


def _looks_like_travel_logistics_only(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u9700\u8981\u5e26\u54ea\u4e9b", "\u9700\u8981\u5e26\u4ec0\u4e48", "\u8981\u5e26\u54ea\u4e9b", "\u8981\u5e26\u4ec0\u4e48", "\u5e26\u54ea\u4e9b\u8bbe\u5907", "\u5e26\u4ec0\u4e48\u8bbe\u5907")) and _contains_any(
        compact,
        ("\u8bbe\u5907", "\u6750\u6599", "\u6848\u5377", "\u8d44\u6599"),
    ):
        return True
    if not _contains_any(compact, ("\u706b\u8f66\u7968", "\u8f66\u7968", "\u673a\u7968", "\u9ad8\u94c1\u7968", "\u9152\u5e97", "\u4f4f\u5bbf")):
        return False
    if _contains_any(compact, ("\u5408\u540c", "\u534f\u8bae", "\u9879\u76ee", "\u6848\u4ef6", "\u6848\u5b50", "\u6cd5\u5f8b\u610f\u89c1")):
        return False
    return _contains_any(compact, ("\u4e70", "\u5b9a", "\u8ba2", "\u9884\u8ba2", "\u5f97\u4e70", "\u5f97\u5b9a")) and not _contains_any(
        compact,
        ("\u5f00\u5ead", "\u6c9f\u901a", "\u5904\u7406", "\u529e\u7406", "\u76d6\u7ae0", "\u7528\u5370", "\u89c1\u5ba2\u6237", "\u8d70\u8bbf"),
    )


def _looks_like_travel_booking_request_only(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u8ba2\u4e2a\u9152\u5e97", "\u8ba2\u9152\u5e97", "\u5b89\u6392\u4e2a\u63a5\u673a", "\u5b89\u6392\u63a5\u673a", "\u5e2e\u6211\u8ba2", "\u5e2e\u6211\u5b89\u6392")) and _contains_any(
        compact,
        ("\u98de\u4e0a\u6d77", "\u98de\u5317\u4eac", "\u51fa\u5dee", "\u673a\u7968", "\u9152\u5e97", "\u63a5\u673a"),
    ) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS)


def _looks_like_bare_city_trip_without_purpose(raw_text: str) -> bool:
    text = str(raw_text or "")
    compact = _compact(text)
    if not compact:
        return False
    if _contains_any(compact, ("\u51fa\u5dee", "\u5f00\u5ead", "\u6cd5\u9662", "\u5ba2\u6237", "\u9879\u76ee", "\u6848", "\u516c\u8bc1\u5904", "\u7528\u5370", "\u76d6\u7ae0")):
        return False
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return False
    return bool(re.fullmatch(r"(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f)?(?:\u98de|\u53bb|\u5230|赴)(?:\u4e0a\u6d77|\u5317\u4eac|\u5357\u4eac|\u676d\u5dde|\u82cf\u5dde|\u5e7f\u5dde|\u6df1\u5733|\u6210\u90fd|\u91cd\u5e86|\u6b66\u6c49|\u5408\u80a5|\u65e0\u9521|\u5e38\u5dde|\u5609\u5174)", compact))


def _looks_like_weekly_report_routing_note(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return "\u5468\u62a5" in compact and _contains_any(compact, ("\u5199\u8fdb\u5468\u62a5", "\u5199\u5230\u5468\u62a5", "\u8bb0\u5230\u5468\u62a5", "\u653e\u5230\u5468\u62a5"))


def _looks_like_clothing_question(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u7a7f\u5565", "\u7a7f\u4ec0\u4e48", "\u7a7f\u54ea", "\u7a7f\u4ec0\u4e48\u8863\u670d", "\u7a7f\u5565\u8863\u670d")) and _contains_any(
        compact,
        ("\u5408\u9002", "\u51fa\u95e8", "\u51fa\u5ead", "\u5f00\u5ead", "\u660e\u5929", "\u660e\u65e5"),
    )


def _looks_like_daily_date_classification_question(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u7b97\u4eca\u5929\u7684\u6d3b\u8fd8\u662f\u660e\u5929", "\u7b97\u4eca\u5929\u8fd8\u662f\u660e\u5929", "\u5199\u4eca\u5929\u8fd8\u662f\u660e\u5929")) and _looks_like_question(raw_text)


def _looks_like_agent_task_instruction(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if not compact or _contains_any(compact, _TODAY_MARKERS + _TOMORROW_MARKERS) or _contains_any(compact, _DAILY_FIELD_MARKERS):
        return False
    if extract_problem_evidence(text).is_problem or _contains_any(compact, ("\u8fd9\u4e2a\u95ee\u9898\u4e5f\u8bb0\u4e0a", "\u95ee\u9898\u4e5f\u8bb0\u4e0a", "\u98ce\u9669\u4e5f\u8bb0\u4e0a")):
        return False
    if _contains_any(compact, _ORDINAL_MARKERS) and _contains_any(compact, _EDIT_ACTION_MARKERS):
        return False
    if compact.startswith("\u628a") and _contains_any(
        compact,
        ("\u5ba1\u6838", "\u5ba1\u67e5", "\u6574\u7406", "\u67e5", "\u67e5\u8be2", "\u7edf\u8ba1", "\u751f\u6210", "\u53d1", "\u53d1\u9001", "\u5199", "\u6539", "\u4fee\u6539"),
    ) and _contains_any(compact, ("\u4e00\u4e0b", "\u4e0b", "\u5427")):
        return True
    return bool(re.search(r"^把.+(?:审核|审查|整理|查|查询|统计|生成|发|发送|写|改|修改).{0,8}(?:一下|下|吧|。)?$", text))


def _looks_like_non_tomorrow_future_deadline(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, _NON_TOMORROW_FUTURE_MARKERS) and _contains_any(compact, ("\u98de", "\u53bb", "\u51fa\u5dee", "\u53c2\u52a0", "\u5cf0\u4f1a", "\u5f00\u5ead")) and not _contains_any(
        compact,
        ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f"),
    ):
        return True
    return _contains_any(compact, ("\u4e0b\u5468", "\u5468\u4e00", "\u5468\u4e8c", "\u5468\u4e09", "\u5468\u56db", "\u5468\u4e94")) and _contains_any(
        compact,
        ("\u524d\u8981", "\u8981\u7ed9", "\u622a\u6b62", "\u5230\u671f", "\u63d0\u4ea4", "\u4ea4\u4ed8"),
    ) and not _contains_any(compact, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f"))


def _looks_like_future_daily_makeup_notice(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7")) and _contains_any(compact, ("\u5468\u4e94\u4e00\u8d77\u8865", "\u56de\u6765\u4e00\u8d77\u8865", "\u5230\u65f6\u5019\u4e00\u8d77\u8865", "\u4e00\u8d77\u8865"))


def _looks_like_moyu_self_deprecation(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if "\u6478\u9c7c" in compact and _contains_any(compact, ("\u5f00\u73a9\u7b11", "\u5220\u6389", "\u5220\u4e86", "\u522b\u5199")):
        return True
    if "\u6478\u9c7c" in compact and _contains_any(compact, ("\u6ca1\u5565\u4e8b", "\u6ca1\u4ec0\u4e48\u4e8b", "\u6ca1\u4e8b")):
        return True
    return "\u6478\u9c7c" in compact and _contains_any(compact, ("\u867d\u7136", "\u4f46", "\u53ea\u5199", "\u771f\u662f\u5145\u5b9e"))


def _looks_like_dream_or_fantasy_chatter(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u505a\u4e86\u4e2a\u68a6", "\u68a6\u5230", "\u68a6\u89c1", "\u7ee7\u7eed\u505a\u68a6", "\u5347\u804c\u52a0\u85aa", "\u4e2d\u4e86\u5f69\u7968", "\u8d62\u4e86\u5f69\u7968"))


def _looks_like_generic_tomorrow_continue(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return False
    if compact in {"\u660e\u5929\u7ee7\u7eed", "\u660e\u65e5\u7ee7\u7eed", "\u660e\u513f\u7ee7\u7eed"}:
        return True
    if _contains_any(compact, ("\u660e\u5929\u518d\u7814\u7a76\u8fd9\u4e9b", "\u660e\u65e5\u518d\u7814\u7a76\u8fd9\u4e9b", "\u660e\u5929\u518d\u770b\u8fd9\u4e9b")):
        return True
    return _contains_any(compact, ("\u522b\u7684\u6ca1\u4e86", "\u6ca1\u5565\u4e86", "\u6ca1\u4e86")) and _contains_any(
        compact,
        ("\u660e\u5929\u7ee7\u7eed", "\u660e\u65e5\u7ee7\u7eed", "\u660e\u513f\u7ee7\u7eed"),
    )


def _looks_like_no_change_statement(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u8ba1\u5212\u4e0d\u53d8", "\u660e\u5929\u8ba1\u5212\u4e0d\u53d8", "\u660e\u65e5\u8ba1\u5212\u4e0d\u53d8", "\u4e0d\u53d8", "\u6ca1\u53d8\u5316")) and not (
        _contains_any(compact, _BUSINESS_ACTION_MARKERS) and _contains_any(compact, _BUSINESS_OBJECT_MARKERS)
    )


def _looks_like_past_result_only_update(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if not (
        any(marker in compact for marker in ("\u6628\u5929", "\u6628\u65e5", "\u524d\u5929"))
        or ("\u524d\u65e5" in compact and "\u5f53\u524d\u65e5" not in compact)
    ):
        return False
    if not _contains_any(compact, ("\u7ed3\u679c\u51fa\u6765", "\u901a\u8fc7", "\u5ba1\u6279\u901a\u8fc7", "\u5ba1\u5b8c")):
        return False
    return not _contains_any(compact, ("\u4eca\u5929", "\u4eca\u65e5", "\u5199\u5230\u4eca\u5929", "\u8bb0\u5230\u4eca\u5929"))


def _looks_like_option_question(raw_text: str) -> bool:
    text = str(raw_text or "")
    compact = _compact(text)
    if not compact:
        return False
    if _contains_any(compact, ("\u7ebf\u4e0a\u8fd8\u662f\u7ebf\u4e0b", "\u7ebf\u4e0b\u8fd8\u662f\u7ebf\u4e0a")):
        return True
    return _contains_any(
        compact,
        (
            "\u53bb\u4e0d\u53bb",
            "\u6765\u4e0d\u6765",
            "\u8981\u4e0d\u8981",
            "\u80fd\u4e0d\u80fd",
            "\u53ef\u4e0d\u53ef\u4ee5",
        ),
    )


def _looks_like_creative_assistant_request(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    return _contains_any(
        compact,
        (
            "\u7ed9\u6211\u5199\u7bc7",
            "\u5e2e\u6211\u5199\u7bc7",
            "\u5199\u7bc7\u5c0f\u8bf4",
            "\u79d1\u5e7b\u5c0f\u8bf4",
            "\u5199\u4e2a\u6545\u4e8b",
            "\u7ed9\u6211\u5199\u4e2a\u6545\u4e8b",
        ),
    )


def _looks_like_meta_test_probe(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u8ba9\u6211\u6d4b\u8bd5", "\u6211\u6d4b\u8bd5\u4e0b", "\u6211\u6d4b\u8bd5\u4e00\u4e0b")) and _contains_any(
        compact,
        ("\u5199\u65e5\u62a5", "\u65e5\u62a5", "\u673a\u5668\u4eba", "\u7cfb\u7edf"),
    ):
        return True
    if _contains_any(compact, ("\u4e0d\u662f\u771f\u7684\u65e5\u62a5", "\u4e0d\u662f\u771f\u65e5\u62a5", "\u5c31\u662f\u8bd5\u8bd5")) and _contains_any(
        compact,
        ("\u6d4b\u8bd5", "\u8bd5\u8bd5", "\u6d4b\u4e00\u4e0b", "\u6d4b\u4e0b"),
    ):
        return True
    if _contains_any(compact, ("test", "\u6d4b\u8bd5", "\u8ba9\u6211\u6d4b\u8bd5")) and _contains_any(
        compact,
        ("\u65e5\u62a5\u529f\u80fd", "\u6708\u62a5\u529f\u80fd", "\u5468\u62a5\u529f\u80fd", "\u6848\u4ef6\u529f\u80fd", "\u51fa\u5dee\u529f\u80fd"),
    ):
        return True
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS) and not _contains_any(compact, ("\u7cfb\u7edf", "\u673a\u5668\u4eba")):
        return False
    return _contains_any(compact, ("\u522b\u5f53\u771f", "\u4e0d\u8981\u5f53\u771f", "\u6211\u5c31\u6d4b\u8bd5", "\u53ea\u662f\u6d4b\u8bd5")) and _contains_any(
        compact,
        ("\u6d4b\u8bd5\u7cfb\u7edf", "\u6d4b\u8bd5\u4e00\u4e0b\u7cfb\u7edf", "\u6d4b\u8bd5\u4e0b\u7cfb\u7edf", "\u6d4b\u8bd5"),
    )


def _looks_like_assistant_service_request(raw_text: str) -> bool:
    text = str(raw_text or "")
    compact = _compact(text)
    if not compact:
        return False
    if _contains_any(compact, ("\u5e2e\u6211\u628a", "\u5e2e\u6211\u5199\u65e5\u62a5", "\u5e2e\u6211\u8bb0\u5230\u65e5\u62a5")):
        return False
    if re.search(r"\u4f60\u628a.+?(\u6574\u7406|\u6c47\u603b|\u603b\u7ed3|\u63d0\u70bc).+?\u7ed9\u6211", text):
        return True
    if _contains_any(compact, ("\u6700\u8fd1\u7684\u8fdb\u5c55\u6574\u7406\u4e00\u4e0b\u7ed9\u6211", "\u8fdb\u5c55\u6574\u7406\u4e00\u4e0b\u7ed9\u6211", "\u6574\u7406\u4e00\u4e0b\u7ed9\u6211")) and _contains_any(
        compact,
        ("\u6848\u5b50", "\u6848\u4ef6", "\u8fdb\u5c55"),
    ):
        return True
    if _contains_any(compact, ("\u5e2e\u6211\u770b", "\u5e2e\u6211\u770b\u770b")) and _contains_any(
        compact,
        ("\u4eca\u5929", "\u8bd5\u4e86", "\u8fd8\u662f\u4e0d\u884c", "\u4e0d\u884c", "\u6545\u969c", "\u95ee\u9898", "\u7cfb\u7edf"),
    ):
        return False
    return _contains_any(compact, ("\u8bb0\u5f97\u5e2e\u6211", "\u5e2e\u6211\u7533\u8bf7", "\u5e2e\u6211\u67e5", "\u5e2e\u6211\u770b"))


def _looks_like_document_correction_problem(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    has_document = _contains_any(
        compact,
        (
            "\u5224\u51b3\u4e66",
            "\u88c1\u5b9a\u4e66",
            "\u6587\u4e66",
            "\u8d77\u8bc9\u72b6",
            "\u7b54\u8fa9\u72b6",
            "\u5408\u540c",
            "\u534f\u8bae",
        ),
    )
    has_error = _contains_any(compact, ("\u5199\u9519", "\u9519\u4e86", "\u6709\u9519", "\u65e5\u671f\u9519", "\u91d1\u989d\u9519", "\u4fe1\u606f\u9519"))
    has_followup = _contains_any(compact, ("\u66f4\u6b63", "\u4fee\u6b63", "\u901a\u77e5\u6cd5\u9662", "\u8054\u7cfb\u6cd5\u9662", "\u8ddf\u6cd5\u9662\u6c9f\u901a"))
    return has_document and has_error and (has_followup or _contains_any(compact, ("\u53d1\u73b0", "\u9014\u4e2d\u53d1\u73b0")))


def _looks_like_external_report_edit_request(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    return _contains_any(compact, ("\u6628\u5929\u7684\u62a5\u544a", "\u6628\u65e5\u7684\u62a5\u544a", "\u524d\u5929\u7684\u62a5\u544a", "\u62a5\u544a")) and _contains_any(
        compact,
        ("\u7b2c\u4e09\u9875", "\u7b2c3\u9875", "\u6570\u636e\u66f4\u65b0", "\u66f4\u65b0\u6570\u636e", "\u6539\u4e00\u4e0b", "\u4fee\u6539\u4e00\u4e0b"),
    )


def _looks_like_office_device_chatter(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u6253\u5370\u673a", "\u590d\u5370\u673a")) and _contains_any(compact, ("\u574f", "\u574f\u4e86", "\u5361\u7eb8", "\u6ca1\u58a8", "\u6253\u4e0d\u51fa"))


def _looks_like_office_device_correction_edit(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u6253\u5370\u673a", "\u590d\u5370\u673a")) and _contains_any(
        compact,
        ("\u5176\u5b9e", "\u4e0d\u662f", "\u6ca1\u574f", "\u662f\u6ca1\u58a8", "\u6ca1\u58a8\u4e86"),
    ) and _contains_any(compact, ("\u6539\u4e00\u4e0b", "\u6539\u4e0b", "\u4fee\u6539\u4e00\u4e0b", "\u4fee\u6539\u4e0b"))


def _looks_like_symbolic_replacement_edit(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if not _contains_any(compact, ("\u628a", "\u6539\u6210", "\u6539\u4e3a")):
        return False
    if not re.search(r"\u628a[A-Za-z0-9\u4e00-\u9fa5]{1,12}\u6539(?:\u6210|\u4e3a)[A-Za-z0-9\u4e00-\u9fa5]{1,12}", compact):
        return False
    return _contains_any(compact, ("\u5176\u5b9e", "\u6ca1\u505a", "\u4e0d\u662f", "\u7b49\u4e00\u4e0b", "\u7b49\u4e0b"))


def _looks_like_process_or_policy_question(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if compact in {"\u62a5\u9500\u6d41\u7a0b", "\u7528\u5370\u6d41\u7a0b", "\u76d6\u7ae0\u6d41\u7a0b", "\u5dee\u65c5\u62a5\u9500\u6d41\u7a0b"}:
        return True
    has_question = _looks_like_question(text) or _contains_any(
        compact,
        ("\u627e\u8c01", "\u95ee\u8c01", "\u95ee\u4e0b", "\u95ee\u4e00\u4e0b", "\u54a8\u8be2\u8c01", "\u548b\u6574", "\u548b\u8d70", "\u600e\u4e48\u8d70", "\u600e\u4e48\u529e", "\u600e\u4e48\u5904\u7406", "\u591a\u4e45", "\u8981\u591a\u4e45", "\u95ee\u95ee"),
    )
    if not has_question:
        return False
    return _contains_any(
        compact,
        (
            "\u7528\u5370",
            "\u76d6\u7ae0",
            "\u5408\u540c\u76d6\u7ae0",
            "\u7535\u5b50\u7ae0",
            "\u5dee\u65c5\u8d39",
            "\u5dee\u65c5",
            "\u62a5\u9500",
            "\u8d85\u6807",
            "\u6d41\u7a0b",
        ),
    )


def _looks_like_tentative_case_resolution_chatter(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u53ef\u80fd\u548c\u89e3", "\u6709\u53ef\u80fd\u548c\u89e3", "\u5e0c\u671b\u987a\u5229")) and _contains_any(
        compact,
        ("\u6848", "\u6848\u4ef6", "\u8fd9\u4e2a\u6848\u4ef6", "\u8fd9\u4e2a\u6848"),
    )


def _looks_like_monthly_meta_request(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact or "\u6708\u62a5" not in compact:
        return False
    return _contains_any(compact, ("\u8d76\u7d27\u5199", "\u8c01\u8fd8\u6ca1\u4ea4", "\u8fd8\u6ca1\u4ea4", "\u50ac", "\u63d0\u9192", "\u63d0\u4ea4", "\u5df2\u63d0\u4ea4", "\u672c\u6708", "\u6c47\u603b", "\u63d0\u53d6", "\u4e0d\u5199", "\u4e0b\u5468\u8865", "\u884c\u4e0d\u884c", "\u5ba1\u6279\u8c01", "\u8c01\u5728\u7ba1", "\u6ca1\u53cd\u5e94"))


def _looks_like_monthly_metric_fragment(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if not _contains_any(compact, ("\u672c\u6708", "\u6708\u5ea6", "\u8fd9\u4e2a\u6708")):
        return False
    return _contains_any(compact, ("\u5904\u7406\u6848\u4ef6", "\u5b8c\u6210", "\u6570\u91cf", "\u4ef6", "\u6761", "\u9879"))


def _looks_like_daily_bot_feedback(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u65e5\u62a5\u7cfb\u7edf", "\u65e5\u62a5agent", "\u65e5\u62a5\u673a\u5668\u4eba")) and _contains_any(
        compact,
        ("\u96be\u7528", "\u4e0d\u597d\u7528", "\u7528\u4e0d\u4e86", "\u4e00\u5768", "\u592a\u8822", "\u771f\u96be\u7528"),
    )


def _looks_like_contextual_case_strategy_plan(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, _TOMORROW_MARKERS) and _contains_any(compact, ("\u90a3\u4e2a\u6848\u5b50", "\u90a3\u4e2a\u6848", "\u6848\u5b50", "\u6848\u4ef6")) and _contains_any(
        compact,
        ("\u7b56\u7565", "\u8001\u677f", "\u9886\u5bfc", "\u786e\u8ba4", "\u518d\u786e\u8ba4", "\u6c9f\u901a"),
    )


def _looks_like_completed_previous_plan_without_new_content(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _looks_like_completed_previous_plan(raw_text) and _contains_any(
        compact,
        ("\u4eca\u5929\u6ca1\u4ec0\u4e48\u65b0\u8ba1\u5212", "\u4eca\u65e5\u6ca1\u4ec0\u4e48\u65b0\u8ba1\u5212", "\u4eca\u5929\u6ca1\u65b0\u8ba1\u5212", "\u4eca\u65e5\u6ca1\u65b0\u8ba1\u5212", "\u4eca\u5929\u65e0\u65b0\u8ba1\u5212"),
    )


def _looks_like_historical_previous_plan_completion(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(
        compact,
        (
            "\u6628\u5929\u5199\u7684\u660e\u65e5\u8ba1\u5212\u662f",
            "\u6628\u5929\u5199\u7684\u660e\u5929\u8ba1\u5212\u662f",
            "\u6628\u65e5\u5199\u7684\u660e\u65e5\u8ba1\u5212\u662f",
            "\u6628\u65e5\u5199\u7684\u660e\u5929\u8ba1\u5212\u662f",
        ),
    ) and _contains_any(compact, ("\u4eca\u5929", "\u4eca\u65e5", "\u786e\u5b9e", "\u5df2\u7ecf", "\u5b8c\u6210", "\u53bb"))


def _looks_like_ambiguous_that_day_daily_reference(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if not _contains_any(compact, ("\u90a3\u5929", "\u5f53\u5929", "\u90a3\u65e5")):
        return False
    return _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7", "\u5de5\u4f5c\u65e5\u62a5", "\u5de5\u4f5c\u65e5\u5fd7"))


def _looks_like_contextual_retract_or_revoke(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u90a3\u4e2a\u4e0d\u7b97", "\u8fd9\u4e2a\u4e0d\u7b97", "\u90a3\u4e0d\u7b97", "\u8fd9\u4e0d\u7b97")) and _contains_any(
        compact,
        ("\u4e0d\u5bf9", "\u64a4\u56de", "\u5220\u6389", "\u5220\u4e86", "\u522b\u5199"),
    )


def _looks_like_named_meeting_delete(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u98ce\u63a7\u4f1a", "\u8bc4\u5ba1\u4f1a", "\u8fc7\u5802\u4f1a", "\u4f1a\u8bae")) and _contains_any(
        compact,
        ("\u5220\u4e86", "\u5220\u6389", "\u5220\u9664", "\u4e0d\u7b97", "\u522b\u5199"),
    )


def _looks_like_emotional_customer_phone_only(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u5ba2\u6237\u7535\u8bdd", "\u63a5\u4e86\u4e2a\u5ba2\u6237\u7535\u8bdd", "\u63a5\u5ba2\u6237\u7535\u8bdd")) and _contains_any(
        compact,
        ("\u6c14\u6b7b", "\u70e6\u6b7b", "\u592a\u70e6", "\u5410\u69fd"),
    ) and not _contains_any(compact, ("\u6c9f\u901a", "\u5904\u7406", "\u89e3\u51b3", "\u8bb0\u5230\u65e5\u62a5", "\u5199\u5230\u65e5\u62a5"))


def _looks_like_subjective_business_chatter(text: str) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    subjective_markers = ("\u8fd8\u884c", "\u633a\u597d", "\u4e00\u822c", "\u62a0\u95e8", "\u592a\u62a0", "\u58a8\u8ff9", "\u78e8\u53fd", "\u70e6")
    if not _contains_any(compact, subjective_markers):
        return False
    substantive_markers = (
        "\u5408\u540c",
        "\u534f\u8bae",
        "\u6848",
        "\u6cd5\u9662",
        "\u6750\u6599",
        "\u8d44\u6599",
        "\u62a5\u544a",
        "\u65b9\u6848",
        "\u4f1a\u8bae",
        "\u9879\u76ee",
        "\u6761\u6b3e",
        "\u9700\u6c42",
    )
    return not _contains_any(compact, substantive_markers)


def _looks_like_case_commentary_without_action(text: str) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    if not _contains_any(compact, ("\u5f8b\u5e08", "\u5bf9\u65b9", "\u8bc1\u636e", "\u6cd5\u5b98")):
        return False
    if _contains_any(compact, ("\u4e0d\u8db3", "\u7f3a\u5931", "\u7f3a\u5c11", "\u4e0d\u5145\u5206", "\u9700\u8981\u8865", "\u5f97\u8865")):
        return False
    action_compact = compact.replace("\u5bf9\u65b9", "")
    meaningful_actions = tuple(marker for marker in _BUSINESS_ACTION_MARKERS if marker not in {"\u5bf9", "\u5bf9\u4e86"})
    if _contains_any(action_compact, meaningful_actions):
        return False
    return _contains_any(compact, ("\u6709\u70b9\u96be\u7f20", "\u6bd4\u8f83\u96be\u7f20", "\u5f88\u96be\u7f20", "\u8bc1\u636e\u5145\u5206"))


def _looks_like_completed_previous_plan(raw_text: str) -> bool:
    compact = _compact(raw_text)
    explicit_plan_done = (
        _contains_any(compact, _YESTERDAY_MARKERS)
        and _contains_any(
            compact,
            (
                "\u660e\u65e5\u8ba1\u5212",
                "\u660e\u5929\u8ba1\u5212",
                "\u8ba1\u5212",
                "\u5f85\u529e",
                "\u4efb\u52a1",
                "\u5b89\u6392",
                "\u4e8b\u9879",
            ),
        )
        and _contains_any(
            compact,
            (
                "\u5df2\u5b8c\u6210",
                "\u5b8c\u6210\u4e86",
                "\u5b8c\u6210",
                "\u505a\u5b8c",
                "\u505a\u5b8c\u4e86",
                "\u641e\u5b9a",
                "\u641e\u5b9a\u4e86",
                "\u641e\u5b8c",
                "\u641e\u5b8c\u4e86",
                "\u5ba1\u5b8c",
                "\u5904\u7406\u5b8c",
                "\u6c47\u62a5\u5b8c",
                "\u6c47\u62a5\u5b8c\u4e86",
            ),
        )
    )
    if explicit_plan_done:
        return True
    return (
        _contains_any(compact, ("\u6628\u5929\u6211\u8bf4\u4eca\u5929\u8981", "\u6628\u5929\u8bf4\u4eca\u5929\u8981", "\u6628\u5929\u63d0\u5230\u4eca\u5929\u8981"))
        and _contains_any(
            compact,
            ("\u5df2\u7ecf", "\u5df2", "\u5b8c\u4e86", "\u5b8c", "\u5b8c\u6210", "\u641e\u5b9a", "\u505a\u5b8c", "\u89c1\u5b8c", "\u5ba1\u5b8c"),
        )
        and (_contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS))
    )


def _looks_like_completed_yesterday_referenced_task_today(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u6628\u5929\u8bf4\u7684", "\u6628\u65e5\u8bf4\u7684", "\u6628\u5929\u63d0\u7684", "\u6628\u5929\u90a3\u4e2a\u4efb\u52a1")) and _contains_any(
        compact,
        (
            "\u4eca\u5929\u5b8c\u6210",
            "\u4eca\u5929\u641e\u5b9a",
            "\u4eca\u5929\u505a\u5b8c",
            "\u5df2\u5b8c\u6210",
            "\u5b8c\u6210\u4e86",
            "\u6539\u5b8c",
            "\u6539\u5b8c\u4e86",
            "\u641e\u5b9a",
            "\u641e\u5b9a\u4e86",
        ),
    ) and (_contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS))


def _completed_previous_plan_has_explicit_today_work(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact or not _looks_like_completed_previous_plan(raw_text):
        return False
    if not _contains_any(compact, _TODAY_MARKERS):
        return False
    if _contains_any(
        compact,
        (
            "\u660e\u65e5\u8ba1\u5212\u5df2\u5b8c\u6210",
            "\u660e\u5929\u8ba1\u5212\u5df2\u5b8c\u6210",
            "\u8ba1\u5212\u5df2\u5b8c\u6210",
            "\u5f85\u529e\u5df2\u5b8c\u6210",
        ),
    ) and not _contains_any(
        compact,
        (
            "\u90a3\u4e2a",
            "\u8fd9\u4e2a",
            "\u5408\u540c",
            "\u6848",
            "\u5ba2\u6237",
            "\u6750\u6599",
            "\u62dc\u8bbf",
            "\u76d6\u7ae0",
            "\u9057\u7559\u95ee\u9898",
        ),
    ):
        return False
    return _contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)


def _looks_like_quoted_previous_item_copy(raw_text: str) -> bool:
    return bool(re.search(r"\u6628\u5929\u7684?\u65e5\u62a5\u91cc[“\"].+?[”\"].{0,12}?\u590d\u5236\u8fc7\u6765", str(raw_text or "")))


def _looks_like_previous_plan_content_reference(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if not _contains_any(compact, _YESTERDAY_MARKERS):
        return False
    if not _contains_any(compact, ("\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u8ba1\u5212", "\u8ba1\u5212", "\u5f85\u529e", "\u5b89\u6392")):
        return False
    return _contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)


def _looks_like_replacement_followup(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    for marker in ("\u6539\u6210", "\u6539\u4e3a", "\u66ff\u6362\u6210", "\u66ff\u6362\u4e3a", "\u6362\u6210", "\u6362\u4e3a"):
        compact_marker = _compact(marker)
        if compact.startswith(compact_marker) and len(compact) > len(compact_marker) + 1:
            return True
    return False


def _looks_like_negative_replacement(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    if not text:
        return False
    return bool(
        re.search(r"不是\s*.+?[，,。；;]?\s*(?:而是|是)\s*.+", text)
        or re.search(r"(?:说错了|错了)[，,。；;]?\s*是\s*.+?[，,。；;]?\s*不是\s*.+", text)
    )


def _looks_like_repeat_previous_work(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact or _looks_like_history_query(compact):
        return False
    if has_previous_to_current_repeat_reference(raw_text):
        return True
    if _contains_any(compact, ("\u4eca\u5929", "\u4eca\u65e5")) and _contains_any(compact, ("\u6628\u5929\u90a3\u4e9b", "\u6628\u5929\u90a3\u4e9b\u4e8b")) and _contains_any(
        compact,
        ("\u8fd8\u662f", "\u505a\u4e86", "\u4e00\u6837", "\u6ca1\u53d8\u5316"),
    ):
        return True
    if not _contains_any(compact, _PREVIOUS_WORK_REFERENCE_MARKERS):
        return False
    has_repeat = _contains_any(
        compact,
        (
            "\u8fd8\u662f",
            "\u4e5f\u662f",
            "\u7167\u7740",
            "\u7167\u65e7",
            "\u4e00\u6837",
            "\u540c\u6837",
            "\u540c\u524d\u5929",
            "\u540c\u524d\u65e5",
            "\u540c\u6628\u5929",
            "\u540c\u6628\u65e5",
            "\u90a3\u4e9b",
            "\u90a3\u51e0\u4e2a",
            "\u8001\u6837\u5b50",
            "\u6ca1\u5565\u53d8\u5316",
            "\u6ca1\u4ec0\u4e48\u53d8\u5316",
            "\u63a5\u7740\u5e72",
        ),
    )
    has_work_hint = _contains_any(
        compact,
        (
            "\u4eca\u5929",
            "\u4eca\u65e5",
            "\u505a",
            "\u5de5\u4f5c",
            "\u4e8b",
            "\u5185\u5bb9",
        ),
    )
    return has_repeat and has_work_hint


def _looks_like_daily_confirmation_text(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if compact in {"\u597d\u4e86\u63d0\u4ea4", "\u597d\u63d0\u4ea4", "\u63d0\u4ea4\u5427"}:
        return True
    if not compact or not _contains_any(compact, _DAILY_FIELD_MARKERS):
        return False
    return _contains_any(compact, ("\u5c31\u8fd9\u6837", "\u5c31\u8fd9\u6837\u5427", "\u8fd9\u6837\u5427", "\u53ef\u4ee5\u63d0\u4ea4", "\u786e\u8ba4\u63d0\u4ea4"))


def _looks_like_field_level_delete(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, _CLEAR_MARKERS) and _contains_any(compact, _DAILY_FIELD_MARKERS)


def _looks_like_risk_field_delete_request(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u5220\u98ce\u9669", "\u5220\u6389\u98ce\u9669", "\u5220\u4e86\u98ce\u9669", "\u98ce\u9669\u5220\u4e86", "\u628a\u98ce\u9669\u5220", "\u628a\u98ce\u9669\u5220\u4e86", "\u98ce\u9669\u5220\u6389", "\u5220\u95ee\u9898", "\u5220\u6389\u95ee\u9898", "\u628a\u95ee\u9898\u5220", "\u628a\u95ee\u9898\u5220\u4e86")) and not _contains_any(
        compact,
        ("\u5386\u53f2", "\u67e5", "\u770b"),
    )


def _copy_previous_targets_today_work(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if _looks_like_repeat_previous_work(raw_text):
        return True
    if _contains_any(compact, ("\u62ff\u6765\u4eca\u5929\u7528", "\u62ff\u5230\u4eca\u5929\u7528")):
        return True
    return _contains_any(
        compact,
        (
            "\u4eca\u5929\u63a5\u7740\u5e72",
            "\u63a5\u7740\u5e72",
            "\u8fd8\u662f\u6628\u5929\u90a3\u4e9b\u4e8b",
            "\u8fd8\u662f\u6628\u5929\u90a3\u4e9b",
            "\u548c\u6628\u5929\u4e00\u6837\u7684\u5de5\u4f5c",
            "\u8ddf\u6628\u5929\u4e00\u6837\u7684\u5de5\u4f5c",
        ),
    )


def _looks_like_affirm_same_as_previous_daily(raw_text: str) -> bool:
    compact = re.sub(r"[\s\u3000:：,，.。;；!！?？()（）\[\]【】\"'“”‘’]+", "", str(raw_text or "")).lower()
    return compact in {"\u5bf9\u4e00\u6837", "\u5bf9\u7684\u4e00\u6837", "\u55ef\u4e00\u6837", "\u662f\u4e00\u6837", "\u4e00\u6837", "\u5c31\u4e00\u6837"}


def _looks_like_copy_current_work_to_tomorrow(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    has_current_work = _contains_any(compact, ("\u4eca\u5929\u7684\u5de5\u4f5c", "\u4eca\u65e5\u7684\u5de5\u4f5c", "\u4eca\u5929\u5de5\u4f5c", "\u4eca\u65e5\u5de5\u4f5c", "\u4eca\u65e5\u4e8b\u9879", "\u4eca\u5929\u4e8b\u9879"))
    has_tomorrow_target = _contains_any(compact, ("\u5230\u660e\u5929", "\u5230\u660e\u65e5", "\u5230\u660e\u65e5\u8ba1\u5212", "\u5230\u660e\u5929\u8ba1\u5212", "\u4f5c\u4e3a\u660e\u5929\u8ba1\u5212", "\u4f5c\u4e3a\u660e\u65e5\u8ba1\u5212"))
    has_copy = _contains_any(compact, ("\u590d\u5236", "\u62f7\u8d1d", "\u518d\u590d\u5236\u4e00\u4efd", "\u5e26\u5230", "\u5e26\u8fc7\u53bb"))
    return has_current_work and has_tomorrow_target and has_copy


def _field_from_text(raw_text: str) -> DailyCommandTargetField | None:
    compact = _compact(raw_text)
    if any(marker in compact for marker in ("今日工作", "今天工作", "今日事项", "工作内容")):
        return "today_work"
    if any(marker in compact for marker in ("问题风险", "问题/风险", "风险问题", "问题", "风险")):
        return "problems"
    if any(marker in compact for marker in ("明日计划", "明天计划", "明天的计划", "明日工作", "明天工作", "计划")):
        return "tomorrow_plan"
    return None


def _looks_like_item_edit(raw_text: str) -> bool:
    compact = _compact(raw_text)
    has_item_ref = bool(
        re.search(
            r"(第?\s*[0-9一二三四五六七八九十两]+\s*[条项])|([0-9一二三四五六七八九十两]+\s*(?:到|至|-|—|~)\s*[0-9一二三四五六七八九十两]+\s*[条项]?)",
            raw_text,
        )
        or re.search(r"(?:前[一二两三四五六七八九十]+条|这条|那条|上一条|上条|最后一条)", raw_text)
    )
    if not has_item_ref:
        return False
    edit_tokens = (
        "改成",
        "改为",
        "修改",
        "替换",
        "删掉",
        "删除",
        "去掉",
        "合并",
        "合成",
        "并成",
        "移到",
        "移入",
        "放到",
        "放进",
        "挪到",
        "复制",
        "加到",
        "加进",
        "加入",
    )
    return any(token in compact for token in edit_tokens)


def _looks_like_short_delete_reference(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not _contains_any(compact, ("\u5220\u6389", "\u5220\u9664", "\u5220\u4e86", "\u5220\u4e00\u4e0b", "\u5220\u4e0b", "\u53bb\u6389")):
        return False
    if _contains_any(
        compact,
        (
            "\u5168\u90e8",
            "\u6240\u6709",
            "\u6574\u4efd",
            "\u4e09\u680f",
            "\u65e5\u62a5",
            "\u4eca\u65e5\u5de5\u4f5c",
            "\u4eca\u5929\u5de5\u4f5c",
            "\u95ee\u9898",
            "\u98ce\u9669",
            "\u660e\u65e5\u8ba1\u5212",
            "\u660e\u5929\u8ba1\u5212",
            "\u8ba1\u5212",
        ),
    ):
        return False
    return len(compact) <= 5 or _contains_any(
        compact,
        (
            "\u521a\u624d",
            "\u521a\u521a",
            "\u8fd9\u6761",
            "\u90a3\u6761",
            "\u8fd9\u4e2a",
            "\u90a3\u4e2a",
            "\u4e0a\u4e00\u6761",
            "\u4e0a\u6761",
            "\u6700\u540e\u4e00\u6761",
            "\u5b83",
        ),
    )


def _looks_like_context_edit(raw_text: str) -> bool:
    if _looks_like_item_edit(raw_text):
        return True
    compact = _compact(raw_text)
    edit_tokens = (
        "\u6539\u6210",
        "\u6539\u4e3a",
        "\u4fee\u6539",
        "\u66ff\u6362",
        "\u5220\u6389",
        "\u5220\u9664",
        "\u53bb\u6389",
        "\u5408\u5e76",
        "\u5408\u6210",
        "\u5e76\u6210",
        "\u79fb\u5230",
        "\u79fb\u5165",
        "\u653e\u5230",
        "\u653e\u8fdb",
        "\u632a\u5230",
        "\u8f6c\u5230",
        "\u5f52\u5230",
        "\u590d\u5236",
        "\u52a0\u5230",
        "\u52a0\u8fdb",
        "\u52a0\u5165",
    )
    return any(_compact(token) in compact for token in edit_tokens)


def _looks_like_report_revoke(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not _contains_any(compact, _REVOKE_MARKERS):
        return False
    return _contains_any(
        compact,
        (
            "\u65e5\u62a5",
            "\u590d\u76d8",
            "\u65e5\u5fd7",
            "\u63d0\u4ea4",
            "\u5df2\u63d0\u4ea4",
            "\u521a\u63d0\u4ea4",
        ),
    )


def _looks_like_bare_completed_report_revoke(raw_text: str, effect: WorkflowEffect) -> bool:
    return _looks_like_bare_revoke(raw_text) and str(effect.target.get("status") or "") == "completed"


def _looks_like_completed_report_revoke_edit(raw_text: str, effect: WorkflowEffect) -> bool:
    return _looks_like_report_revoke_edit(raw_text) and str(effect.target.get("status") or "") == "completed"


def _looks_like_bare_revoke(raw_text: str) -> bool:
    compact = _compact(raw_text)
    return compact in {"\u64a4\u56de", "\u64a4\u56de\u4e00\u4e0b", "\u5148\u64a4\u56de", "\u64a4\u56de\u5427"}


def _looks_like_report_revoke_edit(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not _contains_any(compact, _REVOKE_MARKERS):
        return False
    if not _contains_any(compact, ("\u4fee\u6539", "\u6539\u4e00\u4e0b", "\u6539\u4e0b", "\u7f16\u8f91", "\u91cd\u5199")):
        return False
    return not _contains_any(
        compact,
        (
            "\u8d77\u8bc9\u72b6",
            "\u4e0a\u8bc9\u72b6",
            "\u7b54\u8fa9\u72b6",
            "\u7533\u8bf7\u4e66",
            "\u5f8b\u5e08\u51fd",
            "\u51fd\u4ef6",
            "\u8bc9\u72b6",
        ),
    )


def _looks_like_legal_document_work(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if _contains_any(compact, ("\u6539\u6210", "\u6539\u4e3a", "\u66ff\u6362\u6210", "\u66ff\u6362\u4e3a", "\u6362\u6210", "\u6362\u4e3a", "\u66f4\u65b0\u4e3a")):
        return False
    if not _contains_any(
        compact,
        (
            "\u8d77\u8bc9\u72b6",
            "\u4e0a\u8bc9\u72b6",
            "\u7b54\u8fa9\u72b6",
            "\u7533\u8bf7\u4e66",
            "\u5f8b\u5e08\u51fd",
            "\u51fd\u4ef6",
            "\u8bc9\u72b6",
        ),
    ):
        return False
    return _contains_any(
        compact,
        (
            "\u64a4\u56de",
            "\u64a4\u8bc9",
            "\u4fee\u6539",
            "\u4fee\u8ba2",
            "\u8d77\u8349",
            "\u5ba1\u6838",
            "\u5b8c\u6210",
            "\u5904\u7406",
        ),
    )


def _looks_like_question(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u8bf4\u5565\u4e5f\u5f97", "\u8bf4\u4ec0\u4e48\u4e5f\u5f97", "\u65e0\u8bba\u5982\u4f55\u5f97", "\u600e\u4e48\u4e5f\u5f97")) and _contains_any(
        compact,
        _TOMORROW_MARKERS + _TODAY_MARKERS,
    ):
        return False
    if _contains_any(compact, ("\u98ce\u9669\uff1f", "\u95ee\u9898\uff1f", "\u98ce\u9669?", "\u95ee\u9898?")) and extract_problem_evidence(raw_text).is_business_problem:
        return False
    if _contains_any(compact, ("\u8fd8\u80fd\u6709\u5565", "\u8fd8\u80fd\u6709\u4ec0\u4e48")) and (
        _contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)
    ):
        return False
    if re.search(r"\u80fd.{0,18}\u4e0d$", compact):
        return True
    if _looks_like_business_plan_statement(raw_text):
        return False
    return (
        "?" in str(raw_text or "")
        or "\uff1f" in str(raw_text or "")
        or _looks_like_option_question(raw_text)
    ) or _contains_any(
        compact,
        (
            "\u4ec0\u4e48",
            "\u5565",
            "\u600e\u4e48",
            "\u4e3a\u4ec0\u4e48",
            "\u4e3a\u5565",
            "\u5565\u5b89\u6392",
            "\u4ec0\u4e48\u5b89\u6392",
            "\u662f\u4e0d\u662f",
            "\u662f\u5426",
            "\u5417",
            "\u5462",
            "\u6709\u591a\u5c11",
            "\u591a\u4e45",
            "\u8981\u591a\u4e45",
            "\u4ec0\u4e48\u65f6\u5019",
            "\u51e0\u70b9",
            "\u51e0\u4ef6",
            "\u7a7f\u5565",
            "\u7a7f\u4ec0\u4e48",
            "\u7a7f\u54ea",
            "\u5728\u54ea",
            "\u5728\u54ea\u91cc",
            "\u54ea\u91cc",
            "\u54ea\u513f",
            "\u4e0b\u8f7d",
        ),
    )


def _looks_like_non_daily_feedback(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u4e0d\u7528\u5199\u65e5\u62a5", "\u4e0d\u8981\u5199\u65e5\u62a5", "\u522b\u5199\u65e5\u62a5", "\u53ea\u662f\u540c\u6b65\u4e00\u4e0b", "\u53ea\u662f\u8ddf\u4f60\u540c\u6b65")):
        return True
    if _contains_any(compact, ("\u4f60", "\u673a\u5668\u4eba", "\u7cfb\u7edf")) and _contains_any(
        compact,
        (
            "\u4f60\u4f1a",
            "\u4f60\u80fd",
            "\u80fd\u4e0d\u80fd",
            "\u4f1a\u4e0d\u4f1a",
            "\u5199\u8bd7",
            "\u753b\u753b",
            "\u5531\u6b4c",
            "\u8bb2\u7b11\u8bdd",
        ),
    ):
        return True
    if _contains_any(compact, _DAILY_FIELD_MARKERS) or _contains_any(compact, _BUSINESS_ACTION_MARKERS):
        return False
    return _contains_any(compact, _ASSISTANT_FEEDBACK_MARKERS)


def _looks_like_non_substantive_daily_request(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if (
        _contains_any(compact, ("\u7b97\u4e86\u4e0d\u5199", "\u4e0d\u5199\u4e86", "\u4e0d\u586b\u4e86"))
        and _contains_any(compact, ("\u660e\u5929\u5199", "\u660e\u65e5\u5199", "\u56de\u5934\u5199", "\u4e0b\u6b21\u5199"))
        and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS)
    ):
        return True
    if _contains_any(compact, ("\u522b\u7ed9\u6211\u5199\u65e5\u62a5", "\u4e0d\u8981\u5199\u65e5\u62a5", "\u522b\u5199\u65e5\u62a5")) and _contains_any(
        compact,
        ("\u5410\u69fd", "\u5f00\u73a9\u7b11", "\u522b\u5f53\u771f", "\u968f\u53e3"),
    ):
        return True
    if _contains_any(compact, ("\u518d\u5e2e\u6211\u5199\u65e5\u62a5", "\u5e2e\u6211\u5199\u65e5\u62a5", "\u5e2e\u6211\u586b\u65e5\u62a5")):
        return True
    if "\u65e5\u62a5" not in compact:
        return False
    if _looks_like_daily_start_without_payload(text):
        return True
    if _contains_any(compact, ("\u65e5\u62a5\u8981\u5199", "\u65e5\u62a5\u5df2\u7ecf\u5199\u597d", "\u65e5\u62a5\u5df2\u7ecf\u5199\u597d\u4e86", "\u65e5\u62a5\u5199\u597d\u4e86", "\u4eca\u5929\u65e5\u62a5\u5df2\u7ecf\u5199\u597d")):
        return True
    if _contains_any(compact, ("\u6ca1\u5565\u53ef\u5199", "\u6ca1\u4ec0\u4e48\u53ef\u5199", "\u6ca1\u5565\u597d\u5199", "\u6ca1\u4ec0\u4e48\u597d\u5199")):
        return True
    if compact in {"\u65e5\u62a5", "\u5199\u65e5\u62a5", "\u5199\u4e2a\u65e5\u62a5", "\u5199\u4e2a\u65e5\u62a5\u5427", "\u5199\u65e5\u62a5\u5427", "\u586b\u65e5\u62a5", "\u586b\u4e2a\u65e5\u62a5"}:
        return True
    if compact in {"\u987a\u4fbf\u5199\u4e2a\u65e5\u62a5", "\u5bf9\u4e86\u987a\u4fbf\u5199\u4e2a\u65e5\u62a5"}:
        return True
    if _contains_any(compact, ("\u5c31\u8fd9\u4e9b\u4e86", "\u5e94\u8be5\u5c31\u8fd9\u4e9b", "\u597d\u4e86\u5c31\u8fd9\u4e9b")) and _contains_any(compact, ("\u5199\u65e5\u62a5", "\u53d1\u65e5\u62a5")):
        return True
    if _contains_any(compact, ("\u968f\u4fbf\u5199", "\u968f\u4fbf\u586b", "\u4ea4\u5dee", "\u7cca\u5f04", "\u6577\u884d")):
        return True
    if _contains_any(
        compact,
        (
            "\u65e5\u62a5\u660e\u5929\u518d\u8bf4",
            "\u65e5\u62a5\u660e\u65e5\u518d\u8bf4",
            "\u660e\u5929\u518d\u8bf4\u65e5\u62a5",
            "\u660e\u65e5\u518d\u8bf4\u65e5\u62a5",
            "\u65e5\u62a5\u56de\u5934\u518d\u8bf4",
            "\u4eca\u5929\u65e5\u62a5\u5148\u4e0d\u5199",
            "\u65e5\u62a5\u5148\u4e0d\u5199",
            "\u4eca\u5929\u65e5\u62a5\u4e0d\u5199",
        ),
    ):
        return True
    if _contains_any(compact, ("\u65e5\u62a5\u90fd\u4e0d\u60f3\u5199", "\u65e5\u62a5\u4e0d\u60f3\u5199", "\u4e0d\u60f3\u5199\u65e5\u62a5", "\u4e0d\u5199\u65e5\u62a5")):
        return True
    if _contains_any(compact, ("\u4eca\u5929\u5199\u65e5\u62a5\u4e86\u6ca1", "\u65e5\u62a5\u5199\u4e86\u6ca1")) and _contains_any(
        compact,
        ("\u5e2e\u6211\u628a\u4eca\u5929\u7684\u6d3b\u513f\u8bb0\u4e00\u4e0b", "\u5e2e\u6211\u628a\u4eca\u5929\u7684\u6d3b\u8bb0\u4e00\u4e0b", "\u628a\u4eca\u5929\u7684\u6d3b\u513f\u8bb0\u4e00\u4e0b"),
    ):
        return True
    if _contains_any(compact, ("\u65e5\u62a5\u4e0d\u77e5\u9053\u5199\u5565", "\u65e5\u62a5\u4e0d\u77e5\u9053\u5199\u4ec0\u4e48", "\u4e0d\u77e5\u9053\u5199\u5565", "\u4e0d\u77e5\u9053\u5199\u4ec0\u4e48")) and _contains_any(compact, ("\u91cd\u590d", "\u6ca1\u5565", "\u6ca1\u4ec0\u4e48", "\u65e5\u62a5")):
        return True
    return _contains_any(compact, ("\u4f60\u5565\u4e5f\u4e0d\u61c2", "\u4f60\u4ec0\u4e48\u4e5f\u4e0d\u61c2", "\u4e0d\u8ddf\u4f60\u804a"))


def _looks_like_daily_start_without_payload(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if "\u65e5\u62a5" not in compact:
        return False
    if compact in {"\u5199\u65e5\u62a5\u4e86", "\u586b\u65e5\u62a5\u4e86", "\u65e5\u62a5\u5199\u4e86", "\u65e5\u62a5\u586b\u4e86"}:
        return False
    if any(marker in text for marker in (":", "\uff1a", "\n", "\r")):
        return False
    if _contains_any(compact, ("\u4eca\u65e5\u5de5\u4f5c", "\u4eca\u5929\u5de5\u4f5c", "\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u8ba1\u5212", "\u95ee\u9898\u98ce\u9669", "\u95ee\u9898/\u98ce\u9669")):
        return False
    if not _contains_any(compact, ("\u5199\u65e5\u62a5", "\u586b\u65e5\u62a5", "\u62a5\u65e5\u62a5", "\u5f00\u59cb\u65e5\u62a5")):
        return False
    remainder = compact
    for marker in (
        "\u6211\u8981",
        "\u6211\u6765",
        "\u6211\u60f3",
        "\u6211\u51c6\u5907",
        "\u51c6\u5907",
        "\u5f00\u59cb",
        "\u73b0\u5728",
        "\u5f00\u59cb\u65e5\u62a5",
        "\u5199\u4e2a\u65e5\u62a5",
        "\u5199\u65e5\u62a5",
        "\u586b\u4e2a\u65e5\u62a5",
        "\u586b\u65e5\u62a5",
        "\u62a5\u65e5\u62a5",
        "\u4e86",
        "\u5427",
        "\u4e0b",
        "\u4e00\u4e0b",
        "\u4e00\u4e2a",
    ):
        remainder = remainder.replace(marker, "")
    return not remainder


def _looks_like_personal_state_only(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u6253\u67b6", "\u88ab\u9886\u5bfc\u6279\u8bc4", "\u8ddf\u5c0f\u738b\u6253\u67b6", "\u51bb\u6b7b", "\u51bb\u6b7b\u4e86", "\u5012\u9709", "\u88ab\u5ba2\u6237\u9a82", "\u5ba2\u6237\u9a82\u4e86")):
        return True
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS) or _contains_any(compact, _BUSINESS_ACTION_MARKERS):
        return False
    return _contains_any(compact, _PERSONAL_STATE_MARKERS)


def _looks_like_absurd_content(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if _contains_any(compact, ("\u6211\u662f\u79e6\u59cb\u7687", "\u6211\u662f\u7389\u7687\u5927\u5e1d", "\u6211\u662f\u5965\u7279\u66fc")):
        return True
    if _contains_any(compact, ("\u516c\u53f8\u70b8\u4e86", "\u628a\u516c\u53f8\u70b8\u4e86", "\u6708\u4eae\u662f\u84dd\u8272", "\u6708\u4eae\u84dd\u8272")):
        return True
    if _contains_any(compact, ("\u6d4b\u8bd5\u4e00\u4e0b", "\u6d4b\u8bd5\u4e0b", "\u8bd5\u4e00\u4e0b")) and _contains_any(compact, ("\u54c8\u54c8", "\u5475\u5475")):
        return True
    if _contains_any(compact, ("\u5403\u5c4e", "\u6ca1\u5403\u9971")):
        return True
    if _contains_any(compact, ("\u5f53\u795e\u4ed9", "\u4fee\u4ed9", "\u795e\u4ed9", "\u5965\u7279\u66fc", "\u602a\u517d", "\u6253\u602a\u517d")):
        return True
    return _contains_any(
        str(raw_text or ""),
        (
            "\u706b\u661f",
            "\u5916\u661f\u4eba",
            "\u98de\u8239",
            "\u6708\u7403",
            "\u706b\u7bad",
            "\u5b87\u5b99",
            "\u8fea\u62dc\u5854",
            "\u76f4\u5347\u673a",
            "\u592a\u7a7a",
            "\u62ef\u6551\u5730\u7403",
            "\u53d8\u6210\u4e86\u4e00\u53ea",
            "\u53d8\u6210\u4e00\u53ea",
        ),
    )


def _looks_like_empty_daily_content(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    problem_evidence = extract_problem_evidence(text)
    if _contains_any(compact, ("\u5565\u4e5f\u6ca1\u5e72", "\u4ec0\u4e48\u4e5f\u6ca1\u5e72", "\u597d\u50cf\u6ca1\u505a\u5565", "\u597d\u50cf\u6ca1\u505a\u4ec0\u4e48", "\u6478\u9c7c", "\u6ca1\u5e72\u6d3b", "\u6ca1\u5565\u53ef\u5199", "\u6ca1\u4ec0\u4e48\u53ef\u5199", "\u5c31\u90a3\u6837", "\u660e\u5929\u5468\u672b\u4e86", "\u660e\u5929\u662f\u5468\u672b")):
        return True
    if _contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS) or problem_evidence.is_problem or problem_evidence.is_no_problem:
        return False
    if not _contains_any(compact, ("\u4eca\u5929", "\u4eca\u65e5", "\u5de5\u4f5c", "\u65e5\u62a5", "\u95ee\u9898")):
        return False
    return _contains_any(
        compact,
        (
            "\u6ca1\u5565\u7279\u522b",
            "\u6ca1\u4ec0\u4e48\u7279\u522b",
            "\u6ca1\u5565\u4e8b",
            "\u6ca1\u4ec0\u4e48\u4e8b",
            "\u6ca1\u4ec0\u4e48\u65b0",
            "\u6ca1\u5565\u65b0",
            "\u6ca1\u4e8b",
            "\u65e0\u4e8b",
            "\u8fd8\u884c",
            "\u8fd8\u884c\u5427",
            "\u8fd8\u53ef\u4ee5\u5427",
            "\u4e00\u822c\u5427",
            "\u6ca1\u5565",
            "\u7b97\u4e86\u6ca1\u5565",
        ),
    )


def _looks_like_unfinished_daily_shell(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return False
    return _contains_any(
        compact,
        (
            "\u4eca\u5929\u7684\u4e8b\u8fd8\u6ca1\u5199\u5b8c",
            "\u4eca\u65e5\u7684\u4e8b\u8fd8\u6ca1\u5199\u5b8c",
            "\u4eca\u5929\u4e8b\u8fd8\u6ca1\u5199\u5b8c",
            "\u4eca\u5929\u7684\u65e5\u62a5\u8fd8\u6ca1\u5199\u5b8c",
            "\u8fd8\u6ca1\u5199\u5b8c",
        ),
    )


def _looks_like_summary_or_analysis_request(raw_text: str) -> bool:
    text = str(raw_text or "")
    return _contains_any(
        text,
        (
            "\u603b\u7ed3\u4e00\u4e0b",
            "\u603b\u7ed3\u4e0b",
            "\u5206\u6790\u4e00\u4e0b",
            "\u5206\u6790\u4e0b",
            "\u63d0\u70bc\u4e00\u4e0b",
            "\u63d0\u70bc\u4e0b",
        ),
    ) and _contains_any(text, ("\u521a\u624d", "\u90a3\u4e2a", "\u6848\u5b50", "\u6848", "\u98ce\u9669\u70b9", "\u98ce\u9669"))


def _looks_like_weak_travel_destination_fragment(raw_text: str) -> bool:
    text = str(raw_text or "").strip(" \t\r\n\u3000\uff0c,\u3002.")
    compact = _compact(text)
    if not compact:
        return False
    destination = _known_travel_destination(text) or _loose_travel_destination(text)
    if not destination:
        return False
    if _contains_any(
        compact,
        (
            "\u51fa\u5dee",
            "\u5f00\u5ead",
            "\u51fa\u5ead",
            "\u76d6\u7ae0",
            "\u7528\u5370",
            "\u529e\u7406",
            "\u5904\u7406",
            "\u6c9f\u901a",
            "\u8d70\u8bbf",
            "\u8ba8\u85aa",
            "\u5ba2\u6237",
            "\u6cd5\u9662",
            "\u516c\u8bc1\u5904",
            "\u8d22\u52a1",
            "\u4ea4\u8868",
        ),
    ):
        return False
    if not _contains_any(compact, _TODAY_MARKERS + _TOMORROW_MARKERS):
        return False
    return bool(
        re.search(
            rf"^(?:\u54e6?\u5bf9|(?:\u5bf9\u4e86)|\u5c31\u662f|\u5e94\u8be5\u662f|\u662f)?(?:\u4eca\u5929|\u4eca\u65e5|\u660e\u5929|\u660e\u65e5|\u660e\u513f)?(?:\u662f)?(?:\u53bb|\u5230|\u8d74|\u524d\u5f80){re.escape(destination)}$",
            compact,
        )
    )


def _known_travel_destination(raw_text: str) -> str:
    text = str(raw_text or "")
    for destination in (
        "\u5609\u5174\u5357\u6e56\u8857\u9053",
        "\u5357\u6e56\u8857\u9053",
        "\u5357\u901a",
        "\u5357\u4eac",
        "\u626c\u5dde",
        "\u5609\u5174",
        "\u5e38\u5dde",
        "\u82cf\u5dde",
        "\u4e0a\u6d77",
        "\u5317\u4eac",
        "\u676d\u5dde",
        "\u5e7f\u5dde",
        "\u6df1\u5733",
    ):
        if destination in text:
            return destination
    return ""


def _loose_travel_destination(raw_text: str) -> str:
    match = re.search(r"(?:\u53bb|\u8d74|\u5230|\u524d\u5f80)([\u4e00-\u9fa5]{2,8})$", str(raw_text or ""))
    if not match:
        return ""
    return match.group(1)


def _looks_like_business_plan_statement(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if not compact or text.endswith(("?", "\uff1f")):
        return False
    if not _contains_any(compact, _TODAY_MARKERS + _TOMORROW_MARKERS + ("\u8ba1\u5212", "\u6253\u7b97", "\u51c6\u5907")):
        return False
    if not _contains_any(compact, ("\u600e\u4e48", "\u60f3\u4e2a\u529e\u6cd5", "\u5f97\u60f3", "\u4e0d\u7136", "\u8981\u8fdd\u7ea6")):
        return False
    return _contains_any(compact, _BUSINESS_OBJECT_MARKERS)


def _looks_like_contextual_plan_reference(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if not _contains_any(compact, _TOMORROW_MARKERS):
        return False
    if _contains_any(compact, _PREVIOUS_WORK_REFERENCE_MARKERS) and _contains_any(compact, ("\u8ba1\u5212", "\u7ee7\u7eed", "\u63a5\u7740")):
        return _contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)
    if not _contains_any(compact, ("\u8fd9\u4e2a", "\u90a3\u4e2a", "\u8fd9\u4e8b", "\u90a3\u4e8b", "\u6b64\u4e8b", "\u8fd9\u9879", "\u90a3\u9879")):
        return False
    return _contains_any(
        compact,
        (
            "\u5904\u7406",
            "\u8ddf\u8fdb",
            "\u50ac",
            "\u89e3\u51b3",
            "\u63a8\u8fdb",
            "\u6c9f\u901a",
            "\u8865\u5145",
        ),
    )


def _looks_like_daily_meta_commit_request(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if _has_specific_business_content_for_meta_request(text):
        return False
    has_daily_context = _contains_any(compact, _DAILY_FIELD_MARKERS) or _contains_any(
        compact,
        (
            "\u5199\u65e5\u62a5",
            "\u586b\u65e5\u62a5",
            "\u8bb0\u5230\u65e5\u62a5",
            "\u8bb0\u8fdb\u65e5\u62a5",
        ),
    )
    has_meta_reference = _contains_any(
        compact,
        (
            "\u628a\u8fd9\u4e9b",
            "\u8fd9\u4e9b",
            "\u8fd9\u4e9b\u5185\u5bb9",
            "\u521a\u624d\u8fd9\u4e9b",
            "\u4eca\u5929\u5e72\u7684\u6d3b",
            "\u4eca\u5929\u5e72\u7684\u4e8b",
            "\u4eca\u5929\u505a\u7684\u4e8b",
        ),
    )
    has_commit_verb = _contains_any(
        compact,
        (
            "\u8bb0\u4e00\u4e0b",
            "\u8bb0\u4e0b",
            "\u8bb0\u8fdb\u53bb",
            "\u8bb0\u5230",
            "\u5199\u8fdb\u53bb",
            "\u5199\u5230",
            "\u8bb0\u8fdb\u65e5\u62a5",
            "\u5199\u8fdb\u65e5\u62a5",
        ),
    )
    return has_commit_verb and (has_daily_context or has_meta_reference)


def _looks_like_daily_submitted_status(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(
        compact,
        (
            "\u65e5\u62a5\u63d0\u4ea4\u4e86",
            "\u65e5\u62a5\u5df2\u63d0\u4ea4",
            "\u5df2\u63d0\u4ea4\u65e5\u62a5",
            "\u63d0\u4ea4\u65e5\u62a5\u4e86",
            "\u65e5\u62a5\u5df2\u7ecf\u5199\u597d",
            "\u65e5\u62a5\u5df2\u7ecf\u5199\u597d\u4e86",
            "\u65e5\u62a5\u5199\u597d\u4e86",
            "\u4eca\u5929\u65e5\u62a5\u5df2\u7ecf\u5199\u597d",
        ),
    )


def _looks_like_no_significant_problem_phrase(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(
        compact,
        (
            "\u6ca1\u51fa\u5565\u5927\u95ee\u9898",
            "\u6ca1\u51fa\u4ec0\u4e48\u5927\u95ee\u9898",
            "\u6ca1\u5565\u5927\u95ee\u9898",
            "\u6ca1\u4ec0\u4e48\u5927\u95ee\u9898",
            "\u6ca1\u5927\u95ee\u9898",
        ),
    )


def _looks_like_do_not_write_daily(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u4eca\u5929\u4e0d\u7528\u5199", "\u4eca\u65e5\u4e0d\u7528\u5199", "\u4e0d\u7528\u5199\u4e86", "\u522b\u5199", "\u4e0d\u8981\u5199"))


def _looks_like_submission_status_question(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u8c01\u8fd8\u6ca1\u4ea4", "\u8c01\u6ca1\u4ea4", "\u8fd8\u6709\u8c01\u6ca1\u4ea4")) and _contains_any(
        compact,
        ("\u622a\u6b62", "\u5230\u671f", "\u660e\u5929", "\u660e\u65e5"),
    )


def _looks_like_referential_write_reminder(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if not _contains_any(compact, ("\u521a\u624d", "\u524d\u9762", "\u4e0a\u9762", "\u90a3\u4e2a\u98ce\u9669", "\u90a3\u4e2a\u95ee\u9898")):
        return False
    if not _contains_any(compact, ("\u8bb0\u5f97\u5199\u4e0a", "\u5199\u4e0a\u54c8", "\u5199\u4e0a", "\u8865\u4e0a")):
        return False
    has_detail_marker = _contains_any(compact, ("\u98ce\u9669\u662f", "\u95ee\u9898\u662f", "\u56e0\u4e3a", "\u7531\u4e8e")) or "\uff1a" in str(raw_text or "") or ":" in str(raw_text or "")
    return not has_detail_marker


def _looks_like_system_failure_problem(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u7cfb\u7edf\u5d29", "\u7cfb\u7edf\u53c8\u5d29", "\u7cfb\u7edf\u6545\u969c", "\u7cfb\u7edfbug", "\u7cfb\u7edf\u767b\u5f55\u8001\u8d85\u65f6", "\u7cfb\u7edf\u767b\u5f55\u8d85\u65f6", "\u767b\u5f55\u8001\u8d85\u65f6", "\u767b\u5f55\u8d85\u65f6", "\u670d\u52a1\u5668\u7a81\u7136\u91cd\u542f", "\u7cfb\u7edf\u7a81\u7136\u91cd\u542f", "\u5ba2\u6237\u53cd\u9988\u6162", "\u5ba2\u6237\u53cd\u9988\u5361", "bug\u53c8\u591a", "\u6ca1\u4fdd\u5b58", "\u4fdd\u5b58\u5931\u8d25", "\u5dee\u70b9\u6ca1\u4fdd\u5b58"))


def _looks_like_resolved_risk_update(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u98ce\u9669", "\u95ee\u9898", "\u5361\u70b9", "\u5b95\u673a", "\u6545\u969c")) and _contains_any(
        compact,
        ("\u5df2\u7ecf\u89e3\u51b3", "\u5df2\u89e3\u51b3", "\u89e3\u51b3\u4e86", "\u98ce\u9669\u89e3\u9664", "\u5df2\u89e3\u9664", "\u89e3\u9664\u4e86"),
    )


def _looks_like_quantity_correction(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u9519\u4e86", "\u8bf4\u9519\u4e86")) and _contains_any(
        compact,
        ("\u662f\u4e24\u4efd", "\u662f\u4e8c\u4efd", "\u662f\u4e09\u4efd", "\u662f\u56db\u4efd", "\u662f2\u4efd", "\u662f3\u4efd"),
    ) and _contains_any(compact, ("\u6628\u5929", "\u6628\u65e5", "\u6709\u4e00\u4efd"))


def _looks_like_current_daily_item_retraction(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if not _contains_any(
        compact,
        (
            "\u7b97\u4e86\u5427",
            "\u7b97\u4e86",
            "\u5220\u6389",
            "\u5220\u4e86",
            "\u522b\u5199\u4e86",
            "\u4e0d\u8981\u5199",
            "\u522b\u8bb0\u4e86",
            "\u4e0d\u8981\u8bb0",
            "\u4fdd\u5bc6",
            "\u4e0d\u662f\u4eca\u5929\u505a",
            "\u4e0d\u662f\u4eca\u65e5\u505a",
        ),
    ):
        return False
    if not _contains_any(
        compact,
        (
            "\u6628\u5929",
            "\u6628\u65e5",
            "\u4e0d\u662f\u4eca\u5929",
            "\u4e0d\u662f\u4eca\u65e5",
            "\u522b\u5199\u4e86",
            "\u4e0d\u8981\u5199",
            "\u522b\u8bb0\u4e86",
            "\u4e0d\u8981\u8bb0",
            "\u4fdd\u5bc6",
        ),
    ):
        return False
    return _contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)


def _looks_like_quoted_delete_edit(raw_text: str) -> bool:
    text = str(raw_text or "")
    return bool(re.search(r"[“\"'‘].+?[”\"'’].{0,8}(?:删掉|删除|去掉)", text))


def _looks_like_self_prompted_forgotten_item(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u8fd8\u6709\u522b\u7684\u4e8b\u5417", "\u4eca\u5929\u8fd8\u6709\u522b\u7684\u4e8b\u5417")) and _contains_any(
        compact,
        ("\u5fd8\u4e86", "\u54e6\u5fd8\u4e86"),
    )


def _looks_like_report_later_deferral(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u7b49\u7ed3\u679c\u51fa\u6765\u518d\u62a5", "\u7b49\u6709\u7ed3\u679c\u518d\u62a5", "\u7b49\u7ed3\u679c\u51fa\u6765\u518d\u5199", "\u7b49\u6709\u7ed3\u679c\u518d\u5199", "\u51fa\u7ed3\u679c\u518d\u62a5")) and _contains_any(
        compact,
        ("\u65e5\u62a5", "\u65e5\u5fd7", "\u62a5\u5427", "\u62a5"),
    )


def _looks_like_system_rant_only(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact or not _looks_like_system_failure_problem(raw_text):
        return False
    if _contains_any(compact, ("\u6392\u67e5", "\u4fee\u590d", "\u5904\u7406", "\u89e3\u51b3", "\u5f71\u54cd\u586b\u62a5", "\u65e5\u62a5\u7cfb\u7edf", "\u6d41\u7a0b", "\u63a5\u53e3", "\u5ba1\u6279", "\u5ba2\u6237", "\u5408\u540c")):
        return False
    return _contains_any(compact, ("\u53c8\u5d29", "\u7834\u7cfb\u7edf", "bug\u53c8\u591a", "\u7cfb\u7edfbug\u53c8\u591a", "\u6d6a\u8d39\u6211\u65f6\u95f4", "\u70e6\u6b7b", "\u771f\u70e6"))


def _looks_like_standalone_past_work_without_current_context(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if not _contains_any(compact, ("\u6628\u5929", "\u6628\u65e5", "\u524d\u5929", "\u524d\u65e5")):
        return False
    if _looks_like_completed_yesterday_referenced_task_today(raw_text):
        return False
    if _contains_any(compact, ("\u4eca\u5929", "\u4eca\u65e5", "\u590d\u5236", "\u7167\u642c", "\u62f7\u8d1d", "\u8865\u4ea4", "\u8865\u5199", "\u8865\u4e00\u4e0b", "\u65e5\u62a5", "\u8ba1\u5212\u5df2\u5b8c\u6210", "\u8ba1\u5212\u5b8c\u6210")):
        return False
    if _contains_any(compact, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f")):
        return False
    return _contains_any(compact, _BUSINESS_ACTION_MARKERS) or _contains_any(compact, _BUSINESS_OBJECT_MARKERS)


def _has_specific_business_content_for_meta_request(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    specific_markers = (
        "\u5408\u540c",
        "\u6848\u4ef6",
        "\u6848\u5b50",
        "\u6cd5\u9662",
        "\u6750\u6599",
        "\u8d44\u6599",
        "\u5ba2\u6237",
        "\u9879\u76ee",
        "\u51fd\u4ef6",
        "\u8d77\u8bc9\u72b6",
        "\u7b54\u8fa9\u72b6",
        "\u6570\u636e",
        "\u9700\u6c42",
        "\u6d41\u7a0b",
        "\u7cfb\u7edf",
        "bug",
        "BUG",
    )
    return _contains_any(compact, specific_markers)


def _looks_like_short_contextual_plan(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if not _contains_any(compact, _TOMORROW_MARKERS):
        return False
    if _contains_any(compact, ("\u8fd9\u4e2a\u4e8b", "\u8fd9\u4ef6\u4e8b", "\u8fd9\u4e8b", "\u524d\u8ff0\u4e8b\u9879")) and _contains_any(
        compact,
        ("\u7ee7\u7eed", "\u8fd8\u5f97\u7ee7\u7eed", "\u8fd8\u8981\u7ee7\u7eed", "\u5199\u5230\u660e\u65e5\u8ba1\u5212", "\u5199\u5230\u660e\u5929\u8ba1\u5212"),
    ):
        return True
    if len(compact) > 16:
        return False
    return _contains_any(compact, ("\u8ddf\u8fdb", "\u5904\u7406", "\u63a8\u8fdb", "\u6c9f\u901a", "\u8865\u5145", "\u5b8c\u5584", "\u5199", "\u5f04", "\u641e", "\u6574"))


def _looks_like_vague_repeat_or_workload_statement(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u660e\u5929\u7ee7\u7eed\u5f04\u8fd9\u4e2a", "\u660e\u65e5\u7ee7\u7eed\u5f04\u8fd9\u4e2a", "\u660e\u5929\u7ee7\u7eed\u5f04\u90a3\u4e2a", "\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u8fd9\u4e2a", "\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u524d\u8ff0")):
        return False
    if _contains_any(compact, ("\u660e\u5929\u518d\u505a", "\u660e\u5929\u505a", "\u660e\u5929\u518d\u5f04", "\u660e\u5929\u518d\u641e")) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return True
    if _contains_any(compact, ("\u4eca\u5929\u7684\u4e8b\u90fd\u5904\u7406\u5b8c", "\u4eca\u5929\u7684\u4e8b\u90fd\u5fd9\u5b8c", "\u4eca\u5929\u7684\u4e8b\u90fd\u641e\u5b8c", "\u4eca\u5929\u7684\u6d3b\u5e72\u5b8c", "\u4eca\u5929\u6d3b\u5e72\u5b8c")):
        return True
    if _contains_any(compact, ("\u4eca\u5929\u5e72\u4e86\u597d\u591a\u4e8b", "\u5e72\u4e86\u597d\u591a\u4e8b", "\u4eca\u5929\u505a\u4e86\u597d\u591a\u4e8b", "\u505a\u4e86\u597d\u591a\u4e8b", "\u4eca\u5929\u505a\u4e86\u4e00\u4e9b\u4e8b", "\u505a\u4e86\u4e00\u4e9b\u4e8b", "\u4eca\u5929\u505a\u4e86\u4e9b\u4e8b", "\u505a\u4e86\u4e9b\u4e8b")) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return True
    if _contains_any(compact, ("\u5e72\u4e86\u70b9\u6d3b", "\u505a\u4e86\u70b9\u6d3b", "\u5e72\u4e86\u4e9b\u6d3b", "\u505a\u4e86\u4e9b\u6d3b")) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return True
    if _contains_any(compact, ("\u641e\u4e86\u70b9\u4e1c\u897f", "\u505a\u4e86\u70b9\u4e1c\u897f", "\u5f04\u4e86\u70b9\u4e1c\u897f", "\u4f60\u61c2\u7684")) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return True
    if _contains_any(compact, ("\u597d\u591a\u6d3b", "\u5f88\u591a\u6d3b", "\u4e0d\u5c11\u6d3b", "\u4e00\u5806\u6d3b")) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return True
    if _contains_any(compact, ("\u4eca\u5929\u5de5\u4f5c\u4e86", "\u4eca\u65e5\u5de5\u4f5c\u4e86", "\u4eca\u5929\u4e0a\u73ed\u4e86", "\u4eca\u5929\u5b8c\u4e86", "\u660e\u5929\u7ee7\u7eed\u5f04", "\u660e\u5929\u518d\u8bf4", "\u5b8c\u6210\u4e86\u4efb\u52a1", "\u5b8c\u6210\u4efb\u52a1", "\u6ca1\u5565\u5199\u7684", "\u6ca1\u4ec0\u4e48\u5199\u7684", "\u5c31\u90a3\u6837")) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return True
    if _contains_any(compact, ("\u4eca\u5929\u6ca1\u5565", "\u6ca1\u4ec0\u4e48\u7279\u522b", "\u5176\u4ed6\u6ca1\u4ec0\u4e48\u7279\u522b")) and _contains_any(compact, ("\u5f00\u4e86\u4e2a\u4f1a", "\u5f00\u4f1a")):
        return True
    if _looks_like_vague_completion_without_object(raw_text):
        return True
    if _contains_any(compact, ("\u8fd8\u662f\u90a3\u4e9b", "\u8fd8\u662f\u90a3\u51e0\u4e2a", "\u8fd8\u662f\u90a3\u51e0\u4ef6", "\u8fd8\u662f\u90a3\u4e9b\u4e8b")):
        return True
    if _contains_any(compact, ("\u8fd8\u662f\u90a3\u6837", "\u8fd8\u662f\u8001\u6837\u5b50", "\u8001\u6837\u5b50", "\u6ca1\u5565\u7279\u522b", "\u6ca1\u4ec0\u4e48\u7279\u522b")) and not _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return True
    if _contains_any(compact, ("\u6628\u5929\u90a3\u4e9b", "\u6628\u5929\u90a3\u4e9b\u4e8b", "\u4eca\u5929\u6ca1\u53d8\u5316", "\u6ca1\u53d8\u5316")) and _contains_any(compact, ("\u6628\u5929", "\u4eca\u5929", "\u5c31\u662f", "\u5bf9")):
        return True
    if _contains_any(compact, ("\u90a3\u4e2a\u4e8b", "\u8fd9\u4e2a\u4e8b", "\u90a3\u4ef6\u4e8b", "\u8fd9\u4ef6\u4e8b", "\u90a3\u4e8b", "\u8fd9\u4e8b")) and _contains_any(
        compact,
        ("\u641e\u5b9a", "\u641e\u5b8c", "\u5b8c\u6210", "\u5b8c\u6210\u4e86", "\u505a\u5b8c", "\u5904\u7406\u5b8c"),
    ):
        return True
    if _contains_any(compact, ("\u90a3\u4e2a\u4e8b", "\u8fd9\u4e2a\u4e8b", "\u90a3\u4ef6\u4e8b", "\u8fd9\u4ef6\u4e8b", "\u90a3\u4e8b", "\u8fd9\u4e8b")) and _contains_any(
        compact,
        ("\u5f04\u4e86\u4e00\u4e0b", "\u641e\u4e86\u4e00\u4e0b", "\u5904\u7406\u4e86\u4e00\u4e0b", "\u5dee\u4e0d\u591a"),
    ):
        return True
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS) and not _contains_any(compact, ("\u4e8b\u591a", "\u624b\u5934\u4e8b\u591a")):
        return False
    return _contains_any(compact, ("\u5dee\u4e0d\u591a", "\u4e5f\u5dee\u4e0d\u591a", "\u5e94\u8be5\u4e5f\u5dee\u4e0d\u591a", "\u624b\u5934\u4e8b\u591a", "\u4e8b\u60c5\u591a", "\u4e8b\u60c5\u633a\u591a", "\u4e8b\u633a\u591a")) and not _contains_any(
        compact,
        ("\u5408\u540c", "\u6848", "\u6750\u6599", "\u6cd5\u9662", "\u5ba2\u6237", "\u9879\u76ee", "\u6d41\u7a0b", "\u65b9\u6848"),
    )


def _looks_like_vague_completion_without_object(raw_text: str) -> bool:
    text = str(raw_text or "")
    compact = _compact(text)
    if _looks_like_quantity_correction(text):
        return False
    if _looks_like_resolved_risk_update(text):
        return False
    if not compact:
        return False
    if _contains_any(compact, _BUSINESS_OBJECT_MARKERS):
        return False
    if not _contains_any(compact, ("\u641e\u5b9a", "\u641e\u5b8c", "\u5f04\u5b8c", "\u505a\u5b8c", "\u5b8c\u6210", "\u5b8c\u6210\u4e86", "\u5904\u7406\u5b8c", "\u5b8c\u4e8b")):
        return False
    stripped = compact
    for marker in ("\u4eca\u5929", "\u4eca\u65e5", "\u5df2\u7ecf", "\u90fd", "\u4e86"):
        stripped = stripped.replace(marker, "")
    return len(stripped) <= 4


def _looks_like_contextual_business_detail(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    compact = _compact(text)
    if not compact or _looks_like_question(text) or _looks_like_non_substantive_daily_request(text):
        return False
    if _looks_like_emotional_deferral_without_business(text):
        return False
    if _contains_any(compact, ("\u7ade\u54c1\u5206\u6790", "\u5408\u89c4\u5ba1\u67e5", "\u521d\u7a3f", "\u5bf9\u6bd4")):
        return True
    if _contains_any(compact, ("\u90a3\u4e2a\u6848\u5b50", "\u90a3\u4e2a\u6848", "\u8fd9\u4e2a\u6848\u5b50", "\u8fd9\u4e2a\u6848")) and _contains_any(
        compact,
        ("\u6539\u4e86\u4e00\u904d", "\u53c8\u6539\u4e86", "\u4fee\u6539\u4e86", "\u8c03\u6574\u4e86"),
    ):
        return True
    if _contains_any(compact, ("\u8fd8\u5dee", "\u5dee\u6700\u540e", "\u6700\u540e\u4e00\u4e2a")) and _contains_any(compact, ("\u5408\u540c", "\u7ae0", "\u7528\u5370", "\u6750\u6599")):
        return True
    if _contains_any(compact, ("\u534f\u52a9", "\u914d\u5408")) and _contains_any(compact, ("\u538b\u6d4b", "\u6d4b\u8bd5\u56e2\u961f", "\u6d4b\u8bd5")):
        return True
    if _contains_any(compact, ("\u5f8b\u5e08", "\u88ab\u544a\u5f8b\u5e08")) and _contains_any(compact, ("\u6c9f\u901a", "\u8054\u7cfb", "\u63d0\u524d\u6c9f\u901a")):
        return True
    if _contains_any(compact, ("\u5c31\u662f", "\u5bf9", "\u90a3\u4e2a")) and _contains_any(compact, ("\u516c\u7ae0", "\u5370\u7ae0", "\u7528\u5370", "\u516c\u7ae0\u7ba1\u7406")):
        return True
    if _contains_any(compact, ("\u8c08\u4e86", "\u6c9f\u901a\u4e86", "\u786e\u8ba4\u4e86", "\u6574\u7406\u4e86", "\u7406\u4e86")) and _contains_any(
        compact,
        ("\u5408\u4f5c", "\u610f\u5411", "\u9700\u6c42", "\u6761\u6b3e", "\u7b56\u7565", "\u65b9\u6848", "\u4ef7\u683c"),
    ):
        return True
    if _contains_any(compact, ("\u5ba2\u6237", "\u7532\u65b9", "\u5bf9\u65b9")) and _contains_any(
        compact,
        ("\u540c\u610f", "\u4e0d\u540c\u610f", "\u7591\u8651", "\u5ef6\u671f", "\u63a8\u8fdf", "\u8ba4\u53ef", "\u4e0d\u8ba4\u53ef"),
    ):
        return True
    if _contains_any(compact, ("\u4ed6\u540c\u610f", "\u5979\u540c\u610f", "\u5bf9\u65b9\u540c\u610f", "\u5ba2\u6237\u540c\u610f")) and _contains_any(compact, ("\u7b7e\u7ea6", "\u7b7e\u5408\u540c", "\u4e0b\u5468")):
        return True
    if _contains_any(compact, ("\u68d8\u624b", "\u4e0d\u914d\u5408", "\u6709\u70b9\u68d8\u624b")) and _contains_any(compact, ("\u5bf9\u65b9", "\u5ba2\u6237", "\u7532\u65b9", "\u4f9b\u5e94\u5546", "\u8fd9\u4e2a")):
        return True
    if _contains_any(compact, ("\u4f9b\u5e94\u5546", "\u5ba2\u6237", "\u7532\u65b9", "\u5bf9\u65b9", "\u4ed6\u4eec")) and _contains_any(
        compact,
        ("\u8981\u6c42", "\u91cd\u65b0\u7b97", "\u91cd\u7b97", "\u8fdd\u7ea6\u91d1", "\u8d54\u507f"),
    ):
        return True
    return _contains_any(compact, ("\u521a\u624d", "\u90a3\u5757", "\u90a3\u6761", "\u90a3\u4e2a")) and _contains_any(
        compact,
        ("\u5305\u542b", "\u8865\u5145", "\u52a0\u8fdb\u53bb", "\u52a0\u4e0a", "\u5bf9\u6bd4"),
    )


def _looks_like_meeting_work(raw_text: str) -> bool:
    text = str(raw_text or "")
    if _looks_like_question(text):
        return False
    if re.search(r"(?:\u4e0a\u5348|\u4e0b\u5348|\u665a\u4e0a|\u4e2d\u5348|\d{1,2}\s*\u70b9).{0,8}\u6709\u4e2a\u4f1a", text):
        return True
    if re.search(r"(?:\d{1,2}\s*\u70b9|\u4e0a\u5348|\u4e0b\u5348|\u665a\u4e0a).{0,12}(?:\u5468\u4f1a|\u4f8b\u4f1a|\u4f1a\u8bae)", text):
        return True
    if re.search(r"\u5f00\u4e86?(?:[0-9\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]*\u4e2a?)?[\u4e00-\u9fa5]{0,8}\u4f1a", text):
        return True
    return bool(re.search(r"\u5f00\u4e86?(?:[0-9\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]*\u4e2a?)?(?:\u4f1a|\u77ed\u4f1a|\u5c0f\u4f1a)", text))


_TODAY_MARKERS = ("\u4eca\u5929", "\u4eca\u65e5", "\u4eca\u586b", "\u4eca\u586b\u5199", "\u5f53\u524d", "\u672c\u65e5", "\u4e0a\u5348", "\u4e0b\u5348", "\u665a\u4e0a", "\u4e2d\u5348", "\u65e9\u4e0a")
_YESTERDAY_MARKERS = ("\u6628\u5929", "\u6628\u65e5", "\u524d\u4e00\u5929", "\u4e0a\u4e00\u4e2a\u5de5\u4f5c\u65e5")
_DAY_BEFORE_YESTERDAY_MARKERS = ("\u524d\u5929", "\u524d\u65e5")
_THREE_DAYS_AGO_MARKERS = ("\u5927\u524d\u5929", "\u5927\u524d\u65e5")
_NON_TOMORROW_FUTURE_MARKERS = ("\u540e\u5929", "\u5927\u540e\u5929", "\u4e0b\u5468", "\u4e0b\u5468\u4e00", "\u4e0b\u5468\u4e8c", "\u4e0b\u5468\u4e09", "\u4e0b\u5468\u56db", "\u4e0b\u5468\u4e94", "\u4e0b\u5468\u516d", "\u4e0b\u5468\u65e5")
_PREVIOUS_WORK_REFERENCE_MARKERS = _YESTERDAY_MARKERS + _DAY_BEFORE_YESTERDAY_MARKERS + _THREE_DAYS_AGO_MARKERS
_TOMORROW_MARKERS = (
    "\u660e\u5929",
    "\u660e\u65e5",
    "\u660e\u513f",
    "\u660e\u65e9",
    "\u660e\u5929\u65e9\u4e0a",
    "\u660e\u65e5\u65e9\u4e0a",
    "\u660e\u5929\u4e0a\u5348",
    "\u660e\u65e5\u4e0a\u5348",
    "\u660e\u513f\u4e2a",
    "\u660e\u4e2a",
    "\u660e\u65e5\u8ba1\u5212",
    "\u660e\u5929\u8ba1\u5212",
    "\u4e0b\u4e00\u6b65\u8ba1\u5212",
)
_PROBLEM_MARKERS = ("\u95ee\u9898", "\u98ce\u9669", "\u6682\u65e0\u95ee\u9898", "\u65e0\u98ce\u9669", "\u56f0\u96be")
_QUERY_CURRENT_MARKERS = (
    "\u53d1\u6211",
    "\u53d1\u6211\u4e0b",
    "\u53d1\u6211\u770b\u4e0b",
    "\u53d1\u6211\u4e00\u4e0b",
    "\u53d1\u6211\u770b\u770b",
    "\u7ed9\u6211\u770b",
    "\u7ed9\u6211\u770b\u4e0b",
    "\u770b\u4e0b",
    "\u770b\u770b",
    "\u67e5\u4e0b",
    "\u5c55\u793a",
    "\u5f53\u524d\u65e5\u62a5",
    "\u65e5\u62a5\u8349\u7a3f",
)
_QUERY_HISTORY_MARKERS = (
    "\u67e5\u5386\u53f2",
    "\u5386\u53f2\u65e5\u62a5",
    "\u53d1\u6211\u4e0b",
    "\u770b\u770b\u5386\u53f2",
)
_COPY_PREVIOUS_MARKERS = (
    "\u590d\u5236\u6628\u5929",
    "\u6628\u5929\u7684\u65e5\u62a5\u590d\u5236\u5230\u4eca\u5929",
    "\u6628\u65e5\u7684\u65e5\u62a5\u590d\u5236\u5230\u4eca\u5929",
    "\u628a\u6628\u5929\u7684\u65e5\u62a5\u590d\u5236\u5230\u4eca\u5929",
    "\u628a\u6628\u65e5\u7684\u65e5\u62a5\u590d\u5236\u5230\u4eca\u5929",
    "\u628a\u6628\u5929\u7684\u5e26\u8fc7\u6765",
    "\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u62ff\u6765\u4eca\u5929\u7528",
    "\u6628\u5929\u7684\u660e\u5929\u8ba1\u5212\u62ff\u6765\u4eca\u5929\u7528",
    "\u7167\u65e7",
    "\u548c\u6628\u5929\u4e00\u6837",
    "\u8ddf\u6628\u5929\u4e00\u6837",
    "\u8fd8\u662f\u6628\u5929\u90a3\u4e9b\u4e8b",
    "\u6628\u5929\u90a3\u4efd\u65e5\u62a5",
    "\u4eca\u5929\u63a5\u7740\u5e72",
)
_CLEAR_MARKERS = ("\u6e05\u7a7a", "\u5220\u9664", "\u5220\u6389", "\u5220\u4e86", "\u5220\u4e86", "\u6e05\u6389", "\u91cd\u65b0\u5199", "\u91cd\u5199")
_REVOKE_MARKERS = ("\u64a4\u56de", "\u64a4\u9500", "\u53d6\u6d88\u63d0\u4ea4", "\u9000\u56de")
_DESTRUCTIVE_MARKERS = _CLEAR_MARKERS + _REVOKE_MARKERS
_DAILY_FIELD_MARKERS = (
    "\u65e5\u62a5",
    "\u65e5\u5fd7",
    "\u8349\u7a3f",
    "\u4eca\u65e5\u5de5\u4f5c",
    "\u4eca\u5929\u5de5\u4f5c",
    "\u95ee\u9898/\u98ce\u9669",
    "\u95ee\u9898\u98ce\u9669",
    "\u660e\u65e5\u8ba1\u5212",
    "\u660e\u5929\u8ba1\u5212",
)
_ORDINAL_MARKERS = (
    "\u7b2c1",
    "\u7b2c2",
    "\u7b2c3",
    "\u7b2c4",
    "\u7b2c5",
    "\u7b2c6",
    "\u7b2c7",
    "\u7b2c8",
    "\u7b2c9",
    "\u7b2c\u4e00",
    "\u7b2c\u4e8c",
    "\u7b2c\u4e09",
    "\u7b2c\u56db",
    "\u7b2c\u4e94",
    "\u7b2c\u516d",
    "\u7b2c\u4e03",
    "\u7b2c\u516b",
    "\u7b2c\u4e5d",
)
_EDIT_ACTION_MARKERS = (
    "\u6539",
    "\u6539\u6210",
    "\u6539\u4e3a",
    "\u4fee\u6539",
    "\u66ff\u6362",
    "\u5220\u6389",
    "\u5220\u9664",
    "\u53bb\u6389",
    "\u5408\u5e76",
    "\u5408\u6210",
    "\u79fb\u5230",
    "\u590d\u5236",
    "\u52a0\u5230",
    "\u52a0\u8fdb",
    "\u52a0\u5165",
)
_REPLACEMENT_EDIT_MARKERS = (
    "\u6539\u6210",
    "\u6539\u4e3a",
    "\u66ff\u6362\u6210",
    "\u66ff\u6362\u4e3a",
)
_BUSINESS_ACTION_MARKERS = (
    "\u641e\u5b8c",
    "\u641e",
    "\u5f04",
    "\u6574",
    "\u6539",
    "\u6362",
    "\u5199",
    "\u5f00\u4e86",
    "\u53ec\u5f00",
    "\u8ba2\u4e86",
    "\u8ba2\u597d",
    "\u5f00\u4f1a",
    "\u89c1",
    "\u62dc\u8bbf",
    "\u8c08",
    "\u8c08\u7ec6\u8282",
    "\u6495\u9700\u6c42",
    "\u7b7e",
    "\u7b7e\u4e86",
    "\u7b7e\u8ba2",
    "\u95ee",
    "\u95ee\u4e0b",
    "\u95ee\u4e00\u4e0b",
    "\u54a8\u8be2",
    "\u8054\u7cfb",
    "\u63a5\u4e86",
    "\u5b8c\u6210",
    "\u5904\u7406",
    "\u5e26",
    "\u5e26\u4e0a",
    "\u53bb",
    "\u53bb\u5ba2\u6237",
    "\u8ba1\u5212",
    "\u7ea6",
    "\u7406\u51fa",
    "\u7406\u51fa\u6765",
    "\u78b0",
    "\u78b0\u4e00\u4e0b",
    "\u5bf9\u4e00\u904d",
    "\u6838\u5bf9",
    "\u590d\u6838",
    "\u67e5",
    "\u67e5\u4e86",
    "\u67e5\u9605",
    "\u67e5\u8be2",
    "\u68c0\u7d22",
    "\u8c03\u53d6",
    "\u6838\u67e5",
    "\u770b",
    "\u770b\u4e86",
    "\u4ea4",
    "\u63d0\u4ea4",
    "\u8bc4\u5ba1",
    "\u5ba1",
    "\u5ba1\u6838",
    "\u590d\u6838",
    "\u6c9f\u901a",
    "\u8ba8\u8bba",
    "\u6c47\u62a5",
    "\u901a\u7535\u8bdd",
    "\u6574\u7406",
    "\u68b3\u7406",
    "\u8d77\u8349",
    "\u53c2\u52a0",
    "\u6536\u96c6",
    "\u53d1",
    "\u7f16\u8f91",
    "\u4fee\u6539",
    "\u8bbe\u8ba1",
    "\u8ddf\u8fdb",
    "\u63a8\u52a8",
    "\u66f4\u65b0",
    "\u62df",
    "\u62df\u5b9a",
    "\u53d1\u7ed9",
    "\u5bc4",
    "\u5bc4\u51fa",
    "\u6536\u5230",
    "\u7b49\u5f85",
    "\u5d29",
    "\u4fdd\u5b58",
    "\u5f00\u5ead",
    "\u76d6\u7ae0",
    "\u7528\u5370",
    "\u5f52\u6863",
    "\u7533\u62a5",
    "\u6267\u884c",
    "\u51fa\u5dee",
    "\u4f18\u5316",
    "\u5b8c\u5584",
    "\u5f00\u53d1",
    "\u5efa\u8bbe",
    "\u6d4b\u8bd5",
    "\u7edf\u8ba1",
    "\u8c03\u7814",
    "\u6c47\u603b",
    "\u5bf9\u63a5",
    "\u540c\u6b65",
    "\u63a8\u8fdb",
    "\u542f\u52a8",
    "\u5b9a\u7a3f",
    "\u5b89\u629a",
    "\u5bf9",
    "\u5bf9\u4e86",
    "\u4fee",
    "\u4fee\u4e86",
    "\u53d1\u51fd",
    "\u51fa\u5177",
    "\u5f55\u5165",
    "\u51c6\u5907",
    "\u626b\u5c3e",
    "\u4e0a\u7ebf",
    "review",
    "Review",
)
_BUSINESS_OBJECT_MARKERS = (
    "bug",
    "BUG",
    "UI",
    "PPT",
    "ppt",
    "review",
    "Review",
    "\u4ee3\u7801",
    "\u4e0a\u7ebf",
    "\u5c41\u5c41\u8e22",
    "\u6d4b\u8bd5\u7528\u4f8b",
    "\u7528\u4f8b",
    "\u9700\u6c42\u6587\u6863",
    "\u9700\u6c42\u5206\u6790",
    "\u6587\u6863",
    "\u6587\u4ef6",
    "\u8bbe\u8ba1",
    "\u767b\u5f55\u529f\u80fd",
    "\u529f\u80fd",
    "\u9884\u7b97",
    "\u8d22\u52a1",
    "\u73b0\u573a",
    "\u5c3d\u8c03",
    "\u5408\u540c",
    "\u6761\u6b3e",
    "\u534f\u8bae",
    "\u4e89\u8bae\u70b9",
    "\u4ef2\u88c1",
    "\u6d89\u5916\u4ef2\u88c1",
    "\u88c1\u5b9a\u4e66",
    "\u4fdd\u5168\u88c1\u5b9a\u4e66",
    "\u987a\u4e30\u5355\u53f7",
    "\u7ade\u54c1\u5206\u6790",
    "\u5408\u89c4\u5ba1\u67e5",
    "\u521d\u7a3f",
    "\u5bf9\u6bd4",
    "\u6cd5\u52a1",
    "\u7ec6\u8282",
    "\u5cf0\u4f1a",
    "\u6848\u4ef6",
    "\u6848\u5b50",
    "\u6848",
    "\u6cd5\u9662",
    "\u6750\u6599",
    "\u8d44\u6599",
    "\u6863\u6848",
    "\u8bc1\u636e",
    "\u8bc1\u636e\u6e05\u5355",
    "\u6e05\u5355",
    "\u5ba2\u6237",
    "\u4f9b\u5e94\u5546",
    "\u53d1\u7968",
    "\u90ae\u4ef6",
    "\u6295\u8bc9",
    "\u5ba2\u6237\u6295\u8bc9",
    "\u6f0f\u6d1e",
    "\u5b89\u5168\u6f0f\u6d1e",
    "\u5ba2\u8bc9",
    "\u7535\u8bdd",
    "\u51fd\u4ef6",
    "\u5f8b\u5e08\u51fd",
    "\u8d77\u8bc9\u72b6",
    "\u7b54\u8fa9\u72b6",
    "\u8bc9\u8bbc",
    "\u6267\u884c",
    "\u5f52\u6863",
    "\u53f0\u8d26",
    "\u6d41\u7a0b",
    "\u9879\u76ee",
    "\u8fdb\u5ea6",
    "\u4e1a\u52a1",
    "\u4f1a\u8bae",
    "\u98ce\u63a7\u4f1a",
    "\u57f9\u8bad",
    "\u8bc4\u5ba1\u4f1a",
    "\u4f8b\u4f1a",
    "\u65b0\u89c4",
    "\u5ead\u524d\u4f1a\u8bae",
    "\u6cd5\u5f8b\u610f\u89c1\u4e66",
    "\u516c\u7ae0",
    "\u516c\u8bc1\u5904",
    "\u5f8b\u5e08",
    "\u88ab\u544a\u5f8b\u5e08",
    "\u8f66\u7968",
    "\u6cd5\u5b98",
    "\u6bd4\u8d5b",
    "\u516c\u4f17\u53f7",
    "\u901a\u62a5",
    "\u90ae\u4ef6",
    "\u5185\u5bb9",
    "\u7cfb\u7edf",
    "\u6a21\u5757",
    "\u7b97\u6cd5",
    "\u6280\u672f",
    "\u8c03\u7814",
    "\u9700\u6c42",
    "\u53d8\u66f4",
    "\u5f00\u53d1",
    "\u7814\u53d1",
    "\u8fed\u4ee3",
    "\u63a5\u53e3",
    "\u5546\u52a1",
    "\u5de5\u5177",
    "\u6708\u62a5",
    "\u62a5\u8868",
    "\u4ea4\u8868",
    "\u62a5\u544a",
    "\u6570\u636e",
    "\u6536\u6b3e",
    "\u94f6\u884c",
    "\u8ba8\u85aa",
    "\u7528\u5370",
    "\u5370\u7ae0",
    "\u76d6\u7ae0",
    "\u6708\u5ea6",
    "\u8ba1\u5212",
)
_STRONG_BUSINESS_EVENT_MARKERS = (
    "\u5f00\u5ead",
    "\u51fa\u5dee",
    "\u76d6\u7ae0",
    "\u7528\u5370",
    "\u516c\u8bc1\u5904",
    "\u5408\u540c",
    "\u6848\u4ef6",
    "\u6cd5\u9662",
    "\u6750\u6599",
    "\u6536\u6b3e",
    "\u6d41\u7a0b",
)
_ASSISTANT_FEEDBACK_MARKERS = (
    "\u673a\u5668\u4eba",
    "\u7cfb\u7edf",
    "\u4f60\u5199\u9519",
    "\u5199\u9519\u4e86\u5427",
    "\u6709\u70b9\u50bb",
    "\u592a\u50bb",
    "\u592a\u8822",
    "\u4e0d\u597d\u7528",
    "\u4e00\u5768\u5c4e",
    "\u5947\u602a",
    "\u79bb\u8c31",
)
_PERSONAL_STATE_MARKERS = (
    "\u592a\u7d2f",
    "\u70ed\u6b7b",
    "\u70ed\u6b7b\u4e86",
    "\u7d2f\u4e86",
    "\u597d\u7d2f",
    "\u597d\u70e6",
    "\u70e6",
    "\u5fc3\u60c5\u4e0d\u597d",
    "\u5bb3\u6015",
    "\u62c5\u5fc3",
    "\u7126\u8651",
    "\u65e0\u804a",
    "\u597d\u65e0\u804a",
    "\u56f0\u4e86",
    "\u4e0d\u60f3\u52a8",
    "\u4e0d\u60f3\u4e0a\u73ed",
)
_LIFESTYLE_OBJECT_MARKERS = (
    "\u5403",
    "\u559d",
    "\u559d\u6c34",
    "\u6ca1\u7a7a\u559d\u6c34",
    "\u7761",
    "\u73a9",
    "\u770b\u7535\u5f71",
    "\u770b\u89c6\u9891",
    "\u5c0f\u756a\u8304",
    "\u756a\u8304",
    "\u98df\u5802",
    "\u5929\u6c14",
    "\u5929\u513f",
    "\u7a7f\u5565",
    "\u7a7f\u4ec0\u4e48",
    "\u7a7f\u54ea",
    "\u70ed\u6b7b",
    "\u70ed\u6b7b\u4e86",
    "\u4e0d\u60f3\u52a8",
    "\u5730\u94c1",
    "\u8fdf\u5230",
    "\u6324\u6b7b",
    "\u70e6",
    "\u96e8\u597d\u5927",
    "\u96e8\u592a\u5927",
    "\u96e8\u4e0b\u5f97\u771f\u5927",
    "\u96e8\u4e0b\u5f97\u5927",
    "\u4e0b\u96e8",
    "\u5fd9\u6b7b",
    "\u7d2f\u6b7b",
    "\u8fd8\u6ca1\u4ea4",
    "\u6ca1\u4ea4",
    "\u660e\u5929\u8865\u5427",
    "\u7ea2\u70e7\u8089",
    "\u53a8\u5e08",
    "\u597d\u54b8",
    "\u592a\u54b8",
    "\u6c34\u679c",
    "\u5348\u996d",
    "\u665a\u996d",
    "\u65e9\u996d",
    "\u5496\u5561",
    "\u5976\u8336",
    "\u96f6\u98df",
)


def _looks_like_history_query(compact: str) -> bool:
    if not compact:
        return False
    if _looks_like_historical_daily_risk_lookup(compact):
        return True
    if _contains_any(compact, ("\u5386\u53f2", "\u8fc7\u53bb", "\u4e4b\u524d")):
        return True
    has_report_display = _contains_any(compact, ("\u53d1\u6211", "\u770b", "\u770b\u770b", "\u770b\u4e0b", "\u67e5", "\u67e5\u4e0b", "\u5c55\u793a"))
    has_history_date = _contains_any(
        compact,
        _YESTERDAY_MARKERS + _DAY_BEFORE_YESTERDAY_MARKERS + _THREE_DAYS_AGO_MARKERS,
    ) or bool(re.search(r"\d{1,2}\u6708?\d{1,2}\u65e5|\d{1,2}\u53f7", compact))
    return has_report_display and has_history_date


def _looks_like_current_daily_status_query(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact or not _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7", "\u8349\u7a3f")):
        return False
    return _contains_any(
        compact,
        (
            "\u5199\u5b8c\u4e86\u6ca1",
            "\u5199\u5b8c\u6ca1",
            "\u586b\u5b8c\u4e86\u6ca1",
            "\u586b\u5b8c\u6ca1",
            "\u5199\u4e86\u6ca1",
            "\u5199\u4e86\u6ca1\u554a",
            "\u586b\u4e86\u6ca1",
            "\u586b\u4e86\u6ca1\u554a",
            "\u4eca\u5929\u7684\u5de5\u4f5c\u591f\u4e86\u5417",
            "\u4eca\u5929\u5de5\u4f5c\u591f\u4e86\u5417",
            "\u5de5\u4f5c\u591f\u4e86\u5417",
            "\u5199\u4e86\u5565",
            "\u5199\u4e86\u4ec0\u4e48",
            "\u65e5\u62a5\u5199\u4e86\u5565",
            "\u65e5\u62a5\u5199\u4e86\u4ec0\u4e48",
            "\u6211\u770b\u770b",
            "\u770b\u770b",
            "\u770b\u4e0b",
            "\u53d1\u6211",
            "\u53d1\u7ed9\u6211",
        ),
    )


def _looks_like_daily_meta_or_date_question(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    if _contains_any(compact, ("\u4eca\u5929\u661f\u671f\u51e0", "\u661f\u671f\u51e0", "\u51e0\u53f7", "\u4eca\u5929\u51e0\u53f7")):
        return True
    return _contains_any(compact, ("\u65e5\u62a5\u91cc\u8fd8\u8981\u5199\u5565", "\u65e5\u62a5\u91cc\u8fd8\u8981\u5199\u4ec0\u4e48", "\u8be5\u5199\u65e5\u62a5\u4e86", "\u8981\u5199\u65e5\u62a5\u5417"))


def _looks_like_historical_daily_risk_lookup(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    has_history_anchor = _contains_any(
        compact,
        (
            "\u4e0a\u5468",
            "\u4e0a\u5468\u4e00",
            "\u4e0a\u5468\u4e8c",
            "\u4e0a\u5468\u4e09",
            "\u4e0a\u5468\u56db",
            "\u4e0a\u5468\u4e94",
            "\u6628\u5929",
            "\u6628\u65e5",
            "\u524d\u5929",
            "\u524d\u65e5",
        ),
    ) or bool(re.search(r"\d{1,2}\u6708?\d{1,2}\u65e5|\d{1,2}\u53f7", compact))
    if not has_history_anchor or not _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    return _contains_any(compact, ("\u98ce\u9669\u70b9", "\u98ce\u9669", "\u95ee\u9898", "\u95ee\u9898\u70b9")) and _contains_any(
        compact,
        ("\u627e", "\u67e5", "\u770b", "\u8bb0\u5f97", "\u5e2e\u6211"),
    )


def _looks_like_history_info_delivery_request(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    has_history_scope = _contains_any(compact, ("\u4e0a\u5468", "\u4e0a\u4e2a\u6708", "\u4e0a\u6708", "\u4e0a\u5b63\u5ea6", "\u524d\u51e0\u5929", "\u8fc7\u53bb"))
    has_subject = _contains_any(compact, ("\u5f00\u5ead\u60c5\u51b5", "\u5f00\u5ead", "\u6848\u4ef6\u60c5\u51b5", "\u5de5\u4f5c\u60c5\u51b5", "\u65e5\u62a5\u60c5\u51b5"))
    has_delivery = _contains_any(compact, ("\u53d1\u6211", "\u53d1\u6211\u4e0b", "\u53d1\u7ed9\u6211", "\u7ed9\u6211", "\u770b\u4e0b", "\u67e5\u4e0b"))
    return has_history_scope and has_subject and has_delivery


def _looks_like_case_progress_record_request(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    return _contains_any(compact, ("\u8bb0\u5f55\u4e00\u4e0b\u8fdb\u5c55", "\u8bb0\u4e00\u4e0b\u8fdb\u5c55", "\u8bb0\u5f55\u8fdb\u5c55")) and _contains_any(
        compact,
        ("\u6848", "\u6848\u4ef6", "\u6cd5\u5b98", "\u6cd5\u9662", "\u8bc1\u636e", "\u5f00\u5ead", "\u4f20\u7968"),
    )
