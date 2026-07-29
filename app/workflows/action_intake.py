from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any

from app.agent2.daily_edit_intent import looks_like_contextual_daily_edit
from app.workflows.action_context import resolve_action_context
from app.workflows.problem_evidence import extract_problem_evidence
from app.workflows.relative_dates import (
    date_hint_from_text,
    has_previous_to_current_repeat_reference,
    has_relative_day_anchor,
)


WORKFLOW_DAILY_REPORT = "daily_report"
WORKFLOW_MONTHLY_REPORT = "monthly_report"
WORKFLOW_WEEKLY_REPORT = "weekly_report"
WORKFLOW_CASE_PROGRESS = "case_progress"
WORKFLOW_TRAVEL_COORDINATION = "travel_coordination"
WORKFLOW_LEGAL_RESEARCH = "legal_research"
WORKFLOW_INTERNAL_QA = "internal_qa"
WORKFLOW_CHAT = "chat"
WORKFLOW_UNKNOWN_OR_HELP = "unknown_or_help"

ACTION_DAILY_WRITE = "daily_write"
ACTION_DAILY_EDIT = "daily_edit"
ACTION_DAILY_CONFIRM = "daily_confirm"
ACTION_DAILY_READ_CURRENT = "daily_read_current"
ACTION_DAILY_READ_HISTORY = "daily_read_history"
ACTION_MONTHLY_REPLY = "monthly_reply"
ACTION_MONTHLY_STATUS_QUERY = "monthly_status_query"
ACTION_WEEKLY_REQUEST = "weekly_request"
ACTION_TRAVEL_COORDINATION = "travel_coordination"
ACTION_CASE_PROGRESS = "case_progress"
ACTION_LEGAL_RESEARCH = "legal_research"
ACTION_INTERNAL_QA = "internal_qa"
ACTION_SMALL_TALK = "small_talk"
ACTION_ASSISTANT_FEEDBACK = "assistant_feedback"
ACTION_DISAMBIGUATION_REQUIRED = "disambiguation_required"
ACTION_UNKNOWN = "unknown"

POLICY_WRITE = "write"
POLICY_READ_ONLY = "read_only"
POLICY_NO_WRITE = "no_write"
POLICY_PENDING = "pending"
POLICY_SANDBOX = "sandbox"


@dataclass(frozen=True)
class UserAction:
    """Action-first frame for one user segment before workflow ownership."""

    action_type: str
    workflow: str
    operation: str
    target_field: str = "none"
    write_policy: str = POLICY_NO_WRITE
    target: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    requires_confirmation: bool = False
    source_segment_index: int = 0
    source_text_hash: str = ""
    source_text_chars: int = 0
    safety_flags: list[str] = field(default_factory=list)
    reason: str = ""

    def as_observation(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "workflow": self.workflow,
            "operation": self.operation,
            "target_field": self.target_field,
            "write_policy": self.write_policy,
            "target": dict(self.target),
            "payload_keys": sorted(self.payload.keys()),
            "confidence": self.confidence,
            "requires_confirmation": self.requires_confirmation,
            "source_segment_index": self.source_segment_index,
            "source_text_hash": self.source_text_hash,
            "source_text_chars": self.source_text_chars,
            "safety_flags": list(self.safety_flags),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class UserActionPlan:
    """A turn-level action plan that intentionally precedes workflow routing."""

    actions: list[UserAction] = field(default_factory=list)
    commit_policy: str = "blocked"
    warnings: list[str] = field(default_factory=list)
    action_context: dict[str, Any] = field(default_factory=dict)
    source_text_hash: str = ""
    source_text_chars: int = 0

    def action_types(self) -> list[str]:
        return [action.action_type for action in self.actions]

    def has_action(self, *action_types: str) -> bool:
        wanted = set(action_types)
        return any(action.action_type in wanted for action in self.actions)

    def has_workflow(self, *workflows: str) -> bool:
        wanted = set(workflows)
        return any(action.workflow in wanted for action in self.actions)

    def write_actions(self) -> list[UserAction]:
        return [action for action in self.actions if action.write_policy == POLICY_WRITE]

    def as_observation(self) -> dict[str, Any]:
        return {
            "action_types": self.action_types(),
            "workflows": _dedupe([action.workflow for action in self.actions if action.workflow]),
            "commit_policy": self.commit_policy,
            "warnings": list(self.warnings),
            "action_context": dict(self.action_context),
            "source_text_hash": self.source_text_hash,
            "source_text_chars": self.source_text_chars,
            "actions": [action.as_observation() for action in self.actions],
        }


@dataclass(frozen=True)
class _SegmentFrame:
    text: str
    inherited_field: str = ""


def plan_user_actions(envelope: Any) -> UserActionPlan:
    """Classify the user's intended actions before any workflow claims the turn."""

    raw_text = str(getattr(envelope, "raw_text", "") or "")
    active_tasks = tuple(getattr(envelope, "active_tasks", ()) or ())
    active_daily = _has_active_task(active_tasks, WORKFLOW_DAILY_REPORT)
    active_monthly = _has_active_task(active_tasks, WORKFLOW_MONTHLY_REPORT)
    pending_confirmation_tasks = [task for task in active_tasks if bool(getattr(task, "awaiting_confirmation", False))]
    daily_confirmation_pending = (
        len(pending_confirmation_tasks) == 1
        and str(getattr(pending_confirmation_tasks[0], "workflow", "") or "") == WORKFLOW_DAILY_REPORT
    )
    received_at = getattr(envelope, "received_at", None)
    action_context = resolve_action_context(raw_text, received_at=received_at, active_tasks=active_tasks)

    actions: list[UserAction] = []
    for index, frame in enumerate(_split_segment_frames(raw_text, received_at=received_at), start=1):
        actions.extend(
            _actions_for_segment(
                frame.text,
                index=index,
                active_daily=active_daily,
                active_monthly=active_monthly,
                daily_confirmation_pending=daily_confirmation_pending,
                whole_text=raw_text,
                received_at=received_at,
                inherited_field=frame.inherited_field,
            )
        )

    actions = _suppress_resolved_daily_ambiguity(_dedupe_actions(actions))
    if _looks_like_do_not_write_daily(raw_text):
        actions = [
            action
            for action in actions
            if not (
                action.workflow == WORKFLOW_DAILY_REPORT
                and action.write_policy == POLICY_WRITE
                and action.action_type == ACTION_DAILY_WRITE
            )
        ]
    return UserActionPlan(
        actions=actions,
        commit_policy=_commit_policy(actions),
        warnings=_warnings(actions),
        action_context=action_context.as_dict(),
        source_text_hash=_hash_text(raw_text),
        source_text_chars=len(raw_text),
    )


def _actions_for_segment(
    segment: str,
    *,
    index: int,
    active_daily: bool,
    active_monthly: bool,
    daily_confirmation_pending: bool,
    whole_text: str,
    received_at: Any = None,
    inherited_field: str = "",
) -> list[UserAction]:
    segment = str(segment or "").strip()
    if not segment:
        return []
    if active_daily and _looks_like_contextual_system_failure_followup(whole_text):
        if index != 1:
            return []
        content = _contextual_system_failure_content(whole_text)
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                content,
                index,
                target_field="problems",
                write_policy=POLICY_WRITE,
                payload={"content": content},
                confidence=0.86,
                reason="active daily context reports a concrete system failure",
            ),
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                whole_text,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.76,
                safety_flags=["side_reply_only"],
                reason="user also asks the assistant to inspect the reported failure",
            ),
        ]
    if _looks_like_current_daily_status_query(whole_text) and not _looks_like_daily_history_query(whole_text):
        if index != 1:
            return []
        return [
            _action(
                ACTION_DAILY_READ_CURRENT,
                WORKFLOW_DAILY_REPORT,
                "query_current",
                whole_text,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.9,
                reason="user asks for the current daily-report status",
            )
        ]
    if active_daily and _looks_like_ambiguous_daily_editorial_request(whole_text):
        if index != 1:
            return []
        return [
            _action(
                ACTION_DISAMBIGUATION_REQUIRED,
                WORKFLOW_DAILY_REPORT,
                "clarify_target",
                whole_text,
                index,
                target_field="unknown",
                write_policy=POLICY_PENDING,
                confidence=0.84,
                safety_flags=["ambiguous_daily_edit_target"],
                reason="daily editorial request does not identify the item or field to change",
            )
        ]
    if _looks_like_historical_daily_destructive_request(whole_text):
        if index != 1:
            return []
        return [
            _action(
                ACTION_DAILY_READ_HISTORY,
                WORKFLOW_DAILY_REPORT,
                "begin_edit",
                whole_text,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.9,
                safety_flags=["blocks_current_daily_write"],
                reason="user refers to yesterday's daily report, which must not be written into today's draft",
            )
        ]
    if (
        _looks_like_daily_start_without_payload(whole_text)
        and not _looks_like_daily_meta_status_statement(whole_text)
        and not _looks_like_absurd_content(whole_text)
        and not _looks_like_lifestyle_report_meta_chatter(whole_text)
        and not _looks_like_non_substantive_daily_request(whole_text)
    ):
        if index != 1:
            return []
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "start_collection",
                whole_text,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.84,
                reason="user explicitly asks to write or start a daily report",
            )
        ]
    if active_daily and index == 1 and _looks_like_contextual_retract_or_revoke(whole_text):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                whole_text,
                index,
                target_field="unknown",
                write_policy=POLICY_WRITE,
                confidence=0.84,
                reason="active daily context receives a contextual retract/revoke instruction",
            )
        ]
    if active_daily and index == 1 and _compact(whole_text) == "\u64a4\u56de":
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                whole_text,
                index,
                target_field="all",
                write_policy=POLICY_WRITE,
                confidence=0.84,
                safety_flags=["destructive_or_overwrite", "requires_bound_typed_validation"],
                reason="user asks to revoke the active daily report; typed validation must bind report state",
            )
        ]
    if _looks_like_emotional_customer_phone_only(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_SMALL_TALK,
                    WORKFLOW_CHAT,
                    "small_talk",
                    whole_text,
                    index,
                    write_policy=POLICY_NO_WRITE,
                    confidence=0.76,
                    safety_flags=["blocks_context_write", "emotional_phone_chatter"],
                    reason="customer-phone mention is emotional chatter without a concrete work outcome",
                )
            ]
        return []
    if _looks_like_ambiguous_that_day_daily_reference(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_DISAMBIGUATION_REQUIRED,
                    WORKFLOW_UNKNOWN_OR_HELP,
                    "clarify_action",
                    whole_text,
                    index,
                    write_policy=POLICY_PENDING,
                    confidence=0.8,
                    safety_flags=["ambiguous_daily_context"],
                    reason="deictic day reference is too ambiguous to write into a daily report",
                )
            ]
        return []
    if _looks_like_lifestyle_question_turn(whole_text) and not _has_current_reportable_work_piece(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_SMALL_TALK,
                    WORKFLOW_CHAT,
                    "small_talk",
                    segment,
                    index,
                    write_policy=POLICY_NO_WRITE,
                    confidence=0.84,
                    safety_flags=["blocks_context_write", "lifestyle_question"],
                    reason="whole turn is a lifestyle question with incidental work context, not a daily update",
                )
            ]
        return []
    if _looks_like_today_task_question(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_INTERNAL_QA,
                    WORKFLOW_INTERNAL_QA,
                    "answer_question",
                    whole_text,
                    index,
                    write_policy=POLICY_READ_ONLY,
                    confidence=0.82,
                    safety_flags=["blocks_context_write", "today_task_question"],
                    reason="user asks what to do today rather than reporting completed work",
                )
            ]
        return []
    if active_daily and index != 1 and _looks_like_quantity_correction(whole_text):
        return []
    if active_daily and index != 1 and _looks_like_current_daily_item_retraction(whole_text):
        return []
    if active_daily and index == 1 and _looks_like_current_daily_item_retraction(whole_text):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                whole_text,
                index,
                target_field="unknown",
                write_policy=POLICY_WRITE,
                confidence=0.84,
                reason="user retracts an item from the active daily report",
            )
        ]
    if _looks_like_self_prompted_forgotten_item(whole_text):
        if index != 1:
            return []
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.76,
                safety_flags=["blocks_context_write", "self_prompted_uncertain_item"],
                reason="message is phrased as a self-question rather than a stable daily-report entry",
            )
        ]
    previous_makeup_today = _today_makeup_previous_work_content(whole_text)
    if previous_makeup_today:
        if index != 1:
            return []
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                previous_makeup_today,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                payload={"content": previous_makeup_today},
                confidence=0.86,
                reason="user says a previous work artifact is being completed today",
            )
        ]
    whole_daily_start_payload = _daily_start_payload(whole_text)
    if whole_daily_start_payload:
        if (
            _looks_like_vague_repeat_or_workload_statement(whole_daily_start_payload)
            or _looks_like_emotional_deferral_without_business(whole_daily_start_payload)
            or _looks_like_personal_workplace_rant(whole_daily_start_payload)
            or _looks_like_generic_busy_chatter(whole_daily_start_payload)
            or _looks_like_lifestyle_plan_question(whole_daily_start_payload)
            or _looks_like_travel_application_only(whole_daily_start_payload)
            or _looks_like_reminder_request_only(whole_daily_start_payload)
            or _looks_like_weather_question(whole_daily_start_payload)
            or _looks_like_food_or_rest_plan(whole_daily_start_payload)
            or _looks_like_non_tomorrow_future_deadline(whole_daily_start_payload)
            or _looks_like_moyu_self_deprecation(whole_daily_start_payload)
            or _looks_like_dream_or_fantasy_chatter(whole_daily_start_payload)
            or _looks_like_absurd_content(whole_daily_start_payload)
            or _looks_like_non_substantive_daily_request(whole_daily_start_payload)
            or _looks_like_travel_logistics_only(whole_daily_start_payload)
            or _looks_like_agent_task_instruction(whole_daily_start_payload)
        ) and not _looks_like_copy_previous_daily_request(whole_text):
            if index != 1:
                return []
            return [
                _action(
                    ACTION_SMALL_TALK,
                    WORKFLOW_CHAT,
                    "small_talk",
                    segment,
                    index,
                    write_policy=POLICY_NO_WRITE,
                    confidence=0.82,
                    safety_flags=["blocks_context_write", "non_substantive_daily_payload"],
                    reason="daily-start shell does not include concrete work content",
                )
            ]
        if index != 1:
            return []
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                whole_text,
                index,
                target_field=_daily_field_for_segment(
                    whole_daily_start_payload,
                    whole_text=whole_daily_start_payload,
                    active_daily=active_daily,
                    received_at=received_at,
                    inherited_field=inherited_field,
                )
                or "today_work",
                write_policy=POLICY_WRITE,
                payload={"content": whole_daily_start_payload},
                confidence=0.88,
                reason="user starts a daily report and provides concrete content in the same turn",
            )
        ]
    if _looks_like_daily_start_request(whole_text):
        if index != 1:
            return []
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "start_collection",
                whole_text,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.84,
                reason="user explicitly asks to write or start a daily report",
            )
        ]
    if _looks_like_copy_previous_daily_request(whole_text):
        if index != 1:
            return []
        target_field = _copy_previous_target_field(whole_text)
        safety_flags = ["destructive_or_overwrite"] if target_field == "all" else []
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "copy_previous",
                whole_text,
                index,
                target_field=target_field,
                write_policy=POLICY_WRITE,
                confidence=0.88,
                safety_flags=safety_flags,
                reason="user asks to copy previous daily report content",
            )
        ]
    explicit_repeat_payload = _explicit_yesterday_items_repeated_today(whole_text)
    if explicit_repeat_payload:
        if index != 1:
            return []
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                whole_text,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                payload={"content": explicit_repeat_payload},
                confidence=0.86,
                reason="user states explicit yesterday items and says today repeats them",
            )
        ]
    moved_yesterday_item = _explicit_yesterday_item_moved_to_tomorrow_plan(whole_text)
    if moved_yesterday_item:
        if index != 1:
            return []
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                whole_text,
                index,
                target_field="tomorrow_plan",
                write_policy=POLICY_WRITE,
                payload={"content": moved_yesterday_item},
                confidence=0.84,
                reason="user moves an explicit yesterday report item into today's tomorrow plan",
            )
        ]
    if active_daily and _looks_like_quoted_delete_edit(segment):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                segment,
                index,
                target_field="unknown",
                write_policy=POLICY_WRITE,
                confidence=0.86,
                reason="user edits quoted content in the active daily report",
            )
        ]
    if active_daily and (_looks_like_quantity_correction(segment) or (index == 1 and _looks_like_quantity_correction(whole_text))):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                whole_text if _looks_like_quantity_correction(whole_text) else segment,
                index,
                target_field="unknown",
                write_policy=POLICY_WRITE,
                confidence=0.84,
                reason="user corrects a previously recorded quantity in the active daily report",
            )
        ]
    if _looks_like_meta_test_probe(whole_text) and (
        not _has_reportable_work_piece(whole_text) or _looks_like_explicit_daily_report_probe(whole_text)
    ):
        if index == 1:
            return [
                _action(
                    ACTION_SMALL_TALK,
                    WORKFLOW_CHAT,
                    "small_talk",
                    segment,
                    index,
                    write_policy=POLICY_NO_WRITE,
                    confidence=0.88,
                    safety_flags=["blocks_context_write", "meta_or_routing_request"],
                    reason="user is testing or routing content to another workflow, not providing daily report content",
                )
            ]
        return []
    if _looks_like_future_case_schedule_update(whole_text, whole_text=whole_text, received_at=received_at):
        if index != 1:
            return []
        return _sidecar_candidate_actions(
            whole_text,
            index,
            received_at=received_at,
            reason_prefix="future case schedule is collected as a sidecar candidate, not today's daily work",
        )
    if _looks_like_absurd_content(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_SMALL_TALK,
                    WORKFLOW_CHAT,
                    "small_talk",
                    segment,
                    index,
                    write_policy=POLICY_NO_WRITE,
                    confidence=0.9,
                    safety_flags=["blocks_context_write", "non_work_or_absurd_content"],
                    reason="whole message contains absurd content and must not seed a report",
                )
            ]
        return []
    if _looks_like_previous_daily_plan_completion_question(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_DAILY_READ_HISTORY,
                    WORKFLOW_DAILY_REPORT,
                    "query_history",
                    segment,
                    index,
                    write_policy=POLICY_READ_ONLY,
                    confidence=0.9,
                    safety_flags=["blocks_current_daily_write"],
                    reason="user asks about yesterday's daily plan completion status, not a new daily item",
                )
            ]
        return []
    if _looks_like_completed_previous_plan(whole_text) and not _has_current_work_after_completed_previous_plan(whole_text):
        if index != 1:
            return []
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                whole_text,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                payload={"content": whole_text},
                confidence=0.88,
                reason="user reports yesterday's plan or todo items as completed",
            )
        ]
    conditional_supplement = _conditional_daily_supplement_content(whole_text)
    if conditional_supplement:
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                conditional_supplement,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                target={"target_date": "yesterday"} if _contains_any(_compact(conditional_supplement), ("\u6628\u5929", "\u6628\u65e5")) else None,
                payload={"content": conditional_supplement},
                confidence=0.86,
                safety_flags=["conditional_daily_supplement"],
                reason="user asks to supplement a concrete daily-report item if missing",
            )
        ]
    yesterday_makeup_content = _yesterday_makeup_daily_content(whole_text)
    if yesterday_makeup_content:
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                yesterday_makeup_content,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                target={"target_date": "yesterday"},
                payload={"content": yesterday_makeup_content, "safety_flags": ["conditional_daily_supplement"]},
                confidence=0.87,
                safety_flags=["conditional_daily_supplement"],
                reason="user supplies concrete content while making up yesterday's daily report",
            )
        ]
    if _looks_like_yesterday_work_field_reply(segment):
        content = _yesterday_work_field_content(segment)
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                content,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                target={"target_date": "yesterday"},
                payload={"content": content, "safety_flags": ["conditional_daily_supplement"]},
                confidence=0.86,
                safety_flags=["conditional_daily_supplement"],
                reason="user supplies yesterday-work content while making up yesterday's daily report",
            )
        ]
    if (
        _looks_like_previous_daily_submission_request(whole_text)
        or _looks_like_previous_day_makeup_statement(whole_text)
    ) and not _looks_like_current_daily_explicit_segment(segment):
        if index == 1:
            return [
                _action(
                    ACTION_DAILY_READ_HISTORY,
                    WORKFLOW_DAILY_REPORT,
                    "begin_edit",
                    segment,
                    index,
                    write_policy=POLICY_READ_ONLY,
                    confidence=0.9,
                    safety_flags=["blocks_current_daily_write"],
                    reason="user refers to yesterday's daily report, which must not be written into today's draft",
                )
            ]
        return []
    if _looks_like_standalone_past_work_without_current_context(segment, whole_text=whole_text, received_at=received_at):
        return [
            _action(
                ACTION_DAILY_READ_HISTORY,
                WORKFLOW_DAILY_REPORT,
                "historical_reference",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.84,
                safety_flags=["blocks_current_daily_write", "historical_reference"],
                reason="standalone past-day work statement should not mutate today's draft",
            )
        ]
    if _looks_like_historical_previous_plan_statement(segment):
        return [
            _action(
                ACTION_DAILY_READ_HISTORY,
                WORKFLOW_DAILY_REPORT,
                "historical_reference",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.82,
                safety_flags=["blocks_current_daily_write", "historical_reference"],
                reason="segment describes yesterday's recorded plan as context and must not overwrite history",
            )
        ]
    if _looks_like_ambiguous_that_day_daily_reference(segment):
        return [
            _action(
                ACTION_DISAMBIGUATION_REQUIRED,
                WORKFLOW_UNKNOWN_OR_HELP,
                "clarify_action",
                segment,
                index,
                write_policy=POLICY_PENDING,
                confidence=0.8,
                safety_flags=["ambiguous_daily_context"],
                reason="deictic day reference is too ambiguous to write into a daily report",
            )
        ]
    if _looks_like_today_yesterday_ambiguous_reference(segment) or _looks_like_current_work_retracted_to_yesterday(whole_text):
        return [
            _action(
                ACTION_DISAMBIGUATION_REQUIRED,
                WORKFLOW_UNKNOWN_OR_HELP,
                "clarify_action",
                segment,
                index,
                write_policy=POLICY_PENDING,
                confidence=0.78,
                safety_flags=["ambiguous_daily_context"],
                reason="date reference is too ambiguous to write into today's daily report",
            )
        ]
    if _looks_like_overtime_status_not_reportable(whole_text):
        if index != 1:
            return []
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.78,
                safety_flags=["blocks_context_write", "overtime_status_chatter"],
                reason="message describes fatigue or rest around overtime rather than a stable daily-report item",
            )
        ]
    if (
        _looks_like_assistant_feedback(segment)
        and not _daily_edit_allowed(segment, active_daily=active_daily)
        and not _looks_like_business_problem_statement(segment)
    ):
        return [
            _action(
                ACTION_ASSISTANT_FEEDBACK,
                WORKFLOW_CHAT,
                "feedback",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.94,
                safety_flags=["blocks_context_write"],
                reason="user is reacting to bot output, not providing report content",
            )
        ]
    if _looks_like_meta_test_probe(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.88,
                safety_flags=["blocks_context_write", "meta_test_probe"],
                reason="user is testing the assistant interaction, not providing report content",
            )
        ]
    if _looks_like_previous_daily_submission_request(segment) or _looks_like_previous_daily_plan_completion_question(segment):
        return [
            _action(
                ACTION_DAILY_READ_HISTORY,
                WORKFLOW_DAILY_REPORT,
                "begin_edit",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.9,
                safety_flags=["blocks_current_daily_write"],
                reason="user refers to yesterday's daily report, which must not be written into today's draft",
            )
        ]
    if _looks_like_daily_meta_status_statement(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.9,
                safety_flags=["blocks_context_write", "daily_meta_status"],
                reason="user mentions writing the daily report but does not provide report content",
            )
        ]
    if _looks_like_report_later_deferral(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.86,
                safety_flags=["blocks_context_write", "report_later_deferral"],
                reason="user says to report later after a result is available, not to write current daily content",
            )
        ]
    if _looks_like_absurd_content(segment) or _looks_like_lifestyle_report_meta_chatter(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.9,
                safety_flags=["blocks_context_write", "non_work_or_absurd_content"],
                reason="segment is lifestyle chatter or absurd content, not reportable work",
            )
        ]
    if _looks_like_empty_daily_content(segment) or _looks_like_unfinished_daily_shell(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.82,
                safety_flags=["blocks_context_write", "empty_daily_content"],
                reason="segment says there is no concrete work content to record",
            )
        ]
    if _looks_like_lifestyle_question_turn(whole_text) and not _has_reportable_work_piece(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_SMALL_TALK,
                    WORKFLOW_CHAT,
                    "small_talk",
                    segment,
                    index,
                    write_policy=POLICY_NO_WRITE,
                    confidence=0.84,
                    safety_flags=["blocks_context_write", "lifestyle_question"],
                    reason="whole turn is a lifestyle question with incidental work context, not a daily update",
                )
            ]
        return []
    if _looks_like_business_reference_question_turn(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_INTERNAL_QA,
                    WORKFLOW_INTERNAL_QA,
                    "answer_question",
                    whole_text,
                    index,
                    write_policy=POLICY_READ_ONLY,
                    confidence=0.82,
                    safety_flags=["blocks_context_write", "business_reference_question"],
                    reason="whole turn asks about a business record or clause, not a daily update",
                )
            ]
        return []
    if _looks_like_schedule_check_or_conditional_plan(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_INTERNAL_QA,
                    WORKFLOW_INTERNAL_QA,
                    "answer_question",
                    segment,
                    index,
                    write_policy=POLICY_READ_ONLY,
                    confidence=0.82,
                    safety_flags=["blocks_context_write", "conditional_plan_not_committed"],
                    reason="user asks to check schedule before deciding whether to do the work",
                )
            ]
        return []
    if _looks_like_assistant_arrangement_request(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_INTERNAL_QA,
                    WORKFLOW_INTERNAL_QA,
                    "answer_question",
                    whole_text,
                    index,
                    write_policy=POLICY_READ_ONLY,
                    confidence=0.78,
                    safety_flags=["blocks_context_write", "assistant_arrangement_request"],
                    reason="whole turn asks the assistant to arrange something, not to record completed work",
                )
            ]
        return []
    if _looks_like_process_help_question(whole_text) and not _has_reportable_work_piece(whole_text):
        if index == 1:
            return [
                _action(
                    ACTION_INTERNAL_QA,
                    WORKFLOW_INTERNAL_QA,
                    "answer_question",
                    whole_text,
                    index,
                    write_policy=POLICY_READ_ONLY,
                    confidence=0.84,
                    safety_flags=["blocks_context_write", "process_help_question"],
                    reason="whole turn asks how to do a process and must not seed daily-plan fragments",
                )
            ]
        return []
    if _looks_like_process_help_question(segment) or _looks_like_reimbursement_policy_question(segment) or _looks_like_daily_meta_or_date_question(segment):
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.82,
                safety_flags=["blocks_context_write", "question_not_daily_content"],
                reason="segment asks a question and must not be written into the daily report",
            )
        ]
    if _looks_like_active_daily_add_instruction(segment):
        content = _active_daily_add_content(segment)
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                content,
                index,
                target_field=_daily_field_for_segment(
                    segment,
                    whole_text=whole_text,
                    active_daily=active_daily,
                    received_at=received_at,
                    inherited_field=inherited_field,
                )
                or "today_work",
                write_policy=POLICY_WRITE,
                payload={"content": content},
                confidence=0.84,
                reason="user explicitly asks to add a concrete item into the daily report",
            )
        ]
    if active_daily and _looks_like_active_daily_concrete_reminder(segment):
        content = _active_daily_reminder_content(segment)
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                content,
                index,
                target_field=inherited_field if inherited_field in {"today_work", "problems", "tomorrow_plan"} else "today_work",
                write_policy=POLICY_WRITE,
                payload={"content": content, "safety_flags": ["active_daily_concrete_reminder"]},
                confidence=0.8,
                safety_flags=["active_daily_concrete_reminder"],
                reason="active daily context receives a concrete reminder item",
            )
        ]
    if _looks_like_tentative_case_resolution_chatter(segment):
        return [
            _action(
                ACTION_CASE_PROGRESS,
                WORKFLOW_CASE_PROGRESS,
                "upsert_progress_candidate",
                segment,
                index,
                write_policy=POLICY_SANDBOX,
                confidence=0.72,
                requires_confirmation=True,
                safety_flags=["case_progress_candidate", "blocks_daily_write"],
                reason="segment mentions a tentative case resolution but no completed daily work",
            )
        ]
    if _looks_like_monthly_coordination_followup(segment):
        return [
            _action(
                ACTION_MONTHLY_STATUS_QUERY,
                WORKFLOW_MONTHLY_REPORT,
                "query_status",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.82,
                safety_flags=["blocks_context_write", "monthly_coordination_followup"],
                reason="segment follows monthly-report coordination semantics, not daily-report content",
            )
        ]
    if _looks_like_non_substantive_daily_request(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.82,
                safety_flags=["blocks_context_write", "non_substantive_daily_request"],
                reason="user mentions daily report without concrete report content",
            )
        ]
    if _contains_any(_compact(segment), ("\u8bf4\u4e0d\u4e0a\u6765", "\u53cd\u6b63\u4e0d\u5927", "\u53ef\u80fd\u660e\u5929\u5c31\u597d")) and _contains_any(_compact(segment), ("\u98ce\u9669", "\u95ee\u9898")):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.78,
                safety_flags=["blocks_context_write", "vague_risk_without_detail"],
                reason="risk wording is too vague to write into daily report",
            )
        ]
    if active_monthly and _looks_like_monthly_collection_fragment(segment):
        return [
            _action(
                ACTION_DISAMBIGUATION_REQUIRED,
                WORKFLOW_UNKNOWN_OR_HELP,
                "clarify_action",
                segment,
                index,
                write_policy=POLICY_PENDING,
                confidence=0.78,
                safety_flags=["active_monthly_context", "not_daily_content"],
                reason="active monthly task receives a terse metric reply fragment; do not treat it as daily/chat",
            )
        ]
    if _looks_like_vague_repeat_or_workload_statement(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.78,
                safety_flags=["blocks_context_write", "vague_workload_statement"],
                reason="segment refers to workload or sameness without a concrete work item",
            )
        ]
    if _looks_like_travel_logistics_only(segment) or (
        _looks_like_agent_task_instruction(segment) and not (active_daily and _looks_like_symbolic_replacement_edit(segment))
    ):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.78,
                safety_flags=["blocks_context_write", "non_daily_instruction_or_logistics"],
                reason="segment is a logistics reminder or instruction to the agent rather than a daily report entry",
            )
        ]
    if _looks_like_referential_write_reminder(segment) and not _looks_like_business_problem_statement(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.78,
                safety_flags=["blocks_context_write", "referential_write_reminder_without_content"],
                reason="segment only reminds the assistant to write a previous risk/problem without restating concrete content",
            )
        ]
    conditional_supplement = _conditional_daily_supplement_content(segment)
    if conditional_supplement:
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                conditional_supplement,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                target={"target_date": "yesterday"} if _contains_any(_compact(segment), ("\u6628\u5929", "\u6628\u65e5")) else None,
                payload={"content": conditional_supplement},
                confidence=0.86,
                safety_flags=["conditional_daily_supplement"],
                reason="user asks to supplement a concrete daily-report item if missing",
            )
        ]
    if _looks_like_case_reply_status_question(whole_text):
        if index != 1:
            return []
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                whole_text,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.84,
                safety_flags=["blocks_context_write", "whole_turn_question"],
                reason="user asks for case or counterpart reply status rather than reporting work",
            )
        ]
    if _looks_like_daily_date_classification_question(whole_text):
        if index != 1:
            return []
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                whole_text,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.8,
                safety_flags=["blocks_context_write", "daily_date_classification_question"],
                reason="user asks which report date a work item belongs to rather than giving a direct write command",
            )
        ]
    if (
        index == 1
        and not _has_daily_time_anchor(whole_text)
        and not _looks_like_monthly_status_query(whole_text)
        and not _looks_like_daily_current_query(whole_text, active_daily=active_daily)
        and not _looks_like_daily_history_query(whole_text)
        and not _looks_like_explicit_legal_research_request(whole_text)
        and not _looks_like_lifestyle_question_turn(whole_text)
        and not _looks_like_small_talk(whole_text)
        and not _looks_like_mixed_question_with_daily_business(whole_text)
        and not _looks_like_business_problem_statement(whole_text)
        and not _looks_like_daily_problem_reply(whole_text, active_daily=active_daily)
        and _looks_like_question(whole_text)
    ):
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                whole_text,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.82,
                safety_flags=["blocks_context_write", "whole_turn_question"],
                reason="whole turn is a question and should not be split into daily content",
            )
        ]
    if (
        index > 1
        and not _has_daily_time_anchor(whole_text)
        and not _looks_like_monthly_status_query(whole_text)
        and not _looks_like_daily_current_query(whole_text, active_daily=active_daily)
        and not _looks_like_daily_history_query(whole_text)
        and not _looks_like_explicit_legal_research_request(whole_text)
        and not _looks_like_lifestyle_question_turn(whole_text)
        and not _looks_like_small_talk(whole_text)
        and not _looks_like_mixed_question_with_daily_business(whole_text)
        and not _looks_like_business_problem_statement(whole_text)
        and _looks_like_question(whole_text)
    ):
        return []
    if _looks_like_weak_travel_destination_fragment(segment):
        return [
            _action(
                ACTION_DISAMBIGUATION_REQUIRED,
                WORKFLOW_UNKNOWN_OR_HELP,
                "clarify_action",
                segment,
                index,
                write_policy=POLICY_PENDING,
                confidence=0.76,
                safety_flags=["weak_travel_fragment"],
                reason="segment only supplies a destination without a concrete work activity",
            )
        ]
    if _looks_like_case_schedule_only_fragment(segment):
        return []
    if _looks_like_case_metric_data_request(segment):
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.86,
                reason="user asks for case metric data rather than the current daily draft",
            )
        ]
    if _looks_like_history_info_delivery_request(segment):
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.84,
                safety_flags=["blocks_context_write", "history_info_request"],
                reason="user asks to retrieve historical work/case information rather than write a daily report",
            )
        ]
    if _looks_like_daily_edit_entry_request(segment):
        return [
            _action(
                ACTION_DAILY_READ_HISTORY,
                WORKFLOW_DAILY_REPORT,
                "begin_edit",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.88,
                reason="user opens a dated daily-report edit context but has not provided a concrete edit yet",
            )
        ]
    if _looks_like_daily_history_query(segment):
        return [
            _action(
                ACTION_DAILY_READ_HISTORY,
                WORKFLOW_DAILY_REPORT,
                "query_history",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.9,
                reason="user asks to view a dated or historical daily report",
            )
        ]
    if _looks_like_historical_daily_risk_lookup(segment):
        return [
            _action(
                ACTION_DAILY_READ_HISTORY,
                WORKFLOW_DAILY_REPORT,
                "query_history",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.88,
                reason="user asks to retrieve a risk/problem item from a historical daily report",
            )
        ]
    if _looks_like_case_progress_routing_request(segment):
        return [
            _action(
                ACTION_CASE_PROGRESS,
                WORKFLOW_CASE_PROGRESS,
                "append_case_progress",
                segment,
                index,
                write_policy=POLICY_SANDBOX,
                target={"matter_hint": _specific_matter_hint(segment)},
                confidence=0.82,
                requires_confirmation=True,
                reason="user explicitly asks to record a case-progress update",
            )
        ]
    if _looks_like_daily_clear_request(segment, active_daily=active_daily):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "clear",
                segment,
                index,
                target_field=_clear_target_field(segment),
                write_policy=POLICY_WRITE,
                confidence=0.9,
                safety_flags=["destructive_or_overwrite"],
                reason="user asks to clear a daily report or daily-report field",
            )
        ]
    if active_daily and _looks_like_risk_field_delete_request(segment):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "clear",
                segment,
                index,
                target_field="problems",
                write_policy=POLICY_WRITE,
                confidence=0.88,
                safety_flags=["destructive_or_overwrite", "risk_field_delete"],
                reason="user asks to remove the current daily report risk/problem field",
            )
        ]
    if active_daily and _looks_like_risk_field_delete_request(whole_text) and _looks_like_resolved_risk_update(segment):
        return []
    if _looks_like_daily_current_query(segment, active_daily=active_daily):
        return [
            _action(
                ACTION_DAILY_READ_CURRENT,
                WORKFLOW_DAILY_REPORT,
                "query_current",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.88,
                reason="user asks to view the current daily draft",
            )
        ]
    if _looks_like_daily_confirm_request(
        segment,
        active_daily=active_daily,
        daily_confirmation_pending=daily_confirmation_pending,
    ):
        return [
            _action(
                ACTION_DAILY_CONFIRM,
                WORKFLOW_DAILY_REPORT,
                "confirm",
                segment,
                index,
                target_field="all",
                write_policy=POLICY_WRITE,
                confidence=0.92,
                reason="user confirms the active daily report operation",
            )
        ]
    if _looks_like_cross_date_daily_copy_request(segment):
        return [
            _action(
                ACTION_DISAMBIGUATION_REQUIRED,
                WORKFLOW_UNKNOWN_OR_HELP,
                "clarify_action",
                segment,
                index,
                write_policy=POLICY_PENDING,
                confidence=0.84,
                safety_flags=["unsupported_cross_date_daily_copy"],
                reason="copying daily report content between two historical dates needs a source and target date contract",
            )
        ]
    if active_daily and _looks_like_affirm_same_as_previous_daily(segment):
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "copy_previous",
                segment,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                confidence=0.78,
                reason="active daily context treats affirmative sameness as copying previous work",
            )
        ]
    if _looks_like_copy_previous_daily_request(segment):
        target_field = _copy_previous_target_field(segment)
        safety_flags = ["destructive_or_overwrite"] if target_field == "all" else []
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "copy_previous",
                segment,
                index,
                target_field=target_field,
                write_policy=POLICY_WRITE,
                confidence=0.88,
                safety_flags=safety_flags,
                reason="user asks to copy previous daily report content",
            )
        ]
    if _looks_like_copy_current_work_to_tomorrow(segment):
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "copy_current_to_tomorrow",
                segment,
                index,
                target_field="tomorrow_plan",
                write_policy=POLICY_WRITE,
                confidence=0.88,
                safety_flags=["copy_current_work_to_tomorrow"],
                reason="user asks to copy current today-work draft into tomorrow plan",
            )
        ]
    if _looks_like_daily_editorial_edit_request(segment, active_daily=active_daily):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                segment,
                index,
                target_field="unknown",
                write_policy=POLICY_WRITE,
                confidence=0.86,
                reason="user asks to polish or correct existing daily report wording",
            )
        ]
    if _looks_like_meta_conversation_request(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.84,
                safety_flags=["blocks_context_write", "meta_request"],
                reason="user asks the assistant to summarize or discuss content, not to record a report field",
            )
        ]
    if _looks_like_referential_write_reminder(segment) and not _looks_like_business_problem_statement(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.78,
                safety_flags=["blocks_context_write", "referential_write_reminder_without_content"],
                reason="segment only reminds the assistant to write a previous risk/problem without restating concrete content",
            )
        ]
    if _looks_like_explicit_legal_research_request(segment):
        return [
            _action(
                ACTION_LEGAL_RESEARCH,
                WORKFLOW_LEGAL_RESEARCH,
                "run_research",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.86,
                reason="user asks the assistant to perform legal research",
            )
        ]
    if _looks_like_assistant_service_request(segment):
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.8,
                safety_flags=["blocks_context_write", "assistant_service_request"],
                reason="user asks the assistant to do or check something; it is not the user's daily work item",
            )
        ]
    if _looks_like_completed_previous_plan_without_new_content(whole_text):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.76,
                safety_flags=["blocks_context_write", "completed_previous_plan_without_new_content"],
                reason="previous plan completion has no concrete current work content to write",
            )
        ]
    if active_daily and index == 1 and _looks_like_office_device_correction_edit(whole_text):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                whole_text,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                confidence=0.78,
                safety_flags=["contextual_correction_edit"],
                reason="active daily context receives a correction to a previously reported office-device issue",
            )
        ]
    if _looks_like_external_report_edit_request(whole_text) or _looks_like_office_device_chatter(segment):
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.78,
                safety_flags=["blocks_context_write", "non_daily_operational_chatter"],
                reason="segment is not a daily-report work item",
            )
        ]
    if _looks_like_monthly_meta_request(segment) or _looks_like_daily_bot_feedback(whole_text):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.78,
                safety_flags=["blocks_context_write"],
                reason="segment is meta chatter about reporting rather than report content",
            )
        ]
    if _looks_like_problem_question(segment):
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.82,
                safety_flags=["blocks_context_write", "problem_question"],
                reason="user asks whether something has a problem; it is not a problem/risk field reply",
            )
        ]
    if (
        _looks_like_daily_problem_reply(segment, active_daily=active_daily)
        and inherited_field not in {"today_work", "tomorrow_plan"}
        and not _has_current_work_after_completed_previous_plan(whole_text)
    ):
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                segment,
                index,
                target_field="problems",
                write_policy=POLICY_WRITE,
                confidence=0.86,
                reason="segment is a problem/risk field reply in active daily context",
            )
        ]
    if _looks_like_short_leave_daily_entry(segment):
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                segment,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                confidence=0.82,
                reason="short leave/status fragment is accepted as daily content",
            )
        ]
    if _looks_like_monthly_status_query(segment):
        return [
            _action(
                ACTION_MONTHLY_STATUS_QUERY,
                WORKFLOW_MONTHLY_REPORT,
                "query_status",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.86,
                safety_flags=["blocks_context_write"],
                reason="user asks for monthly-report collection status",
            )
        ]
    if _looks_like_monthly_reply(segment, active_monthly=active_monthly):
        return [
            _action(
                ACTION_MONTHLY_REPLY,
                WORKFLOW_MONTHLY_REPORT,
                "capture_reply",
                segment,
                index,
                write_policy=POLICY_WRITE,
                confidence=0.88,
                requires_confirmation=True,
                reason="segment matches a monthly metric reply",
            )
        ]
    if _looks_like_weekly_request(segment):
        return [
            _action(
                ACTION_WEEKLY_REQUEST,
                WORKFLOW_WEEKLY_REPORT,
                "draft_or_query_weekly",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.84,
                reason="user asks for a weekly report or weekly summary",
            )
        ]
    daily_start_payload = _daily_start_payload(segment)
    if daily_start_payload:
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                segment,
                index,
                target_field=_daily_field_for_segment(
                    daily_start_payload,
                    whole_text=daily_start_payload,
                    active_daily=active_daily,
                    received_at=received_at,
                    inherited_field=inherited_field,
                )
                or "today_work",
                write_policy=POLICY_WRITE,
                payload={"content": daily_start_payload},
                confidence=0.88,
                reason="user starts a daily report and provides concrete content in the same turn",
            )
        ]
    if _looks_like_daily_start_request(segment):
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "start_collection",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.84,
                reason="user explicitly asks to write or start a daily report",
            )
        ]
    if _looks_like_internal_qa(segment) and not (
        active_daily and (_has_reportable_work_piece(segment) or _daily_edit_allowed(segment, active_daily=True))
    ):
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.82,
                reason="user asks a question rather than providing report content",
            )
        ]
    if _looks_like_process_learning_context(segment):
        return [
            _action(
                ACTION_INTERNAL_QA,
                WORKFLOW_INTERNAL_QA,
                "answer_question",
                segment,
                index,
                write_policy=POLICY_READ_ONLY,
                confidence=0.78,
                safety_flags=["blocks_context_write", "process_learning_context"],
                reason="user is discussing how to understand a process, not committing a daily-report item",
            )
        ]
    if active_daily and _looks_like_negative_replacement(segment):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                segment,
                index,
                target_field=_daily_field_for_segment(
                    segment,
                    whole_text=whole_text,
                    active_daily=active_daily,
                    received_at=received_at,
                    inherited_field=inherited_field,
                )
                or "unknown",
                write_policy=POLICY_WRITE,
                confidence=0.86,
                reason="segment corrects existing daily report content with a negative replacement",
            )
        ]
    if _looks_like_completed_previous_plan(segment):
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                segment,
                index,
                target_field="today_work",
                write_policy=POLICY_WRITE,
                confidence=0.88,
                reason="user reports yesterday's plan or todo items as completed",
            )
        ]
    if _looks_like_document_correction_problem(segment):
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                segment,
                index,
                target_field="problems",
                write_policy=POLICY_WRITE,
                confidence=0.86,
                reason="segment reports a business document correction risk",
            )
        ]
    if _daily_edit_allowed(segment, active_daily=active_daily):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                segment,
                index,
                target_field=_daily_field_for_segment(
                    segment,
                    whole_text=whole_text,
                    active_daily=active_daily,
                    received_at=received_at,
                    inherited_field=inherited_field,
                )
                or "unknown",
                write_policy=POLICY_WRITE,
                confidence=0.86,
                reason="segment edits existing daily report content in active daily context",
            )
        ]
    if _looks_like_case_progress_only_update(segment):
        return _sidecar_candidate_actions(
            segment,
            index,
            received_at=received_at,
            reason_prefix="case progress update is collected as a sidecar candidate, not direct daily content",
        )
    if _looks_like_case_candidate_detail_only(segment):
        return _sidecar_candidate_actions(
            segment,
            index,
            received_at=received_at,
            reason_prefix="case/travel follow-up detail is collected as a sidecar candidate, not direct daily content",
        )
    if _looks_like_future_case_schedule_update(segment, whole_text=whole_text, received_at=received_at):
        return _sidecar_candidate_actions(
            segment,
            index,
            received_at=received_at,
            reason_prefix="future case schedule is collected as a sidecar candidate, not today's daily work",
        )
    if _looks_like_legal_document_work(segment):
        if _blocks_current_daily_for_relative_date(segment, whole_text=whole_text, received_at=received_at):
            return _sidecar_candidate_actions(
                segment,
                index,
                received_at=received_at,
                reason_prefix="future-dated legal/case work is not written to daily report",
            )
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                segment,
                index,
                target_field=_daily_field_for_segment(
                    segment,
                    whole_text=whole_text,
                    active_daily=active_daily,
                    received_at=received_at,
                    inherited_field=inherited_field,
                )
                or "today_work",
                write_policy=POLICY_WRITE,
                confidence=0.86,
                reason="segment contains legal-document operational work",
            )
        ]
    if _looks_like_active_daily_add_instruction(segment):
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                _active_daily_add_content(segment),
                index,
                target_field=_daily_field_for_segment(
                    segment,
                    whole_text=whole_text,
                    active_daily=active_daily,
                    received_at=received_at,
                    inherited_field=inherited_field,
                )
                or "today_work",
                write_policy=POLICY_WRITE,
                payload={"content": _active_daily_add_content(segment)},
                confidence=0.84,
                reason="user explicitly asks to add a concrete item into the daily report",
            )
        ]
    if active_daily and _looks_like_contextual_business_detail(segment):
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                segment,
                index,
                target_field=inherited_field if inherited_field in {"today_work", "problems", "tomorrow_plan"} else "today_work",
                write_policy=POLICY_WRITE,
                confidence=0.8,
                reason="active daily context receives a concrete business detail continuation",
            )
        ]
    if _looks_like_case_progress_only_update(whole_text) and _contains_any(segment, ("\u6536\u5230", "\u4f20\u7968", "\u6cd5\u9662\u901a\u77e5", "\u901a\u77e5", "\u6392\u671f", "\u5f8b\u5e08", "\u548c\u89e3\u534f\u8bae", "\u53d1\u6765")):
        return []
    if _looks_like_case_progress_continuation_without_daily_time(segment):
        return [
            _action(
                ACTION_CASE_PROGRESS,
                WORKFLOW_CASE_PROGRESS,
                "append_case_progress",
                segment,
                index,
                write_policy=POLICY_SANDBOX,
                confidence=0.76,
                requires_confirmation=True,
                reason="untimed case-progress continuation is collected as a sidecar candidate, not daily work",
            )
        ]
    if _looks_like_emotional_customer_phone_only(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.76,
                safety_flags=["blocks_context_write", "emotional_phone_chatter"],
                reason="customer-phone mention is emotional chatter without a concrete work outcome",
            )
        ]
    if _looks_like_untimed_business_work_update(segment) and not (
        _looks_like_system_rant_only(segment)
        or _looks_like_case_progress_routing_request(segment)
        or _looks_like_meta_test_probe(segment)
    ):
        actions = []
        if not _blocks_current_daily_for_relative_date(segment, whole_text=whole_text, received_at=received_at):
            actions.append(
                _action(
                    ACTION_DAILY_WRITE,
                    WORKFLOW_DAILY_REPORT,
                    "fill",
                    segment,
                    index,
                    target_field=inherited_field if inherited_field in {"today_work", "problems", "tomorrow_plan"} else "today_work",
                    write_policy=POLICY_WRITE,
                    confidence=0.84,
                    reason="segment contains an operational work update without an explicit date anchor",
                )
            )
        matter_hint = _specific_matter_hint(segment)
        if matter_hint:
            actions.append(
                _action(
                    ACTION_CASE_PROGRESS,
                    WORKFLOW_CASE_PROGRESS,
                    "append_case_progress",
                    segment,
                    index,
                    write_policy=POLICY_SANDBOX,
                    target={"matter_hint": matter_hint},
                    confidence=0.78,
                    requires_confirmation=True,
                    reason="segment mentions a specific matter for case-progress collection",
                )
            )
        if _looks_like_travel_event(segment):
            actions.append(
                _action(
                    ACTION_TRAVEL_COORDINATION,
                    WORKFLOW_TRAVEL_COORDINATION,
                    "upsert_travel_plan",
                    segment,
                    index,
                    write_policy=POLICY_SANDBOX,
                    target={
                        "destination": _travel_destination(segment),
                        "date_hint": _date_hint(segment, received_at=received_at),
                        "status": _travel_status(segment, received_at=received_at),
                    },
                    confidence=0.86,
                    requires_confirmation=True,
                    reason="segment contains a concrete trip for travel coordination",
                )
            )
        return actions
    if _looks_like_lifestyle_chatter(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.84,
                safety_flags=["blocks_context_write"],
                reason="segment is a lifestyle question without work evidence",
            )
        ]
    if _daily_edit_allowed(segment, active_daily=active_daily):
        return [
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                segment,
                index,
                target_field=_daily_field_for_segment(
                    segment,
                    whole_text=whole_text,
                    active_daily=active_daily,
                    received_at=received_at,
                    inherited_field=inherited_field,
                )
                or "unknown",
                write_policy=POLICY_WRITE,
                confidence=0.86,
                reason="segment edits existing daily report content in active daily context",
            )
        ]
    if _looks_like_non_work_chatter(segment, whole_text=whole_text):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.76,
                safety_flags=["blocks_context_write"],
                reason="segment is personal chatter and not work-report content",
            )
        ]
    if _looks_like_bare_daily_fragment(segment, active_daily=active_daily, whole_text=whole_text):
        return [
            _action(
                ACTION_DISAMBIGUATION_REQUIRED,
                WORKFLOW_UNKNOWN_OR_HELP,
                "clarify_action",
                segment,
                index,
                write_policy=POLICY_PENDING,
                confidence=0.72,
                safety_flags=["ambiguous_daily_context"],
                reason="short daily-looking fragment has no active daily context",
            )
        ]
    if _looks_like_document_business_revision(segment):
        target_field = _daily_field_for_segment(
            segment,
            whole_text=whole_text,
            active_daily=active_daily,
            received_at=received_at,
            inherited_field=inherited_field,
        ) or "today_work"
        return [
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                segment,
                index,
                target_field=target_field,
                write_policy=POLICY_WRITE,
                confidence=0.84,
                reason="segment describes a business document revision, not an edit to the daily report draft",
            )
        ]
    if _looks_like_daily_edit(segment) and not _daily_edit_allowed(segment, active_daily=active_daily) and not _looks_like_document_business_revision(segment):
        return [
            _action(
                ACTION_DISAMBIGUATION_REQUIRED,
                WORKFLOW_UNKNOWN_OR_HELP,
                "clarify_action",
                segment,
                index,
                write_policy=POLICY_PENDING,
                confidence=0.74,
                safety_flags=["ambiguous_daily_edit_without_context"],
                reason="daily edit command has no active or explicit report target",
            )
        ]
    if _looks_like_ambiguous_legal_research(segment) and not (
        whole_text != segment and _has_reportable_work_piece(whole_text)
    ):
        return [
            _action(
                ACTION_DISAMBIGUATION_REQUIRED,
                WORKFLOW_UNKNOWN_OR_HELP,
                "clarify_action",
                segment,
                index,
                write_policy=POLICY_PENDING,
                confidence=0.78,
                safety_flags=["ambiguous_daily_vs_research"],
                reason="legal-research words appear without an assistant request or daily time anchor",
            )
        ]
    actions: list[UserAction] = []
    daily_field = _daily_field_for_segment(
        segment,
        whole_text=whole_text,
        active_daily=active_daily,
        received_at=received_at,
        inherited_field=inherited_field,
    )
    if daily_field:
        actions.append(
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                segment,
                index,
                target_field=daily_field,
                write_policy=POLICY_WRITE,
                confidence=0.84,
                reason="segment contains daily-report work, problem, or plan content",
            )
        )
    if _daily_edit_allowed(segment, active_daily=active_daily):
        actions.append(
            _action(
                ACTION_DAILY_EDIT,
                WORKFLOW_DAILY_REPORT,
                "edit",
                segment,
                index,
                target_field=_daily_field_for_segment(
                    segment,
                    whole_text=whole_text,
                    active_daily=active_daily,
                    received_at=received_at,
                    inherited_field=inherited_field,
                )
                or "unknown",
                write_policy=POLICY_WRITE,
                confidence=0.84,
                reason="segment edits existing daily report content",
            )
        )
    if _looks_like_travel_event(segment):
        actions.append(
            _action(
                ACTION_TRAVEL_COORDINATION,
                WORKFLOW_TRAVEL_COORDINATION,
                "upsert_travel_plan",
                segment,
                index,
                write_policy=POLICY_SANDBOX,
                target={
                    "destination": _travel_destination(segment),
                    "date_hint": _date_hint(segment, received_at=received_at),
                    "status": _travel_status(segment, received_at=received_at),
                },
                confidence=0.86,
                requires_confirmation=True,
                reason="segment contains a concrete trip for travel coordination",
            )
        )
    matter_hint = _specific_matter_hint(segment)
    if matter_hint:
        actions.append(
            _action(
                ACTION_CASE_PROGRESS,
                WORKFLOW_CASE_PROGRESS,
                "append_case_progress",
                segment,
                index,
                write_policy=POLICY_SANDBOX,
                target={"matter_hint": matter_hint},
                confidence=0.78,
                requires_confirmation=True,
                reason="segment mentions a specific matter for case-progress collection",
            )
        )
    if actions:
        return actions
    if _looks_like_small_talk(segment):
        return [
            _action(
                ACTION_SMALL_TALK,
                WORKFLOW_CHAT,
                "small_talk",
                segment,
                index,
                write_policy=POLICY_NO_WRITE,
                confidence=0.7,
                safety_flags=["blocks_context_write"],
                reason="segment is small talk or non-workflow chatter",
            )
        ]
    return []


def _sidecar_candidate_actions(
    segment: str,
    index: int,
    *,
    received_at: Any = None,
    reason_prefix: str,
) -> list[UserAction]:
    actions: list[UserAction] = []
    if _looks_like_travel_event(segment):
        actions.append(
            _action(
                ACTION_TRAVEL_COORDINATION,
                WORKFLOW_TRAVEL_COORDINATION,
                "upsert_travel_plan",
                segment,
                index,
                write_policy=POLICY_SANDBOX,
                target={
                    "destination": _travel_destination(segment),
                    "date_hint": _date_hint(segment, received_at=received_at),
                    "status": _travel_status(segment, received_at=received_at),
                },
                confidence=0.84,
                requires_confirmation=True,
                reason=f"{reason_prefix}; segment contains a concrete trip for travel coordination",
            )
        )
    matter_hint = _specific_matter_hint(segment)
    if matter_hint:
        actions.append(
            _action(
                ACTION_CASE_PROGRESS,
                WORKFLOW_CASE_PROGRESS,
                "append_case_progress",
                segment,
                index,
                write_policy=POLICY_SANDBOX,
                target={"matter_hint": matter_hint},
                confidence=0.78,
                requires_confirmation=True,
                reason=f"{reason_prefix}; segment mentions a specific matter for case-progress collection",
            )
        )
    compact = _compact(segment)
    if _contains_any(compact, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f")) and (
        _contains_any(compact, ("\u5f00\u5ead", "\u51fa\u5ead", "\u51c6\u5907\u6750\u6599", "\u5ead\u524d\u6750\u6599"))
        or (_has_business_work_action(segment) and _has_business_work_object(segment))
    ):
        actions.append(
            _action(
                ACTION_DAILY_WRITE,
                WORKFLOW_DAILY_REPORT,
                "fill",
                segment,
                index,
                target_field="tomorrow_plan",
                write_policy=POLICY_WRITE,
                target={"target_date": "tomorrow"},
                confidence=0.82,
                reason=f"{reason_prefix}; tomorrow case schedule is also a daily tomorrow-plan item",
            )
        )
    return actions


def _action(
    action_type: str,
    workflow: str,
    operation: str,
    segment: str,
    index: int,
    *,
    target_field: str = "none",
    write_policy: str,
    target: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    confidence: float,
    requires_confirmation: bool = False,
    safety_flags: list[str] | None = None,
    reason: str,
) -> UserAction:
    payload_data = dict(payload or {})
    payload_data.setdefault("content", segment)
    return UserAction(
        action_type=action_type,
        workflow=workflow,
        operation=operation,
        target_field=target_field,
        write_policy=write_policy,
        target=dict(target or {}),
        payload=payload_data,
        confidence=confidence,
        requires_confirmation=requires_confirmation,
        source_segment_index=index,
        source_text_hash=_hash_text(segment),
        source_text_chars=len(segment),
        safety_flags=list(safety_flags or []),
        reason=reason,
    )


def _split_segments(raw_text: str) -> list[str]:
    return [frame.text for frame in _split_segment_frames(raw_text)]


def split_user_action_segments(raw_text: str, *, received_at: Any = None) -> list[str]:
    """Return the action-intake segment source used before workflow routing."""

    return [frame.text for frame in _split_segment_frames(raw_text, received_at=received_at)]


def _split_segment_frames(raw_text: str, *, received_at: Any = None) -> list[_SegmentFrame]:
    text = str(raw_text or "").strip()
    if not text:
        return []
    conditional_supplement = _conditional_daily_supplement_content(text)
    if conditional_supplement:
        return [_SegmentFrame(conditional_supplement, _explicit_split_field(conditional_supplement, received_at=received_at))]
    if _looks_like_compound_delete_append_edit(text):
        return [_SegmentFrame(text, _explicit_split_field(text, received_at=received_at))]
    if _looks_like_equipment_logistics_question(text):
        return [_SegmentFrame(text)]
    if _looks_like_daily_date_classification_question(text):
        return [_SegmentFrame(text)]
    if looks_like_contextual_daily_edit(text):
        return [_SegmentFrame(text)]
    structured_frames = _split_structured_daily_report_frames(text)
    if structured_frames:
        return structured_frames
    if re.match(r"^\s*(?:\u98ce\u9669|\u95ee\u9898)[\uff1f?]", text) and _has_business_work_object(text):
        return [_SegmentFrame(text, "problems")]
    if _looks_like_monthly_coordination_followup(text):
        return [_SegmentFrame(text)]
    if _looks_like_non_substantive_daily_request(text):
        return [_SegmentFrame(text)]
    if (
        _looks_like_completed_previous_plan(text)
        and not _looks_like_previous_daily_plan_completion_question(text)
        and not _has_current_work_after_completed_previous_plan(text)
    ):
        return [_SegmentFrame(text)]
    frames: list[_SegmentFrame] = []
    for sentence in re.split(r"[\r\n;；。！？?]+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if _looks_like_negative_replacement(sentence):
            frames.append(_SegmentFrame(sentence, _explicit_split_field(sentence, received_at=received_at)))
            continue
        comma_pieces = [piece.strip() for piece in re.split(r"[，,、]", sentence) if piece.strip()]
        comma_pieces = _merge_attached_detail_pieces(comma_pieces)
        if _should_keep_comma_sentence(sentence, comma_pieces) and not _should_split_daily_list_sentence(sentence, comma_pieces):
            frames.append(_SegmentFrame(sentence, _explicit_split_field(sentence, received_at=received_at)))
        else:
            current_field = ""
            for comma_piece in comma_pieces:
                list_pieces = _split_list_piece_for_context(
                    comma_piece,
                    current_field=current_field,
                    received_at=received_at,
                )
                for piece in list_pieces:
                    explicit_field = _explicit_split_field(piece, received_at=received_at)
                    inherited_field = explicit_field
                    if not inherited_field and _can_inherit_split_field(piece, current_field):
                        inherited_field = current_field
                    frames.append(_SegmentFrame(piece, inherited_field))
                    if explicit_field in {"today_work", "tomorrow_plan"}:
                        current_field = explicit_field
                    elif inherited_field in {"today_work", "tomorrow_plan"}:
                        current_field = inherited_field
                    elif explicit_field == "problems":
                        current_field = ""
    return frames


def _looks_like_compound_delete_append_edit(text: str) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    return _contains_any(compact, ("去掉", "删掉", "删除", "移除")) and _contains_any(
        compact,
        ("加上", "加入", "补上", "追加"),
    )


def _looks_like_equipment_logistics_question(text: str) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    return _contains_any(compact, ("需要带哪些", "需要带什么", "要带哪些", "要带什么", "带哪些设备", "带什么设备")) and _contains_any(
        compact,
        ("设备", "材料", "案卷", "资料"),
    )


def _looks_like_daily_date_classification_question(text: str) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    return _contains_any(compact, ("\u7b97\u4eca\u5929\u7684\u6d3b\u8fd8\u662f\u660e\u5929", "\u7b97\u4eca\u5929\u8fd8\u662f\u660e\u5929", "\u5199\u4eca\u5929\u8fd8\u662f\u660e\u5929")) and _looks_like_question(text)


def _merge_attached_detail_pieces(pieces: list[str]) -> list[str]:
    merged: list[str] = []
    for piece in pieces:
        compact = _compact(piece)
        if merged and _looks_like_problem(merged[-1]) and _contains_any(compact, ("\u53ef\u80fd\u8fdd\u7ea6", "\u53ef\u80fd\u5ef6\u671f", "\u53ef\u80fd\u62d6", "\u53ef\u80fd\u5f71\u54cd", "\u8bc1\u636e\u4e0d\u8db3")):
            merged[-1] = f"{merged[-1]}\uff0c{piece}"
            continue
        if merged and _contains_any(compact, ("\u987a\u4e30\u5355\u53f7", "\u5feb\u9012\u5355\u53f7", "\u5355\u53f7", "\u5bc4\u4ef6\u5355\u53f7")):
            merged[-1] = f"{merged[-1]}\uff0c{piece}"
            continue
        if merged and _contains_any(compact, ("\u4eca\u5929\u8981\u4ea4", "\u4eca\u5929\u63d0\u4ea4", "\u73b0\u5728\u5199", "\u6211\u73b0\u5728\u5199")):
            merged[-1] = f"{merged[-1]}\uff0c{piece}"
            continue
        merged.append(piece)
    return merged


def _split_structured_daily_report_frames(text: str) -> list[_SegmentFrame]:
    lines = [line.strip() for line in str(text or "").splitlines()]
    if not lines:
        return []
    headings = 0
    frames: list[_SegmentFrame] = []
    current_field = ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        field, remainder = _structured_daily_heading(line)
        if field:
            headings += 1
            current_field = field
            if remainder:
                for item in _structured_daily_items(remainder):
                    frames.append(_SegmentFrame(item, current_field))
            continue
        if current_field:
            for item in _structured_daily_items(line):
                frames.append(_SegmentFrame(item, current_field))
    if headings < 2:
        return []
    return frames


def _structured_daily_heading(line: str) -> tuple[str, str]:
    original = str(line or "").strip()
    if not original:
        return "", ""
    value = re.sub(r"^#+\s*", "", original)
    value = value.strip(" *_-—=【】[]（）()：:")
    value = re.sub(r"^[一二三四五六七八九十0-9]+[、.．)]\s*", "", value)
    normalized = re.sub(r"\s+", "", value)
    heading_patterns = (
        ("today_work", ("今日工作完成情况", "今日工作", "今天工作", "今日完成", "今天完成", "工作完成情况")),
        ("tomorrow_plan", ("明日工作计划", "明天工作计划", "明日计划", "明天计划", "明日工作", "明天工作")),
        ("problems", ("碰到问题与风险", "问题与风险", "问题/风险", "问题风险", "存在问题", "困难与问题", "问题", "风险")),
    )
    for field, headings in heading_patterns:
        for heading in headings:
            if normalized == heading:
                return field, ""
            if normalized.startswith(heading):
                remainder = value[len(heading) :].strip(" ：:，,。；;、")
                if remainder:
                    return field, remainder
    return "", ""


def _structured_daily_items(line: str) -> list[str]:
    value = str(line or "").strip()
    if not value:
        return []
    value = re.sub(r"^[\-•·]\s*", "", value)
    value = re.sub(r"^[0-9一二三四五六七八九十]+[、.．)]\s*", "", value).strip()
    if not value:
        return []
    return [value]


def _should_keep_comma_sentence(sentence: str, pieces: list[str]) -> bool:
    if len(pieces) <= 1:
        return False
    if _looks_like_monthly_reply(sentence, active_monthly=True) and _contains_any(
        sentence,
        ("\u6539\u4e3a", "\u6539\u6210", "\u4fee\u6539", "\u66ff\u6362", "\u884c\u52a8\u65b9\u6848"),
    ):
        return True
    if looks_like_contextual_daily_edit(sentence):
        return True
    if _looks_like_negative_replacement(sentence):
        return True
    if (
        not _looks_like_problem(pieces[0])
        and _looks_like_problem(sentence)
        and any(_has_positive_daily_work_evidence(piece) for piece in pieces)
        and any(_has_problem_context_piece(piece) for piece in pieces[1:])
    ):
        return False
    if (
        not _looks_like_daily_edit(sentence)
        and _looks_like_problem(sentence)
        and not extract_problem_evidence(sentence).is_no_problem
        and any(_has_problem_context_piece(piece) for piece in pieces)
    ):
        return True
    if _looks_like_problem(pieces[0]) and any(_has_positive_daily_work_evidence(piece) for piece in pieces[1:]):
        return True
    if _looks_like_business_list_sentence(pieces):
        return True
    if not _looks_like_daily_edit(sentence):
        return False
    if _has_comma_separated_item_numbers(sentence):
        return True
    return _has_item_range_reference(pieces[0]) and any(_looks_like_daily_edit(piece) for piece in pieces[1:])


def _has_problem_context_piece(piece: str) -> bool:
    value = str(piece or "")
    if _looks_like_problem(value):
        return True
    return _contains_any(
        value,
        (
            "延期",
            "推迟",
            "临时有事",
            "卡住",
            "没谈拢",
            "不配合",
            "不同意",
            "风险",
            "法官",
            "法院",
            "庭",
            "开庭",
        ),
    )


def _should_split_daily_list_sentence(sentence: str, pieces: list[str]) -> bool:
    if len(pieces) <= 1:
        return False
    if _looks_like_problem(pieces[0]) or _looks_like_monthly_reply(sentence, active_monthly=True):
        return False
    if not (_has_daily_time_anchor(sentence) or _has_explicit_daily_context_in_text(sentence)):
        return False
    work_like = 0
    for piece in pieces:
        if _has_positive_daily_work_evidence(piece) or _has_business_work_object(piece) or _has_business_work_action(piece):
            work_like += 1
    return work_like >= 2


def _split_list_piece_for_context(
    piece: str,
    *,
    current_field: str,
    received_at: Any = None,
) -> list[str]:
    value = str(piece or "").strip()
    if "、" not in value:
        return [value] if value else []
    explicit_field = _explicit_split_field(value, received_at=received_at)
    if not explicit_field and current_field not in {"today_work", "tomorrow_plan"}:
        return [value]
    parts = [part.strip() for part in value.split("、") if part.strip()]
    return parts or ([value] if value else [])


def _explicit_split_field(segment: str, *, received_at: Any = None) -> str:
    text = str(segment or "").strip()
    if not text or _looks_like_question(text):
        return ""
    if _looks_like_process_help_reason_fragment(text):
        return ""
    if _looks_like_completed_previous_plan(text):
        return "today_work"
    problem_evidence = extract_problem_evidence(text)
    if _looks_like_system_failure_problem(text):
        return "problems"
    if problem_evidence.is_problem or problem_evidence.is_no_problem:
        return "problems"
    relative_hint = date_hint_from_text(text, received_at=received_at)
    if relative_hint == "today":
        return "today_work"
    if relative_hint == "tomorrow":
        return "tomorrow_plan"
    if _contains_any(text, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u65e9", "\u660e\u5929\u65e9\u4e0a", "\u660e\u65e5\u65e9\u4e0a", "\u660e\u5929\u4e0a\u5348", "\u660e\u65e5\u4e0a\u5348", "\u660e\u4e2a", "\u660e\u5929\u8ba1\u5212", "\u660e\u65e5\u8ba1\u5212")):
        return "tomorrow_plan"
    if _contains_any(text, ("\u4eca\u5929", "\u4eca\u65e5", "\u4eca\u5929\u5de5\u4f5c", "\u4eca\u65e5\u5de5\u4f5c")):
        return "today_work"
    return ""


def _can_inherit_split_field(segment: str, current_field: str) -> bool:
    if current_field not in {"today_work", "tomorrow_plan"}:
        return False
    if _looks_like_process_help_reason_fragment(segment):
        return False
    if _looks_like_question(segment) or _looks_like_non_work_chatter(segment):
        return False
    if extract_problem_evidence(segment).is_problem:
        return False
    return (
        _has_positive_daily_work_evidence(segment)
        or _has_business_work_action(segment)
        or _has_business_work_object(segment)
        or _looks_like_travel_event(segment)
    )


def _has_comma_separated_item_numbers(text: str) -> bool:
    ordinal = r"[0-9一二三四五六七八九十两]+"
    return bool(re.search(rf"{ordinal}\s*[，,、]\s*{ordinal}(?:\s*[，,、]\s*{ordinal})*", str(text or "")))


def _has_item_range_reference(text: str) -> bool:
    ordinal = r"[0-9一二三四五六七八九十两]+"
    return bool(re.search(rf"第?\s*{ordinal}\s*(?:到|至|-|—|~)\s*第?\s*{ordinal}\s*[条项点个]?", str(text or "")))


def _looks_like_business_list_sentence(pieces: list[str]) -> bool:
    if len(pieces) < 3:
        return False
    if any(_looks_like_question(piece) or _looks_like_problem(piece) or _looks_like_no_problem(piece) or _looks_like_travel_event(piece) for piece in pieces):
        return False
    work_like = 0
    for piece in pieces:
        if _has_positive_daily_work_evidence(piece) or (
            _has_business_work_object(piece) and (_has_business_work_action(piece) or len(_compact(piece)) <= 10)
        ):
            work_like += 1
    return work_like >= 2


def _blocks_current_daily_for_relative_date(segment: str, *, whole_text: str, received_at: Any = None) -> bool:
    relative_hint = date_hint_from_text(segment, received_at=received_at)
    if relative_hint in {"future_weekday", "next_week", "past_weekday"} and not _has_explicit_daily_context_in_text(segment):
        return True
    whole_hint = date_hint_from_text(whole_text, received_at=received_at)
    if (
        relative_hint == "unknown"
        and whole_hint in {"future_weekday", "next_week", "past_weekday"}
        and not _contains_any(whole_text, ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u65e9"))
        and not _has_explicit_daily_context_in_text(whole_text)
    ):
        return True
    return False


def _daily_field_for_segment(
    segment: str,
    *,
    whole_text: str,
    active_daily: bool = False,
    received_at: Any = None,
    inherited_field: str = "",
) -> str:
    if _looks_like_external_report_edit_request(whole_text) or _looks_like_office_device_chatter(segment):
        return ""
    if _looks_like_unfinished_daily_shell(segment):
        return ""
    if _looks_like_do_not_write_daily(segment) or _looks_like_do_not_write_daily(whole_text):
        return ""
    if _looks_like_submission_status_question(segment):
        return ""
    if _looks_like_personal_state_only(segment) or _looks_like_calendar_or_offday_chatter(segment):
        return ""
    if _looks_like_monthly_meta_request(segment) or _looks_like_monthly_meta_request(whole_text) or _looks_like_daily_bot_feedback(whole_text):
        return ""
    if _looks_like_process_help_reason_fragment(segment):
        return ""
    if _looks_like_reimbursement_policy_question(segment):
        return ""
    if _looks_like_daily_meta_or_date_question(segment):
        return ""
    if _looks_like_past_result_only_update(segment):
        return ""
    if _looks_like_question(segment) and not _looks_like_business_plan_statement(segment):
        return ""
    if _looks_like_daily_edit(segment):
        return ""
    if _looks_like_case_progress_routing_request(segment) or _looks_like_case_progress_routing_request(whole_text):
        return ""
    if _looks_like_system_rant_only(segment):
        return ""
    if _looks_like_standalone_past_work_without_current_context(segment, whole_text=whole_text, received_at=received_at):
        return ""
    if _looks_like_personal_workplace_rant(segment) or _looks_like_generic_busy_chatter(segment) or _looks_like_lifestyle_plan_question(segment):
        return ""
    if _looks_like_travel_application_only(segment) or _looks_like_reminder_request_only(segment) or _looks_like_weather_question(segment) or _looks_like_calendar_event_only(segment):
        return ""
    if _looks_like_food_or_rest_plan(segment) or _looks_like_bare_emotional_travel(segment) or _looks_like_non_tomorrow_future_deadline(segment) or _looks_like_future_daily_makeup_notice(segment) or _looks_like_moyu_self_deprecation(segment) or _looks_like_dream_or_fantasy_chatter(segment):
        return ""
    if _contains_any(_compact(segment), ("\u8bf4\u4e0d\u4e0a\u6765", "\u53cd\u6b63\u4e0d\u5927", "\u53ef\u80fd\u660e\u5929\u5c31\u597d")) and _contains_any(_compact(segment), ("\u98ce\u9669", "\u95ee\u9898")):
        return ""
    if _looks_like_travel_logistics_only(segment) or _looks_like_agent_task_instruction(segment):
        return ""
    if _looks_like_generic_tomorrow_continue(segment):
        return ""
    if _looks_like_no_change_statement(segment):
        return ""
    if _looks_like_vague_repeat_or_workload_statement(segment):
        return ""
    if _looks_like_emotional_deferral_without_business(segment) or _looks_like_emotional_deferral_without_business(whole_text):
        return ""
    if _looks_like_creative_assistant_request(whole_text):
        return ""
    if _looks_like_non_work_chatter(segment, whole_text=whole_text):
        return ""
    if (
        _looks_like_absurd_content(segment)
        or _looks_like_case_progress_only_update(segment)
        or (_looks_like_case_progress_only_update(whole_text) and _contains_any(segment, ("\u6536\u5230", "\u4f20\u7968", "\u6cd5\u9662\u901a\u77e5", "\u901a\u77e5", "\u6392\u671f")))
        or _looks_like_case_schedule_only_fragment(segment)
    ):
        return ""
    if _looks_like_completed_previous_plan(whole_text):
        return "today_work"
    relative_hint = date_hint_from_text(segment, received_at=received_at)
    if relative_hint in {"future_weekday", "next_week", "past_weekday"} and not _has_explicit_daily_context_in_text(segment):
        return ""
    if _looks_like_resolved_risk_update(segment):
        return "today_work"
    if _looks_like_contextual_problem_followup(segment, whole_text=whole_text):
        return "problems"
    if _looks_like_document_correction_problem(segment):
        return "problems"
    if _has_daily_time_anchor(segment) and not _contains_any(segment, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u65e9")) and _has_business_work_action(segment) and _has_business_work_object(segment):
        return "today_work"
    if inherited_field in {"today_work", "tomorrow_plan"}:
        return inherited_field
    if inherited_field == "problems":
        return "problems"
    if _looks_like_business_plan_statement(segment):
        if _contains_any(segment, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u65e9", "\u8ba1\u5212", "\u6253\u7b97", "\u51c6\u5907")):
            return "tomorrow_plan"
        return "today_work"
    if _contains_any(segment, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u65e9")) and _has_business_work_action(segment) and _has_business_work_object(segment):
        return "tomorrow_plan"
    if _looks_like_contextual_case_strategy_plan(segment):
        return "tomorrow_plan"
    if _looks_like_followup_plan_segment(segment):
        if _contains_any(whole_text, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f")):
            return "tomorrow_plan"
        if _contains_any(whole_text, ("\u4eca\u5929", "\u4eca\u65e5", "\u53bb\u4e86", "\u5df2\u7ecf")):
            return "today_work"
    if _looks_like_completed_previous_plan(segment):
        return "today_work"
    if _looks_like_legal_document_work(segment):
        if _contains_any(segment, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u8ba1\u5212", "\u62df")):
            return "tomorrow_plan"
        return "today_work"
    problem_evidence = extract_problem_evidence(segment)
    if problem_evidence.is_no_problem:
        if not active_daily and not _has_daily_context_in_text(whole_text):
            return ""
        return "problems"
    if problem_evidence.is_explicit_problem:
        field_update_has_content = _looks_like_daily_field_update(segment) and (
            _has_positive_daily_work_evidence(segment) or _has_business_work_object(segment)
        )
        if (
            not active_daily
            and not _has_daily_context_in_text(whole_text)
            and not field_update_has_content
        ):
            return ""
        return "problems"
    if problem_evidence.is_business_problem:
        return "problems"
    if relative_hint in {"future_weekday", "next_week", "past_weekday"}:
        return ""
    if _blocks_current_daily_for_relative_date(segment, whole_text=whole_text, received_at=received_at):
        return ""
    if relative_hint == "tomorrow" and (
        _looks_like_daily_write(segment) or _looks_like_travel_event(segment) or _has_positive_daily_work_evidence(segment)
    ):
        return "tomorrow_plan"
    if relative_hint == "today" and (
        _looks_like_daily_write(segment) or _looks_like_travel_event(segment) or _has_positive_daily_work_evidence(segment)
    ):
        return "today_work"
    if _contains_any(segment, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u65e9", "\u660e\u5929\u65e9\u4e0a", "\u660e\u65e5\u65e9\u4e0a", "\u660e\u5929\u4e0a\u5348", "\u660e\u65e5\u4e0a\u5348", "\u8ba1\u5212", "\u62df")):
        if _looks_like_daily_write(segment) or _looks_like_travel_event(segment) or _has_positive_daily_work_evidence(segment):
            return "tomorrow_plan"
    if _contains_any(segment, ("\u4eca\u5929", "\u4eca\u65e5", "\u5df2\u7ecf", "\u53bb\u4e86")) and (
        _looks_like_daily_write(segment) or _looks_like_travel_event(segment)
    ):
        return "today_work"
    if _has_positive_daily_work_evidence(segment):
        if _contains_any(whole_text, ("\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u8ba1\u5212")):
            return "tomorrow_plan"
        if _contains_any(whole_text, ("\u4eca\u65e5\u5de5\u4f5c", "\u4eca\u5929\u5de5\u4f5c", "\u4eca\u65e5\u5b8c\u6210", "\u4eca\u5929\u5b8c\u6210")):
            return "today_work"
        if active_daily and _looks_like_active_daily_business_continuation(segment):
            return inherited_field if inherited_field in {"today_work", "tomorrow_plan"} else "today_work"
    if _looks_like_daily_write(segment):
        return "today_work"
    if _looks_like_travel_event(segment) and _contains_any(
        whole_text,
        ("\u65e5\u62a5", "\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5"),
    ):
        return "today_work"
    return ""


def _looks_like_active_daily_business_continuation(segment: str) -> bool:
    text = str(segment or "").strip()
    if not text or _looks_like_question(text) or _looks_like_non_work_chatter(text):
        return False
    if _looks_like_vague_repeat_or_workload_statement(text) or _looks_like_non_substantive_daily_request(text):
        return False
    if _looks_like_daily_edit(text) or _looks_like_case_schedule_only_fragment(text):
        return False
    if _looks_like_case_progress_only_update(text):
        return False
    return bool(_specific_matter_hint(text)) or (_has_business_work_action(text) and _has_business_work_object(text))


def _looks_like_assistant_feedback(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if compact in {"??", "???", "\uff1f\uff1f", "\uff1f\uff1f\uff1f"}:
        return True
    if _contains_any(
        text,
        (
            "\u5565\u73a9\u610f",
            "\u4ec0\u4e48\u9b3c",
            "\u8fd9\u662f\u5565",
            "\u8fd9\u662f\u4ec0\u4e48",
            "\u8fd9\u5565\u610f\u601d",
            "\u5565\u610f\u601d",
            "\u4e0d\u5bf9",
            "\u4e0d\u662f\u8fd9\u4e2a",
            "\u8bb0\u9519",
            "\u5199\u9519",
            "\u4f60\u5199\u9519",
            "\u600e\u4e48\u53d8\u6210\u8fd9\u6837",
            "\u600e\u4e48\u53c8\u53d8\u6210\u8fd9\u6837",
            "\u663e\u793a\u7684\u683c\u5f0f",
            "\u663e\u793a\u683c\u5f0f",
            "\u683c\u5f0f\u6709\u95ee\u9898",
            "\u6ca1\u52a0\u7c97",
            "\u52a0\u7c97",
            "\u89c4\u5219\u91cc\u5199",
            "\u4f60\u662f\u50bb\u5b50",
            "\u50bb\u5b50",
            "\u53d8\u8822",
            "\u4f60\u53d8\u8822",
            "\u4e0d\u77e5\u9053\u6211\u60f3\u5e72\u561b",
            "\u4e0d\u77e5\u9053\u6211\u60f3\u5e72\u4ec0\u4e48",
            "\u592a\u8822",
            "\u7528\u4e0d\u4e86",
            "\u4e0d\u597d\u7528",
            "\u4e00\u5768\u5c4e",
        ),
    ):
        return True
    if _contains_any(text, ("\u4f60", "\u673a\u5668\u4eba", "\u7cfb\u7edf")) and _contains_any(
        text,
        (
            "\u8bb0\u5f55",
            "\u91cd\u590d",
            "\u6ca1\u6709",
            "\u6ca1\u52a0\u7c97",
            "\u6ca1\u6539",
            "\u6ca1\u7528",
            "\u4e0d\u597d\u7528",
            "\u4e0d\u8bb0",
            "\u89c4\u5219",
            "\u9650\u5236",
            "\u88ab\u6c14",
            "\u6c14\u6b7b",
            "\u5168\u90fd\u6ca1\u6709",
            "\u6709\u70b9\u50bb",
            "\u50bb",
            "\u8822",
            "\u7b28",
            "\u5947\u602a",
            "\u79bb\u8c31",
            "\u62bd\u98ce",
            "\u6b7b\u6837\u5b50",
        ),
    ):
        return not _has_positive_daily_work_evidence(text)
    return False


def _looks_like_business_problem_statement(segment: str) -> bool:
    if _looks_like_question(segment):
        return False
    evidence = extract_problem_evidence(segment)
    if not evidence.is_problem:
        return False
    return _has_concrete_problem_detail(segment)


def _has_concrete_problem_detail(segment: str) -> bool:
    if _specific_matter_hint(segment):
        return True
    stripped = re.sub(
        r"[\s，,。.!！?？:：；;]+|刚才说的|前面说的|这个|那个|风险点|风险|问题|困难|卡点|存在|记得|帮我|给我|写上|记上|哈",
        "",
        str(segment or ""),
    )
    return _has_business_work_object(stripped)


def _looks_like_meta_test_probe(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
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
    if _contains_any(compact, ("\u8ba9\u6211\u6d4b\u8bd5\u4e0b", "\u8ba9\u6211\u6d4b\u8bd5\u4e00\u4e0b", "\u6211\u6d4b\u8bd5\u4e0b", "\u6211\u6d4b\u8bd5\u4e00\u4e0b")) and _contains_any(
        compact,
        ("\u4f60\u80fd\u5199\u65e5\u62a5\u5417", "\u4f60\u4f1a\u5199\u65e5\u62a5\u5417", "\u80fd\u5199\u65e5\u62a5\u5417", "\u4f1a\u5199\u65e5\u62a5\u5417"),
    ):
        return True
    if _contains_any(compact, ("\u522b\u5f53\u771f", "\u4e0d\u8981\u5f53\u771f", "\u6211\u5c31\u6d4b\u8bd5", "\u53ea\u662f\u6d4b\u8bd5")) and _contains_any(
        compact,
        ("\u6d4b\u8bd5\u7cfb\u7edf", "\u6d4b\u8bd5\u4e00\u4e0b\u7cfb\u7edf", "\u6d4b\u8bd5\u4e0b\u7cfb\u7edf", "\u6d4b\u8bd5"),
    ):
        return True
    if _contains_any(compact, ("test", "\u6d4b\u8bd5", "\u8ba9\u6211\u6d4b\u8bd5")) and _contains_any(
        compact,
        ("\u65e5\u62a5\u529f\u80fd", "\u6708\u62a5\u529f\u80fd", "\u5468\u62a5\u529f\u80fd", "\u6848\u4ef6\u529f\u80fd", "\u51fa\u5dee\u529f\u80fd"),
    ):
        return True
    if _has_reportable_work_piece(text):
        return False
    if _has_daily_time_anchor(text) and _has_positive_daily_work_evidence(text):
        return False
    if _contains_any(text, ("\u65e5\u62a5\u7cfb\u7edf", "\u6848\u4ef6\u7cfb\u7edf", "\u6d4b\u8bd5\u7528\u4f8b")) and _has_daily_time_anchor(text):
        return False
    if _contains_any(text, ("\u4f60", "\u673a\u5668\u4eba", "\u7cfb\u7edf")) and _contains_any(
        text,
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
    if compact in {
        "\u6d4b\u8bd5",
        "\u6d4b\u6d4b",
        "\u8bd5\u8bd5",
        "\u8bd5\u4e00\u4e0b",
        "\u8bd5\u4e0b",
        "\u6d4b\u4e00\u4e0b",
        "\u6d4b\u4e0b",
        "\u5148\u6d4b\u4e00\u4e0b",
        "\u5148\u6d4b\u4e0b",
        "\u6211\u5148\u6d4b\u4e00\u4e0b",
        "\u6211\u5148\u6d4b\u4e0b",
        "\u6d4b\u8bd5\u4e0b",
        "\u6d4b\u8bd5\u4e00\u4e0b",
        "\u6211\u6d4b\u8bd5\u4e0b",
        "\u6211\u6d4b\u8bd5\u4e00\u4e0b",
        "\u8ba9\u6211\u6d4b\u8bd5\u4e0b",
        "\u8ba9\u6211\u6d4b\u8bd5\u4e00\u4e0b",
        "\u6211\u8bd5\u8bd5",
        "\u8ba9\u6211\u8bd5\u8bd5",
    }:
        return True
    if _contains_any(text, ("\u6d4b\u8bd5", "\u6d4b\u4e00\u4e0b", "\u8bd5\u4e00\u4e0b", "\u8bd5\u4e0b")) and _contains_any(text, ("\u673a\u5668\u4eba", "\u4f60\u4f1a", "\u4f60\u80fd")):
        return not extract_problem_evidence(text).is_business_problem
    return _contains_any(text, ("\u8ba9\u6211\u6d4b\u8bd5", "\u6211\u5148\u6d4b", "\u5148\u6d4b", "\u6211\u6d4b\u8bd5\u4e0b", "\u6d4b\u8bd5\u4e0b", "\u6d4b\u4e00\u4e0b", "\u6d4b\u4e0b", "\u8bd5\u4e00\u4e0b", "\u8bd5\u4e0b")) and not (
        _has_business_work_object(text) or extract_problem_evidence(text).is_business_problem
    )


def _looks_like_daily_history_query(segment: str) -> bool:
    return (
        _contains_any(segment, ("\u5386\u53f2", "\u6700\u8fd1", "\u8fd1\u51e0\u5929", "\u8fd9\u51e0\u5929", "\u6628\u5929", "\u6628\u65e5", "\u524d\u5929", "\u524d\u65e5", "\u5927\u524d\u5929", "\u5927\u524d\u65e5"))
        and _contains_any(segment, ("\u65e5\u62a5", "\u65e5\u5fd7"))
        and _contains_any(
            segment,
            ("\u53d1\u6211", "\u7ed9\u6211", "\u60f3\u770b", "\u770b", "\u67e5", "\u5c55\u793a"),
        )
    )


def _looks_like_historical_daily_risk_lookup(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
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


def _looks_like_current_daily_status_query(segment: str) -> bool:
    compact = _compact(segment)
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


def _looks_like_daily_edit_entry_request(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if not _contains_any(text, ("\u65e5\u62a5", "\u65e5\u5fd7", "\u8349\u7a3f", "\u6c47\u62a5")):
        return False
    if not _contains_any(text, ("\u6539", "\u4fee\u6539", "\u8c03\u6574", "\u7f16\u8f91", "\u5904\u7406")):
        return False
    if _contains_any(
        text,
        (
            "\u6539\u6210",
            "\u6539\u4e3a",
            "\u66ff\u6362",
            "\u5220\u6389",
            "\u5220\u9664",
            "\u53bb\u6389",
            "\u6e05\u7a7a",
            "\u5408\u5e76",
            "\u79fb\u5230",
            "\u8865\u5145",
            "\u65b0\u589e",
            "\u52a0\u4e00\u6761",
        ),
    ):
        return False
    if re.search(r"\u7b2c[0-9\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+\u6761|\d+[.．、]", text):
        return False
    if len(compact) <= 18:
        return True
    return bool(
        re.fullmatch(
            r"(?:\u6211\u60f3|\u6211\u8981|\u5e2e\u6211|\u9ebb\u70e6)?"
            r"(?:\u6539|\u4fee\u6539|\u8c03\u6574|\u7f16\u8f91|\u5904\u7406)"
            r"(?:\u4e00\u4e0b|\u4e0b)?"
            r"(?:\u6628\u5929|\u6628\u65e5|\u524d\u5929|\u524d\u65e5|\u4eca\u5929|\u4eca\u65e5)?"
            r"(?:\u7684)?(?:\u65e5\u62a5|\u65e5\u5fd7|\u8349\u7a3f|\u6c47\u62a5)",
            compact,
        )
    )


def _looks_like_daily_current_query(segment: str, *, active_daily: bool) -> bool:
    if _looks_like_case_metric_data_request(segment):
        return False
    compact = _compact(segment)
    if _looks_like_business_problem_statement(segment) or _looks_like_system_failure_problem(segment):
        return False
    if _contains_any(compact, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f")) and _contains_any(compact, ("\u8ba1\u5212", "\u51c6\u5907", "\u6253\u7b97", "\u8981", "\u53bb")) and _has_business_work_object(segment):
        return False
    if _looks_like_current_daily_status_query(segment):
        return True
    if not active_daily and not _contains_any(segment, ("\u65e5\u62a5", "\u65e5\u5fd7", "\u8349\u7a3f", "\u5f53\u524d", "\u76ee\u524d")):
        return False
    return _contains_any(
        segment,
        (
            "\u53d1\u6211",
            "\u53d1\u6211\u4e0b",
            "\u53d1\u6211\u770b\u4e0b",
            "\u7ed9\u6211\u770b",
            "\u770b\u4e0b",
            "\u770b\u770b",
            "\u5c55\u793a",
            "\u5565\u6837",
            "\u5565\u6837\u5b50",
            "\u5f53\u524d\u65e5\u62a5",
            "\u5f53\u524d\u65e5\u5fd7",
            "\u76ee\u524d\u7684\u65e5\u5fd7",
            "\u65e5\u62a5\u8349\u7a3f",
        ),
    )


def _looks_like_daily_clear_request(segment: str, *, active_daily: bool) -> bool:
    if not _contains_any(segment, ("\u6e05\u7a7a", "\u6e05\u6389", "\u91cd\u7f6e", "\u5220\u6389\u5168\u90e8", "\u5168\u90e8\u5220\u6389", "\u5220\u9664\u5168\u90e8", "\u5168\u90e8\u5220\u9664")):
        return False
    if active_daily:
        return True
    return _contains_any(
        segment,
        (
            "\u65e5\u62a5",
            "\u65e5\u5fd7",
            "\u8349\u7a3f",
            "\u4eca\u65e5\u5de5\u4f5c",
            "\u4eca\u5929\u5de5\u4f5c",
            "\u95ee\u9898/\u98ce\u9669",
            "\u95ee\u9898\u98ce\u9669",
            "\u660e\u65e5\u8ba1\u5212",
            "\u660e\u5929\u8ba1\u5212",
        ),
    )


def _looks_like_risk_field_delete_request(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u5220\u98ce\u9669", "\u5220\u6389\u98ce\u9669", "\u5220\u4e86\u98ce\u9669", "\u98ce\u9669\u5220\u4e86", "\u628a\u98ce\u9669\u5220", "\u628a\u98ce\u9669\u5220\u4e86", "\u98ce\u9669\u5220\u6389", "\u5220\u95ee\u9898", "\u5220\u6389\u95ee\u9898", "\u628a\u95ee\u9898\u5220", "\u628a\u95ee\u9898\u5220\u4e86")) and not _contains_any(
        compact,
        ("\u5386\u53f2", "\u67e5", "\u770b"),
    )


def _clear_target_field(segment: str) -> str:
    if _contains_any(segment, ("\u4eca\u65e5\u5de5\u4f5c", "\u4eca\u5929\u5de5\u4f5c", "\u4eca\u65e5", "\u4eca\u5929\u5de5\u4f5c")):
        return "today_work"
    if _contains_any(segment, ("\u95ee\u9898/\u98ce\u9669", "\u95ee\u9898\u98ce\u9669", "\u95ee\u9898", "\u98ce\u9669")):
        return "problems"
    if _contains_any(segment, ("\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u8ba1\u5212", "\u660e\u65e5", "\u660e\u5929\u8ba1\u5212")):
        return "tomorrow_plan"
    return "all"


def _looks_like_daily_confirm_request(
    segment: str,
    *,
    active_daily: bool,
    daily_confirmation_pending: bool,
) -> bool:
    if not active_daily:
        return False
    compact = _compact(segment)
    if _contains_any(compact, ("\u65e5\u62a5\u5c31\u8fd9\u6837", "\u65e5\u62a5\u5c31\u8fd9\u6837\u5427", "\u65e5\u62a5\u8fd9\u6837\u5427")):
        return True
    if compact in {
        "\u63d0\u4ea4",
        "\u63d0\u4ea4\u65e5\u62a5",
        "\u65e5\u62a5\u63d0\u4ea4",
        "\u786e\u8ba4\u63d0\u4ea4",
        "\u53ef\u4ee5\u63d0\u4ea4",
        "\u5e2e\u6211\u63d0\u4ea4",
        "\u4ea4",
        "\u4ea4\u5427",
        "\u4ea4\u4e86",
    }:
        return True
    return daily_confirmation_pending and compact in {
        "\u786e\u8ba4",
        "\u786e\u5b9a",
        "\u5c31\u8fd9\u6837",
        "\u662f",
        "\u662f\u7684",
        "\u5bf9",
        "\u5bf9\u7684",
        "ok",
        "okay",
    }


def _looks_like_copy_previous_daily_request(segment: str) -> bool:
    compact = _compact(segment)
    if re.search(r"\u6628\u5929\u7684?\u65e5\u62a5\u91cc[“\"].+?[”\"].{0,12}?\u590d\u5236\u8fc7\u6765", str(segment or "")):
        return False
    if has_previous_to_current_repeat_reference(segment):
        return True
    if compact in {
        "\u4eca\u5929\u548c\u6628\u5929\u4e00\u6837",
        "\u4eca\u5929\u8ddf\u6628\u5929\u4e00\u6837",
        "\u4eca\u5929\u548c\u6628\u5929\u4e00\u6837\u5427",
        "\u4eca\u5929\u8ddf\u6628\u5929\u4e00\u6837\u5427",
        "\u8fd8\u662f\u6628\u5929\u90a3\u4e9b\u4e8b",
        "\u8fd8\u662f\u6628\u5929\u90a3\u4e9b",
        "\u8fd8\u662f\u6628\u5929\u90a3\u4e9b\u4e8b\u5427",
        "\u8fd8\u662f\u6628\u5929\u90a3\u4e9b\u5427",
    }:
        return True
    if _contains_any(compact, ("\u6628\u5929\u90a3\u4e9b", "\u6628\u5929\u90a3\u4e9b\u4e8b", "\u6628\u5929\u90a3\u4e9b\u4e8b\u513f")) and _contains_any(
        compact,
        ("\u8fd8\u662f", "\u505a\u4e86", "\u4e00\u6837", "\u6ca1\u53d8\u5316", "\u4eca\u5929", "\u4eca\u65e5"),
    ):
        return True
    if _contains_any(compact, ("\u6628\u5929\u90a3\u4efd\u65e5\u62a5", "\u6628\u5929\u65e5\u62a5", "\u6628\u65e5\u65e5\u62a5")) and _contains_any(
        compact,
        ("\u4eca\u5929\u63a5\u7740\u5e72", "\u6ca1\u5565\u53d8\u5316", "\u6ca1\u4ec0\u4e48\u53d8\u5316", "\u7167\u65e7", "\u4e00\u6837"),
    ):
        return True
    if _contains_any(compact, ("\u590d\u5236", "\u7167", "\u5e26\u8fc7\u6765", "\u62f7\u8d1d")) and _contains_any(
        compact,
        ("\u6628\u5929", "\u6628\u65e5", "\u524d\u5929", "\u524d\u65e5"),
    ) and _contains_any(compact, ("\u65e5\u62a5", "\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u8ba1\u5212", "\u660e\u5929\u7684\u8ba1\u5212", "\u5de5\u4f5c", "\u8ba1\u5212")):
        return True
    if _contains_any(compact, ("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u62ff\u6765\u4eca\u5929\u7528", "\u6628\u5929\u7684\u660e\u5929\u8ba1\u5212\u62ff\u6765\u4eca\u5929\u7528")):
        return True
    return _contains_any(
        segment,
        (
            "\u628a\u6628\u5929\u7684\u5e26\u8fc7\u6765",
            "\u628a\u6628\u65e5\u7684\u5e26\u8fc7\u6765",
            "\u590d\u5236\u6628\u5929\u65e5\u62a5",
            "\u590d\u5236\u6628\u65e5\u65e5\u62a5",
            "\u6628\u5929\u7684\u65e5\u62a5\u590d\u5236\u5230\u4eca\u5929",
            "\u6628\u65e5\u7684\u65e5\u62a5\u590d\u5236\u5230\u4eca\u5929",
            "\u628a\u6628\u5929\u7684\u65e5\u62a5\u590d\u5236\u5230\u4eca\u5929",
            "\u628a\u6628\u65e5\u7684\u65e5\u62a5\u590d\u5236\u5230\u4eca\u5929",
            "\u590d\u5236\u524d\u65e5\u5185\u5bb9",
            "\u590d\u5236\u524d\u5929\u5185\u5bb9",
            "\u7167\u6628\u5929\u7684",
            "\u548c\u6628\u5929\u4e00\u6837",
            "\u8ddf\u6628\u5929\u4e00\u6837",
        ),
    )


def _copy_previous_target_field(segment: str) -> str:
    compact = _compact(segment)
    if _contains_any(compact, ("\u62ff\u6765\u4eca\u5929\u7528", "\u62ff\u5230\u4eca\u5929\u7528")):
        return "today_work"
    if _contains_any(compact, ("\u590d\u5236\u6628\u5929\u65e5\u62a5", "\u6628\u5929\u7684\u65e5\u62a5\u590d\u5236", "\u628a\u6628\u5929\u7684\u65e5\u62a5")):
        return "all"
    if _contains_any(compact, ("\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u8ba1\u5212", "\u660e\u5929\u7684\u8ba1\u5212", "\u660e\u65e5\u5de5\u4f5c", "\u660e\u5929\u5de5\u4f5c")):
        return "tomorrow_plan"
    if has_previous_to_current_repeat_reference(segment) or _contains_any(compact, ("\u5de5\u4f5c", "\u4e8b", "\u4eca\u5929\u63a5\u7740\u5e72", "\u90a3\u4e9b", "\u4e00\u6837")):
        return "today_work"
    return "all"


def _looks_like_copy_current_work_to_tomorrow(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    has_current_work = _contains_any(compact, ("\u4eca\u5929\u7684\u5de5\u4f5c", "\u4eca\u65e5\u7684\u5de5\u4f5c", "\u4eca\u5929\u5de5\u4f5c", "\u4eca\u65e5\u5de5\u4f5c", "\u4eca\u5929\u4e8b\u9879", "\u4eca\u65e5\u4e8b\u9879"))
    has_tomorrow_target = _contains_any(compact, ("\u5230\u660e\u5929", "\u5230\u660e\u65e5", "\u5230\u660e\u5929\u8ba1\u5212", "\u5230\u660e\u65e5\u8ba1\u5212", "\u4f5c\u4e3a\u660e\u5929\u8ba1\u5212", "\u4f5c\u4e3a\u660e\u65e5\u8ba1\u5212"))
    has_copy = _contains_any(compact, ("\u590d\u5236", "\u62f7\u8d1d", "\u5e26\u5230", "\u5e26\u8fc7\u53bb", "\u518d\u590d\u5236\u4e00\u4efd"))
    return has_current_work and has_tomorrow_target and has_copy


def _looks_like_affirm_same_as_previous_daily(segment: str) -> bool:
    compact = _compact(segment)
    return compact in {"\u5bf9\u4e00\u6837", "\u5bf9\u7684\u4e00\u6837", "\u55ef\u4e00\u6837", "\u662f\u4e00\u6837", "\u4e00\u6837", "\u5c31\u4e00\u6837"}


def _explicit_yesterday_items_repeated_today(segment: str) -> str:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not _contains_any(compact, ("\u6628\u5929\u7684\u65e5\u62a5\u662f", "\u6628\u65e5\u7684\u65e5\u62a5\u662f", "\u6628\u5929\u65e5\u62a5\u662f")):
        return ""
    if not _contains_any(compact, ("\u4eca\u5929\u786e\u5b9e\u4e5f\u662f\u8fd9\u6837", "\u4eca\u65e5\u786e\u5b9e\u4e5f\u662f\u8fd9\u6837", "\u4eca\u5929\u4e5f\u662f\u8fd9\u6837", "\u4eca\u5929\u4e5f\u4e00\u6837", "\u4eca\u5929\u548c\u6628\u5929\u4e00\u6837")):
        return ""
    match = re.search(r"(?:\u6628\u5929|\u6628\u65e5)\u7684?\u65e5\u62a5\u662f\s*[:\uff1a]\s*(.+?)(?:\u3002|\uff1b|;|$)", text)
    if not match:
        return ""
    payload = match.group(1).strip(" \t\r\n\u3000\uff0c,\u3002.")
    if not payload or _looks_like_question(payload) or _looks_like_empty_daily_content(payload):
        return ""
    return payload


def _explicit_yesterday_item_moved_to_tomorrow_plan(segment: str) -> str:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not _contains_any(compact, ("\u6628\u5929\u65e5\u62a5", "\u6628\u5929\u7684\u65e5\u62a5", "\u6628\u65e5\u65e5\u62a5", "\u6628\u65e5\u7684\u65e5\u62a5")):
        return ""
    if not _contains_any(compact, ("\u79fb\u5230\u4eca\u5929\u7684\u660e\u65e5\u8ba1\u5212", "\u79fb\u5230\u4eca\u5929\u7684\u660e\u5929\u8ba1\u5212", "\u79fb\u5230\u660e\u65e5\u8ba1\u5212", "\u79fb\u5230\u660e\u5929\u8ba1\u5212")):
        return ""
    match = re.search(r"[\u2018\u2019'\"\u201c\u201d](.+?)[\u2018\u2019'\"\u201c\u201d]", text)
    if not match:
        match = re.search(r"\u65e5\u62a5\u91cc\u7684?(.{2,24}?)\u79fb\u5230", text)
    if not match:
        return ""
    payload = match.group(1).strip(" \t\r\n\u3000\uff0c,\u3002.")
    if not payload or _looks_like_question(payload):
        return ""
    return payload


def _daily_start_payload(segment: str) -> str:
    text = str(segment or "").strip()
    compact = _compact(text)
    if _contains_any(compact, ("\u4e0d\u7528\u5199\u65e5\u62a5", "\u4e0d\u8981\u5199\u65e5\u62a5", "\u522b\u5199\u65e5\u62a5", "\u4e0d\u5199\u65e5\u62a5")):
        return ""
    if not _contains_any(text, ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return ""
    match = re.search(r"(?:\u5e2e\u6211)?(?:\u5199|\u586b)(?:\u4e2a|\u4e00\u4e2a|\u4e00\u4e0b)?(?:\u4eca\u65e5|\u4eca\u5929|\u5f53\u65e5)?(?:\u65e5\u62a5|\u65e5\u5fd7)(?:\u4e86|\u5427)?\s*[:\uff1a\uff0c,]\s*(.+)$", text)
    if not match:
        return ""
    payload = match.group(1).strip()
    if not payload or _looks_like_question(payload) or _looks_like_empty_daily_content(payload):
        return ""
    if _looks_like_absurd_content(payload) or _looks_like_dream_or_fantasy_chatter(payload) or _looks_like_non_substantive_daily_request(payload):
        return ""
    if not (_has_positive_daily_work_evidence(payload) or _has_business_work_action(payload) or _has_business_work_object(payload)):
        return ""
    return payload


def _looks_like_cross_date_daily_copy_request(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    has_copy = _contains_any(compact, ("\u590d\u5236", "\u62f7\u8d1d", "\u7167\u642c", "\u5e26\u8fc7\u53bb", "\u5e26\u5230"))
    has_daily_object = _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7", "\u6c47\u62a5", "\u5185\u5bb9"))
    if not (has_copy and has_daily_object):
        return False
    has_source_date = _contains_any(compact, ("\u524d\u5929", "\u524d\u65e5", "\u5927\u524d\u5929"))
    has_target_date = _contains_any(compact, ("\u5230\u6628\u5929", "\u8fdb\u6628\u5929", "\u5f80\u6628\u5929", "\u6628\u5929\u7684\u91cc\u9762", "\u6628\u5929\u91cc\u9762"))
    return has_source_date and has_target_date


def _looks_like_daily_editorial_edit_request(segment: str, *, active_daily: bool) -> bool:
    text = str(segment or "")
    if _contains_any(text, ("\u8868\u8ff0", "\u9519\u522b\u5b57", "\u4e0d\u592a\u81ea\u7136", "\u5199\u5dee\u4e86", "\u4f18\u9009", "\u4f18\u5316\u4e0b")):
        return active_daily or _contains_any(text, ("\u95ee\u9898", "\u98ce\u9669", "\u4eca\u5929\u7684\u5de5\u4f5c", "\u4eca\u65e5\u5de5\u4f5c", "\u660e\u65e5\u8ba1\u5212", "\u65e5\u62a5", "\u5185\u5bb9"))
    return False


def _looks_like_ambiguous_daily_editorial_request(segment: str) -> bool:
    compact = _compact(segment)
    return compact in {
        "\u5e2e\u6211\u6574\u5408\u4f18\u5316",
        "\u5e2e\u6211\u6574\u5408\u4e00\u4e0b",
        "\u6574\u5408\u4f18\u5316",
        "\u6574\u4f53\u4f18\u5316\u4e00\u4e0b",
    }


def _looks_like_daily_problem_reply(segment: str, *, active_daily: bool) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if _contains_any(compact, ("\u8bb0\u5230\u65e5\u62a5\u91cc\u4e86\u5417", "\u8bb0\u8fdb\u65e5\u62a5\u91cc\u4e86\u5417", "\u5199\u5230\u65e5\u62a5\u91cc\u4e86\u5417")):
        return False
    explicit_problem_field = (
        any(
            marker in text
            for marker in (
                "\u95ee\u9898\uff1a",
                "\u95ee\u9898:",
                "\u98ce\u9669\uff1a",
                "\u98ce\u9669:",
            )
        )
        or any(
            phrase in text
            for phrase in (
                "\u95ee\u9898\u548c\u98ce\u9669",
                "\u6d89\u53ca\u98ce\u9669",
                "\u4e0d\u600e\u4e48\u6d89\u53ca\u98ce\u9669",
                "\u6ca1\u78b0\u5230\u4ec0\u4e48\u95ee\u9898",
            )
        )
    )
    if not active_daily and not explicit_problem_field:
        return False
    if _contains_any(text, ("\u660e\u5929", "\u660e\u65e5")) and _has_business_work_action(text):
        return False
    if _looks_like_no_problem(text) or _contains_any(text, ("\u6ca1\u78b0\u5230\u4ec0\u4e48\u95ee\u9898", "\u4e0d\u6d89\u53ca\u98ce\u9669", "\u4e0d\u600e\u4e48\u6d89\u53ca\u98ce\u9669")):
        return True
    if _looks_like_daily_edit(text) or _contains_any(text, ("\u6cd5\u5f8b\u95ee\u9898", "\u518d\u95ee", "\u95ee\u4e2a")):
        return False
    if _contains_any(compact, ("\u98ce\u9669\uff1f", "\u95ee\u9898\uff1f", "\u98ce\u9669?", "\u95ee\u9898?")) and (
        _has_business_problem_evidence(text) or _has_business_work_object(text)
    ):
        return True
    if "?" in text or "\uff1f" in text or _contains_any(text, ("\u600e\u4e48", "\u4ec0\u4e48", "\u5565", "\u5417")):
        return False
    return _contains_any(text, ("\u95ee\u9898\uff1a", "\u95ee\u9898:", "\u98ce\u9669\uff1a", "\u98ce\u9669:", "\u95ee\u9898\u548c\u98ce\u9669", "\u6d89\u53ca\u98ce\u9669"))


def _looks_like_short_leave_daily_entry(segment: str) -> bool:
    return _compact(segment) in {
        "\u4f11\u5047",
        "\u8bf7\u5047",
        "\u5e74\u4f11\u5047",
        "\u8c03\u4f11",
    }


def _looks_like_monthly_reply(segment: str, *, active_monthly: bool) -> bool:
    if _contains_any(segment, ("\u672a\u5b8c\u6210\u539f\u56e0", "\u5b58\u5728\u95ee\u9898", "\u4e0b\u6708\u76ee\u6807", "\u884c\u52a8\u65b9\u6848")):
        return True
    if not active_monthly:
        return False
    return _contains_any(segment, ("\u7ee9\u6548", "\u6307\u6807", "\u6708\u62a5")) and _contains_any(
        segment,
        ("\u76ee\u6807", "\u5b8c\u6210\u7387", "\u884c\u52a8", "\u539f\u56e0"),
    )


def _looks_like_monthly_collection_fragment(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if not compact:
        return False
    if _contains_any(text, ("\u672a\u5b8c\u6210\u539f\u56e0", "\u5b58\u5728\u95ee\u9898", "\u4e0b\u6708\u76ee\u6807", "\u884c\u52a8\u65b9\u6848")):
        return True
    has_monthly_value = _contains_any(compact, ("\u76ee\u6807", "\u5b8c\u6210", "\u63a8\u8fdb", "\u8fbe\u6210", "\u5b8c\u6210\u7387", "\u4e0b\u6708")) or "%" in text
    has_daily_anchor = _has_daily_time_anchor(text)
    return has_monthly_value and not has_daily_anchor


def _looks_like_monthly_status_query(segment: str) -> bool:
    text = str(segment or "")
    if not _contains_any(text, ("\u6708\u62a5", "\u7ee9\u6548", "\u586b\u62a5", "\u586b\u7684")):
        return False
    if _contains_any(text, ("\u672a\u5b8c\u6210\u539f\u56e0", "\u4e0b\u6708\u76ee\u6807", "\u884c\u52a8\u65b9\u6848")):
        return False
    return _contains_any(
        text,
        (
            "\u600e\u6837",
            "\u600e\u4e48\u6837",
            "\u5982\u4f55",
            "\u8fdb\u5ea6",
            "\u60c5\u51b5",
            "\u8fd8\u6709\u8c01",
            "\u8c01\u6ca1",
            "\u6ca1\u4ea4",
            "\u6ca1\u586b",
            "\u63d0\u4ea4\u60c5\u51b5",
            "\u586b\u62a5\u60c5\u51b5",
            "\u586b\u4e86\u5417",
            "\u90fd\u586b",
            "\u4ea4\u4e86\u5417",
            "\u5199\u4e86\u6ca1",
            "\u8fd8\u5dee\u8c01",
            "\u5dee\u8c01",
        ),
    )


def _looks_like_weekly_request(segment: str) -> bool:
    if _contains_any(segment, ("\u5468\u4f1a", "\u5f00\u5468\u4f1a")) and not _contains_any(segment, ("\u5468\u62a5", "\u751f\u6210\u5468\u62a5", "\u5199\u5468\u62a5", "\u53d1\u6211\u5468\u62a5")):
        return False
    if _has_daily_time_anchor(segment) and _has_positive_daily_work_evidence(segment):
        return False
    if _has_explicit_daily_context_in_text(segment):
        return False
    if _contains_any(segment, ("\u5468\u62a5", "\u672c\u5468", "\u4e0b\u5468")) and _contains_any(
        segment,
        ("\u5f04", "\u505a", "\u66f4\u65b0", "\u5904\u7406", "\u8865", "\u6539", "\u5199", "\u5f00"),
    ) and not _contains_any(segment, ("\u5e2e\u6211", "\u751f\u6210", "\u53d1\u6211", "\u7ed9\u6211", "\u603b\u7ed3\u4e0b", "\u603b\u7ed3\u4e00\u4e0b")):
        return False
    if _contains_any(segment, ("\u672c\u5468", "\u4e0b\u5468")) and _contains_any(
        segment,
        (
            "\u5b8c\u6210",
            "\u5de5\u4f5c",
            "\u8ba1\u5212",
            "\u603b\u7ed3",
            "\u91cd\u70b9",
        ),
    ):
        return True
    return _contains_any(segment, ("\u5468\u62a5", "\u672c\u5468", "\u4e0b\u5468")) and _contains_any(
        segment,
        ("\u5e2e\u6211", "\u751f\u6210", "\u5199", "\u6574\u7406", "\u6c47\u603b", "\u53d1\u6211"),
    )


def _looks_like_daily_start_request(segment: str) -> bool:
    compact = _compact(segment)
    if compact in {"\u65e5\u62a5", "\u65e5\u5fd7", "\u5199\u4e2a\u65e5\u62a5", "\u5199\u4e2a\u65e5\u62a5\u5427", "\u5199\u65e5\u62a5\u5427", "\u586b\u4e2a\u65e5\u62a5"}:
        return True
    if not _contains_any(segment, ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    if (
        _looks_like_daily_meta_status_statement(segment)
        or _looks_like_absurd_content(segment)
        or _looks_like_lifestyle_report_meta_chatter(segment)
        or _looks_like_non_substantive_daily_request(segment)
    ):
        return False
    if _looks_like_daily_start_without_payload(segment):
        return True
    if _looks_like_question(segment) or _looks_like_meta_test_probe(segment):
        return False
    if _has_daily_time_anchor(segment) and _has_positive_daily_work_evidence(segment):
        return False
    return _contains_any(
        segment,
        (
            "\u5e2e\u6211\u5199",
            "\u5e2e\u6211\u586b",
            "\u5199\u65e5\u62a5",
            "\u5199\u65e5\u5fd7",
            "\u586b\u65e5\u62a5",
            "\u586b\u65e5\u5fd7",
            "\u586b\u62a5\u65e5\u62a5",
            "\u5f00\u59cb\u5199",
            "\u5f00\u59cb\u586b",
            "\u53d1\u4e2a\u63d0\u9192",
            "\u63d0\u9192\u65e5\u62a5",
        ),
    )


def _looks_like_daily_meta_status_statement(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if any(marker in compact for marker in ("今日工作", "今天工作", "问题风险", "明日计划", "明天计划")):
        return False
    if _contains_any(
        compact,
        (
            "\u4e0d\u60f3\u5199\u65e5\u62a5",
            "\u65e5\u62a5\u90fd\u4e0d\u60f3\u5199",
            "\u65e5\u62a5\u4e0d\u60f3\u5199",
            "\u4e0d\u60f3\u586b\u65e5\u62a5",
            "\u61d2\u5f97\u5199\u65e5\u62a5",
            "\u61d2\u5f97\u586b\u65e5\u62a5",
            "\u4e0d\u5199\u65e5\u62a5",
            "\u4e0d\u586b\u65e5\u62a5",
        ),
    ) and not _has_positive_daily_work_evidence(segment):
        return True
    if compact in {
        "\u5199\u65e5\u62a5\u4e86",
        "\u6211\u5199\u65e5\u62a5\u4e86",
        "\u5df2\u7ecf\u5199\u65e5\u62a5\u4e86",
        "\u6211\u5df2\u7ecf\u5199\u65e5\u62a5\u4e86",
        "\u65e5\u62a5\u5199\u4e86",
        "\u65e5\u62a5\u5199\u8fc7\u4e86",
        "\u6211\u65e5\u62a5\u5199\u4e86",
        "\u5728\u5199\u65e5\u62a5",
        "\u6211\u5728\u5199\u65e5\u62a5",
        "\u6b63\u5728\u5199\u65e5\u62a5",
        "\u6211\u6b63\u5728\u5199\u65e5\u62a5",
        "\u586b\u65e5\u62a5\u4e86",
        "\u6211\u586b\u65e5\u62a5\u4e86",
        "\u5df2\u7ecf\u586b\u65e5\u62a5\u4e86",
        "\u6211\u5df2\u7ecf\u586b\u65e5\u62a5\u4e86",
    }:
        return True
    return compact in {
        "写日报了",
        "我写日报了",
        "已经写日报了",
        "我已经写日报了",
        "日报写了",
        "日报写过了",
        "我日报写了",
        "在写日报",
        "我在写日报",
        "正在写日报",
        "我正在写日报",
        "填日报了",
        "我填日报了",
        "已经填日报了",
        "我已经填日报了",
    }


def _looks_like_previous_daily_submission_request(segment: str) -> bool:
    text = str(segment or "")
    if _contains_any(text, ("\u5f53\u524d\u65e5\u62a5", "\u76ee\u524d\u65e5\u62a5", "\u5f53\u524d\u7684\u65e5\u62a5", "\u76ee\u524d\u7684\u65e5\u62a5")):
        return False
    if not _contains_any(text, ("\u6628\u5929", "\u6628\u65e5", "\u524d\u5929", "\u524d\u65e5")):
        return False
    if not _contains_any(text, ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    return _contains_any(
        text,
        (
            "\u8865",
            "\u8865\u4ea4",
            "\u8865\u4e00\u4e0b",
            "\u5199",
            "\u586b",
            "\u6539",
            "\u4fee\u6539",
            "\u52a0",
            "\u52a0\u5230",
            "\u52a0\u8fdb",
            "\u52a0\u5165",
            "\u8ffd\u52a0",
            "\u5220",
            "\u5220\u9664",
            "\u5220\u6389",
            "\u6e05\u7a7a",
            "\u64a4\u56de",
        ),
    )


def _looks_like_historical_daily_destructive_request(segment: str) -> bool:
    text = str(segment or "")
    if _contains_any(text, ("\u5f53\u524d\u65e5\u62a5", "\u76ee\u524d\u65e5\u62a5", "\u5f53\u524d\u7684\u65e5\u62a5", "\u76ee\u524d\u7684\u65e5\u62a5")):
        return False
    if not _contains_any(text, ("\u6628\u5929", "\u6628\u65e5", "\u524d\u5929", "\u524d\u65e5")):
        return False
    if not _contains_any(text, ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    if _contains_any(text, ("\u590d\u5236", "\u62f7\u8d1d", "copy", "COPY")) and _contains_any(text, ("\u8fc7\u6765", "\u5230\u4eca\u5929", "\u5230\u4eca\u65e5")):
        return False
    return _contains_any(text, ("\u5220", "\u5220\u9664", "\u5220\u6389", "\u6e05\u7a7a", "\u64a4\u56de", "\u4fee\u6539", "\u6539"))


def _looks_like_previous_day_makeup_statement(segment: str) -> bool:
    text = str(segment or "")
    if _looks_like_completed_previous_plan(text):
        return False
    if _looks_like_current_daily_explicit_segment(text):
        return False
    if not _contains_any(text, ("\u6628\u5929", "\u6628\u65e5", "\u524d\u5929", "\u524d\u65e5")):
        return False
    if not _contains_any(text, ("\u5fd8\u4e86\u5199", "\u5fd8\u5199", "\u6f0f\u5199", "\u8865\u4e00\u4e0b", "\u8865\u4ea4", "\u8865\u5199")):
        return False
    return _has_positive_daily_work_evidence(text) or _has_business_work_action(text) or _has_business_work_object(text)


def _looks_like_previous_daily_plan_completion_question(segment: str) -> bool:
    text = str(segment or "")
    if not _contains_any(text, ("\u6628\u5929", "\u6628\u65e5", "\u524d\u5929", "\u524d\u65e5")):
        return False
    if not _contains_any(text, ("\u65e5\u62a5", "\u65e5\u5fd7", "\u660e\u65e5\u8ba1\u5212", "\u660e\u5929\u8ba1\u5212", "\u8ba1\u5212")):
        return False
    return _contains_any(text, ("\u5b8c\u6210\u4e86\u6ca1", "\u5b8c\u6210\u6ca1", "\u505a\u4e86\u6ca1", "\u505a\u5b8c\u6ca1", "\u662f\u5426\u5b8c\u6210"))


def _looks_like_today_yesterday_ambiguous_reference(segment: str) -> bool:
    compact = _compact(segment)
    return compact in {
        "\u4eca\u5929\u8ddf\u6628\u5929\u5dee\u4e0d\u591a",
        "\u4eca\u5929\u548c\u6628\u5929\u5dee\u4e0d\u591a",
        "\u4eca\u5929\u8ddf\u6628\u5929\u5dee\u4e0d\u591a\u5427",
        "\u4eca\u5929\u548c\u6628\u5929\u5dee\u4e0d\u591a\u5427",
    }


def _looks_like_current_work_retracted_to_yesterday(segment: str) -> bool:
    text = str(segment or "")
    return _contains_any(text, ("\u4eca\u5929\u5de5\u4f5c", "\u4eca\u65e5\u5de5\u4f5c", "\u4eca\u5929\u5b8c\u6210", "\u4eca\u65e5\u5b8c\u6210")) and _contains_any(
        text,
        (
            "\u6628\u5929\u7684\u4e8b",
            "\u6628\u65e5\u7684\u4e8b",
            "\u6628\u5929\u5de5\u4f5c",
            "\u5176\u5b9e\u662f\u6628\u5929",
            "\u5df2\u7ecf\u662f\u6628\u5929",
        ),
    )


def _looks_like_current_daily_explicit_segment(segment: str) -> bool:
    text = str(segment or "")
    return _contains_any(
        text,
        (
            "\u4eca\u5929\u7684\u65e5\u62a5",
            "\u4eca\u65e5\u65e5\u62a5",
            "\u4eca\u5929\u65e5\u62a5",
            "\u4eca\u65e5\u5de5\u4f5c",
            "\u4eca\u5929\u5de5\u4f5c",
            "\u4eca\u5929\u7684\u5de5\u4f5c",
            "\u4eca\u5929\u7ee7\u7eed",
            "\u4eca\u65e5\u7ee7\u7eed",
        ),
    )


def _looks_like_explicit_legal_research_request(segment: str) -> bool:
    if _looks_like_daily_edit(segment):
        return False
    if _has_daily_time_anchor(segment):
        return False
    if _looks_like_legal_subject(segment) and _looks_like_question(segment):
        return True
    return _looks_like_legal_subject(segment) and _contains_any(
        segment,
        (
            "\u5e2e\u6211\u7814\u7a76",
            "\u5e2e\u6211\u67e5",
            "\u518d\u95ee\u4e2a\u6cd5\u5f8b\u95ee\u9898",
            "\u95ee\u4e2a\u6cd5\u5f8b\u95ee\u9898",
            "\u6cd5\u5f8b\u95ee\u9898",
            "\u6cd5\u5f8b\u4e0a",
            "\u67e5\u4e00\u4e0b",
            "\u68c0\u7d22",
            "\u7814\u7a76\u4e00\u4e0b",
            "\u5206\u6790\u4e00\u4e0b",
            "\u6cd5\u5f8b\u610f\u89c1",
            "\u88c1\u5224\u89c2\u70b9",
            "\u6700\u65b0\u88c1\u5224",
        ),
    )


def _looks_like_ambiguous_legal_research(segment: str) -> bool:
    if _has_daily_time_anchor(segment) or _looks_like_question(segment):
        return False
    return _looks_like_legal_subject(segment) and _contains_any(segment, ("\u7814\u7a76", "\u6848\u4f8b", "\u88c1\u5224"))


def _looks_like_internal_qa(segment: str) -> bool:
    if _looks_like_meta_conversation_request(segment):
        return False
    if _looks_like_lifestyle_chatter(segment):
        return False
    if _looks_like_business_ask_action(segment):
        return False
    if _looks_like_business_plan_statement(segment):
        return False
    if _looks_like_conditional_feedback_request(segment):
        return True
    if _looks_like_case_metric_data_request(segment):
        return True
    if _looks_like_daily_write(segment):
        return False
    if _looks_like_case_metric_followup_question(segment):
        return True
    scan = str(segment or "").replace("\u4e0d\u600e\u4e48", "")
    return _looks_like_question(segment) or _contains_any(
        scan,
        (
            "\u987a\u4fbf\u95ee",
            "\u95ee\u4e0b",
            "\u6709\u4ec0\u4e48",
            "\u6709\u5565",
            "\u5565\u5b89\u6392",
            "\u4ec0\u4e48\u5b89\u6392",
            "\u4ec0\u4e48\u533a\u522b",
            "\u5565\u533a\u522b",
            "\u4e0d\u4e00\u6837\u7684\u540e\u679c",
            "\u662f\u4ec0\u4e48",
            "\u6709\u591a\u5c11",
            "\u591a\u5c11",
            "\u51e0\u4e2a",
            "\u51e0\u4ef6",
            "\u51e0\u6761",
            "\u54ea\u4e9b",
            "\u5728\u54ea",
            "\u5728\u54ea\u91cc",
            "\u54ea\u91cc",
            "\u54ea\u513f",
            "\u4e0b\u8f7d",
            "\u5b58\u91cf",
            "\u65b0\u589e",
            "\u4e0b\u964d\u7387",
            "\u540d\u4e0b",
            "\u624b\u91cc",
            "\u600e\u4e48",
            "\u5982\u4f55",
            "\u80fd\u5426",
            "\u53ef\u4e0d\u53ef\u4ee5",
        ),
    )


def _looks_like_business_ask_action(segment: str) -> bool:
    text = str(segment or "").strip()
    if not text:
        return False
    if not _has_daily_time_anchor(text):
        return False
    if not _contains_any(text, ("\u95ee", "\u95ee\u4e0b", "\u95ee\u4e00\u4e0b", "\u54a8\u8be2", "\u8054\u7cfb")):
        return False
    if _contains_any(text, ("\u662f\u4e0d\u662f", "\u8981\u4e0d\u8981", "\u600e\u4e48", "\u5982\u4f55", "\u80fd\u5426", "\u80fd\u4e0d\u80fd", "\u53ef\u4e0d\u53ef\u4ee5", "\u5417")):
        return False
    return _has_business_work_object(text)


def _looks_like_conditional_feedback_request(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u7684\u8bdd\u7ed9\u4e2a\u53cd\u9988", "\u7684\u8bdd\u53cd\u9988", "\u6536\u5230\u7684\u8bdd", "\u6709\u6d88\u606f\u7684\u8bdd"))


def _looks_like_process_learning_context(segment: str) -> bool:
    text = str(segment or "")
    if _has_explicit_daily_context_in_text(text):
        return False
    if not _contains_any(text, ("\u6d41\u7a0b", "\u6a21\u677f", "\u6807\u51c6")):
        return False
    return _contains_any(
        text,
        (
            "\u5f04\u61c2",
            "\u5f04\u6e05",
            "\u7814\u7a76\u6d41\u7a0b",
            "\u4e86\u89e3\u6d41\u7a0b",
            "\u5148\u7814\u7a76",
            "\u5148\u5f04",
            "\u5148\u4e86\u89e3",
            "\u6240\u4ee5",
            "\u90a3\u4eca\u5929\u5c31\u5148",
        ),
    )


def _looks_like_case_metric_followup_question(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact or len(compact) > 12:
        return False
    if "\u5462" not in compact:
        return False
    if _contains_any(text, ("\u6cd5\u52a1\u4e00\u90e8", "\u6cd5\u52a1\u4e8c\u90e8", "\u6cd5\u52a1\u4e09\u90e8", "\u6cd5\u52a1\u56db\u90e8", "\u6cd5\u52a1\u4e94\u90e8", "\u6cd5\u52a1\u516d\u90e8")):
        return True
    if re.fullmatch(r"(?:\u4e00\u90e8|\u4e8c\u90e8|\u4e09\u90e8|\u56db\u90e8|\u4e94\u90e8|\u516d\u90e8)\u5462", compact):
        return True
    if compact.removesuffix("\u5462") in {"\u4eca\u5929", "\u660e\u5929", "\u6628\u5929", "\u6211", "\u4f60", "\u4ed6", "\u5979", "\u8fd9\u4e2a", "\u90a3\u4e2a"}:
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}\u5462", compact))


def _looks_like_daily_write(segment: str) -> bool:
    if _looks_like_conditional_feedback_request(segment):
        return False
    if _looks_like_business_ask_action(segment):
        return True
    if _looks_like_question(segment):
        return False
    if _looks_like_completed_previous_plan(segment):
        return True
    if _contains_any(
        segment,
        (
            "\u4eca\u65e5\u5de5\u4f5c",
            "\u4eca\u5929\u5de5\u4f5c",
            "\u4eca\u65e5\u5b8c\u6210",
            "\u4eca\u5929\u5b8c\u6210",
            "\u660e\u65e5\u8ba1\u5212",
            "\u660e\u5929\u8ba1\u5212",
            "\u95ee\u9898/\u98ce\u9669",
            "\u95ee\u9898\u98ce\u9669",
            "\u5de5\u4f5c\u6c47\u62a5",
        ),
    ):
        return True
    if _looks_like_daily_field_update(segment) and _has_positive_daily_work_evidence(segment):
        return True
    return _has_daily_time_anchor(segment) and _has_positive_daily_work_evidence(segment)


def _looks_like_untimed_business_work_update(segment: str) -> bool:
    if _looks_like_question(segment) or _looks_like_meta_conversation_request(segment):
        return False
    if _looks_like_daily_start_request(segment):
        return False
    if _has_daily_time_anchor(segment) or extract_problem_evidence(segment).is_problem:
        return False
    if _looks_like_followup_plan_segment(segment) and not (
        _has_business_work_action(segment) and _has_business_work_object(segment)
    ):
        return False
    if _looks_like_daily_edit(segment) or _looks_like_monthly_reply(segment, active_monthly=False):
        return False
    if _looks_like_ambiguous_legal_research(segment):
        return False
    if not (_has_business_work_action(segment) or _looks_like_daily_field_update(segment)):
        return False
    if _has_business_work_action(segment) and _has_business_work_object(segment):
        return True
    return _looks_like_legal_document_work(segment) or _contains_any(
        segment,
        (
            "\u9879\u76ee",
            "\u6848\u4ef6",
            "\u6848\u53f7",
            "\u7834\u4ea7\u6848",
            "\u8bc9\u8bbc\u6750\u6599",
            "\u6750\u6599",
            "\u6cd5\u9662",
            "\u5f00\u5ead",
            "\u6267\u884c",
        ),
    )


def _looks_like_legal_document_work(segment: str) -> bool:
    compact = _compact(segment)
    if not compact or _looks_like_question(segment):
        return False
    has_document = _contains_any(
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
    if not has_document:
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


def _looks_like_report_later_deferral(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u7b49\u7ed3\u679c\u51fa\u6765\u518d\u62a5", "\u7b49\u6709\u7ed3\u679c\u518d\u62a5", "\u7b49\u7ed3\u679c\u51fa\u6765\u518d\u5199", "\u7b49\u6709\u7ed3\u679c\u518d\u5199", "\u51fa\u7ed3\u679c\u518d\u62a5")) and _contains_any(
        compact,
        ("\u65e5\u62a5", "\u65e5\u5fd7", "\u62a5\u5427", "\u62a5"),
    )


def _looks_like_meta_conversation_request(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if _contains_any(text, ("\u603b\u7ed3\u4e00\u4e0b", "\u603b\u7ed3\u4e0b", "\u5206\u6790\u4e00\u4e0b", "\u5206\u6790\u4e0b", "\u63d0\u70bc\u4e00\u4e0b", "\u63d0\u70bc\u4e0b")) and _contains_any(
        text, ("\u521a\u624d", "\u90a3\u4e2a", "\u6848\u5b50", "\u6848", "\u98ce\u9669\u70b9", "\u98ce\u9669")
    ):
        return True
    if _looks_like_daily_edit(text) or _has_daily_context_in_text(text):
        return False
    if compact in {
        "咋说",
        "怎么说",
        "咋聊",
        "咋闲聊",
        "怎么闲聊",
        "能闲聊吗",
        "可以闲聊吗",
        "聊聊",
        "聊会",
        "聊一会",
        "和我聊天",
        "和我聊聊",
        "跟我聊",
        "跟我聊聊",
        "陪我聊",
        "陪我聊聊",
        "说说话",
        "可以和我聊聊吗",
        "可以和我聊聊",
    }:
        return True
    return _contains_any(
        text,
        (
            "\u6211\u60f3\u804a",
            "\u60f3\u804a",
            "\u804a\u804a",
            "\u804a\u4f1a",
            "\u804a\u4e00\u4f1a",
            "\u548c\u6211\u804a",
            "\u8ddf\u6211\u804a",
            "\u966a\u6211\u804a",
            "\u8bf4\u8bf4\u8bdd",
            "\u95f2\u804a",
            "\u804a\u5929",
            "\u600e\u4e48\u95f2\u804a",
            "\u80fd\u95f2\u804a",
            "\u53ef\u4ee5\u95f2\u804a",
            "\u8ba8\u8bba\u5427",
            "\u60f3\u8bf4\u4e2a",
            "\u60f3\u8bf4\u4e00\u4e2a",
            "\u6211\u60f3\u8bf4\u4e2a",
            "\u6211\u60f3\u8bf4\u4e00\u4e2a",
            "agent2",
            "Agent2",
            "\u7070\u6d4b",
            "\u4f60\u591a\u5927",
            "\u4f60\u51e0\u5c81",
            "\u4f60\u662f\u8c01",
            "\u4f60\u53eb\u4ec0\u4e48",
            "\u4f60\u80fd\u505a\u4ec0\u4e48",
            "\u4f60\u80fd\u5e72\u561b",
            "\u4f60\u90fd\u8bb0\u5f55",
            "\u4f60\u8bb0\u5f55",
            "\u8bb0\u5f55\u7684\u662f\u5565",
            "\u8bb0\u5f55\u7684\u662f\u4ec0\u4e48",
            "\u4f60\u5199\u7684\u662f\u5565",
            "\u4f60\u5199\u7684\u662f\u4ec0\u4e48",
            "\u73b0\u5728\u662f\u5565",
            "\u73b0\u5728\u662f\u4ec0\u4e48",
            "\u73b0\u5728\u662f",
            "\u6a21\u5f0f",
        ),
    )


def _looks_like_daily_field_update(segment: str) -> bool:
    return _contains_any(
        segment,
        (
            "\u8865\u5145",
            "\u52a0\u4e00\u4e2a",
            "\u65b0\u589e",
            "\u518d\u52a0",
            "\u8bb0\u4e00\u4e2a",
            "\u95ee\u9898/\u98ce\u9669",
            "\u95ee\u9898\u98ce\u9669",
        ),
    )


def _has_positive_daily_work_evidence(segment: str) -> bool:
    compact = _compact(segment)
    if _contains_any(
        compact,
        (
            "\u4e0d\u60f3\u5199\u65e5\u62a5",
            "\u65e5\u62a5\u90fd\u4e0d\u60f3\u5199",
            "\u65e5\u62a5\u4e0d\u60f3\u5199",
            "\u4e0d\u60f3\u586b\u65e5\u62a5",
            "\u61d2\u5f97\u5199\u65e5\u62a5",
            "\u61d2\u5f97\u586b\u65e5\u62a5",
            "\u4e0d\u5199\u65e5\u62a5",
            "\u4e0d\u586b\u65e5\u62a5",
        ),
    ):
        return False
    if _looks_like_absurd_content(segment) or _looks_like_lifestyle_report_meta_chatter(segment):
        return False
    if _looks_like_business_ask_action(segment):
        return True
    if _looks_like_question(segment):
        return False
    if _looks_like_travel_event(segment):
        return True
    if _specific_matter_hint(segment):
        return True
    if _contains_any(segment, ("\u5f00\u5ead", "\u51fa\u5ead", "\u76d6\u7ae0", "\u7528\u5370")):
        return True
    return _has_business_work_action(segment) and _has_business_work_object(segment)


def _has_business_problem_evidence(segment: str) -> bool:
    return extract_problem_evidence(segment).is_business_problem


def _has_business_work_action(segment: str) -> bool:
    return _contains_any(
        segment,
        (
            "\u641e\u5b8c",
            "\u641e",
            "\u5f04",
            "\u6574",
            "\u6539",
            "\u6362",
            "\u505a",
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
            "\u8ddf\u8fdb",
            "\u53d1",
            "\u53d1\u7ed9",
            "\u5bc4",
            "\u5bc4\u51fa",
            "\u6536\u5230",
            "\u7b49\u5f85",
            "\u62df",
            "\u62df\u5b9a",
            "\u5ba1",
            "\u5ba1\u6838",
            "\u6574\u7406",
            "\u68b3\u7406",
            "\u540c\u6b65",
            "\u6c9f\u901a",
            "\u8ba8\u8bba",
            "\u6c47\u62a5",
            "\u901a\u7535\u8bdd",
            "\u901a\u8fc7\u7535\u8bdd",
            "\u786e\u8ba4",
            "\u4f18\u5316",
            "\u5f00\u53d1",
            "\u6d4b\u8bd5",
            "\u8bbe\u8ba1",
            "\u8c03\u7814",
            "\u7814\u7a76",
            "\u64b0\u5199",
            "\u8d77\u8349",
            "\u4fee\u8ba2",
            "\u5b8c\u5584",
            "\u63d0\u4ea4",
            "\u7533\u62a5",
            "\u6267\u884c",
            "\u6062\u590d",
            "\u63a8\u8fdb",
            "\u6536\u96c6",
            "\u8865\u5145",
            "\u5bf9\u63a5",
            "\u53cd\u9988",
            "\u5d29",
            "\u4fdd\u5b58",
            "\u767b\u8bb0",
            "\u5f52\u6863",
            "\u66f4\u65b0",
            "\u95ed\u73af",
            "\u95ed\u5408",
            "\u6392\u67e5",
            "\u4fee\u590d",
            "\u4e0a\u7ebf",
            "\u90e8\u7f72",
            "\u586b\u62a5",
            "\u6c47\u603b",
            "\u6c47\u62a5",
            "\u53d1\u9001",
            "\u51fa\u5177",
            "\u529e\u7406",
            "\u8c03\u6574",
            "\u8c03\u89e3",
            "\u5b9a\u7a3f",
            "\u5b89\u629a",
            "\u626b\u5c3e",
            "\u7acb\u6848",
            "\u64a4\u8bc9",
            "\u64a4\u56de",
            "\u56de\u6b3e",
            "\u50ac\u6536",
            "\u6e05\u6536",
            "\u5316\u503a",
            "\u8bc4\u5ba1",
            "\u542f\u52a8",
            "\u5bf9",
            "\u5bf9\u4e86",
            "\u4fee",
            "\u4fee\u4e86",
            "\u4e0a\u7ebf",
            "review",
            "Review",
        ),
    )


def _has_business_work_object(segment: str) -> bool:
    text = str(segment or "")
    if re.search(r"\u5f00\u4e86?[0-9\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]*\u4e2a?\u4f1a", text):
        return True
    if _contains_any(text, ("\u79bb\u804c", "\u5165\u804c")) and _contains_any(
        text,
        ("\u624b\u7eed", "\u6750\u6599", "\u4ea4\u63a5", "\u5bf9\u63a5"),
    ):
        return True
    return _contains_any(
        text,
        (
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
            "\u5370\u7ae0",
            "\u516c\u7ae0",
            "\u516c\u8bc1\u5904",
            "\u7528\u5370",
            "\u51fd\u4ef6",
            "\u5f8b\u5e08",
            "\u88ab\u544a\u5f8b\u5e08",
            "\u6750\u6599",
            "\u8d44\u6599",
            "\u6863\u6848",
            "\u8bc1\u636e",
            "\u8bc1\u636e\u6e05\u5355",
            "\u6e05\u5355",
            "\u6e05\u5355",
            "\u6848\u4ef6",
            "\u6848\u5b50",
            "\u6848\u53f7",
            "\u8ba8\u85aa",
            "\u8d77\u8bc9\u72b6",
            "\u9879\u76ee",
            "\u8fdb\u5ea6",
            "\u7cfb\u7edf",
            "\u6a21\u5757",
            "\u7b97\u6cd5",
            "\u6280\u672f",
            "\u8c03\u7814",
            "\u53f0\u8d26",
            "\u6d41\u7a0b",
            "\u5de5\u5177",
            "\u65e5\u62a5",
            "\u65e5\u5fd7",
            "\u6708\u62a5",
            "\u5468\u62a5",
            "\u6587\u6863",
            "\u6587\u4ef6",
            "\u8bbe\u8ba1",
            "\u767b\u5f55\u529f\u80fd",
            "\u529f\u80fd",
            "\u9884\u7b97",
            "\u8d22\u52a1",
            "\u73b0\u573a",
            "\u9700\u6c42\u5206\u6790",
            "\u6a21\u677f",
            "\u6570\u636e",
            "\u9700\u6c42",
            "\u53d8\u66f4",
            "\u5f00\u53d1",
            "\u7814\u53d1",
            "\u8fed\u4ee3",
            "\u63a5\u53e3",
            "\u5546\u52a1",
            "\u4e1a\u52a1",
            "\u90ae\u4ef6",
            "\u673a\u5668\u4eba",
            "\u4f1a\u8bae",
            "\u8bae\u7a0b",
            "\u901a\u77e5",
            "\u57f9\u8bad",
            "\u8bc4\u5ba1\u4f1a",
            "\u4f8b\u4f1a",
            "\u65b0\u89c4",
            "\u5ead\u524d\u4f1a\u8bae",
            "\u6cd5\u5f8b\u610f\u89c1\u4e66",
            "\u8f66\u7968",
            "\u6cd5\u9662",
            "\u6cd5\u5b98",
            "\u5ba2\u6237",
            "\u53d1\u7968",
            "\u94f6\u884c",
            "\u6295\u8bc9",
            "\u5ba2\u6237\u6295\u8bc9",
            "\u5ba2\u8bc9",
            "\u4f9b\u5e94\u5546",
            "\u4e1a\u52a1",
            "\u90e8\u95e8",
            "\u5ba1\u6279",
            "\u8868\u683c",
            "\u62a5\u8868",
            "\u4ea4\u8868",
            "\u5de5\u5355",
            "\u98ce\u9669",
            "\u6f0f\u6d1e",
            "\u5b89\u5168\u6f0f\u6d1e",
            "\u65b9\u6848",
            "\u62a5\u544a",
            "\u7ed3\u7b97",
            "\u6536\u6b3e",
            "\u56de\u6b3e",
            "\u5229\u606f",
            "\u7d22\u8d54",
            "\u975e\u8bc9",
            "\u6267\u884c",
            "\u7834\u4ea7",
            "\u503a\u6743",
            "\u6280\u80fd",
            "\u6848\u4f8b",
            "\u88c1\u5224",
            "\u6cd5\u5f8b",
            "\u4f18\u5148\u53d7\u507f\u6743",
            "\u8bc9\u8bbc\u65f6\u6548",
            "\u62b5\u62bc\u6743",
        ),
    )


def _looks_like_daily_edit(segment: str) -> bool:
    if _looks_like_symbolic_replacement_edit(segment):
        return True
    if looks_like_contextual_daily_edit(segment):
        return True
    if _contains_any(
        segment,
        (
            "\u6539\u6210",
            "\u6539\u4e3a",
            "\u4fee\u6539",
            "\u66ff\u6362",
            "\u5220\u6389",
            "\u5220\u9664",
            "\u5220\u4e86",
            "\u53bb\u6389",
            "\u5408\u5e76",
            "\u5408\u6210",
            "\u79fb\u5230",
            "\u590d\u5236",
            "\u52a0\u5230",
            "\u52a0\u8fdb",
            "\u52a0\u5165",
            "\u6e05\u7a7a",
            "\u590d\u5236\u6628\u5929",
        ),
    ):
        return True
    text = str(segment or "")
    daily_target = r"(\u65e5\u62a5|\u65e5\u5fd7|\u8349\u7a3f|\u4eca\u65e5\u5de5\u4f5c|\u4eca\u5929\u5de5\u4f5c|\u4eca\u5929\u7684?\u7b2c[0-9\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+\u6761\u5de5\u4f5c|\u7b2c[0-9\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+\u6761\u5de5\u4f5c|\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212|\u95ee\u9898/\u98ce\u9669|\u95ee\u9898\u98ce\u9669)"
    edit_verb = r"(\u6539|\u5220|\u5408\u5e76|\u6e05\u7a7a|\u4f18\u5316\u63aa\u8f9e)"
    return bool(re.search(rf"{daily_target}.{{0,16}}{edit_verb}|{edit_verb}.{{0,16}}{daily_target}", text))


def _daily_edit_allowed(segment: str, *, active_daily: bool) -> bool:
    if active_daily and _looks_like_symbolic_replacement_edit(segment):
        return True
    if active_daily and _looks_like_quoted_delete_edit(segment):
        return True
    if active_daily and _looks_like_quantity_correction(segment):
        return True
    if not _looks_like_daily_edit(segment):
        return False
    if active_daily:
        return True
    return _contains_any(
        segment,
        (
            "\u65e5\u62a5",
            "\u65e5\u5fd7",
            "\u8349\u7a3f",
            "\u4eca\u65e5\u5de5\u4f5c",
            "\u4eca\u5929\u5de5\u4f5c",
            "\u4eca\u5929\u7684\u7b2c\u4e00\u6761\u5de5\u4f5c",
            "\u4eca\u5929\u7b2c\u4e00\u6761\u5de5\u4f5c",
            "\u7b2c\u4e00\u6761\u5de5\u4f5c",
            "\u7b2c1\u6761\u5de5\u4f5c",
            "\u95ee\u9898/\u98ce\u9669",
            "\u95ee\u9898\u98ce\u9669",
            "\u660e\u65e5\u8ba1\u5212",
            "\u660e\u5929\u8ba1\u5212",
            "\u65e5\u5fd7",
        ),
    )


def _looks_like_symbolic_replacement_edit(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return bool(re.search(r"\u628a[A-Za-z0-9\u4e00-\u9fa5]{1,12}\u6539(?:\u6210|\u4e3a)[A-Za-z0-9\u4e00-\u9fa5]{1,12}", compact))


def _looks_like_bare_daily_fragment(segment: str, *, active_daily: bool, whole_text: str) -> bool:
    if active_daily or _has_daily_context_in_text(whole_text):
        return False
    compact = re.sub(r"\s+", "", str(segment or ""))
    if not compact:
        return False
    if compact in {"\u95ee\u9898", "\u95ee\u9898\u5427", "\u95ee\u9898\u5427\u554a", "\u6709\u95ee\u9898", "\u6709\u70b9\u5927\u95ee\u9898", "\u6ca1\u95ee\u9898", "\u6ca1\u5565\u95ee\u9898"}:
        return True
    return len(compact) <= 8 and _contains_any(compact, ("\u95ee\u9898", "\u98ce\u9669")) and not _contains_any(
        compact,
        ("\u65e5\u62a5", "\u4eca\u65e5", "\u4eca\u5929", "\u660e\u65e5", "\u660e\u5929"),
    )


def _has_daily_context_in_text(text: str) -> bool:
    if _has_explicit_daily_context_in_text(text):
        return True
    return _has_daily_time_anchor(text) and _has_positive_daily_work_evidence(text)


def _has_explicit_daily_context_in_text(text: str) -> bool:
    return _contains_any(
        text,
        (
            "\u65e5\u62a5",
            "\u65e5\u5fd7",
            "\u4eca\u65e5\u5de5\u4f5c",
            "\u4eca\u5929\u5de5\u4f5c",
            "\u4eca\u65e5\u5b8c\u6210",
            "\u4eca\u5929\u5b8c\u6210",
            "\u660e\u65e5\u8ba1\u5212",
            "\u660e\u5929\u8ba1\u5212",
        ),
    )


def _looks_like_problem(segment: str) -> bool:
    return extract_problem_evidence(segment).is_explicit_problem


def _looks_like_followup_plan_segment(segment: str) -> bool:
    if _looks_like_question(segment):
        return False
    compact = _compact(segment)
    if _contains_any(compact, ("\u8fd9\u4e2a\u4e8b", "\u8fd9\u4ef6\u4e8b", "\u8fd9\u4e8b", "\u524d\u8ff0\u4e8b\u9879")) and _contains_any(
        compact,
        ("\u660e\u5929\u8fd8\u5f97\u7ee7\u7eed", "\u660e\u5929\u8fd8\u8981\u7ee7\u7eed", "\u660e\u65e5\u8fd8\u5f97\u7ee7\u7eed", "\u5199\u5230\u660e\u65e5\u8ba1\u5212", "\u5199\u5230\u660e\u5929\u8ba1\u5212"),
    ):
        return True
    return _contains_any(segment, ("\u56de\u6765\u540e", "\u4e4b\u540e", "\u968f\u540e", "\u518d", "\u7ee7\u7eed")) and (
        _contains_any(
            segment,
            (
                "\u5b8c\u5584",
                "\u5904\u7406",
                "\u8ddf\u8fdb",
                "\u6574\u7406",
                "\u68b3\u7406",
                "\u6c9f\u901a",
                "\u63a8\u8fdb",
                "\u5ba1\u6838",
            ),
        )
        or _contains_any(segment, ("\u53f0\u8d26", "\u6750\u6599", "\u5408\u540c", "\u51fd\u4ef6", "\u6848\u4ef6", "\u6d41\u7a0b"))
    )


def _looks_like_no_problem(segment: str) -> bool:
    return extract_problem_evidence(segment).is_no_problem


def _looks_like_completed_previous_plan(segment: str) -> bool:
    if _looks_like_question(segment) or _looks_like_previous_daily_plan_completion_question(segment):
        return False
    explicit_plan_done = (
        _contains_any(segment, ("\u6628\u5929", "\u6628\u65e5", "\u524d\u4e00\u5929", "\u4e0a\u4e00\u4e2a\u5de5\u4f5c\u65e5"))
        and _contains_any(
            segment,
            (
                "\u660e\u65e5\u8ba1\u5212",
                "\u660e\u5929\u8ba1\u5212",
                "\u8ba1\u5212",
                "\u5f85\u529e",
                "\u5b89\u6392",
                "\u4e8b\u9879",
            ),
        )
        and _contains_any(
            segment,
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
    compact = _compact(segment)
    return (
        _contains_any(compact, ("\u6628\u5929\u6211\u8bf4\u4eca\u5929\u8981", "\u6628\u5929\u8bf4\u4eca\u5929\u8981", "\u6628\u5929\u63d0\u5230\u4eca\u5929\u8981"))
        and _contains_any(
            segment,
            ("\u5df2\u7ecf", "\u5df2", "\u5b8c\u4e86", "\u5b8c", "\u5b8c\u6210", "\u641e\u5b9a", "\u505a\u5b8c", "\u89c1\u5b8c", "\u5ba1\u5b8c"),
        )
        and (_has_business_work_action(segment) or _has_business_work_object(segment))
    )


def _has_current_work_after_completed_previous_plan(segment: str) -> bool:
    text = str(segment or "")
    if not _looks_like_completed_previous_plan(text):
        return False
    for marker in ("\u4eca\u5929", "\u4eca\u65e5"):
        index = text.find(marker)
        if index < 0:
            continue
        tail = text[index:]
        if re.match(
            rf"{marker}(?:\u8fd8|\u8fd8\u8981|\u53c8|\u4e5f|\u540c\u65f6|\u7ee7\u7eed|\u53e6\u5916|\u6b64\u5916|\u518d|\u65b0\u589e|\u8865\u5145)",
            tail,
        ) and (_has_positive_daily_work_evidence(tail) or _has_business_work_action(tail) or _has_business_work_object(tail)):
            return True
        if re.match(
            rf"{marker}(?:\u4e3b\u8981|\u4e3b\u8981\u662f|\u5c31\u662f|\u5b8c\u6210|\u5904\u7406|\u63a8\u8fdb|\u8ddf\u8fdb|\u5ba1\u6838|\u6574\u7406|\u67e5\u9605|\u67e5|\u6c9f\u901a|\u626b\u5c3e)",
            tail,
        ) and (_has_positive_daily_work_evidence(tail) or _has_business_work_action(tail) or _has_business_work_object(tail)):
            return True
    return False


def _looks_like_travel_event(segment: str) -> bool:
    if _looks_like_product_work(segment) and not _looks_like_concrete_trip(segment):
        return False
    return _looks_like_concrete_trip(segment)


def _looks_like_concrete_trip(segment: str) -> bool:
    text = str(segment or "")
    if _looks_like_absurd_content(text) or _looks_like_weak_travel_destination_fragment(text):
        return False
    if "\u51fa\u5dee" in text and _travel_destination(text):
        return True
    known_destination = _known_travel_destination(text)
    if known_destination and _has_daily_time_anchor(text) and re.search(
        rf"(?:\u8981|\u8ba1\u5212|\u51c6\u5907|\u4f30\u8ba1|\u53ef\u80fd)?(?:\u53bb|\u8d74|\u5230|\u524d\u5f80)\s*{re.escape(known_destination)}",
        text,
    ):
        return True
    if _has_daily_time_anchor(text) and _travel_destination(text) and _contains_any(
        text, ("\u5f00\u5ead", "\u76d6\u7ae0", "\u8d70\u8bbf", "\u5904\u7406", "\u6c9f\u901a", "\u8ba8\u85aa", "\u53c2\u52a0", "\u5cf0\u4f1a")
    ):
        return True
    return bool(
        re.search(
            r"(\u53bb|\u8d74|\u5230|\u53bb\u4e86).{0,12}(\u5f00\u5ead|\u76d6\u7ae0|\u8d70\u8bbf|\u5904\u7406|\u6c9f\u901a|\u8ba8\u85aa|\u53c2\u52a0|\u5cf0\u4f1a)",
            text,
        )
        and _travel_destination(text)
    )


def _looks_like_product_work(segment: str) -> bool:
    return _contains_any(segment, ("\u505a", "\u5f00\u53d1", "\u4f18\u5316", "\u5efa\u8bbe", "\u642d\u5efa", "\u6d4b\u8bd5")) and _contains_any(
        segment,
        (
            "\u7cfb\u7edf",
            "\u6a21\u5757",
            "\u529f\u80fd",
            "\u5de5\u5177",
            "\u5e73\u53f0",
            "\u673a\u5668\u4eba",
            "\u51fa\u5dee\u534f\u540c",
            "\u6848\u4ef6\u8fdb\u5c55",
        ),
    )


def _travel_destination(segment: str) -> str:
    text = str(segment or "")
    known_destination = _known_travel_destination(text)
    if known_destination:
        return known_destination
    match = re.search(
        r"(?:\u51fa\u5dee|\u53bb|\u8d74|\u5230|\u53bb\u4e86)([\u4e00-\u9fa5]{2,8}?)(?:\u529e\u7406|\u5f00\u5ead|\u76d6\u7ae0|\u8d70\u8bbf|\u5904\u7406|\u6c9f\u901a|\u8ba8\u85aa|\u51fa\u5dee|$)",
        text,
    )
    if not match:
        return ""
    candidate = re.sub(r"^(?:\u53bb|\u5230|\u8d74|\u524d\u5f80)", "", match.group(1))
    if _contains_any(
        candidate,
        (
            "\u534f\u540c",
            "\u534f\u540c\u7cfb\u7edf",
            "\u7cfb\u7edf",
            "\u6a21\u5757",
            "\u529f\u80fd",
            "\u5de5\u5177",
            "\u5e73\u53f0",
            "\u673a\u5668\u4eba",
            "\u5de5\u4f5c",
        ),
    ):
        return ""
    if _invalid_travel_destination_candidate(candidate):
        return ""
    return candidate


def _known_travel_destination(text: str) -> str:
    value = str(text or "")
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
        if destination in value:
            return destination
    return ""


def _invalid_travel_destination_candidate(candidate: str) -> bool:
    value = str(candidate or "").strip(" ：:，,。；;、的")
    if not value:
        return True
    if _contains_any(
        value,
        (
            "\u5f00\u5ead",
            "\u51fa\u5ead",
            "\u76d6\u7ae0",
            "\u8d70\u8bbf",
            "\u5904\u7406",
            "\u6c9f\u901a",
            "\u8ba8\u85aa",
            "\u6848\u4ef6",
            "\u6848",
            "\u8fdb\u5c55",
            "\u6750\u6599",
            "\u5dee\u5f02",
            "\u7ade\u4e89",
            "\u7b56\u7565",
            "\u7b80\u62a5",
            "\u6c47\u62a5",
            "\u62a5\u544a",
            "\u65b9\u6848",
        ),
    ):
        return True
    return False


def _date_hint(segment: str, *, received_at: Any = None) -> str:
    relative_hint = date_hint_from_text(segment, received_at=received_at)
    if relative_hint != "unknown":
        return relative_hint
    if _contains_any(segment, ("\u660e\u5929", "\u660e\u65e5")):
        return "tomorrow"
    if _contains_any(segment, ("\u4eca\u5929", "\u4eca\u65e5")):
        return "today"
    if "\u4e0b\u5468" in segment:
        return "next_week"
    return "unknown"


def _travel_status(segment: str, *, received_at: Any = None) -> str:
    if "\u53ef\u80fd" in segment:
        return "tentative"
    relative_hint = date_hint_from_text(segment, received_at=received_at)
    if relative_hint in {"tomorrow", "future_weekday", "next_week"}:
        return "planned"
    if _contains_any(segment, ("\u660e\u5929", "\u660e\u65e5", "\u8ba1\u5212", "\u62df", "\u9884\u8ba1", "\u5e94\u8be5", "\u6253\u7b97", "\u51c6\u5907")):
        return "planned"
    if _contains_any(segment, ("\u53bb\u4e86", "\u5df2\u53bb", "\u5df2\u7ecf\u5230", "\u4eca\u5929", "\u4eca\u65e5")):
        return "already_traveled"
    if "\u51fa\u5dee" in segment:
        return "already_traveled"
    return "unknown"


def _specific_matter_hint(segment: str) -> str:
    text = str(segment or "")
    if _looks_like_case_count_or_lookup_question(text):
        return ""
    if not _contains_any(text, ("\u6848", "\u6848\u4ef6", "\u6848\u53f7", "\u6cd5\u9662", "\u6267\u884c\u8fdb\u5c55", "\u5f00\u5ead")):
        return ""
    if _looks_like_case_product_build_work(text):
        return ""
    if _contains_any(
        text,
        (
            "\u6848\u4f8b",
            "\u672c\u6848",
            "\u65b9\u6848",
            "\u6863\u6848",
            "\u5224\u6848",
            "\u6848\u4ef6\u8fdb\u5c55",
            "\u6848\u4ef6\u6c47\u62a5",
            "\u6848\u4ef6\u6c9f\u901a",
            "\u6848\u4ef6\u6750\u6599",
            "\u6848\u4ef6\u8d44\u6599",
            "\u6848\u4ef6\u7ba1\u7406",
            "\u6848\u4ef6\u53f0\u8d26",
            "\u6848\u4ef6\u8282\u70b9",
            "\u6848\u4ef6\u4e8b\u9879",
            "\u91cd\u70b9\u6848\u4ef6",
            "\u76f8\u5173\u6848\u4ef6",
            "\u90e8\u5206\u6848\u4ef6",
            "\u6cd5\u52a1\u5c0f\u7fa4\u6848\u4ef6",
        ),
    ):
        return ""
    patterns = [r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}?\u6848\u4ef6)", r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}?\u6848)(?!\u4ef6)"]
    if _contains_any(text, ("\u6cd5\u9662", "\u6267\u884c\u8fdb\u5c55", "\u5f00\u5ead")):
        patterns.append(r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}?\u4e8b\u9879)")
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            candidate = _clean_matter_hint_candidate(match.group(1))
            if _valid_matter_hint(candidate):
                return candidate
    return ""


def _looks_like_case_count_or_lookup_question(text: str) -> bool:
    value = str(text or "")
    if not _contains_any(value, ("\u6848", "\u6848\u4ef6", "\u539f\u544a", "\u88ab\u544a", "\u5b58\u91cf", "\u65b0\u589e")):
        return False
    return _contains_any(
        value,
        (
            "\u6709\u591a\u5c11",
            "\u591a\u5c11",
            "\u51e0\u4e2a",
            "\u51e0\u4ef6",
            "\u51e0\u6761",
            "\u54ea\u4e9b",
            "\u67e5\u4e00\u4e0b",
            "\u67e5\u4e0b",
            "\u770b\u4e0b",
            "\u7edf\u8ba1",
            "\u5b58\u91cf",
            "\u65b0\u589e",
            "\u4e0b\u964d\u7387",
            "\u540d\u4e0b",
            "\u624b\u91cc",
        ),
    )


def _looks_like_case_metric_data_request(text: str) -> bool:
    value = str(text or "")
    if not value:
        return False
    if _has_daily_time_anchor(value) and _looks_like_daily_write(value):
        return False
    case_subject = _contains_any(value, ("\u6848", "\u6848\u4ef6", "\u539f\u544a", "\u88ab\u544a"))
    metric_subject = _contains_any(value, ("\u5b58\u91cf", "\u65b0\u589e", "\u4e0b\u964d\u7387", "\u672a\u7ed3\u6848", "\u5df2\u7ed3\u6848"))
    if not case_subject and not metric_subject:
        return False
    request_marker = _contains_any(
        value,
        (
            "\u6709\u591a\u5c11",
            "\u591a\u5c11",
            "\u51e0\u4e2a",
            "\u51e0\u4ef6",
            "\u51e0\u6761",
            "\u7edf\u8ba1",
            "\u6570\u91cf",
            "\u6570\u636e",
            "\u60c5\u51b5",
            "\u54ea\u4e9b",
            "\u660e\u7ec6",
            "\u540d\u5355",
            "\u53d1\u6211",
            "\u53d1\u4e00\u4e0b",
            "\u53d1\u4e0b",
            "\u53d1\u7ed9\u6211",
            "\u7ed9\u6211",
            "\u770b\u4e00\u4e0b",
            "\u770b\u4e0b",
            "\u67e5\u4e00\u4e0b",
            "\u67e5\u4e0b",
        ),
    )
    scope_marker = _contains_any(
        value,
        (
            "\u603b\u4f53",
            "\u6574\u4f53",
            "\u603b\u91cf",
            "\u603b\u6570",
            "\u5168\u90e8",
            "\u6240\u6709",
            "\u6cd5\u52a1\u4e00\u90e8",
            "\u6cd5\u52a1\u4e8c\u90e8",
            "\u6cd5\u52a1\u4e09\u90e8",
            "\u6cd5\u52a1\u56db\u90e8",
            "\u6cd5\u52a1\u4e94\u90e8",
            "\u6cd5\u52a1\u516d\u90e8",
        ),
    )
    return metric_subject and (request_marker or scope_marker)


def _looks_like_history_info_delivery_request(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    has_history_scope = _contains_any(compact, ("\u4e0a\u5468", "\u4e0a\u4e2a\u6708", "\u4e0a\u6708", "\u4e0a\u5b63\u5ea6", "\u524d\u51e0\u5929", "\u8fc7\u53bb"))
    has_subject = _contains_any(compact, ("\u5f00\u5ead\u60c5\u51b5", "\u5f00\u5ead", "\u6848\u4ef6\u60c5\u51b5", "\u5de5\u4f5c\u60c5\u51b5", "\u65e5\u62a5\u60c5\u51b5"))
    has_delivery = _contains_any(compact, ("\u53d1\u6211", "\u53d1\u6211\u4e0b", "\u53d1\u7ed9\u6211", "\u7ed9\u6211", "\u770b\u4e0b", "\u67e5\u4e0b"))
    return has_history_scope and has_subject and has_delivery


def _clean_matter_hint_candidate(candidate: str) -> str:
    value = str(candidate or "").strip(" ：:，,。；;、")
    value = re.sub(r"^(?:\u4eca\u5929|\u4eca\u65e5|\u660e\u5929|\u660e\u65e5|\u6628\u5929|\u6628\u65e5|\u4e0b\u5468[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u5929]?)", "", value)
    value = re.sub(r"^(?:\u540e\u5929|\u672c\u5468[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u5929]?|\u53bb|\u8d74|\u5230|\u524d\u5f80|\u51fa\u5dee)+", "", value)
    for marker in (
        "\u6c9f\u901a",
        "\u5904\u7406",
        "\u8ddf\u8fdb",
        "\u63a8\u8fdb",
        "\u529e\u7406",
        "\u534f\u8c03",
        "\u5bf9\u63a5",
        "\u7814\u7a76",
        "\u6574\u7406",
        "\u8865\u5145",
        "\u5f00\u5ead",
    ):
        if marker in value:
            tail = value.rsplit(marker, 1)[-1].strip(" ：:，,。；;、")
            if len(tail) >= 3:
                value = tail
    return value


def _valid_matter_hint(candidate: str) -> bool:
    value = str(candidate or "").strip(" ：:，,。；;、")
    if len(value) < 3:
        return False
    if _contains_any(
        value,
        (
            "\u65b9\u6848",
            "\u6863\u6848",
            "\u5224\u6848",
            "\u672c\u6848",
            "\u6848\u60c5",
            "\u6848\u4f8b",
            "\u7b54\u6848",
            "\u884c\u52a8\u65b9\u6848",
            "\u7ecf\u8425\u65b9\u6848",
            "\u5de5\u4f5c\u65b9\u6848",
            "\u5206\u914d\u65b9\u6848",
            "\u670d\u52a1\u5668\u65b9\u6848",
            "\u516c\u53f8\u6863\u6848",
            "\u6cd5\u52a1\u5c0f\u7fa4\u6848\u4ef6",
            "\u6848\u4ef6\u6c47\u62a5",
        ),
    ):
        return False
    if value.endswith(("\u65b9\u6848", "\u6863\u6848", "\u5224\u6848")):
        return False
    if value in {
        "\u6848\u4ef6",
        "\u62df\u8bc9\u6848\u4ef6",
        "\u5f85\u8bc9\u6848\u4ef6",
        "\u8bc9\u8bbc\u6848\u4ef6",
        "\u88ab\u544a\u6848\u4ef6",
        "\u8bbe\u8ba1\u6848\u4ef6",
        "\u90e8\u5206\u6848\u4ef6",
        "\u76f8\u5173\u6848\u4ef6",
        "\u91cd\u70b9\u6848\u4ef6",
        "\u6240\u6709\u6848\u4ef6",
        "\u5168\u90e8\u6848\u4ef6",
        "\u6cd5\u52a1\u5c0f\u7fa4\u6848\u4ef6",
        "\u6848\u4ef6\u6c47\u62a5",
    }:
        return False
    if value.endswith("\u6848\u4ef6"):
        prefix = value[: -len("\u6848\u4ef6")]
        if len(prefix) < 2:
            return False
        if _contains_any(prefix, ("\u51fa\u5dee", "\u6c9f\u901a", "\u5904\u7406", "\u529e\u7406", "\u5f00\u5ead", "\u53bb", "\u8d74", "\u5230")):
            return False
        if prefix in {"\u62df\u8bc9", "\u5f85\u8bc9", "\u8bc9\u8bbc", "\u88ab\u544a", "\u8bbe\u8ba1", "\u90e8\u5206", "\u76f8\u5173", "\u91cd\u70b9", "\u6240\u6709", "\u5168\u90e8", "\u4e00\u822c", "\u591a\u4e2a", "\u5404\u7c7b"}:
            return False
    return True


def _looks_like_case_product_build_work(text: str) -> bool:
    return _contains_any(text, ("\u6848\u4ef6", "\u6848\u4ef6\u8fdb\u5c55", "\u6848")) and _contains_any(
        text,
        (
            "\u7cfb\u7edf",
            "\u6a21\u5757",
            "\u5de5\u5177",
            "\u5e73\u53f0",
            "\u529f\u80fd",
            "\u81ea\u52a8\u5173\u8054",
            "\u56fa\u5b9a\u65f6\u95f4",
            "\u8282\u70b9\u8be2\u95ee",
            "\u6784\u5efa",
            "\u5b9e\u73b0",
            "\u5f00\u53d1",
            "\u4f18\u5316",
        ),
    )


def _looks_like_legal_subject(segment: str) -> bool:
    return _contains_any(
        segment,
        (
            "\u6cd5\u5f8b",
            "\u6cd5\u89c4",
            "\u6cd5\u6761",
            "\u53f8\u6cd5\u89e3\u91ca",
            "\u6cd5\u5f8b\u95ee\u9898",
            "\u6cd5\u5f8b\u540e\u679c",
            "\u5224\u4f8b",
            "\u6848\u4f8b",
            "\u88c1\u5224",
            "\u4f18\u5148\u53d7\u507f\u6743",
            "\u7ade\u4e1a\u9650\u5236",
            "\u7834\u4ea7",
            "\u6267\u884c\u5f02\u8bae",
            "\u8bc9\u8bbc\u65f6\u6548",
            "\u62b5\u62bc\u6743",
        ),
    )


def _looks_like_small_talk(segment: str) -> bool:
    compact = _compact(segment)
    if compact in {
        "\u54c8\u54e6",
        "\u54ce",
        "\u5509",
        "\u989d",
        "\u5443",
        "\u54e6",
        "\u54e6\u54e6",
        "\u55ef",
        "\u55ef\u55ef",
    }:
        return True
    if _looks_like_meta_conversation_request(segment):
        return True
    if _looks_like_short_vent_or_frustration(segment):
        return True
    if _looks_like_lifestyle_chatter(segment):
        return True
    return _contains_any(
        segment,
        (
            "\u54c8\u54c8",
            "\u563f\u563f",
            "\u4f60\u597d",
            "\u65e9\u4e0a\u597d",
            "\u65e9\u554a",
            "\u8f9b\u82e6",
            "\u5929\u6c14",
            "\u5496\u5561",
            "\u65e0\u8bed",
            "\u6709\u70b9\u56f0",
            "\u597d\u56f0",
            "\u56f0\u4e86",
            "\u592a\u56f0",
            "\u624b\u6293\u997c",
            "\u9e21\u86cb\u997c",
            "\u86cb\u7ed9\u6211",
            "\u52a0\u5c11",
            "\u6c49\u5821",
            "\u70b8\u9e21",
            "\u5403\u996d",
            "\u5348\u996d",
            "\u4e0d\u6d3b\u4e86",
            "\u6c14\u6b7b",
            "\u88ab\u6c14\u6b7b",
            "\u5fd9\u6b7b",
            "\u7d2f\u6b7b",
            "\u8fd8\u6ca1\u4ea4",
            "\u6ca1\u4ea4",
            "\u660e\u5929\u8865\u5427",
            "\u9a6c\u51ac\u6885",
            "\u60f3\u8bf4\u6e38\u6cf3",
            "\u5403\u5c4e",
            "\u62c9\u5c4e",
            "\u4e00\u5768\u5c4e",
        ),
    )


def _looks_like_non_work_chatter(segment: str, *, whole_text: str = "") -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if _contains_any(compact, ("\u4e0d\u7528\u5199\u65e5\u62a5", "\u4e0d\u8981\u5199\u65e5\u62a5", "\u522b\u5199\u65e5\u62a5", "\u53ea\u662f\u540c\u6b65\u4e00\u4e0b", "\u53ea\u662f\u8ddf\u4f60\u540c\u6b65")):
        return True
    if _looks_like_system_rant_only(text) or _looks_like_case_progress_routing_request(text):
        return True
    if _looks_like_case_commentary_without_action(text):
        return True
    if _looks_like_subjective_business_rant(text):
        return True
    if _looks_like_absurd_content(text) or _looks_like_lifestyle_report_meta_chatter(text):
        return True
    if _looks_like_meta_conversation_request(text):
        return True
    if _looks_like_short_vent_or_frustration(text):
        return True
    if _looks_like_lifestyle_chatter(text):
        return True
    scan = f"{whole_text} {text}"
    has_life_marker = _contains_any(
        scan,
        (
            "\u624b\u6293\u997c",
            "\u98df\u5802",
            "\u7ea2\u70e7\u8089",
            "\u53a8\u5e08",
            "\u597d\u54b8",
            "\u592a\u54b8",
            "\u9e21\u86cb\u997c",
            "\u86cb\u7ed9\u6211",
            "\u52a0\u5c11",
            "\u6c49\u5821",
            "\u70b8\u9e21",
            "\u5403\u996d",
            "\u559d\u6c34",
            "\u6ca1\u7a7a\u559d\u6c34",
            "\u5403\u4e00\u4e2a",
            "\u597d\u5403",
            "\u5348\u996d",
            "\u5496\u5561",
            "\u5929\u6c14",
            "\u5929\u513f",
            "\u96e8\u597d\u5927",
            "\u96e8\u592a\u5927",
            "\u96e8\u4e0b\u5f97\u771f\u5927",
            "\u96e8\u4e0b\u5f97\u5927",
            "\u4e0b\u96e8",
            "\u95f7\u70ed",
            "\u597d\u70ed",
            "\u592a\u70ed",
            "\u4e00\u70b9\u98ce\u90fd\u6ca1\u6709",
            "\u6362\u4e00\u5bb6\u5e97",
            "\u4f60\u662f\u50bb\u5b50",
            "\u50bb\u5b50",
            "\u4e0d\u6d3b\u4e86",
            "\u6c14\u6b7b",
            "\u88ab\u6c14\u6b7b",
            "\u9a6c\u51ac\u6885",
            "\u60f3\u8bf4\u6e38\u6cf3",
            "\u5403\u5c4e",
            "\u62c9\u5c4e",
            "\u4e00\u5768\u5c4e",
        ),
    )
    if not has_life_marker:
        return _looks_like_small_talk(text)
    return not _has_positive_daily_work_evidence(text)


def _looks_like_lifestyle_chatter(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if _looks_like_lifestyle_report_meta_chatter(text):
        return True
    if (
        _has_positive_daily_work_evidence(text)
        or (_has_business_work_action(text) and _has_business_work_object(text))
        or extract_problem_evidence(text).is_business_problem
        or _specific_matter_hint(text)
    ):
        return False
    life_markers = (
        "\u5929\u6c14",
        "\u5929\u513f",
        "\u96e8\u597d\u5927",
        "\u96e8\u592a\u5927",
        "\u95f7\u70ed",
        "\u597d\u70ed",
        "\u592a\u70ed",
        "\u70ed\u6b7b",
        "\u70ed\u6b7b\u4e86",
        "\u4e0d\u60f3\u52a8",
        "\u4e00\u70b9\u98ce\u90fd\u6ca1\u6709",
        "\u4e0b\u96e8",
        "\u4e0b\u96ea",
        "\u51b7\u4e0d\u51b7",
        "\u70ed\u4e0d\u70ed",
            "\u591a\u5c11\u5ea6",
            "\u5730\u94c1",
            "\u8fdf\u5230",
            "\u6324\u6b7b",
            "\u70e6",
            "\u7a7f\u5565",
        "\u7a7f\u4ec0\u4e48",
        "\u7a7f\u54ea\u4ef6",
        "\u8863\u670d",
        "\u5916\u5957",
        "\u77ed\u8896",
        "\u957f\u8896",
        "\u51fa\u95e8",
        "\u6253\u4f1e",
        "\u5e26\u4f1e",
        "\u4f1a\u4e0d\u4f1a\u4e0b\u96e8",
        "\u5403\u5565",
        "\u5403\u4ec0\u4e48",
        "\u5403\u4e86",
        "\u5403\u70b9",
        "\u5403\u4e2a",
        "\u98df\u5802",
        "\u7ea2\u70e7\u8089",
        "\u53a8\u5e08",
        "\u597d\u54b8",
        "\u592a\u54b8",
        "\u559d\u5565",
        "\u559d\u4ec0\u4e48",
        "\u559d\u6c34",
        "\u6ca1\u7a7a\u559d\u6c34",
        "\u62c9\u809a\u5b50",
        "\u809a\u5b50",
        "\u5395\u6240",
        "\u5403\u996d",
        "\u65e9\u996d",
        "\u5348\u996d",
        "\u665a\u996d",
        "\u5c0f\u756a\u8304",
        "\u756a\u8304",
        "\u6c34\u679c",
        "\u96f6\u98df",
        "\u706b\u9505",
        "\u5496\u5561",
        "\u5976\u8336",
        "\u5403\u5c4e",
        "\u62c9\u5c4e",
        "\u4e00\u5768\u5c4e",
        "\u7761\u89c9",
        "\u51e0\u70b9\u7761",
            "\u7761\u4e0d\u7740",
            "\u597d\u56f0",
            "\u5fd9\u6b7b",
            "\u5fd9\u6210\u72d7",
            "\u7d2f\u6b7b",
        "\u8fd8\u6ca1\u4ea4",
        "\u6ca1\u4ea4",
        "\u660e\u5929\u8865\u5427",
        "\u505a\u68a6",
        "\u5065\u8eab",
        "\u8dd1\u6b65",
        "\u770b\u7535\u5f71",
        "\u73a9\u6e38\u620f",
        "\u6253\u6e38\u620f",
        "\u6d17\u8f66",
        "\u53bb\u54ea\u73a9",
        "\u5468\u672b\u53bb\u54ea",
        "\u4eca\u5929\u53bb\u54ea",
        "\u660e\u5929\u53bb\u54ea",
    )
    if not _contains_any(text, life_markers):
        return False
    if _looks_like_question(text):
        return True
    return _contains_any(text, ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5", "\u540e\u5929", "\u5468\u672b"))


def _looks_like_case_commentary_without_action(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if not _contains_any(compact, ("\u5f8b\u5e08", "\u5bf9\u65b9", "\u8bc1\u636e", "\u6cd5\u5b98")):
        return False
    if _contains_any(compact, ("\u4e0d\u8db3", "\u7f3a\u5931", "\u7f3a\u5c11", "\u4e0d\u5145\u5206", "\u9700\u8981\u8865", "\u5f97\u8865")):
        return False
    action_compact = compact.replace("\u5bf9\u65b9", "")
    if _contains_any(action_compact, ("\u6c9f\u901a", "\u8054\u7cfb", "\u63d0\u4ea4", "\u6574\u7406", "\u8865\u5145", "\u51c6\u5907", "\u5199", "\u5ba1", "\u5904\u7406", "\u8c08")):
        return False
    return _contains_any(compact, ("\u6709\u70b9\u96be\u7f20", "\u6bd4\u8f83\u96be\u7f20", "\u5f88\u96be\u7f20", "\u8bc1\u636e\u5145\u5206"))


def _looks_like_subjective_business_rant(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _has_daily_time_anchor(segment) and _has_positive_daily_work_evidence(segment):
        return False
    return _contains_any(compact, ("\u6211\u771f\u7684\u670d\u4e86", "\u771f\u670d\u4e86", "\u670d\u4e86", "\u53c8\u6539\u9700\u6c42", "\u6539\u4e86\u516b\u904d", "\u6539\u4e86\u597d\u51e0\u904d")) and _contains_any(
        compact,
        ("\u9886\u5bfc", "\u5ba2\u6237", "\u7532\u65b9", "\u5408\u540c", "\u534f\u8bae", "\u9700\u6c42"),
    )


def _looks_like_absurd_content(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if _contains_any(compact, ("\u6211\u662f\u79e6\u59cb\u7687", "\u6211\u662f\u7389\u7687\u5927\u5e1d", "\u6211\u662f\u5965\u7279\u66fc")):
        return True
    if _contains_any(compact, ("\u516c\u53f8\u70b8\u4e86", "\u628a\u516c\u53f8\u70b8\u4e86", "\u6708\u4eae\u662f\u84dd\u8272", "\u6708\u4eae\u84dd\u8272")):
        return True
    if _contains_any(compact, ("\u5403\u5c4e", "\u6ca1\u5403\u9971")):
        return True
    if _contains_any(compact, ("\u6d4b\u8bd5\u4e00\u4e0b", "\u6d4b\u8bd5\u4e0b", "\u8bd5\u4e00\u4e0b")) and _contains_any(compact, ("\u54c8\u54c8", "\u5475\u5475")):
        return True
    if _contains_any(compact, ("\u5f53\u795e\u4ed9", "\u4fee\u4ed9", "\u795e\u4ed9", "\u5965\u7279\u66fc", "\u602a\u517d", "\u6253\u602a\u517d")):
        return True
    return _contains_any(
        text,
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


def _looks_like_empty_daily_content(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    problem_evidence = extract_problem_evidence(text)
    if _contains_any(compact, ("\u5565\u4e5f\u6ca1\u5e72", "\u4ec0\u4e48\u4e5f\u6ca1\u5e72", "\u597d\u50cf\u6ca1\u505a\u5565", "\u597d\u50cf\u6ca1\u505a\u4ec0\u4e48", "\u6478\u9c7c", "\u6ca1\u5e72\u6d3b", "\u6ca1\u5565\u53ef\u5199", "\u6ca1\u4ec0\u4e48\u53ef\u5199", "\u5c31\u90a3\u6837", "\u660e\u5929\u5468\u672b\u4e86", "\u660e\u5929\u662f\u5468\u672b")):
        return True
    if _has_business_work_action(text) or _has_business_work_object(text) or problem_evidence.is_problem or problem_evidence.is_no_problem:
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


def _looks_like_unfinished_daily_shell(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _has_business_work_object(segment) or _specific_matter_hint(segment):
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


def _looks_like_lifestyle_question_turn(segment: str) -> bool:
    text = str(segment or "")
    if not _looks_like_question(text):
        return False
    if (
        _has_positive_daily_work_evidence(text)
        or _has_current_reportable_work_piece(text)
        or _has_explicit_daily_plan_after_question(text)
        or _contains_any(text, ("\u7528\u5370\u6d41\u7a0b", "\u6d41\u7a0b\u600e\u4e48", "\u6d41\u7a0b\u600e\u4e48\u8d70"))
    ):
        return False
    return _contains_any(
        text,
        (
            "\u7a7f\u5565",
            "\u7a7f\u4ec0\u4e48",
            "\u7a7f\u54ea\u4ef6",
            "\u600e\u4e48\u7a7f",
            "\u5929\u6c14",
            "\u5929\u513f",
            "\u51b7\u4e0d\u51b7",
            "\u70ed\u4e0d\u70ed",
            "\u4e0b\u96e8",
        ),
    )


def _looks_like_today_task_question(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u4eca\u5929\u8981\u505a\u5565", "\u4eca\u5929\u8981\u505a\u4ec0\u4e48", "\u4eca\u5929\u8981\u5e72\u5565", "\u4eca\u5929\u8981\u5e72\u4ec0\u4e48", "\u4eca\u5929\u505a\u5565", "\u4eca\u5929\u505a\u4ec0\u4e48", "\u4eca\u5929\u5e72\u5565", "\u4eca\u5929\u5e72\u4ec0\u4e48"))


def _looks_like_business_reference_question_turn(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if not compact:
        return False
    if _looks_like_monthly_status_query(text):
        return False
    if not ("?" in text or "\uff1f" in text or _looks_like_question(text)):
        return False
    if _has_daily_time_anchor(text) or _has_explicit_daily_context_in_text(text):
        return False
    if _looks_like_process_help_question(text) or _looks_like_lifestyle_question_turn(text):
        return False
    has_question_shape = _contains_any(
        compact,
        (
            "\u662f\u4e0d\u662f",
            "\u662f\u5426",
            "\u6709\u6ca1\u6709",
            "\u6709\u65e0",
            "\u5417",
            "\u5565\u60c5\u51b5",
            "\u4ec0\u4e48\u60c5\u51b5",
            "\u600e\u4e48\u56de\u4e8b",
            "\u600e\u6837",
            "\u600e\u4e48\u6837",
            "\u6539\u8fc7",
            "\u5199\u7684\u662f",
        ),
    )
    if not has_question_shape:
        return False
    return _has_business_work_object(text) or _specific_matter_hint(text)


def _looks_like_assistant_arrangement_request(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if not compact or _has_daily_time_anchor(text):
        return False
    if not _contains_any(compact, ("\u5b89\u6392\u4e0b", "\u5b89\u6392\u4e00\u4e0b", "\u5e2e\u6211\u5b89\u6392", "\u987a\u4fbf\u5b89\u6392")):
        return False
    return _contains_any(compact, ("\u4f1a\u89c1", "\u5f00\u4f1a", "\u4f1a\u8bae", "\u89c1"))


def _looks_like_referential_write_reminder(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if not _contains_any(compact, ("\u521a\u624d", "\u524d\u9762", "\u4e0a\u9762", "\u90a3\u4e2a\u98ce\u9669", "\u90a3\u4e2a\u95ee\u9898")):
        return False
    if not _contains_any(compact, ("\u8bb0\u5f97\u5199\u4e0a", "\u5199\u4e0a\u54c8", "\u5199\u4e0a", "\u8865\u4e0a")):
        return False
    has_detail_marker = _contains_any(compact, ("\u98ce\u9669\u662f", "\u95ee\u9898\u662f", "\u56e0\u4e3a", "\u7531\u4e8e")) or "\uff1a" in str(segment or "") or ":" in str(segment or "")
    return not has_detail_marker


def _has_reportable_work_piece(text: str) -> bool:
    for piece in re.split(r"[\r\n;；。！？?!，,]+", str(text or "")):
        piece = piece.strip()
        if not piece or _looks_like_question(piece) or _looks_like_process_help_reason_fragment(piece) or _looks_like_daily_meta_or_date_question(piece):
            continue
        if (
            _has_positive_daily_work_evidence(piece)
            or _looks_like_travel_event(piece)
            or _looks_like_business_plan_statement(piece)
            or (_has_daily_time_anchor(piece) and (_has_business_work_action(piece) or _has_business_work_object(piece)))
        ):
            return True
    return False


def _has_explicit_daily_plan_after_question(text: str) -> bool:
    parts = re.split(r"[?\uff1f]", str(text or ""), maxsplit=1)
    if len(parts) < 2:
        return False
    tail = parts[1].strip()
    if not tail:
        return False
    if not _contains_any(tail, ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5", "\u660e\u513f")):
        return False
    if not _contains_any(tail, ("\u8ba1\u5212", "\u51c6\u5907", "\u6253\u7b97", "\u8981", "\u5f97", "\u628a")):
        return False
    return _has_reportable_work_piece(tail)


def _has_current_reportable_work_piece(text: str) -> bool:
    for piece in re.split(r"[\r\n;；。！？?!，,]+", str(text or "")):
        piece = piece.strip()
        if not piece or _looks_like_question(piece) or _looks_like_process_help_reason_fragment(piece) or _looks_like_daily_meta_or_date_question(piece):
            continue
        if not _contains_any(piece, ("\u4eca\u5929", "\u4eca\u65e5", "\u5b8c\u6210", "\u5b8c\u6210\u4e86", "\u505a\u4e86", "\u5df2\u7ecf", "\u641e\u5b9a", "\u5ba1\u4e86")):
            continue
        if _has_positive_daily_work_evidence(piece) or (_has_business_work_action(piece) and _has_business_work_object(piece)):
            return True
    return False


def _looks_like_problem_question(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if not compact:
        return False
    if not _looks_like_question(text):
        return False
    if not _contains_any(compact, ("\u95ee\u9898", "\u98ce\u9669", "\u98ce\u9669\u70b9")):
        return False
    if re.match(r"^\s*(?:\u98ce\u9669|\u95ee\u9898)[\uff1f?]", text) and _has_business_work_object(text):
        return False
    if _looks_like_business_problem_statement(text):
        return False
    return not ("\uff1a" in text or ":" in text)


def _looks_like_assistant_service_request(segment: str) -> bool:
    text = str(segment or "")
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


def _looks_like_system_failure_problem(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u7cfb\u7edf\u5d29", "\u7cfb\u7edf\u53c8\u5d29", "\u7cfb\u7edf\u6545\u969c", "\u7cfb\u7edfbug", "\u7cfb\u7edf\u767b\u5f55\u8001\u8d85\u65f6", "\u7cfb\u7edf\u767b\u5f55\u8d85\u65f6", "\u767b\u5f55\u8001\u8d85\u65f6", "\u767b\u5f55\u8d85\u65f6", "\u670d\u52a1\u5668\u7a81\u7136\u91cd\u542f", "\u7cfb\u7edf\u7a81\u7136\u91cd\u542f", "\u5ba2\u6237\u53cd\u9988\u6162", "\u5ba2\u6237\u53cd\u9988\u5361", "bug\u53c8\u591a", "\u6ca1\u4fdd\u5b58", "\u4fdd\u5b58\u5931\u8d25", "\u5dee\u70b9\u6ca1\u4fdd\u5b58"))


def _looks_like_contextual_system_failure_followup(segment: str) -> bool:
    compact = _compact(segment)
    if not compact or "\u7cfb\u7edf" not in compact:
        return False
    return _contains_any(compact, ("\u8fd8\u662f\u4e0d\u884c", "\u4ecd\u7136\u4e0d\u884c", "\u4f9d\u7136\u4e0d\u884c")) and _contains_any(
        compact,
        ("\u4eca\u5929\u8bd5", "\u4eca\u65e5\u8bd5", "\u8bd5\u4e86", "\u5c1d\u8bd5\u4e86"),
    )


def _contextual_system_failure_content(segment: str) -> str:
    text = str(segment or "").strip()
    match = re.search(
        r"([^\uff0c,\u3002]{2,40}\u7cfb\u7edf)[\uff0c,]([^\uff0c,\u3002]*(?:\u8fd8\u662f|\u4ecd\u7136|\u4f9d\u7136)\u4e0d\u884c)",
        text,
    )
    if not match:
        return text
    system = re.sub(r"^.*?(?:\u90a3\u4e2a|\u8fd9\u4e2a)", "", match.group(1)).strip()
    return f"{system}\uff0c{match.group(2).strip()}"


def _looks_like_system_rant_only(segment: str) -> bool:
    compact = _compact(segment)
    if not compact or not _looks_like_system_failure_problem(segment):
        return False
    if _contains_any(compact, ("\u6392\u67e5", "\u4fee\u590d", "\u5904\u7406", "\u89e3\u51b3", "\u5f71\u54cd\u586b\u62a5", "\u65e5\u62a5\u7cfb\u7edf", "\u6d41\u7a0b", "\u63a5\u53e3", "\u5ba1\u6279", "\u5ba2\u6237", "\u5408\u540c")):
        return False
    return _contains_any(compact, ("\u53c8\u5d29", "\u7834\u7cfb\u7edf", "bug\u53c8\u591a", "\u7cfb\u7edfbug\u53c8\u591a", "\u6d6a\u8d39\u6211\u65f6\u95f4", "\u70e6\u6b7b", "\u771f\u70e6"))


def _looks_like_resolved_risk_update(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u98ce\u9669", "\u95ee\u9898", "\u5361\u70b9", "\u5b95\u673a", "\u6545\u969c")) and _contains_any(
        compact,
        ("\u5df2\u7ecf\u89e3\u51b3", "\u5df2\u89e3\u51b3", "\u89e3\u51b3\u4e86", "\u98ce\u9669\u89e3\u9664", "\u5df2\u89e3\u9664", "\u89e3\u9664\u4e86"),
    )


def _looks_like_quantity_correction(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u9519\u4e86", "\u8bf4\u9519\u4e86")) and _contains_any(
        compact,
        ("\u662f\u4e24\u4efd", "\u662f\u4e8c\u4efd", "\u662f\u4e09\u4efd", "\u662f\u56db\u4efd", "\u662f2\u4efd", "\u662f3\u4efd"),
    ) and _contains_any(compact, ("\u6628\u5929", "\u6628\u65e5", "\u6709\u4e00\u4efd"))


def _looks_like_current_daily_item_retraction(segment: str) -> bool:
    compact = _compact(segment)
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
    return _has_business_work_action(segment) or _has_business_work_object(segment)


def _looks_like_contextual_retract_or_revoke(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u90a3\u4e2a\u4e0d\u7b97", "\u8fd9\u4e2a\u4e0d\u7b97", "\u90a3\u4e0d\u7b97", "\u8fd9\u4e0d\u7b97")) and _contains_any(
        compact,
        ("\u4e0d\u5bf9", "\u64a4\u56de", "\u5220\u6389", "\u5220\u4e86", "\u522b\u5199"),
    )


def _looks_like_emotional_customer_phone_only(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u5ba2\u6237\u7535\u8bdd", "\u63a5\u4e86\u4e2a\u5ba2\u6237\u7535\u8bdd", "\u63a5\u5ba2\u6237\u7535\u8bdd")) and _contains_any(
        compact,
        ("\u6c14\u6b7b", "\u70e6\u6b7b", "\u592a\u70e6", "\u5410\u69fd"),
    ) and not _contains_any(compact, ("\u6c9f\u901a", "\u5904\u7406", "\u89e3\u51b3", "\u8bb0\u5230\u65e5\u62a5", "\u5199\u5230\u65e5\u62a5"))


def _looks_like_personal_state_only(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _contains_any(compact, ("\u6253\u67b6", "\u88ab\u9886\u5bfc\u6279\u8bc4", "\u8ddf\u5c0f\u738b\u6253\u67b6")):
        return True
    if _has_business_work_action(segment) and _has_business_work_object(segment):
        return False
    return _contains_any(
        compact,
        (
            "\u5fc3\u60c5\u4e0d\u592a\u597d",
            "\u5fc3\u60c5\u4e0d\u597d",
            "\u4e0d\u60f3\u5e72\u6d3b",
            "\u4e0d\u60f3\u5de5\u4f5c",
            "\u4e0d\u60f3\u4e0a\u73ed",
            "\u597d\u5f00\u5fc3",
            "\u5f00\u5fc3",
            "\u51bb\u6b7b",
            "\u51bb\u6b7b\u4e86",
            "\u7d2f\u6b7b",
            "\u70e6\u6b7b",
        ),
    )


def _looks_like_calendar_or_offday_chatter(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _has_business_work_action(segment) and _has_business_work_object(segment):
        return False
    if _contains_any(compact, ("\u4eca\u5929\u662f\u661f\u671f", "\u4eca\u65e5\u662f\u661f\u671f", "\u660e\u5929\u4e0d\u4e0a\u73ed", "\u660e\u65e5\u4e0d\u4e0a\u73ed")):
        return True
    return False


def _looks_like_quoted_delete_edit(segment: str) -> bool:
    text = str(segment or "")
    return bool(re.search(r"[“\"'‘].+?[”\"'’].{0,8}(?:删掉|删除|去掉)", text))


def _looks_like_self_prompted_forgotten_item(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u8fd8\u6709\u522b\u7684\u4e8b\u5417", "\u4eca\u5929\u8fd8\u6709\u522b\u7684\u4e8b\u5417")) and _contains_any(
        compact,
        ("\u5fd8\u4e86", "\u54e6\u5fd8\u4e86"),
    )


def _looks_like_case_progress_routing_request(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _contains_any(compact, ("\u8bb0\u5f55\u4e00\u4e0b\u8fdb\u5c55", "\u8bb0\u4e00\u4e0b\u8fdb\u5c55", "\u8bb0\u5f55\u8fdb\u5c55")) and _contains_any(
        compact,
        ("\u6848", "\u6848\u4ef6", "\u6cd5\u5b98", "\u6cd5\u9662", "\u8bc1\u636e", "\u5f00\u5ead", "\u4f20\u7968"),
    ):
        return True
    if not _contains_any(compact, ("\u6848\u4ef6\u8fdb\u5c55", "\u8bb0\u5230\u6848\u4ef6\u8fdb\u5c55", "\u8bb0\u8fdb\u6848\u4ef6\u8fdb\u5c55")):
        return False
    return _contains_any(compact, ("\u8bb0\u5230", "\u8bb0\u8fdb", "\u5199\u5230", "\u5199\u8fdb", "\u653e\u5230", "\u653e\u8fdb"))


def _looks_like_standalone_past_work_without_current_context(segment: str, *, whole_text: str, received_at: Any = None) -> bool:
    text = str(segment or "")
    whole = str(whole_text or "")
    compact = _compact(text)
    if _looks_like_quantity_correction(text):
        return False
    if _looks_like_resolved_risk_update(text) or _looks_like_resolved_risk_update(whole):
        return False
    if not (
        any(marker in compact for marker in ("\u6628\u5929", "\u6628\u65e5", "\u524d\u5929"))
        or ("\u524d\u65e5" in compact and "\u5f53\u524d\u65e5" not in compact)
    ):
        return False
    if any(marker in compact for marker in ("\u590d\u5236", "\u62f7\u8d1d", "\u7167\u642c")) or any(marker in compact for marker in ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    if _looks_like_completed_previous_plan(whole) or _looks_like_copy_previous_daily_request(whole):
        return False
    if _contains_any(whole, ("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212", "\u6628\u5929\u660e\u65e5\u8ba1\u5212", "\u6628\u5929\u7684\u8ba1\u5212", "\u6628\u5929\u8ba1\u5212")) and _contains_any(
        whole,
        ("\u5b8c\u6210", "\u641e\u5b8c", "\u641e\u5b9a", "\u505a\u5b8c", "\u5ba1\u5b8c", "\u5904\u7406\u5b8c"),
    ):
        return False
    if _contains_any(whole, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f")):
        return False
    if _looks_like_previous_daily_submission_request(whole) or _looks_like_previous_day_makeup_statement(whole):
        return False
    if _contains_any(whole, ("\u4eca\u5929", "\u4eca\u65e5", "\u5199\u5230\u4eca\u5929", "\u8bb0\u5230\u4eca\u5929", "\u590d\u5236\u5230\u4eca\u5929")):
        return False
    return _has_positive_daily_work_evidence(text) or _has_business_work_action(text) or _has_business_work_object(text)


def _looks_like_historical_previous_plan_statement(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(
        compact,
        (
            "\u6628\u5929\u5199\u7684\u660e\u65e5\u8ba1\u5212\u662f",
            "\u6628\u5929\u5199\u7684\u660e\u5929\u8ba1\u5212\u662f",
            "\u6628\u5929\u65e5\u62a5\u91cc\u7684\u660e\u65e5\u8ba1\u5212\u662f",
            "\u6628\u5929\u65e5\u62a5\u91cc\u7684\u660e\u5929\u8ba1\u5212\u662f",
        ),
    )


def _looks_like_ambiguous_that_day_daily_reference(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if not _contains_any(compact, ("\u90a3\u5929", "\u5f53\u5929", "\u90a3\u65e5")):
        return False
    return _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7", "\u5de5\u4f5c\u65e5\u62a5", "\u5de5\u4f5c\u65e5\u5fd7"))


def _looks_like_overtime_status_not_reportable(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    has_overtime_anchor = _contains_any(compact, ("\u6628\u665a", "\u51cc\u6668", "\u52a0\u73ed\u5230", "\u665a\u4e0a\u52a0\u73ed"))
    if not has_overtime_anchor:
        return False
    if _contains_any(compact, ("\u5199\u65e5\u62a5", "\u8bb0\u4e0a", "\u8bb0\u5230\u65e5\u62a5", "\u4eca\u5929\u5b8c\u6210", "\u4eca\u5929\u5904\u7406")):
        return False
    return _contains_any(compact, ("\u56f0\u6b7b", "\u8865\u89c9", "\u4eca\u5929\u5c31\u8fd9\u6837", "\u4eca\u5929\u5148\u8fd9\u6837", "\u4eca\u5929\u4e0a\u5348\u8865\u89c9"))


def _looks_like_case_progress_continuation_without_daily_time(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _has_daily_time_anchor(segment):
        return False
    if _contains_any(compact, ("\u5199\u65e5\u62a5", "\u8bb0\u5230\u65e5\u62a5", "\u8bb0\u8fdb\u65e5\u62a5")):
        return False
    return _contains_any(compact, ("\u5bf9\u65b9\u5f8b\u5e08", "\u5bf9\u65b9\u5f53\u4e8b\u4eba", "\u5bf9\u65b9\u6cd5\u52a1", "\u6cd5\u9662", "\u6cd5\u5b98")) and _contains_any(
        compact,
        ("\u65b0\u7684\u8bc1\u636e", "\u53d1\u6765\u8bc1\u636e", "\u4f20\u6765\u8bc1\u636e", "\u548c\u89e3", "\u6392\u671f", "\u5f00\u5ead", "\u8981\u8bc4\u4f30", "\u6211\u4eec\u8981\u8bc4\u4f30"),
    )


def _looks_like_contextual_problem_followup(segment: str, *, whole_text: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if not compact or _looks_like_no_problem(text):
        return False
    if _contains_any(compact, ("\u60c5\u7eea\u6fc0\u52a8", "\u60c5\u7eea\u633a\u6fc0\u52a8", "\u4e00\u5ba1\u4e0d\u516c\u5e73", "\u4e0d\u516c\u5e73")) and _contains_any(
        compact,
        ("\u4ed6\u4eec", "\u5bf9\u65b9", "\u5ba2\u6237", "\u5458\u5de5", "\u5de5\u4eba", "\u8bf4"),
    ):
        return True
    if not _contains_any(compact, ("\u4e0d\u884c", "\u4e0d\u80fd\u7528", "\u5931\u8d25", "\u62a5\u9519", "\u5361\u4f4f", "\u5361\u4e86", "\u62d6\u540e\u817f")):
        return False
    whole = str(whole_text or "")
    return _contains_any(whole, ("\u7cfb\u7edf", "\u63a5\u53e3", "\u6d41\u7a0b", "\u5ba1\u6279", "\u4f1a\u8bae\u5ba4", "\u9884\u8ba2")) or _has_business_work_object(whole)


def _looks_like_non_substantive_daily_request(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if (
        _contains_any(compact, ("\u7b97\u4e86\u4e0d\u5199", "\u4e0d\u5199\u4e86", "\u4e0d\u586b\u4e86"))
        and _contains_any(compact, ("\u660e\u5929\u5199", "\u660e\u65e5\u5199", "\u56de\u5934\u5199", "\u4e0b\u6b21\u5199"))
        and not _has_business_work_object(text)
    ):
        return True
    if _contains_any(compact, ("\u522b\u7ed9\u6211\u5199\u65e5\u62a5", "\u4e0d\u8981\u5199\u65e5\u62a5", "\u522b\u5199\u65e5\u62a5")) and _contains_any(
        compact,
        ("\u5410\u69fd", "\u5f00\u73a9\u7b11", "\u522b\u5f53\u771f", "\u968f\u53e3"),
    ):
        return True
    if _contains_any(compact, ("\u518d\u5e2e\u6211\u5199\u65e5\u62a5", "\u518d\u5e2e\u6211\u586b\u65e5\u62a5")) and not _daily_start_payload(text):
        return True
    if "\u65e5\u62a5" not in compact:
        return False
    if _daily_start_payload(text):
        return False
    if _contains_any(compact, ("\u65e5\u62a5\u8981\u5199", "\u65e5\u62a5\u5df2\u7ecf\u5199\u597d", "\u65e5\u62a5\u5df2\u7ecf\u5199\u597d\u4e86", "\u65e5\u62a5\u5199\u597d\u4e86", "\u4eca\u5929\u65e5\u62a5\u5df2\u7ecf\u5199\u597d")):
        return True
    if _contains_any(compact, ("\u6ca1\u5565\u53ef\u5199", "\u6ca1\u4ec0\u4e48\u53ef\u5199", "\u6ca1\u5565\u597d\u5199", "\u6ca1\u4ec0\u4e48\u597d\u5199")):
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


def _looks_like_daily_start_without_payload(segment: str) -> bool:
    text = str(segment or "").strip()
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


def _conditional_daily_supplement_content(segment: str) -> str:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return ""
    if not _contains_any(compact, ("\u6ca1\u5199\u7684\u8bdd\u5e2e\u6211\u8865\u4e0a", "\u6ca1\u5199\u5e2e\u6211\u8865\u4e0a", "\u5e2e\u6211\u8865\u4e0a", "\u8865\u4e00\u4e0b", "\u8865\u4e0a")):
        return ""
    match = re.search(r"(?:\uff1a|:)\s*(.+)$", text)
    content = match.group(1).strip() if match else ""
    if not content:
        return ""
    if _looks_like_question(content) or _looks_like_non_substantive_daily_request(content):
        return ""
    if not (
        _has_reportable_work_piece(content)
        or _has_positive_daily_work_evidence(content)
        or _has_business_work_object(content)
        or re.search(r"(\u4fee|\u5904\u7406).{0,8}bug", content, re.IGNORECASE)
        or re.search(r"\u5f00\u4e86?.{0,6}\u4f1a", content)
    ):
        return ""
    return content


def _yesterday_makeup_daily_content(segment: str) -> str:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return ""
    if not _contains_any(compact, ("\u6628\u5929\u65e5\u62a5\u5fd8\u4e86\u5199", "\u6628\u65e5\u65e5\u62a5\u5fd8\u4e86\u5199", "\u6628\u5929\u65e5\u62a5\u6ca1\u5199", "\u6628\u65e5\u65e5\u62a5\u6ca1\u5199", "\u8865\u4e00\u4e0b\u6628\u5929\u7684\u65e5\u62a5", "\u8865\u4e0b\u6628\u5929\u7684\u65e5\u62a5", "\u8865\u5199\u6628\u5929\u7684\u65e5\u62a5")):
        return ""
    if not _contains_any(compact, ("\u8865\u4e00\u4e0b", "\u8865\u4e0b", "\u8865\u5199", "\u8865\u4e0a", "\u8865")):
        return ""
    parts = re.split(r"(?:\u6628\u5929|\u6628\u65e5)", text)
    if len(parts) < 2:
        return ""
    content = parts[-1].strip(" ，,。；;")
    if content.startswith("\u5c31"):
        content = content[1:].strip(" ，,。；;")
    if not content:
        return ""
    content = re.sub(r"^(?:\u7684)?\u65e5\u62a5\s*[\uff0c,]\s*", "", content)
    content = re.sub(r"^(?:\u4e3b\u8981)?(?:\u5de5\u4f5c)?(?:\u662f)?", "", content).strip(" \t\r\n\u3000\uff0c,")
    if not content:
        return ""
    if not (_has_reportable_work_piece(content) or _has_positive_daily_work_evidence(content) or _has_business_work_object(content)):
        return ""
    return f"\u6628\u5929{content}"


def _looks_like_yesterday_work_field_reply(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if not _contains_any(compact, ("\u6628\u5929\u7684\u5de5\u4f5c\u662f", "\u6628\u65e5\u7684\u5de5\u4f5c\u662f", "\u6628\u5929\u5de5\u4f5c\u662f", "\u6628\u65e5\u5de5\u4f5c\u662f")):
        return False
    return bool(_yesterday_work_field_content(text))


def _yesterday_work_field_content(segment: str) -> str:
    text = str(segment or "").strip()
    content = re.sub(r"^(?:\u6628\u5929|\u6628\u65e5)(?:\u7684)?\u5de5\u4f5c(?:\u662f)?\s*(?:[:\uff1a])?\s*", "", text).strip()
    if not content:
        return ""
    if _looks_like_question(content) or _looks_like_non_substantive_daily_request(content):
        return ""
    if not (_has_reportable_work_piece(content) or _has_positive_daily_work_evidence(content) or _has_business_work_object(content)):
        return ""
    return content


def _today_makeup_previous_work_content(segment: str) -> str:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return ""
    if not _contains_any(compact, ("\u4eca\u5929\u8865\u4e0a", "\u4eca\u65e5\u8865\u4e0a", "\u4eca\u5929\u8865\u5199", "\u4eca\u65e5\u8865\u5199")):
        return ""
    if not _contains_any(compact, ("\u6628\u5929", "\u6628\u65e5", "\u4e0a\u5468", "\u4e4b\u524d", "\u524d\u9762")):
        return ""
    if not _contains_any(compact, ("\u8fd8\u6ca1\u5199", "\u6ca1\u5199", "\u6ca1\u6574\u7406", "\u672a\u5199", "\u672a\u6574\u7406")):
        return ""
    content = re.sub(r"[\uff0c,].*$", "", text).strip()
    content = re.sub(r"(?:\u8fd8)?(?:\u6ca1|\u672a)(?:\u5199|\u6574\u7406).*$", "", content).strip()
    if not content:
        return ""
    return f"\u8865\u5199{content}"


def _looks_like_process_help_question(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    has_question_shape = _looks_like_question(text) or _contains_any(
        compact,
        ("\u95ee\u4e0b", "\u95ee\u4e00\u4e0b", "\u54a8\u8be2\u4e00\u4e0b", "\u627e\u8c01", "\u95ee\u8c01", "\u54a8\u8be2\u8c01", "\u548b\u6574", "\u600e\u4e48\u529e", "\u600e\u4e48\u5904\u7406", "\u95ee\u95ee"),
    )
    if not has_question_shape:
        return False
    return _contains_any(compact, ("\u600e\u4e48\u63d0", "\u600e\u4e48\u7533\u8bf7", "\u600e\u4e48\u64cd\u4f5c", "\u6d41\u7a0b\u600e\u4e48", "\u7cfb\u7edf\u600e\u4e48", "\u6015\u641e\u9519", "\u7533\u8bf7\u6d41\u7a0b", "\u627e\u8c01", "\u95ee\u8c01", "\u54a8\u8be2\u8c01", "\u548b\u6574", "\u600e\u4e48\u529e", "\u600e\u4e48\u5904\u7406")) and _contains_any(
        compact,
        ("\u7528\u5370", "\u76d6\u7ae0", "\u5408\u540c\u76d6\u7ae0", "\u7533\u8bf7", "\u7cfb\u7edf", "\u6d41\u7a0b", "\u7535\u5b50\u7ae0", "\u57f9\u8bad\u8d44\u6599", "\u5dee\u65c5\u8d39", "\u5dee\u65c5", "\u62a5\u9500", "\u8d85\u6807"),
    )


def _looks_like_reimbursement_policy_question(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if not compact:
        return False
    if not (_looks_like_question(text) or _contains_any(compact, ("\u662f\u5565", "\u662f\u4ec0\u4e48", "\u95ee\u4e0b", "\u95ee\u4e00\u4e0b"))):
        return False
    return _contains_any(compact, ("\u51fa\u5dee\u62a5\u9500\u6807\u51c6", "\u62a5\u9500\u6807\u51c6", "\u5dee\u65c5\u6807\u51c6", "\u51fa\u5dee\u6807\u51c6"))


def _looks_like_daily_meta_or_date_question(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _contains_any(compact, ("\u4eca\u5929\u661f\u671f\u51e0", "\u661f\u671f\u51e0", "\u51e0\u53f7", "\u4eca\u5929\u51e0\u53f7")):
        return True
    return _contains_any(compact, ("\u65e5\u62a5\u91cc\u8fd8\u8981\u5199\u5565", "\u65e5\u62a5\u91cc\u8fd8\u8981\u5199\u4ec0\u4e48", "\u8be5\u5199\u65e5\u62a5\u4e86", "\u8981\u5199\u65e5\u62a5\u5417"))


def _looks_like_do_not_write_daily(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u4eca\u5929\u4e0d\u7528\u5199", "\u4eca\u65e5\u4e0d\u7528\u5199", "\u4e0d\u7528\u5199\u4e86", "\u522b\u5199", "\u4e0d\u8981\u5199"))


def _looks_like_submission_status_question(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u8c01\u8fd8\u6ca1\u4ea4", "\u8c01\u6ca1\u4ea4", "\u8fd8\u6709\u8c01\u6ca1\u4ea4")) and _contains_any(
        compact,
        ("\u622a\u6b62", "\u5230\u671f", "\u660e\u5929", "\u660e\u65e5"),
    )


def _looks_like_process_help_reason_fragment(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _contains_any(compact, ("\u51fa\u5dee", "\u5357\u4eac", "\u4e0a\u6d77", "\u6cd5\u9662", "\u6848", "\u5f00\u5ead")):
        return False
    return _contains_any(compact, ("\u6025\u7740\u8981\u7528\u5370", "\u6015\u641e\u9519", "\u522b\u641e\u9519", "\u62c5\u5fc3\u641e\u9519"))


def _looks_like_past_result_only_update(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if not _contains_any(compact, ("\u6628\u5929", "\u6628\u65e5", "\u524d\u5929", "\u524d\u65e5")):
        return False
    if not _contains_any(compact, ("\u7ed3\u679c\u51fa\u6765", "\u901a\u8fc7", "\u5ba1\u6279\u901a\u8fc7", "\u5ba1\u5b8c")):
        return False
    return not _contains_any(compact, ("\u4eca\u5929", "\u4eca\u65e5", "\u5199\u5230\u4eca\u5929", "\u8bb0\u5230\u4eca\u5929"))


def _looks_like_monthly_coordination_followup(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if _contains_any(compact, ("\u50ac\u4e00\u4e0b", "\u50ac\u4e0b", "\u63d0\u9192\u4e00\u4e0b", "\u63d0\u9192\u4e0b")) and _contains_any(
        compact,
        ("\u622a\u6b62", "\u5230\u671f", "deadline", "\u6ca1\u4ea4", "\u6ca1\u586b"),
    ):
        return not (_has_business_work_action(text) and _has_business_work_object(text))
    return _contains_any(compact, ("\u50ac\u4e00\u4e0b\u6ca1\u4ea4", "\u50ac\u4e00\u4e0b\u6ca1\u586b", "\u50ac\u4e0b\u6ca1\u4ea4", "\u50ac\u4e0b\u6ca1\u586b", "deadline", "\u6263\u7ee9\u6548")) and not _has_positive_daily_work_evidence(text)


def _looks_like_schedule_check_or_conditional_plan(segment: str) -> bool:
    text = str(segment or "")
    if not _looks_like_question(text):
        return False
    if not _contains_any(text, ("\u65e5\u7a0b", "\u51b2\u7a81", "\u6709\u6ca1\u6709\u51b2\u7a81", "\u5e2e\u6211\u770b\u770b", "\u770b\u770b\u660e\u5929")):
        return False
    return _contains_any(text, ("\u5982\u679c", "\u6ca1\u4e8b", "\u6211\u5c31", "\u518d\u53bb", "\u80fd\u4e0d\u80fd"))


def _looks_like_vague_repeat_or_workload_statement(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if not compact:
        return False
    if _contains_any(compact, ("\u660e\u5929\u7ee7\u7eed\u5f04\u8fd9\u4e2a", "\u660e\u65e5\u7ee7\u7eed\u5f04\u8fd9\u4e2a", "\u660e\u5929\u7ee7\u7eed\u5f04\u90a3\u4e2a", "\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u8fd9\u4e2a", "\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u524d\u8ff0")):
        return False
    if _contains_any(compact, ("\u4eca\u5929\u548c\u6628\u5929\u5dee\u4e0d\u591a", "\u4eca\u5929\u8ddf\u6628\u5929\u5dee\u4e0d\u591a")):
        return True
    if _contains_any(compact, ("\u660e\u5929\u518d\u505a", "\u660e\u5929\u505a", "\u660e\u5929\u518d\u5f04", "\u660e\u5929\u518d\u641e")) and not _has_business_work_object(text):
        return True
    if _contains_any(compact, ("\u4eca\u5929\u7684\u4e8b\u90fd\u5904\u7406\u5b8c", "\u4eca\u5929\u7684\u4e8b\u90fd\u5fd9\u5b8c", "\u4eca\u5929\u7684\u4e8b\u90fd\u641e\u5b8c", "\u4eca\u5929\u7684\u6d3b\u5e72\u5b8c", "\u4eca\u5929\u6d3b\u5e72\u5b8c")):
        return True
    if _contains_any(compact, ("\u4eca\u5929\u5e72\u4e86\u597d\u591a\u4e8b", "\u5e72\u4e86\u597d\u591a\u4e8b", "\u4eca\u5929\u505a\u4e86\u597d\u591a\u4e8b", "\u505a\u4e86\u597d\u591a\u4e8b", "\u4eca\u5929\u505a\u4e86\u4e00\u4e9b\u4e8b", "\u505a\u4e86\u4e00\u4e9b\u4e8b", "\u4eca\u5929\u505a\u4e86\u4e9b\u4e8b", "\u505a\u4e86\u4e9b\u4e8b")) and not _has_business_work_object(text):
        return True
    if _contains_any(compact, ("\u5e72\u4e86\u70b9\u6d3b", "\u505a\u4e86\u70b9\u6d3b", "\u5e72\u4e86\u4e9b\u6d3b", "\u505a\u4e86\u4e9b\u6d3b")) and not _has_business_work_object(text):
        return True
    if _contains_any(compact, ("\u641e\u4e86\u70b9\u4e1c\u897f", "\u505a\u4e86\u70b9\u4e1c\u897f", "\u5f04\u4e86\u70b9\u4e1c\u897f", "\u4f60\u61c2\u7684")) and not _has_business_work_object(text):
        return True
    if _contains_any(compact, ("\u597d\u591a\u6d3b", "\u5f88\u591a\u6d3b", "\u4e0d\u5c11\u6d3b", "\u4e00\u5806\u6d3b")) and not _has_business_work_object(text):
        return True
    if _contains_any(compact, ("\u4eca\u5929\u5de5\u4f5c\u4e86", "\u4eca\u65e5\u5de5\u4f5c\u4e86", "\u4eca\u5929\u4e0a\u73ed\u4e86", "\u4eca\u5929\u5b8c\u4e86", "\u660e\u5929\u7ee7\u7eed\u5f04", "\u660e\u5929\u518d\u8bf4", "\u5b8c\u6210\u4e86\u4efb\u52a1", "\u5b8c\u6210\u4efb\u52a1", "\u6ca1\u5565\u5199\u7684", "\u6ca1\u4ec0\u4e48\u5199\u7684", "\u5c31\u90a3\u6837")) and not _has_business_work_object(text):
        return True
    if _contains_any(compact, ("\u4eca\u5929\u6ca1\u5565", "\u6ca1\u4ec0\u4e48\u7279\u522b", "\u5176\u4ed6\u6ca1\u4ec0\u4e48\u7279\u522b")) and _contains_any(compact, ("\u5f00\u4e86\u4e2a\u4f1a", "\u5f00\u4f1a")):
        return True
    if _looks_like_vague_completion_without_object(text):
        return True
    if _contains_any(compact, ("\u8fd8\u662f\u90a3\u4e9b", "\u8fd8\u662f\u90a3\u51e0\u4e2a", "\u8fd8\u662f\u90a3\u51e0\u4ef6", "\u8fd8\u662f\u90a3\u4e9b\u4e8b")):
        return True
    if _contains_any(compact, ("\u8fd8\u662f\u90a3\u6837", "\u8fd8\u662f\u8001\u6837\u5b50", "\u8001\u6837\u5b50", "\u6ca1\u5565\u7279\u522b", "\u6ca1\u4ec0\u4e48\u7279\u522b")) and not _has_business_work_object(text):
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
    if _has_business_work_object(text) and not _contains_any(text, ("\u4e8b\u591a", "\u624b\u5934\u4e8b\u591a")):
        return False
    return _contains_any(compact, ("\u5dee\u4e0d\u591a", "\u4e5f\u5dee\u4e0d\u591a", "\u5e94\u8be5\u4e5f\u5dee\u4e0d\u591a", "\u624b\u5934\u4e8b\u591a", "\u4e8b\u60c5\u591a", "\u4e8b\u60c5\u633a\u591a", "\u4e8b\u633a\u591a")) and not _contains_any(
        compact,
        ("\u5408\u540c", "\u6848", "\u6750\u6599", "\u6cd5\u9662", "\u5ba2\u6237", "\u9879\u76ee", "\u6d41\u7a0b", "\u65b9\u6848"),
    )


def _looks_like_vague_completion_without_object(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if not compact:
        return False
    if _has_business_work_object(text) or _specific_matter_hint(text):
        return False
    if not _contains_any(compact, ("\u641e\u5b9a", "\u641e\u5b8c", "\u5f04\u5b8c", "\u505a\u5b8c", "\u5b8c\u6210", "\u5b8c\u6210\u4e86", "\u5904\u7406\u5b8c", "\u5b8c\u4e8b")):
        return False
    stripped = compact
    for marker in ("\u4eca\u5929", "\u4eca\u65e5", "\u5df2\u7ecf", "\u90fd", "\u4e86"):
        stripped = stripped.replace(marker, "")
    return len(stripped) <= 4


def _looks_like_active_daily_add_instruction(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact or _looks_like_question(text):
        return False
    if _contains_any(compact, ("\u5220\u6389", "\u5220\u9664", "\u53bb\u6389", "\u5220\u4e86")):
        return False
    if _looks_like_referential_write_reminder(text) and not _has_concrete_problem_detail(text):
        return False
    if not _contains_any(
        compact,
        (
            "\u5199\u8fdb\u53bb",
            "\u5199\u8fdb",
            "\u5199\u4e0a",
            "\u8bb0\u4e0a",
            "\u8bb0\u8fdb\u53bb",
            "\u8bb0\u8fdb",
            "\u8865\u4e0a",
            "\u52a0\u4e0a",
            "\u52a0\u4e00\u4e2a",
        ),
    ):
        return False
    content = _active_daily_add_content(text)
    if not content or _looks_like_question(content):
        return False
    if _contains_any(_compact(text), ("\u4f1a\u7684\u7ed3\u8bba", "\u4f1a\u8bae\u7ed3\u8bba", "\u7ed3\u8bba\u4e5f\u5199")) and _contains_any(
        _compact(content),
        ("\u548c\u89e3", "\u8c03\u89e3", "\u5ef6\u671f", "\u8d54\u4ed8", "\u5ead\u5ba1", "\u5f00\u5ead"),
    ):
        return True
    return _has_reportable_work_piece(content) or _has_positive_daily_work_evidence(content) or _has_business_work_object(content)


def _active_daily_add_content(segment: str) -> str:
    text = str(segment or "").strip()
    quoted = re.search(r"[\u2018\u201c\u300e\"'](.+?)[\u2019\u201d\u300f\"']", text)
    if quoted:
        return quoted.group(1).strip()
    if "\uff1a" in text or ":" in text:
        text = re.split(r"[:\uff1a]", text, maxsplit=1)[-1].strip()
    text = re.sub(r"^(?:\u628a)?", "", text).strip()
    text = re.sub(
        r"^(?:\u521a\u624d\u8bf4\u7684\u90a3\u4e9b)?(?:\u518d)?(?:\u52a0\u4e0a|\u52a0\u4e00\u4e2a)(?:\u4e00\u4e2a)?",
        "",
        text,
    ).strip()
    text = re.sub(r"(?:\u5199\u8fdb\u53bb|\u5199\u8fdb|\u5199\u4e0a|\u8bb0\u4e0a|\u8bb0\u8fdb\u53bb|\u8bb0\u8fdb|\u8865\u4e0a)$", "", text).strip()
    return text.strip(" \t\r\n\u3000\uff0c,\u3002.:\uff1a\u300e\u300f\u2018\u2019\u201c\u201d\"'")


def _looks_like_active_daily_concrete_reminder(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if not _contains_any(compact, ("\u522b\u5fd8\u4e86\u8fd8\u6709", "\u522b\u5fd8\u4e86", "\u8fd8\u6709")):
        return False
    content = _active_daily_reminder_content(segment)
    if not content:
        return False
    return _has_business_work_object(content) or _has_positive_daily_work_evidence(content) or _has_reportable_work_piece(content)


def _active_daily_reminder_content(segment: str) -> str:
    text = str(segment or "").strip()
    text = re.sub(r"^(?:\u522b\u5fd8\u4e86)?(?:\u8fd8\u6709)?", "", text).strip(" \t\r\n\u3000\uff0c,\u3002.:\uff1a")
    return text


def _looks_like_contextual_business_detail(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact or _looks_like_question(text) or _looks_like_non_work_chatter(text):
        return False
    if _looks_like_non_substantive_daily_request(text) or _looks_like_vague_repeat_or_workload_statement(text):
        return False
    if _has_business_work_object(text) and re.search(
        r"(?:\u4e0a\u5348|\u4e0b\u5348|\u4e2d\u5348|\u665a\u4e0a|\u65e9\u4e0a)?\d{1,2}(?:\u70b9|\u70b9\u534a|:\d{2})",
        text,
    ):
        return True
    if _contains_any(compact, ("\u7ade\u54c1\u5206\u6790", "\u5408\u89c4\u5ba1\u67e5", "\u521d\u7a3f", "\u5bf9\u6bd4")):
        return True
    if _contains_any(compact, ("\u90a3\u4e2a\u6848\u5b50", "\u90a3\u4e2a\u6848", "\u8fd9\u4e2a\u6848\u5b50", "\u8fd9\u4e2a\u6848")) and _contains_any(
        compact,
        ("\u6539\u4e86\u4e00\u904d", "\u53c8\u6539\u4e86", "\u4fee\u6539\u4e86", "\u8c03\u6574\u4e86"),
    ):
        return True
    if _contains_any(compact, ("\u8c08\u4e86", "\u6c9f\u901a\u4e86", "\u786e\u8ba4\u4e86", "\u6574\u7406\u4e86", "\u7406\u4e86")) and _contains_any(
        compact,
        ("\u5408\u4f5c", "\u610f\u5411", "\u9700\u6c42", "\u6761\u6b3e", "\u7b56\u7565", "\u65b9\u6848", "\u4ef7\u683c"),
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
    if _contains_any(compact, ("\u521a\u624d", "\u90a3\u5757", "\u90a3\u6761", "\u90a3\u4e2a")) and _contains_any(compact, ("\u5305\u542b", "\u8865\u5145", "\u52a0\u8fdb\u53bb", "\u52a0\u4e0a", "\u5bf9\u6bd4")):
        return True
    return False


def _looks_like_mixed_question_with_daily_business(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if not compact or not _looks_like_question(text):
        return False
    if not _has_daily_time_anchor(text):
        return False
    if not _contains_any(compact, ("\u987a\u4fbf\u95ee", "\u95ee\u4e0b", "\u95ee\u4e00\u4e0b", "\u600e\u4e48\u8d70", "\u8981\u4e0d\u8981", "\u80fd\u4e0d\u80fd")):
        return False
    return _has_business_work_action(text) and _has_business_work_object(text)


def _looks_like_emotional_deferral_without_business(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _has_business_work_object(segment) or _specific_matter_hint(segment):
        return False
    return _contains_any(compact, ("\u7d2f\u6b7b", "\u597d\u7d2f", "\u70e6\u6b7b", "\u4e0d\u60f3\u5e72\u6d3b", "\u4e0d\u60f3\u52a8")) and _contains_any(
        compact,
        ("\u660e\u5929\u518d\u8bf4", "\u660e\u65e5\u518d\u8bf4", "\u56de\u5934\u518d\u8bf4", "\u518d\u8bf4\u5427"),
    )


def _looks_like_personal_workplace_rant(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _has_business_work_object(segment) or _specific_matter_hint(segment):
        return False
    return _contains_any(compact, ("\u8001\u677f", "\u9886\u5bfc")) and _contains_any(compact, ("\u603c", "\u9a82", "\u51f6", "\u70e6", "\u751f\u6c14"))


def _looks_like_generic_busy_chatter(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _contains_any(compact, ("\u5565\u4e5f\u6ca1\u5e72\u6210", "\u4ec0\u4e48\u4e5f\u6ca1\u5e72\u6210", "\u6ca1\u5e72\u6210")) and _contains_any(compact, ("\u5168\u5728\u5f00\u4f1a", "\u4e00\u76f4\u5f00\u4f1a", "\u5168\u662f\u4f1a")):
        return True
    if (_has_business_work_object(segment) or _specific_matter_hint(segment)) and not _contains_any(compact, ("\u7535\u8bdd", "\u63a5\u7535\u8bdd")):
        return False
    return _contains_any(compact, ("\u5fd9\u5230\u98de\u8d77", "\u5fd9\u6b7b", "\u5149\u63a5\u7535\u8bdd", "\u63a5\u4e8620\u4e2a", "\u4e8b\u60c5\u633a\u591a", "\u4e8b\u633a\u591a", "\u4eca\u5929\u53c8\u52a0\u73ed", "\u53c8\u52a0\u73ed\u7d2f\u6b7b", "\u52a0\u73ed\u7d2f\u6b7b"))


def _looks_like_lifestyle_plan_question(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u53bb\u54ea\u55e8", "\u53bb\u54ea\u91cc\u55e8", "\u665a\u4e0a\u53bb\u54ea", "\u5468\u4e94\u5566", "\u5468\u4e94\u4e86")) and not (
        _has_business_work_object(segment) or _specific_matter_hint(segment)
    )


def _looks_like_travel_application_only(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u7533\u8bf7\u4e2a\u51fa\u5dee", "\u7533\u8bf7\u51fa\u5dee", "\u5148\u7533\u8bf7\u4e2a\u51fa\u5dee")) and not _contains_any(
        compact,
        ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u5df2\u7ecf"),
    )


def _looks_like_reminder_request_only(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u5e2e\u6211\u63d0\u9192", "\u63d0\u9192\u4e0b", "\u63d0\u9192\u6211", "\u5e2e\u6211\u8bb0\u5f97", "\u8bb0\u5f97\u63d0\u9192\u6211")) and _contains_any(
        compact,
        ("\u4f1a\u8bae", "\u5f00\u4f1a", "\u65e5\u7a0b", "\u65f6\u95f4", "\u5468\u62a5", "\u65e5\u62a5", "\u63d0\u4ea4", "\u4ea4"),
    )


def _looks_like_weather_question(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u5929\u6c14", "\u4e0b\u96e8", "\u964d\u6e29", "\u51e0\u5ea6")) and _contains_any(
        compact,
        ("\u600e\u6837", "\u548b\u6837", "\u600e\u4e48\u6837", "\u5417", "\u4e0d\u4e0b", "\u51e0\u5ea6"),
    )


def _looks_like_calendar_event_only(segment: str) -> bool:
    text = str(segment or "")
    compact = _compact(text)
    if not compact:
        return False
    if _has_business_work_object(segment) or _specific_matter_hint(segment):
        return False
    return bool(re.search(r"(?:\u660e\u5929|\u660e\u65e5|\u660e\u513f).{0,6}\d{1,2}\s*\u70b9.{0,4}(?:\u6709\u4e2a\u4f1a|\u5f00\u4f1a|\u4f1a\u8bae)", text))


def _looks_like_food_or_rest_plan(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _contains_any(compact, ("\u5403\u996d", "\u5403\u4e86\u4e2a\u996d", "\u9ec4\u7116\u9e21", "\u65e5\u6599")) and not _contains_any(compact, ("\u8c08", "\u6c9f\u901a", "\u4f1a\u8bae", "\u5408\u540c", "\u9879\u76ee", "\u6848")):
        return True
    return _contains_any(compact, ("\u60f3\u5403", "\u53bb\u5403", "\u5403\u5c0f\u9f99\u867e", "\u5c0f\u9f99\u867e", "\u6708\u4eae", "\u6708\u997c", "\u7761\u89c9", "\u7761\u61d2\u89c9", "\u4f11\u606f")) and not (
        _has_business_work_object(segment) or _specific_matter_hint(segment)
    )


def _looks_like_bare_emotional_travel(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if not _contains_any(compact, ("\u51fa\u5dee", "\u53bb\u51fa\u5dee")):
        return False
    has_destination_or_work = _known_travel_destination(str(segment or "")) or _travel_destination_without_fragment_guard(str(segment or "")) or _has_business_work_object(segment)
    return not has_destination_or_work and _contains_any(compact, ("\u70e6", "\u53c8\u8981", "\u597d\u7d2f", "\u7d2f"))


def _looks_like_travel_logistics_only(segment: str) -> bool:
    compact = _compact(segment)
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


def _looks_like_agent_task_instruction(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact or _has_daily_time_anchor(text) or _has_explicit_daily_context_in_text(text):
        return False
    if extract_problem_evidence(text).is_problem or _contains_any(compact, ("\u8fd9\u4e2a\u95ee\u9898\u4e5f\u8bb0\u4e0a", "\u95ee\u9898\u4e5f\u8bb0\u4e0a", "\u98ce\u9669\u4e5f\u8bb0\u4e0a")):
        return False
    if _contains_any(compact, ("\u7b2c1", "\u7b2c2", "\u7b2c3", "\u7b2c\u4e00", "\u7b2c\u4e8c", "\u7b2c\u4e09")) and _contains_any(
        compact,
        ("\u6539", "\u6539\u6210", "\u6539\u4e3a", "\u5220", "\u5408\u5e76"),
    ):
        return False
    return bool(re.search(r"^把.+(?:审核|审查|整理|查|查询|统计|生成|发|发送|写|改|修改).{0,8}(?:一下|下|吧|。)?$", text))


def _looks_like_non_tomorrow_future_deadline(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _contains_any(compact, ("\u540e\u5929", "\u5927\u540e\u5929", "\u4e0b\u5468", "\u4e0b\u5468\u4e00", "\u4e0b\u5468\u4e8c", "\u4e0b\u5468\u4e09", "\u4e0b\u5468\u56db", "\u4e0b\u5468\u4e94", "\u4e0b\u5468\u516d", "\u4e0b\u5468\u65e5")) and _contains_any(
        compact,
        ("\u98de", "\u53bb", "\u51fa\u5dee", "\u53c2\u52a0", "\u5cf0\u4f1a", "\u5f00\u5ead"),
    ) and not _contains_any(compact, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f")):
        return True
    return _contains_any(compact, ("\u4e0b\u5468", "\u5468\u4e00", "\u5468\u4e8c", "\u5468\u4e09", "\u5468\u56db", "\u5468\u4e94")) and _contains_any(
        compact,
        ("\u524d\u8981", "\u8981\u7ed9", "\u622a\u6b62", "\u5230\u671f", "\u63d0\u4ea4", "\u4ea4\u4ed8"),
    ) and not _contains_any(compact, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f"))


def _looks_like_future_daily_makeup_notice(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7")) and _contains_any(compact, ("\u5468\u4e94\u4e00\u8d77\u8865", "\u56de\u6765\u4e00\u8d77\u8865", "\u5230\u65f6\u5019\u4e00\u8d77\u8865", "\u4e00\u8d77\u8865"))


def _looks_like_moyu_self_deprecation(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if "\u6478\u9c7c" in compact and _contains_any(compact, ("\u5f00\u73a9\u7b11", "\u5220\u6389", "\u5220\u4e86", "\u522b\u5199")):
        return True
    if "\u6478\u9c7c" in compact and _contains_any(compact, ("\u6ca1\u5565\u4e8b", "\u6ca1\u4ec0\u4e48\u4e8b", "\u6ca1\u4e8b")):
        return True
    return "\u6478\u9c7c" in compact and _contains_any(compact, ("\u867d\u7136", "\u4f46", "\u53ea\u5199", "\u771f\u662f\u5145\u5b9e"))


def _looks_like_dream_or_fantasy_chatter(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u505a\u4e86\u4e2a\u68a6", "\u68a6\u5230", "\u68a6\u89c1", "\u7ee7\u7eed\u505a\u68a6", "\u5347\u804c\u52a0\u85aa", "\u4e2d\u4e86\u5f69\u7968", "\u8d62\u4e86\u5f69\u7968"))


def _looks_like_generic_tomorrow_continue(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _has_business_work_object(segment) or _specific_matter_hint(segment):
        return False
    if compact in {"\u660e\u5929\u7ee7\u7eed", "\u660e\u65e5\u7ee7\u7eed", "\u660e\u513f\u7ee7\u7eed"}:
        return True
    if _contains_any(compact, ("\u660e\u5929\u518d\u7814\u7a76\u8fd9\u4e9b", "\u660e\u65e5\u518d\u7814\u7a76\u8fd9\u4e9b", "\u660e\u5929\u518d\u770b\u8fd9\u4e9b")):
        return True
    return _contains_any(compact, ("\u522b\u7684\u6ca1\u4e86", "\u6ca1\u5565\u4e86", "\u6ca1\u4e86")) and _contains_any(
        compact,
        ("\u660e\u5929\u7ee7\u7eed", "\u660e\u65e5\u7ee7\u7eed", "\u660e\u513f\u7ee7\u7eed"),
    )


def _looks_like_explicit_daily_report_probe(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u8ba9\u6211\u6d4b\u8bd5\u4e0b", "\u8ba9\u6211\u6d4b\u8bd5\u4e00\u4e0b", "\u6211\u6d4b\u8bd5\u4e0b", "\u6211\u6d4b\u8bd5\u4e00\u4e0b")) and _contains_any(
        compact,
        ("\u4f60\u80fd\u5199\u65e5\u62a5\u5417", "\u4f60\u4f1a\u5199\u65e5\u62a5\u5417", "\u80fd\u5199\u65e5\u62a5\u5417", "\u4f1a\u5199\u65e5\u62a5\u5417"),
    )


def _looks_like_no_change_statement(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u8ba1\u5212\u4e0d\u53d8", "\u660e\u5929\u8ba1\u5212\u4e0d\u53d8", "\u660e\u65e5\u8ba1\u5212\u4e0d\u53d8", "\u4e0d\u53d8", "\u6ca1\u53d8\u5316")) and not (
        _has_business_work_action(segment) and _has_business_work_object(segment)
    )


def _looks_like_weak_travel_destination_fragment(segment: str) -> bool:
    text = str(segment or "").strip(" \t\r\n\u3000\uff0c,\u3002.")
    compact = _compact(text)
    if not compact:
        return False
    destination = _known_travel_destination(text) or _travel_destination_without_fragment_guard(text)
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
    if not _contains_any(compact, ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5", "\u660e\u513f")):
        return False
    return bool(
        re.search(
            rf"^(?:\u54e6?\u5bf9|(?:\u5bf9\u4e86)|\u5c31\u662f|\u5e94\u8be5\u662f|\u662f)?(?:\u4eca\u5929|\u4eca\u65e5|\u660e\u5929|\u660e\u65e5|\u660e\u513f)?(?:\u662f)?(?:\u53bb|\u5230|\u8d74|\u524d\u5f80){re.escape(destination)}$",
            compact,
        )
    )


def _travel_destination_without_fragment_guard(segment: str) -> str:
    text = str(segment or "")
    match = re.search(
        r"(?:\u51fa\u5dee|\u53bb|\u8d74|\u5230|\u53bb\u4e86)([\u4e00-\u9fa5]{2,8}?)(?:\u529e\u7406|\u5f00\u5ead|\u76d6\u7ae0|\u8d70\u8bbf|\u5904\u7406|\u6c9f\u901a|\u8ba8\u85aa|\u51fa\u5dee|$)",
        text,
    )
    if not match:
        return ""
    candidate = re.sub(r"^(?:\u53bb|\u5230|\u8d74|\u524d\u5f80)", "", match.group(1))
    if _invalid_travel_destination_candidate(candidate):
        return ""
    return candidate


def _looks_like_lifestyle_report_meta_chatter(segment: str) -> bool:
    text = str(segment or "")
    if not _contains_any(text, ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    if not _contains_any(
        text,
        (
            "\u98df\u5802",
            "\u7ea2\u70e7\u8089",
            "\u53a8\u5e08",
            "\u597d\u54b8",
            "\u592a\u54b8",
            "\u597d\u5403",
            "\u996d",
            "\u5403",
            "\u559d",
        ),
    ):
        return False
    substantive_business_markers = (
        "\u5408\u540c",
        "\u6848\u4ef6",
        "\u6848",
        "\u6cd5\u9662",
        "\u5ba2\u6237",
        "\u4f9b\u5e94\u5546",
        "\u6295\u8bc9",
        "\u5ba1\u6279",
        "\u6750\u6599",
        "\u8d44\u6599",
        "\u56de\u6b3e",
        "\u7528\u5370",
        "\u5370\u7ae0",
        "\u4f1a\u8bae",
    )
    return not _contains_any(text, substantive_business_markers)


def _looks_like_business_plan_statement(segment: str) -> bool:
    text = str(segment or "").strip()
    if not text or text.endswith(("?", "\uff1f")):
        return False
    if not _contains_any(text, ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u8ba1\u5212", "\u6253\u7b97", "\u51c6\u5907")):
        return False
    if not _contains_any(text, ("\u600e\u4e48", "\u600e\u4e48\u8ddf", "\u60f3\u4e2a\u529e\u6cd5", "\u5f97\u60f3", "\u4e0d\u7136", "\u8981\u8fdd\u7ea6")):
        return False
    return _has_business_work_object(text) or extract_problem_evidence(text).is_business_problem


def _looks_like_case_progress_only_update(segment: str) -> bool:
    text = str(segment or "").strip()
    if not _specific_matter_hint(text):
        return False
    if not _contains_any(text, ("\u6536\u5230", "\u4f20\u7968", "\u6cd5\u9662\u901a\u77e5", "\u901a\u77e5", "\u6392\u671f", "\u5f00\u5ead\u65f6\u95f4")):
        return False
    return not _contains_any(
        text,
        (
            "\u53bb",
            "\u62ff\u5230",
            "\u63d0\u4ea4",
            "\u4ea4\u4e86",
            "\u4ea4\u6750\u6599",
            "\u6574\u7406",
            "\u51c6\u5907",
            "\u6c9f\u901a",
            "\u5904\u7406",
            "\u8ddf\u8fdb",
            "\u51fa\u5dee",
        ),
    )


def _looks_like_future_case_schedule_update(segment: str, *, whole_text: str, received_at: Any = None) -> bool:
    text = str(segment or "").strip()
    if not text or _has_explicit_daily_context_in_text(whole_text):
        return False
    if text == str(whole_text or "").strip() and _contains_any(whole_text, ("\u4eca\u5929", "\u4eca\u65e5")) and _has_reportable_work_piece(whole_text):
        return False
    if not _specific_matter_hint(text):
        return False
    if not _contains_any(text, ("\u5f00\u5ead", "\u51fa\u5ead", "\u6392\u671f", "\u5ead\u524d\u4f1a\u8bae")):
        return False
    relative_hint = date_hint_from_text(text, received_at=received_at)
    if relative_hint == "tomorrow" and _contains_any(_compact(text), ("\u6750\u6599\u90fd\u51c6\u5907\u597d", "\u6750\u6599\u51c6\u5907\u597d", "\u51c6\u5907\u597d\u4e86", "\u6750\u6599\u5df2\u51c6\u5907", "\u51c6\u5907\u4e86\u6750\u6599")):
        return False
    if _contains_any(
        text,
        (
            "\u51fa\u5dee",
            "\u53bb",
            "\u8d74",
            "\u524d\u5f80",
            "\u529e\u7406",
            "\u5904\u7406",
            "\u53c2\u52a0",
            "\u51fa\u5ead",
            "\u63d0\u4ea4",
            "\u4ea4\u4e86",
            "\u6c9f\u901a",
            "\u8ddf\u8fdb",
        ),
    ):
        return False
    if re.search(r"(?:\u8981|\u5f97|\u9700\u8981|\u51c6\u5907|\u8ba1\u5212).{0,6}(?:\u51c6\u5907|\u6574\u7406).{0,8}(?:\u6750\u6599|\u8bc1\u636e|\u5ead\u5ba1)", text):
        return False
    return relative_hint in {"tomorrow", "future_weekday", "next_week"} or _contains_any(
        text,
        (
            "\u540e\u5929",
            "\u5927\u540e\u5929",
            "\u4e0b\u5468",
            "\u5468\u4e00",
            "\u5468\u4e8c",
            "\u5468\u4e09",
            "\u5468\u56db",
            "\u5468\u4e94",
            "\u5468\u516d",
            "\u5468\u65e5",
            "\u661f\u671f\u4e00",
            "\u661f\u671f\u4e8c",
            "\u661f\u671f\u4e09",
            "\u661f\u671f\u56db",
            "\u661f\u671f\u4e94",
            "\u661f\u671f\u516d",
            "\u661f\u671f\u65e5",
        ),
    )


def _looks_like_case_candidate_detail_only(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not text or _has_daily_time_anchor(text):
        return False
    if not _specific_matter_hint(text):
        return False
    if not _contains_any(compact, ("\u5f00\u5ead", "\u5ead\u5ba1", "\u6848\u5377\u6750\u6599", "\u6848\u5377", "\u6750\u6599")):
        return False
    if not _contains_any(compact, ("\u9700\u8981\u5e26", "\u8981\u5e26", "\u5f97\u5e26", "\u5e26\u6848\u5377", "\u5e26\u6750\u6599", "\u90a3\u4e2a", "\u5c31\u662f\u90a3\u4e2a", "\u5bf9\u5c31\u662f")):
        return False
    return True


def _looks_like_case_schedule_only_fragment(segment: str) -> bool:
    text = str(segment or "").strip()
    if not _contains_any(text, ("\u5f00\u5ead", "\u6392\u671f")):
        return False
    if _contains_any(text, ("\u53bb", "\u51fa\u5dee", "\u53c2\u52a0", "\u51fa\u5ead", "\u51c6\u5907", "\u6574\u7406", "\u63d0\u4ea4", "\u4ea4\u4e86")):
        return False
    return bool(re.search(r"(?:\u4e0b\u4e2a\u6708|\u672c\u6708|\d{1,2}\u6708|\d{1,2}\u53f7|\d{1,2}\u65e5)", text))


def _looks_like_short_vent_or_frustration(segment: str) -> bool:
    text = str(segment or "").strip()
    compact = _compact(text)
    if not compact:
        return False
    if _has_positive_daily_work_evidence(text) or extract_problem_evidence(text).is_business_problem or _specific_matter_hint(text):
        return False
    vent_markers = (
        "\u5988\u7684",
        "\u70e6",
        "\u70e6\u6b7b",
        "\u597d\u70e6",
        "\u597d\u7d2f",
        "\u7d2f\u6b7b",
        "\u592a\u7d2f",
        "\u6709\u70b9\u7d2f",
        "\u597d\u56f0",
        "\u56f0\u6b7b",
        "\u4e0d\u8212\u670d",
        "\u5934\u75bc",
        "\u65e0\u804a",
        "\u597d\u65e0\u804a",
        "\u65e0\u8bed",
        "\u5bb3\u6015",
        "\u62c5\u5fc3",
        "\u7126\u8651",
        "\u6709\u70b9\u614c",
        "\u670d\u4e86",
        "\u5d29\u6e83",
        "\u6c14\u6b7b",
        "\u88ab\u6c14\u6b7b",
        "\u592a\u96be\u7528",
        "\u7528\u4e0d\u4e86",
        "\u4e0d\u60f3\u6d4b",
        "\u61d2\u5f97\u6d4b",
        "\u4e0d\u60f3\u5e72\u6d3b",
        "\u4e0d\u60f3\u5de5\u4f5c",
    )
    if not _contains_any(text, vent_markers):
        return False
    return len(compact) <= 18 or _contains_any(text, ("\u6211", "\u8fd9\u4e2a", "\u8fd9\u73a9\u610f", "\u4f60"))


def _has_daily_time_anchor(segment: str) -> bool:
    return has_relative_day_anchor(segment) or _contains_any(segment, ("\u4eca\u586b", "\u4eca\u586b\u5199", "\u4e0a\u5348", "\u4e0b\u5348", "\u665a\u4e0a", "\u4e2d\u5348", "\u65e9\u4e0a"))


def _looks_like_question(segment: str) -> bool:
    text = str(segment or "").strip()
    if _contains_any(_compact(text), ("\u98ce\u9669\uff1f", "\u95ee\u9898\uff1f", "\u98ce\u9669?", "\u95ee\u9898?")) and _has_business_problem_evidence(text):
        return False
    if "?" in text or "\uff1f" in text:
        return True
    compact = _compact(text)
    if _contains_any(compact, ("\u8fd8\u80fd\u6709\u5565", "\u8fd8\u80fd\u6709\u4ec0\u4e48")) and (
        _has_business_work_action(text) or _has_business_work_object(text)
    ):
        return False
    if re.search(r"\u80fd.{0,18}\u4e0d$", compact):
        return True
    if compact.endswith(("\u5417", "\u5462")):
        return True
    if _looks_like_option_question(text):
        return True
    scan = text.replace("\u4e0d\u600e\u4e48", "")
    return _contains_any(
        scan,
        (
            "\u600e\u4e48",
            "\u600e\u6837",
            "\u600e\u4e48\u6837",
            "\u4e3a\u4ec0\u4e48",
            "\u4ec0\u4e48\u65f6\u5019",
            "\u8c01\u8fd8\u6ca1",
            "\u662f\u4e0d\u662f",
            "\u662f\u5565",
            "\u6709\u6ca1\u6709",
            "\u6709\u6ca1",
            "\u80fd\u5426",
            "\u80fd\u4e0d\u80fd",
            "\u53ef\u4e0d\u53ef\u4ee5",
            "\u6709\u4ec0\u4e48",
            "\u6709\u5565",
            "\u4ec0\u4e48\u533a\u522b",
            "\u5565\u533a\u522b",
            "\u4e0d\u4e00\u6837\u7684\u540e\u679c",
            "\u4ec0\u4e48\u540e\u679c",
            "\u5565\u540e\u679c",
            "\u662f\u4ec0\u4e48",
            "\u4ec0\u4e48\u610f\u601d",
            "\u5565\u610f\u601d",
            "\u5565\u6765\u7740",
            "\u4ec0\u4e48\u6765\u7740",
            "\u4ec0\u4e48\u539f\u56e0",
            "\u4f18\u5316\u4e86\u5565",
            "\u4f18\u5316\u4e86\u4ec0\u4e48",
            "\u505a\u4e86\u5565",
            "\u505a\u4e86\u4ec0\u4e48",
            "\u5e72\u4e86\u5565",
            "\u5e72\u4e86\u4ec0\u4e48",
            "\u7a7f\u5565",
            "\u7a7f\u4ec0\u4e48",
            "\u7a7f\u54ea",
            "\u9700\u8981\u8c01",
            "\u8c01\u6765",
            "\u8c01\u786e\u8ba4",
            "\u54ea\u4e9b",
            "\u54ea\u91cc",
            "\u5728\u54ea",
            "\u5728\u54ea\u91cc",
            "\u54ea\u513f",
            "\u4e0b\u8f7d",
            "\u662f\u5426",
            "\u591a\u4e45",
            "\u8981\u591a\u4e45",
            "\u5417",
        ),
    )


def _looks_like_option_question(segment: str) -> bool:
    compact = _compact(segment)
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


def _looks_like_creative_assistant_request(segment: str) -> bool:
    compact = _compact(segment)
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


def _looks_like_document_correction_problem(segment: str) -> bool:
    compact = _compact(segment)
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


def _looks_like_document_business_revision(segment: str) -> bool:
    compact = _compact(segment)
    if not compact or not _has_daily_time_anchor(segment):
        return False
    if _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7", "\u7b2c\u4e00\u6761", "\u7b2c\u4e8c\u6761", "\u7b2c\u4e09\u6761")):
        return False
    return _contains_any(compact, ("\u4fee\u6539", "\u4fee\u8ba2", "\u8c03\u6574", "\u5b8c\u5584", "\u66f4\u65b0")) and _contains_any(
        compact,
        ("\u5408\u540c", "\u534f\u8bae", "\u4fdd\u5bc6\u534f\u8bae", "\u9700\u6c42\u6587\u6863", "\u6587\u6863", "\u6cd5\u5f8b\u610f\u89c1\u4e66", "\u610f\u89c1\u4e66", "\u8d77\u8bc9\u72b6", "\u7b54\u8fa9\u72b6", "\u6587\u4e66"),
    )


def _looks_like_external_report_edit_request(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if _contains_any(compact, ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    return _contains_any(compact, ("\u6628\u5929\u7684\u62a5\u544a", "\u6628\u65e5\u7684\u62a5\u544a", "\u524d\u5929\u7684\u62a5\u544a", "\u62a5\u544a")) and _contains_any(
        compact,
        ("\u7b2c\u4e09\u9875", "\u7b2c3\u9875", "\u6570\u636e\u66f4\u65b0", "\u66f4\u65b0\u6570\u636e", "\u6539\u4e00\u4e0b", "\u4fee\u6539\u4e00\u4e0b"),
    )


def _looks_like_office_device_chatter(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u6253\u5370\u673a", "\u590d\u5370\u673a")) and _contains_any(compact, ("\u574f", "\u574f\u4e86", "\u5361\u7eb8", "\u6ca1\u58a8", "\u6253\u4e0d\u51fa"))


def _looks_like_office_device_correction_edit(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u6253\u5370\u673a", "\u590d\u5370\u673a")) and _contains_any(
        compact,
        ("\u5176\u5b9e", "\u4e0d\u662f", "\u6ca1\u574f", "\u662f\u6ca1\u58a8", "\u6ca1\u58a8\u4e86"),
    ) and _contains_any(compact, ("\u6539\u4e00\u4e0b", "\u6539\u4e0b", "\u4fee\u6539\u4e00\u4e0b", "\u4fee\u6539\u4e0b"))


def _looks_like_tentative_case_resolution_chatter(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u53ef\u80fd\u548c\u89e3", "\u6709\u53ef\u80fd\u548c\u89e3", "\u5e0c\u671b\u987a\u5229")) and _contains_any(
        compact,
        ("\u6848", "\u6848\u4ef6", "\u8fd9\u4e2a\u6848\u4ef6", "\u8fd9\u4e2a\u6848"),
    )


def _looks_like_monthly_meta_request(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    if "\u6708\u62a5" not in compact:
        return False
    return _contains_any(compact, ("\u8d76\u7d27\u5199", "\u8c01\u8fd8\u6ca1\u4ea4", "\u8fd8\u6ca1\u4ea4", "\u50ac", "\u63d0\u9192", "\u63d0\u4ea4", "\u5df2\u63d0\u4ea4", "\u672c\u6708", "\u6c47\u603b", "\u63d0\u53d6", "\u4e0d\u5199", "\u4e0b\u5468\u8865", "\u884c\u4e0d\u884c", "\u5ba1\u6279\u8c01", "\u8c01\u5728\u7ba1", "\u6ca1\u53cd\u5e94"))


def _looks_like_daily_bot_feedback(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u65e5\u62a5\u7cfb\u7edf", "\u65e5\u62a5agent", "\u65e5\u62a5\u673a\u5668\u4eba")) and _contains_any(
        compact,
        ("\u96be\u7528", "\u4e0d\u597d\u7528", "\u7528\u4e0d\u4e86", "\u4e00\u5768", "\u592a\u8822", "\u771f\u96be\u7528"),
    )


def _looks_like_contextual_case_strategy_plan(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _contains_any(compact, ("\u660e\u5929", "\u660e\u65e5", "\u660e\u513f", "\u660e\u65e9")) and _contains_any(compact, ("\u90a3\u4e2a\u6848\u5b50", "\u90a3\u4e2a\u6848", "\u6848\u5b50", "\u6848\u4ef6")) and _contains_any(
        compact,
        ("\u7b56\u7565", "\u8001\u677f", "\u9886\u5bfc", "\u786e\u8ba4", "\u518d\u786e\u8ba4", "\u6c9f\u901a"),
    )


def _looks_like_completed_previous_plan_without_new_content(segment: str) -> bool:
    compact = _compact(segment)
    if not compact:
        return False
    return _looks_like_completed_previous_plan(segment) and _contains_any(
        compact,
        ("\u4eca\u5929\u6ca1\u4ec0\u4e48\u65b0\u8ba1\u5212", "\u4eca\u65e5\u6ca1\u4ec0\u4e48\u65b0\u8ba1\u5212", "\u4eca\u5929\u6ca1\u65b0\u8ba1\u5212", "\u4eca\u65e5\u6ca1\u65b0\u8ba1\u5212", "\u4eca\u5929\u65e0\u65b0\u8ba1\u5212"),
    )


def _looks_like_case_reply_status_question(segment: str) -> bool:
    text = str(segment or "")
    if not _looks_like_question(text):
        return False
    if _contains_any(text, ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5", "\u660e\u513f")):
        return False
    if not (_specific_matter_hint(text) or _contains_any(text, ("\u6848", "\u6848\u4ef6", "\u5f8b\u5e08", "\u5bf9\u65b9"))):
        return False
    return _contains_any(
        text,
        (
            "\u56de\u590d\u4e86\u5417",
            "\u6709\u6ca1\u6709\u56de\u590d",
            "\u6709\u56de\u590d\u5417",
            "\u6709\u6d88\u606f\u5417",
            "\u6709\u8fdb\u5c55\u5417",
            "\u4ec0\u4e48\u8fdb\u5c55",
        ),
    )


def _looks_like_negative_replacement(segment: str) -> bool:
    text = str(segment or "")
    return bool(
        re.search(r"不是\s*.+?[，,。；;]?\s*(?:而是|是)\s*.+", text)
        or re.search(r"(?:说错了|错了)[，,。；;]?\s*是\s*.+?[，,。；;]?\s*不是\s*.+", text)
    )


def _has_active_task(active_tasks: tuple[Any, ...], workflow: str) -> bool:
    return any(str(getattr(task, "workflow", "") or "") == workflow for task in active_tasks)


def _commit_policy(actions: list[UserAction]) -> str:
    if not actions:
        return "blocked"
    if any(action.action_type == ACTION_DISAMBIGUATION_REQUIRED for action in actions):
        return "needs_clarification"
    if any(action.requires_confirmation for action in actions):
        return "needs_confirmation"
    if all(action.write_policy in {POLICY_READ_ONLY, POLICY_NO_WRITE} for action in actions):
        return "read_only"
    return "partial_allowed"


def _warnings(actions: list[UserAction]) -> list[str]:
    warnings: list[str] = []
    workflows = {action.workflow for action in actions if action.workflow != WORKFLOW_UNKNOWN_OR_HELP}
    if len(workflows) > 1:
        warnings.append("multi_workflow_actions")
    if any(action.write_policy == POLICY_PENDING for action in actions):
        warnings.append("pending_clarification")
    if any("blocks_context_write" in action.safety_flags for action in actions):
        warnings.append("blocks_context_write")
    return warnings


def _dedupe_actions(actions: list[UserAction]) -> list[UserAction]:
    seen: set[tuple[str, str, int]] = set()
    result: list[UserAction] = []
    for action in actions:
        key = (action.action_type, action.source_text_hash, action.source_segment_index)
        if key in seen:
            continue
        seen.add(key)
        result.append(action)
    return result


def _suppress_resolved_daily_ambiguity(actions: list[UserAction]) -> list[UserAction]:
    has_concrete_daily_action = any(
        action.workflow == WORKFLOW_DAILY_REPORT and action.write_policy == POLICY_WRITE for action in actions
    )
    if not has_concrete_daily_action:
        return actions
    suppressible_flags = {"ambiguous_daily_context", "ambiguous_daily_edit_without_context"}
    return [
        action
        for action in actions
        if not (
            action.action_type == ACTION_DISAMBIGUATION_REQUIRED
            and suppressible_flags.intersection(action.safety_flags)
        )
        and not (
            action.action_type == ACTION_SMALL_TALK
            and action.workflow == WORKFLOW_CHAT
            and _is_discourse_filler(action.payload.get("content", ""))
        )
    ]


def _is_discourse_filler(value: object) -> bool:
    return _compact(str(value or "")) in {
        "\u54c8\u54e6",
        "\u54ce",
        "\u5509",
        "\u989d",
        "\u5443",
        "\u54e6",
        "\u54e6\u54e6",
        "\u55ef",
        "\u55ef\u55ef",
    }


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    compact = _compact(value)
    return any(_compact(marker) in compact for marker in markers)


def _compact(value: str) -> str:
    return re.sub(r"[\s\u3000:：,，.。;；!！?？()（）\[\]【】\"'“”‘’]+", "", str(value or "")).lower()


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
