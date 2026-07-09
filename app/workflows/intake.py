from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
import hashlib
import re
from typing import Any

from app.agent2.daily_state import PENDING_DAILY_CANDIDATE_KEY
from app.workflows.relative_dates import date_hint_from_text


WORKFLOW_DAILY_REPORT = "daily_report"
WORKFLOW_MONTHLY_REPORT = "monthly_report"
WORKFLOW_WEEKLY_REPORT = "weekly_report"
WORKFLOW_CASE_PROGRESS = "case_progress"
WORKFLOW_TRAVEL_COORDINATION = "travel_coordination"
WORKFLOW_LEGAL_RESEARCH = "legal_research"
WORKFLOW_INTERNAL_QA = "internal_qa"
WORKFLOW_CHAT = "chat"
WORKFLOW_UNKNOWN_OR_HELP = "unknown_or_help"

EFFECT_ADD_DAILY_REPORT_ITEM = "add_daily_report_item"
EFFECT_CONFIRM_DAILY_REPORT = "confirm_daily_report"
EFFECT_LEGACY_DAILY_CONTEXT_ACTION = "legacy_daily_context_action"


@dataclass(frozen=True)
class ActiveWorkflowTask:
    """A domain task that may be waiting for the sender's next message."""

    workflow: str
    task_id: str = ""
    status: str = ""
    reply_candidate: bool = False
    awaiting_confirmation: bool = False
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IncomingMessageEnvelope:
    """Shared intake shape before a message reaches any domain workflow."""

    sender_id: str
    sender_name: str
    dingtalk_user_id: str
    source: str
    raw_text: str
    message_id: str = ""
    conversation_id: str = ""
    received_at: datetime | None = None
    active_tasks: tuple[ActiveWorkflowTask, ...] = ()
    recent_state: dict[str, Any] = field(default_factory=dict)

    @property
    def compact_text(self) -> str:
        return _compact(self.raw_text)

    def observation_base(self) -> dict[str, Any]:
        return {
            "sender_id": self.sender_id,
            "dingtalk_user_id": self.dingtalk_user_id,
            "source": self.source,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "raw_text_hash": _hash_text(self.raw_text),
            "raw_text_chars": len(self.raw_text or ""),
            "active_tasks": [
                {
                    "workflow": task.workflow,
                    "task_id": task.task_id,
                    "status": task.status,
                    "reply_candidate": task.reply_candidate,
                    "awaiting_confirmation": task.awaiting_confirmation,
                }
                for task in self.active_tasks
            ],
        }


@dataclass(frozen=True)
class WorkflowRoute:
    workflow: str
    confidence: float
    reason: str
    task_id: str = ""
    observe_only: bool = True
    signals: dict[str, float] = field(default_factory=dict)

    def as_observation(self, envelope: IncomingMessageEnvelope) -> dict[str, Any]:
        payload = envelope.observation_base()
        payload.update(
            {
                "selected_workflow": self.workflow,
                "confidence": self.confidence,
                "reason": self.reason,
                "task_id": self.task_id,
                "observe_only": self.observe_only,
                "signals": self.signals,
            }
        )
        return payload


@dataclass(frozen=True)
class WorkflowEffect:
    """A planned business impact. It is a plan, not an execution command."""

    effect_type: str
    target_system: str
    target: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    links: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    requires_confirmation: bool = False
    reason: str = ""

    def as_observation(self) -> dict[str, Any]:
        return {
            "effect_type": self.effect_type,
            "target_system": self.target_system,
            "target": self.target,
            "payload_keys": sorted(self.payload.keys()),
            "link_keys": sorted(self.links.keys()),
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorkflowSegment:
    """One independently classified segment inside a multi-intent message."""

    index: int
    text_hash: str
    text_chars: int
    primary_workflow: str
    matched_workflows: list[str]
    effect_types: list[str] = field(default_factory=list)
    confidence: float = 0.0
    intent: str = ""
    reason: str = ""

    def as_observation(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text_hash": self.text_hash,
            "text_chars": self.text_chars,
            "primary_workflow": self.primary_workflow,
            "matched_workflows": list(self.matched_workflows),
            "effect_types": list(self.effect_types),
            "confidence": self.confidence,
            "intent": self.intent,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SafetyDecision:
    """Write-safety decision for a routing plan."""

    commit_policy: str
    flags: list[str] = field(default_factory=list)
    safe_effects: list[str] = field(default_factory=list)
    pending_effects: list[str] = field(default_factory=list)
    blocked_effects: list[str] = field(default_factory=list)
    reason: str = ""

    def as_observation(self) -> dict[str, Any]:
        return {
            "commit_policy": self.commit_policy,
            "flags": list(self.flags),
            "safe_effects": list(self.safe_effects),
            "pending_effects": list(self.pending_effects),
            "blocked_effects": list(self.blocked_effects),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RoutingPlan:
    """Agent 2.0 intake plan.

    A message may hit multiple workflows. This object records the ownership,
    planned effects, and safety decision before any domain state is changed.
    """

    primary_workflow: str
    matched_workflows: list[str]
    effects: list[WorkflowEffect] = field(default_factory=list)
    safety_decision: SafetyDecision = field(
        default_factory=lambda: SafetyDecision(commit_policy="blocked", reason="no safety decision")
    )
    confidence: float = 0.0
    reason: str = ""
    task_id: str = ""
    observe_only: bool = True
    signals: dict[str, float] = field(default_factory=dict)
    entities: dict[str, Any] = field(default_factory=dict)
    segments: list[WorkflowSegment] = field(default_factory=list)

    def as_observation(self, envelope: IncomingMessageEnvelope) -> dict[str, Any]:
        payload = envelope.observation_base()
        payload.update(
            {
                "primary_workflow": self.primary_workflow,
                "matched_workflows": list(self.matched_workflows),
                "confidence": self.confidence,
                "reason": self.reason,
                "task_id": self.task_id,
                "observe_only": self.observe_only,
                "signals": self.signals,
                "entities_keys": sorted(self.entities.keys()),
                "effects": [effect.as_observation() for effect in self.effects],
                "safety_decision": self.safety_decision.as_observation(),
                "segments": [segment.as_observation() for segment in self.segments],
            }
        )
        return payload


class WorkflowRouter:
    """Scores workflow ownership without mutating domain state."""

    def plan(self, envelope: IncomingMessageEnvelope) -> RoutingPlan:
        """Build an Agent 2.0 routing plan without mutating domain state."""

        monthly_task = _latest_active_task(envelope.active_tasks, WORKFLOW_MONTHLY_REPORT)
        daily_task = _latest_active_task(envelope.active_tasks, WORKFLOW_DAILY_REPORT)
        weekly_task = _latest_active_task(envelope.active_tasks, WORKFLOW_WEEKLY_REPORT)
        daily_signal = _daily_report_signal(envelope.raw_text, received_at=envelope.received_at)
        daily_context_signal = _daily_context_signal(envelope.raw_text, daily_task)
        monthly_signal = _monthly_report_signal(envelope.raw_text, monthly_task)
        weekly_signal = _weekly_report_signal(envelope.raw_text, weekly_task)
        case_signal = _case_progress_signal(envelope.raw_text)
        travel_signal = _travel_signal(envelope.raw_text)
        legal_research_signal = _legal_research_signal(envelope.raw_text)
        qa_signal = _internal_qa_signal(envelope.raw_text)
        help_signal = _help_signal(envelope.raw_text)
        chat_signal = _small_talk_signal(envelope.raw_text)
        signals = {
            WORKFLOW_DAILY_REPORT: daily_signal,
            WORKFLOW_MONTHLY_REPORT: monthly_signal,
            WORKFLOW_WEEKLY_REPORT: weekly_signal,
            WORKFLOW_CASE_PROGRESS: case_signal,
            WORKFLOW_TRAVEL_COORDINATION: travel_signal,
            WORKFLOW_LEGAL_RESEARCH: legal_research_signal,
            WORKFLOW_INTERNAL_QA: qa_signal,
            WORKFLOW_CHAT: chat_signal,
            WORKFLOW_UNKNOWN_OR_HELP: help_signal,
            "daily_context": daily_context_signal,
        }
        user_action_plan = _plan_user_actions(envelope)

        if _looks_like_usage_preference_instruction(envelope.raw_text):
            return _with_segments(_blocked_plan(
                primary_workflow=WORKFLOW_UNKNOWN_OR_HELP,
                matched_workflows=[],
                signals=signals,
                flags=["usage_preference_instruction"],
                reason="message changes future bot behavior and must not be written to a daily report",
            ), envelope)

        if _orphan_confirmation(envelope):
            return _with_segments(_blocked_plan(
                primary_workflow=WORKFLOW_UNKNOWN_OR_HELP,
                matched_workflows=[],
                signals=signals,
                flags=["orphan_confirmation"],
                reason="confirmation-like message has no active confirmation context",
            ), envelope)

        if monthly_task and monthly_task.reply_candidate:
            effects = [
                WorkflowEffect(
                    effect_type="capture_monthly_report_reply",
                    target_system=WORKFLOW_MONTHLY_REPORT,
                    target={"task_id": monthly_task.task_id},
                    risk_level="medium",
                    reason=monthly_task.reason or "active monthly task accepted reply candidate",
                )
            ]
            return _with_segments(RoutingPlan(
                primary_workflow=WORKFLOW_MONTHLY_REPORT,
                matched_workflows=[WORKFLOW_MONTHLY_REPORT],
                effects=effects,
                safety_decision=_safety_for_effects(effects, [WORKFLOW_MONTHLY_REPORT]),
                confidence=max(0.86, monthly_signal),
                reason=monthly_task.reason or "active monthly task accepted reply candidate",
                task_id=monthly_task.task_id,
                signals=signals,
            ), envelope)

        if monthly_task and monthly_task.awaiting_confirmation and _looks_like_confirmation(envelope.raw_text):
            effects = [
                WorkflowEffect(
                    effect_type="confirm_monthly_report_submission",
                    target_system=WORKFLOW_MONTHLY_REPORT,
                    target={"task_id": monthly_task.task_id},
                    risk_level="medium",
                    reason="active monthly-report task is awaiting confirmation",
                )
            ]
            return _with_segments(RoutingPlan(
                primary_workflow=WORKFLOW_MONTHLY_REPORT,
                matched_workflows=[WORKFLOW_MONTHLY_REPORT],
                effects=effects,
                safety_decision=_safety_for_effects(effects, [WORKFLOW_MONTHLY_REPORT]),
                confidence=0.88,
                reason="active monthly-report task is awaiting confirmation",
                task_id=monthly_task.task_id,
                signals=signals,
            ), envelope)

        if daily_task and daily_task.awaiting_confirmation and _looks_like_confirmation(envelope.raw_text):
            effects = [
                WorkflowEffect(
                    effect_type=EFFECT_CONFIRM_DAILY_REPORT,
                    target_system=WORKFLOW_DAILY_REPORT,
                    target={
                        "task_id": daily_task.task_id,
                        "report_date": _active_task_report_date(daily_task),
                    },
                    risk_level="low",
                    reason=daily_task.reason or "active daily-report task is awaiting confirmation",
                )
            ]
            return _with_segments(RoutingPlan(
                primary_workflow=WORKFLOW_DAILY_REPORT,
                matched_workflows=[WORKFLOW_DAILY_REPORT],
                effects=effects,
                safety_decision=_safety_for_effects(effects, [WORKFLOW_DAILY_REPORT]),
                confidence=max(0.88, daily_context_signal),
                reason=daily_task.reason or "active daily-report task is awaiting confirmation",
                task_id=daily_task.task_id,
                signals=signals,
            ), envelope)

        if daily_task and _daily_task_has_pending_candidate(daily_task) and _looks_like_candidate_focus_confirmation(envelope.raw_text):
            return _with_segments(_blocked_plan(
                primary_workflow=WORKFLOW_UNKNOWN_OR_HELP,
                matched_workflows=[],
                signals=signals,
                task_id=daily_task.task_id,
                flags=["pending_daily_candidate_confirmation"],
                reason="user confirmed a focused daily draft candidate but did not provide an edit action",
            ), envelope)

        if (
            daily_task
            and daily_task.reply_candidate
            and _looks_like_daily_submit_reply(envelope.raw_text)
            and _active_daily_context_can_claim(
                monthly_signal=monthly_signal,
                weekly_signal=weekly_signal,
                case_signal=case_signal,
                travel_signal=travel_signal,
                legal_research_signal=legal_research_signal,
                qa_signal=qa_signal,
                help_signal=help_signal,
            )
        ):
            effects = [
                WorkflowEffect(
                    effect_type=EFFECT_CONFIRM_DAILY_REPORT,
                    target_system=WORKFLOW_DAILY_REPORT,
                    target={"task_id": daily_task.task_id},
                    risk_level="low",
                    reason=daily_task.reason or "active daily-report context accepted short submit reply",
                )
            ]
            return _with_segments(RoutingPlan(
                primary_workflow=WORKFLOW_DAILY_REPORT,
                matched_workflows=[WORKFLOW_DAILY_REPORT],
                effects=effects,
                safety_decision=_safety_for_effects(effects, [WORKFLOW_DAILY_REPORT]),
                confidence=max(0.86, daily_context_signal),
                reason=daily_task.reason or "active daily-report context accepted short submit reply",
                task_id=daily_task.task_id,
                signals=signals,
            ), envelope)

        action_guard_plan = _action_first_guard_plan(
            envelope=envelope,
            action_plan=user_action_plan,
            signals=signals,
        )
        if action_guard_plan is not None:
            return _with_segments(action_guard_plan, envelope)

        if (
            daily_task
            and daily_task.reply_candidate
            and daily_context_signal >= 0.35
            and monthly_signal < 0.55
            and weekly_signal < 0.45
            and travel_signal < 0.45
            and case_signal < 0.45
            and help_signal < 0.75
            and qa_signal < 0.55
            and legal_research_signal < 0.75
        ):
            effects = [
                WorkflowEffect(
                    effect_type=EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
                    target_system=WORKFLOW_DAILY_REPORT,
                    target={
                        "task_id": daily_task.task_id,
                        "field": _daily_field_hint(envelope.raw_text),
                        "status": daily_task.status,
                    },
                    payload={"content": envelope.raw_text},
                    links={"source_message_id": envelope.message_id},
                    risk_level="low",
                    reason=daily_task.reason or "active daily-report context accepted follow-up text",
                )
            ]
            return _with_segments(RoutingPlan(
                primary_workflow=WORKFLOW_DAILY_REPORT,
                matched_workflows=[WORKFLOW_DAILY_REPORT],
                effects=effects,
                safety_decision=_safety_for_effects(effects, [WORKFLOW_DAILY_REPORT]),
                confidence=max(0.78, daily_context_signal, daily_signal),
                reason=daily_task.reason or "active daily-report context accepted follow-up text",
                task_id=daily_task.task_id,
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ), envelope)

        if monthly_task and daily_signal < 0.7 and daily_context_signal < 0.5 and help_signal < 0.7 and qa_signal < 0.7:
            return _with_segments(_blocked_plan(
                primary_workflow=WORKFLOW_UNKNOWN_OR_HELP,
                matched_workflows=[],
                signals=signals,
                task_id=monthly_task.task_id,
                flags=["active_monthly_task_guard"],
                reason="active monthly-report task exists, but the message is not safe for daily fallback",
            ), envelope)

        matched_workflows = _matched_workflows(
            daily_signal=daily_signal,
            monthly_signal=monthly_signal,
            weekly_signal=weekly_signal,
            case_signal=case_signal,
            travel_signal=travel_signal,
            legal_research_signal=legal_research_signal,
            qa_signal=qa_signal,
            daily_context_signal=daily_context_signal,
            monthly_task=monthly_task,
            daily_task=daily_task,
            weekly_task=weekly_task,
        )
        if not matched_workflows:
            return _with_segments(_blocked_plan(
                primary_workflow=WORKFLOW_UNKNOWN_OR_HELP,
                matched_workflows=[],
                signals=signals,
                reason="no workflow has enough ownership confidence",
            ), envelope)

        primary_workflow = _primary_workflow(matched_workflows)
        effects = _planned_effects(envelope, matched_workflows)
        safety_decision = _safety_for_effects(effects, matched_workflows)
        return _with_segments(RoutingPlan(
            primary_workflow=primary_workflow,
            matched_workflows=matched_workflows,
            effects=effects,
            safety_decision=safety_decision,
            confidence=max(signals.get(workflow, 0.0) for workflow in matched_workflows),
            reason="agent2 routing plan built from workflow signals",
            task_id=_active_task_id(envelope.active_tasks, primary_workflow),
            signals=signals,
            entities=_entity_hints(envelope.raw_text),
        ), envelope)

    def route(self, envelope: IncomingMessageEnvelope) -> WorkflowRoute:
        monthly_task = _latest_active_task(envelope.active_tasks, WORKFLOW_MONTHLY_REPORT)
        daily_task = _latest_active_task(envelope.active_tasks, WORKFLOW_DAILY_REPORT)
        daily_signal = _daily_report_signal(envelope.raw_text, received_at=envelope.received_at)
        daily_context_signal = _daily_context_signal(envelope.raw_text, daily_task)
        monthly_signal = _monthly_report_signal(envelope.raw_text, monthly_task)
        weekly_signal = _weekly_report_signal(envelope.raw_text)
        case_signal = _case_progress_signal(envelope.raw_text)
        travel_signal = _travel_signal(envelope.raw_text)
        legal_research_signal = _legal_research_signal(envelope.raw_text)
        qa_signal = _internal_qa_signal(envelope.raw_text)
        help_signal = _help_signal(envelope.raw_text)
        chat_signal = _small_talk_signal(envelope.raw_text)
        signals = {
            WORKFLOW_DAILY_REPORT: daily_signal,
            WORKFLOW_MONTHLY_REPORT: monthly_signal,
            WORKFLOW_WEEKLY_REPORT: weekly_signal,
            WORKFLOW_CASE_PROGRESS: case_signal,
            WORKFLOW_TRAVEL_COORDINATION: travel_signal,
            WORKFLOW_LEGAL_RESEARCH: legal_research_signal,
            WORKFLOW_INTERNAL_QA: qa_signal,
            WORKFLOW_CHAT: chat_signal,
            WORKFLOW_UNKNOWN_OR_HELP: help_signal,
            "daily_context": daily_context_signal,
        }
        user_action_plan = _plan_user_actions(envelope)

        if _action_plan_is_chat_only(user_action_plan):
            return WorkflowRoute(
                workflow=WORKFLOW_CHAT,
                confidence=max(0.82, chat_signal, help_signal),
                reason="action-first intake identified a chat turn",
                signals=signals,
            )

        if monthly_task and monthly_task.reply_candidate:
            return WorkflowRoute(
                workflow=WORKFLOW_MONTHLY_REPORT,
                confidence=max(0.86, monthly_signal),
                reason=monthly_task.reason or "active monthly-report task accepted the reply candidate",
                task_id=monthly_task.task_id,
                signals=signals,
            )

        if monthly_task and monthly_task.awaiting_confirmation and _looks_like_confirmation(envelope.raw_text):
            return WorkflowRoute(
                workflow=WORKFLOW_MONTHLY_REPORT,
                confidence=0.88,
                reason="active monthly-report task is awaiting confirmation",
                task_id=monthly_task.task_id,
                signals=signals,
            )

        if monthly_task and daily_signal < 0.7 and daily_context_signal < 0.5 and help_signal < 0.7:
            return WorkflowRoute(
                workflow=WORKFLOW_UNKNOWN_OR_HELP,
                confidence=0.66,
                reason="active monthly-report task exists, but the message is not a safe monthly reply or daily report",
                task_id=monthly_task.task_id,
                signals=signals,
            )

        non_daily_route = _strong_non_daily_route(
            monthly_signal=monthly_signal,
            weekly_signal=weekly_signal,
            case_signal=case_signal,
            travel_signal=travel_signal,
            legal_research_signal=legal_research_signal,
            qa_signal=qa_signal,
        )
        if non_daily_route is not None:
            workflow, confidence = non_daily_route
            return WorkflowRoute(
                workflow=workflow,
                confidence=confidence,
                reason="stronger non-daily workflow signal detected",
                task_id=_active_task_id(envelope.active_tasks, workflow),
                signals=signals,
            )

        if daily_task and (
            daily_signal >= 0.45
            or (
                daily_context_signal >= 0.35
                and _active_daily_context_can_claim(
                    monthly_signal=monthly_signal,
                    weekly_signal=weekly_signal,
                    case_signal=case_signal,
                    travel_signal=travel_signal,
                    legal_research_signal=legal_research_signal,
                    qa_signal=qa_signal,
                    help_signal=help_signal,
                )
            )
        ):
            return WorkflowRoute(
                workflow=WORKFLOW_DAILY_REPORT,
                confidence=max(0.78, daily_signal, daily_context_signal),
                reason=daily_task.reason or "active daily-report state and daily-report signal",
                task_id=daily_task.task_id,
                signals=signals,
            )

        if daily_signal >= max(monthly_signal, help_signal, 0.45):
            return WorkflowRoute(
                workflow=WORKFLOW_DAILY_REPORT,
                confidence=daily_signal,
                reason="daily-report wording or structure detected",
                signals=signals,
            )

        if help_signal >= 0.7:
            return WorkflowRoute(
                workflow=WORKFLOW_UNKNOWN_OR_HELP,
                confidence=help_signal,
                reason="message looks like a question or non-report interaction",
                signals=signals,
            )

        return WorkflowRoute(
            workflow=WORKFLOW_UNKNOWN_OR_HELP,
            confidence=0.5,
            reason="no workflow has enough ownership confidence",
            signals=signals,
        )


def _latest_active_task(tasks: tuple[ActiveWorkflowTask, ...], workflow: str) -> ActiveWorkflowTask | None:
    for task in reversed(tasks):
        if task.workflow == workflow:
            return task
    return None


def _daily_task_has_pending_candidate(task: ActiveWorkflowTask | None) -> bool:
    if task is None:
        return False
    pending_keys = task.metadata.get("pending_keys") if isinstance(task.metadata, dict) else None
    return isinstance(pending_keys, (list, tuple)) and PENDING_DAILY_CANDIDATE_KEY in pending_keys


def _active_task_id(tasks: tuple[ActiveWorkflowTask, ...], workflow: str) -> str:
    task = _latest_active_task(tasks, workflow)
    return task.task_id if task else ""


def _active_task_report_date(task: ActiveWorkflowTask | None) -> str:
    if task is None:
        return ""
    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    return str(metadata.get("report_date") or metadata.get("date") or "")


def _with_segments(plan: RoutingPlan, envelope: IncomingMessageEnvelope) -> RoutingPlan:
    segments = _split_message_segments(envelope.raw_text, received_at=envelope.received_at)
    if len(segments) <= 1:
        return plan
    return replace(
        plan,
        segments=[
            _classify_segment(segment, index=index, envelope=envelope)
            for index, segment in enumerate(segments, start=1)
        ],
    )


def _split_message_segments(raw_text: str, *, received_at: Any = None) -> list[str]:
    from app.workflows.action_intake import split_user_action_segments

    return split_user_action_segments(raw_text, received_at=received_at)
    text = str(raw_text or "").strip()
    if not text:
        return []
    normalized_segments = [
        segment.strip()
        for segment in re.split(r"[\r\n。！？!?；;]+", text)
        if segment.strip()
    ]
    return normalized_segments
    segments: list[str] = []
    current: list[str] = []
    for char in text:
        if char in "\r\n。！？!?；;":
            value = "".join(current).strip()
            if value:
                segments.append(value)
            current = []
            continue
        current.append(char)
    value = "".join(current).strip()
    if value:
        segments.append(value)
    return segments


def _classify_segment(segment_text: str, *, index: int, envelope: IncomingMessageEnvelope) -> WorkflowSegment:
    daily_task = _latest_active_task(envelope.active_tasks, WORKFLOW_DAILY_REPORT)
    monthly_task = _latest_active_task(envelope.active_tasks, WORKFLOW_MONTHLY_REPORT)
    weekly_task = _latest_active_task(envelope.active_tasks, WORKFLOW_WEEKLY_REPORT)
    daily_signal = _daily_report_signal(segment_text, received_at=envelope.received_at)
    daily_context_signal = _daily_context_signal(segment_text, daily_task)
    monthly_signal = _monthly_report_signal(segment_text, monthly_task)
    weekly_signal = _weekly_report_signal(segment_text, weekly_task)
    case_signal = _case_progress_signal(segment_text)
    travel_signal = _travel_signal(segment_text)
    legal_research_signal = _legal_research_signal(segment_text)
    qa_signal = _internal_qa_signal(segment_text)
    help_signal = _help_signal(segment_text)
    small_talk_signal = _small_talk_signal(segment_text)

    if small_talk_signal >= 0.45 and max(
        daily_signal,
        daily_context_signal,
        monthly_signal,
        weekly_signal,
        case_signal,
        travel_signal,
        legal_research_signal,
        qa_signal,
        help_signal,
    ) < 0.55:
        return WorkflowSegment(
            index=index,
            text_hash=_hash_text(segment_text),
            text_chars=len(segment_text),
            primary_workflow=WORKFLOW_CHAT,
            matched_workflows=[WORKFLOW_CHAT],
            confidence=small_talk_signal,
            intent="small_talk",
            reason="segment looks like small talk or non-workflow chatter",
        )

    if help_signal >= 0.7 and qa_signal >= 0.55 and legal_research_signal < 0.45:
        return WorkflowSegment(
            index=index,
            text_hash=_hash_text(segment_text),
            text_chars=len(segment_text),
            primary_workflow=WORKFLOW_INTERNAL_QA,
            matched_workflows=[WORKFLOW_INTERNAL_QA],
            confidence=max(help_signal, qa_signal),
            intent="internal_qa",
            reason="segment looks like a question or internal Q&A",
        )

    matched_workflows = _matched_workflows(
        daily_signal=daily_signal,
        monthly_signal=monthly_signal,
        weekly_signal=weekly_signal,
        case_signal=case_signal,
        travel_signal=travel_signal,
        legal_research_signal=legal_research_signal,
        qa_signal=qa_signal,
        daily_context_signal=daily_context_signal,
        monthly_task=monthly_task,
        daily_task=daily_task,
        weekly_task=weekly_task,
    )
    if not matched_workflows:
        return WorkflowSegment(
            index=index,
            text_hash=_hash_text(segment_text),
            text_chars=len(segment_text),
            primary_workflow=WORKFLOW_UNKNOWN_OR_HELP,
            matched_workflows=[],
            confidence=max(
                daily_signal,
                daily_context_signal,
                monthly_signal,
                weekly_signal,
                case_signal,
                travel_signal,
                legal_research_signal,
                qa_signal,
                help_signal,
                small_talk_signal,
            ),
            intent="unknown_or_help",
            reason="segment has no clear workflow owner",
        )

    segment_envelope = replace(envelope, raw_text=segment_text)
    effects = _planned_effects(segment_envelope, matched_workflows)
    effect_types = [effect.effect_type for effect in effects]
    primary_workflow = _primary_workflow(matched_workflows)
    return WorkflowSegment(
        index=index,
        text_hash=_hash_text(segment_text),
        text_chars=len(segment_text),
        primary_workflow=primary_workflow,
        matched_workflows=matched_workflows,
        effect_types=effect_types,
        confidence=max(
            daily_signal,
            daily_context_signal,
            monthly_signal,
            weekly_signal,
            case_signal,
            travel_signal,
            legal_research_signal,
            qa_signal,
            help_signal,
        ),
        intent=_segment_intent(primary_workflow, effect_types),
        reason="segment classified independently inside multi-intent message",
    )


def _segment_intent(primary_workflow: str, effect_types: list[str]) -> str:
    if effect_types:
        return effect_types[0]
    return primary_workflow


def _blocked_plan(
    *,
    primary_workflow: str,
    matched_workflows: list[str],
    signals: dict[str, float],
    flags: list[str] | None = None,
    reason: str,
    task_id: str = "",
) -> RoutingPlan:
    return RoutingPlan(
        primary_workflow=primary_workflow,
        matched_workflows=matched_workflows,
        effects=[],
        safety_decision=SafetyDecision(
            commit_policy="blocked",
            flags=list(flags or []),
            reason=reason,
        ),
        confidence=max(signals.values()) if signals else 0.0,
        reason=reason,
        task_id=task_id,
        signals=signals,
    )


def _plan_user_actions(envelope: IncomingMessageEnvelope):
    from app.workflows.action_intake import plan_user_actions

    return plan_user_actions(envelope)


def _action_plan_is_chat_only(action_plan: Any) -> bool:
    from app.workflows.action_intake import (
        ACTION_ASSISTANT_FEEDBACK,
        ACTION_SMALL_TALK,
        POLICY_WRITE,
    )

    actions = list(getattr(action_plan, "actions", []) or [])
    if not actions:
        return False
    if any(str(getattr(action, "write_policy", "") or "") == POLICY_WRITE for action in actions):
        return False
    return all(
        str(getattr(action, "action_type", "") or "") in {ACTION_ASSISTANT_FEEDBACK, ACTION_SMALL_TALK}
        for action in actions
    )


def _action_first_guard_plan(
    *,
    envelope: IncomingMessageEnvelope,
    action_plan: Any,
    signals: dict[str, float],
) -> RoutingPlan | None:
    from app.workflows.action_intake import (
        ACTION_ASSISTANT_FEEDBACK,
        ACTION_CASE_PROGRESS,
        ACTION_DAILY_CONFIRM,
        ACTION_DAILY_EDIT,
        ACTION_DAILY_READ_CURRENT,
        ACTION_DAILY_WRITE,
        ACTION_DAILY_READ_HISTORY,
        ACTION_DISAMBIGUATION_REQUIRED,
        ACTION_INTERNAL_QA,
        ACTION_LEGAL_RESEARCH,
        ACTION_MONTHLY_REPLY,
        ACTION_MONTHLY_STATUS_QUERY,
        ACTION_SMALL_TALK,
        ACTION_TRAVEL_COORDINATION,
        ACTION_WEEKLY_REQUEST,
        POLICY_WRITE,
    )

    actions = list(getattr(action_plan, "actions", []) or [])
    if not actions:
        return None

    action_types = [str(getattr(action, "action_type", "") or "") for action in actions]
    write_actions = [
        action
        for action in actions
        if str(getattr(action, "write_policy", "") or "") == POLICY_WRITE
    ]
    daily_task = _latest_active_task(envelope.active_tasks, WORKFLOW_DAILY_REPORT)
    daily_write_action_types = {ACTION_DAILY_WRITE, ACTION_DAILY_EDIT}
    read_only_side_action_types = {
        ACTION_ASSISTANT_FEEDBACK,
        ACTION_INTERNAL_QA,
        ACTION_LEGAL_RESEARCH,
        ACTION_SMALL_TALK,
    }

    if ACTION_DISAMBIGUATION_REQUIRED in action_types:
        action_flags = [
            str(flag)
            for action in actions
            for flag in (getattr(action, "safety_flags", None) or [])
            if flag
        ]
        flags = ["action_disambiguation_required", *action_flags]
        if "active_monthly_context" in action_flags:
            flags.append("active_monthly_task_guard")
        return _with_action_entities(
            _blocked_plan(
                primary_workflow=WORKFLOW_UNKNOWN_OR_HELP,
                matched_workflows=[],
                signals=signals,
                flags=list(dict.fromkeys(flags)),
                reason="action-first intake needs clarification before choosing a workflow",
            ),
            action_plan,
        )

    if (
        any(action_type in {ACTION_ASSISTANT_FEEDBACK, ACTION_SMALL_TALK} for action_type in action_types)
        and not write_actions
    ):
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=WORKFLOW_CHAT,
                matched_workflows=[WORKFLOW_CHAT],
                effects=[],
                safety_decision=SafetyDecision(
                    commit_policy="read_only",
                    flags=["chat_no_write", *action_types],
                    reason="chat turns are handled by chat capability and never enter daily execution",
                ),
                confidence=max(0.82, signals.get(WORKFLOW_CHAT, 0.0), signals.get(WORKFLOW_UNKNOWN_OR_HELP, 0.0)),
                reason="action-first intake identified a chat turn",
                signals=signals,
            ),
            action_plan,
        )

    read_actions = [
        action
        for action in actions
        if str(getattr(action, "action_type", "") or "") in {ACTION_DAILY_READ_CURRENT, ACTION_DAILY_READ_HISTORY}
    ]
    daily_start_actions = [
        action
        for action in actions
        if str(getattr(action, "action_type", "") or "") == ACTION_DAILY_WRITE
        and str(getattr(action, "operation", "") or "") == "start_collection"
    ]
    if daily_start_actions and not write_actions:
        start_action = daily_start_actions[0]
        effects = [
            WorkflowEffect(
                effect_type=EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
                target_system=WORKFLOW_DAILY_REPORT,
                target={
                    "task_id": daily_task.task_id if daily_task else "",
                    "field": "none",
                    "report_date": _active_task_report_date(daily_task),
                    "operation": "start_collection",
                    "action_type": ACTION_DAILY_WRITE,
                    "write_policy": str(getattr(start_action, "write_policy", "") or ""),
                },
                payload={"content": envelope.raw_text},
                links={"source_message_id": envelope.message_id},
                risk_level="low",
                reason="action-first intake identified a daily start request without report content",
            )
        ]
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=WORKFLOW_DAILY_REPORT,
                matched_workflows=[WORKFLOW_DAILY_REPORT],
                effects=effects,
                safety_decision=_safety_for_effects(effects, [WORKFLOW_DAILY_REPORT]),
                confidence=max(0.84, signals.get(WORKFLOW_DAILY_REPORT, 0.0), signals.get("daily_context", 0.0)),
                reason="action-first daily start request without report content",
                task_id=daily_task.task_id if daily_task else "",
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ),
            action_plan,
        )
    confirm_actions = [
        action
        for action in actions
        if str(getattr(action, "action_type", "") or "") == ACTION_DAILY_CONFIRM
    ]
    if confirm_actions and not [action for action in actions if action not in confirm_actions]:
        confirm_action = confirm_actions[0]
        effects = [
            WorkflowEffect(
                effect_type=EFFECT_CONFIRM_DAILY_REPORT,
                target_system=WORKFLOW_DAILY_REPORT,
                target={
                    "task_id": daily_task.task_id if daily_task else "",
                    "field": "all",
                    "report_date": _active_task_report_date(daily_task),
                    "operation": "confirm",
                    "action_type": ACTION_DAILY_CONFIRM,
                    "write_policy": str(getattr(confirm_action, "write_policy", "") or ""),
                },
                payload={"content": envelope.raw_text},
                links={"source_message_id": envelope.message_id},
                risk_level="low",
                reason="action-first intake identified a daily confirmation",
            )
        ]
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=WORKFLOW_DAILY_REPORT,
                matched_workflows=[WORKFLOW_DAILY_REPORT],
                effects=effects,
                safety_decision=_safety_for_effects(effects, [WORKFLOW_DAILY_REPORT]),
                confidence=max(0.86, signals.get(WORKFLOW_DAILY_REPORT, 0.0), signals.get("daily_context", 0.0)),
                reason="action-first daily confirmation",
                task_id=daily_task.task_id if daily_task else "",
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ),
            action_plan,
        )
    if read_actions and not write_actions:
        read_action = read_actions[0]
        is_bare_current_query = (
            str(getattr(read_action, "action_type", "") or "") == ACTION_DAILY_READ_CURRENT
            and daily_task is None
            and not _contains_any(envelope.raw_text, ("\u65e5\u62a5", "\u65e5\u5fd7", "\u8349\u7a3f", "\u5f53\u524d", "\u76ee\u524d"))
        )
        if is_bare_current_query:
            return _with_action_entities(
                _blocked_plan(
                    primary_workflow=WORKFLOW_UNKNOWN_OR_HELP,
                    matched_workflows=[],
                    signals=signals,
                    flags=["action_read_without_context"],
                    reason="bare current-report display request has no active daily context",
                ),
                action_plan,
            )
        effects = [
            WorkflowEffect(
                effect_type=EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
                target_system=WORKFLOW_DAILY_REPORT,
                target={
                    "task_id": daily_task.task_id if daily_task else "",
                    "field": "none",
                    "report_date": _active_task_report_date(daily_task),
                    "operation": str(getattr(read_action, "operation", "") or ""),
                    "action_type": str(getattr(read_action, "action_type", "") or ""),
                    "write_policy": str(getattr(read_action, "write_policy", "") or ""),
                },
                payload={"content": envelope.raw_text},
                links={"source_message_id": envelope.message_id},
                risk_level="low",
                reason="action-first intake identified a read-only daily request",
            )
        ]
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=WORKFLOW_DAILY_REPORT,
                matched_workflows=[WORKFLOW_DAILY_REPORT],
                effects=effects,
                safety_decision=_safety_for_effects(effects, [WORKFLOW_DAILY_REPORT]),
                confidence=max(0.86, signals.get(WORKFLOW_DAILY_REPORT, 0.0), signals.get("daily_context", 0.0)),
                reason="action-first read-only daily request",
                task_id=daily_task.task_id if daily_task else "",
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ),
            action_plan,
        )

    sandbox_candidate_action_types = {ACTION_TRAVEL_COORDINATION, ACTION_CASE_PROGRESS}
    sandbox_candidate_actions = [
        action
        for action in actions
        if str(getattr(action, "action_type", "") or "") in sandbox_candidate_action_types
    ]
    if sandbox_candidate_actions and not write_actions:
        matched_workflows = _dedupe_workflows(
            [
                str(getattr(action, "workflow", "") or "")
                for action in actions
                if str(getattr(action, "action_type", "") or "")
                in sandbox_candidate_action_types.union(read_only_side_action_types)
            ]
        )
        effects = _planned_effects_from_action_plan(
            envelope,
            matched_workflows,
            action_plan,
            daily_task=daily_task,
        )
        primary_workflow = _primary_workflow(matched_workflows)
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=primary_workflow,
                matched_workflows=matched_workflows,
                effects=effects,
                safety_decision=_safety_for_effects(effects, matched_workflows),
                confidence=max(0.84, *(signals.get(workflow, 0.0) for workflow in matched_workflows)),
                reason="action-first intake identified sandbox candidate actions without a daily write",
                task_id=_active_task_id(envelope.active_tasks, primary_workflow),
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ),
            action_plan,
        )

    if any(action_type in sandbox_candidate_action_types for action_type in action_types) and any(
        str(getattr(action, "action_type", "") or "") in {ACTION_DAILY_WRITE}
        for action in actions
    ):
        matched_workflows = _dedupe_workflows(
            [
                *[
                    str(getattr(action, "workflow", "") or "")
                    for action in actions
                    if str(getattr(action, "action_type", "") or "")
                    in sandbox_candidate_action_types.union(read_only_side_action_types)
                ],
                WORKFLOW_DAILY_REPORT,
            ]
        )
        effects = _planned_effects_from_action_plan(
            envelope,
            matched_workflows,
            action_plan,
            daily_task=daily_task,
        )
        primary_workflow = _primary_workflow(matched_workflows)
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=primary_workflow,
                matched_workflows=matched_workflows,
                effects=effects,
                safety_decision=_safety_for_effects(effects, matched_workflows),
                confidence=max(0.84, *(signals.get(workflow, 0.0) for workflow in matched_workflows)),
                reason="action-first intake identified daily write plus sandbox candidate action",
                task_id=_active_task_id(envelope.active_tasks, primary_workflow),
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ),
            action_plan,
        )

    if (
        write_actions
        and all(str(getattr(action, "workflow", "") or "") == WORKFLOW_DAILY_REPORT for action in write_actions)
        and all(action_type in daily_write_action_types.union(read_only_side_action_types) for action_type in action_types)
    ):
        matched_workflows = _dedupe_workflows(
            [
                *[
                    str(getattr(action, "workflow", "") or "")
                    for action in actions
                    if str(getattr(action, "action_type", "") or "") in read_only_side_action_types
                ],
                WORKFLOW_DAILY_REPORT,
            ]
        )
        effects = _planned_effects_from_action_plan(
            envelope,
            matched_workflows,
            action_plan,
            daily_task=daily_task,
        )
        primary_workflow = _primary_workflow(matched_workflows)
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=primary_workflow,
                matched_workflows=matched_workflows,
                effects=effects,
                safety_decision=_safety_for_effects(effects, matched_workflows),
                confidence=max(0.84, *(signals.get(workflow, 0.0) for workflow in matched_workflows)),
                reason="action-first intake identified daily write with read-only side intent",
                task_id=daily_task.task_id if daily_task else "",
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ),
            action_plan,
        )

    if write_actions and all(action_type in daily_write_action_types for action_type in action_types):
        matched_workflows = [WORKFLOW_DAILY_REPORT]
        effects = _planned_effects_from_action_plan(
            envelope,
            matched_workflows,
            action_plan,
            daily_task=daily_task,
        )
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=WORKFLOW_DAILY_REPORT,
                matched_workflows=matched_workflows,
                effects=effects,
                safety_decision=_safety_for_effects(effects, matched_workflows),
                confidence=max(0.84, signals.get(WORKFLOW_DAILY_REPORT, 0.0), signals.get("daily_context", 0.0)),
                reason="action-first intake identified only daily report write/edit actions",
                task_id=daily_task.task_id if daily_task else "",
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ),
            action_plan,
        )

    if ACTION_LEGAL_RESEARCH in action_types and not write_actions:
        return _with_action_entities(
            _single_non_daily_action_plan(
                envelope=envelope,
                workflow=WORKFLOW_LEGAL_RESEARCH,
                signals=signals,
                reason="action-first intake identified legal research",
            ),
            action_plan,
        )

    if ACTION_INTERNAL_QA in action_types and not write_actions:
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=WORKFLOW_INTERNAL_QA,
                matched_workflows=[WORKFLOW_INTERNAL_QA],
                effects=[],
                safety_decision=SafetyDecision(
                    commit_policy="blocked",
                    flags=["read_only_internal_qa"],
                    reason="internal Q&A is read-only for daily-report execution",
                ),
                confidence=max(0.82, signals.get(WORKFLOW_INTERNAL_QA, 0.0), signals.get(WORKFLOW_UNKNOWN_OR_HELP, 0.0)),
                reason="action-first intake identified internal Q&A",
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ),
            action_plan,
        )

    if ACTION_WEEKLY_REQUEST in action_types and not write_actions:
        return _with_action_entities(
            _single_non_daily_action_plan(
                envelope=envelope,
                workflow=WORKFLOW_WEEKLY_REPORT,
                signals=signals,
                reason="action-first intake identified a weekly-report request",
            ),
            action_plan,
        )

    if ACTION_MONTHLY_STATUS_QUERY in action_types and not write_actions:
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=WORKFLOW_MONTHLY_REPORT,
                matched_workflows=[WORKFLOW_MONTHLY_REPORT],
                effects=[],
                safety_decision=SafetyDecision(
                    commit_policy="read_only",
                    flags=["read_only_monthly_status_query", *action_types],
                    reason="monthly-report status queries are read-only and never enter daily execution",
                ),
                confidence=max(0.84, signals.get(WORKFLOW_MONTHLY_REPORT, 0.0), signals.get(WORKFLOW_INTERNAL_QA, 0.0)),
                reason="action-first intake identified a monthly-report status query",
                task_id=_active_task_id(envelope.active_tasks, WORKFLOW_MONTHLY_REPORT),
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ),
            action_plan,
        )

    if ACTION_MONTHLY_REPLY in action_types and WORKFLOW_DAILY_REPORT not in [
        str(getattr(action, "workflow", "") or "") for action in write_actions
    ]:
        effects = _planned_effects(envelope, [WORKFLOW_MONTHLY_REPORT])
        return _with_action_entities(
            RoutingPlan(
                primary_workflow=WORKFLOW_MONTHLY_REPORT,
                matched_workflows=[WORKFLOW_MONTHLY_REPORT],
                effects=effects,
                safety_decision=_safety_for_effects(effects, [WORKFLOW_MONTHLY_REPORT]),
                confidence=max(0.86, signals.get(WORKFLOW_MONTHLY_REPORT, 0.0)),
                reason="action-first intake identified a monthly-report reply",
                task_id=_active_task_id(envelope.active_tasks, WORKFLOW_MONTHLY_REPORT),
                signals=signals,
                entities=_entity_hints(envelope.raw_text),
            ),
            action_plan,
        )

    return None


def _single_non_daily_action_plan(
    *,
    envelope: IncomingMessageEnvelope,
    workflow: str,
    signals: dict[str, float],
    reason: str,
) -> RoutingPlan:
    effects = _planned_effects(envelope, [workflow])
    return RoutingPlan(
        primary_workflow=workflow,
        matched_workflows=[workflow],
        effects=effects,
        safety_decision=_safety_for_effects(effects, [workflow]) if effects else SafetyDecision(
            commit_policy="blocked",
            flags=["read_only_non_daily"],
            reason="non-daily action has no daily write effect",
        ),
        confidence=max(0.82, signals.get(workflow, 0.0)),
        reason=reason,
        task_id=_active_task_id(envelope.active_tasks, workflow),
        signals=signals,
        entities=_entity_hints(envelope.raw_text),
    )


def _with_action_entities(plan: RoutingPlan, action_plan: Any) -> RoutingPlan:
    entities = dict(plan.entities)
    if hasattr(action_plan, "as_observation"):
        entities["user_action_plan"] = action_plan.as_observation()
    return replace(plan, entities=entities)


def _planned_effects_from_action_plan(
    envelope: IncomingMessageEnvelope,
    matched_workflows: list[str],
    action_plan: Any,
    *,
    daily_task: ActiveWorkflowTask | None,
) -> list[WorkflowEffect]:
    base_effects = _planned_effects(envelope, matched_workflows)
    daily_effects = _daily_effects_from_action_plan(envelope, action_plan, daily_task=daily_task)
    if not daily_effects:
        return base_effects
    return [
        *[effect for effect in base_effects if effect.target_system != WORKFLOW_DAILY_REPORT],
        *daily_effects,
    ]


def _daily_effects_from_action_plan(
    envelope: IncomingMessageEnvelope,
    action_plan: Any,
    *,
    daily_task: ActiveWorkflowTask | None,
) -> list[WorkflowEffect]:
    effects: list[WorkflowEffect] = []
    for action in list(getattr(action_plan, "actions", []) or []):
        if str(getattr(action, "workflow", "") or "") != WORKFLOW_DAILY_REPORT:
            continue
        action_type = str(getattr(action, "action_type", "") or "")
        if action_type not in {"daily_write", "daily_edit"}:
            continue
        content = str(getattr(action, "payload", {}).get("content") or "").strip()
        if not content:
            continue
        target_field = str(getattr(action, "target_field", "") or _daily_field_hint(content) or "today_work")
        operation = str(getattr(action, "operation", "") or "")
        write_policy = str(getattr(action, "write_policy", "") or "")
        action_target = getattr(action, "target", {}) or {}
        target_daily_task = _daily_task_for_write_target(daily_task, envelope=envelope, action=action, content=content)
        effect_type = (
            EFFECT_LEGACY_DAILY_CONTEXT_ACTION
            if target_daily_task or action_type == "daily_edit"
            else EFFECT_ADD_DAILY_REPORT_ITEM
        )
        effects.append(
            WorkflowEffect(
                effect_type=effect_type,
                target_system=WORKFLOW_DAILY_REPORT,
                target={
                    "task_id": target_daily_task.task_id if target_daily_task else "",
                    "field": target_field,
                    "status": target_daily_task.status if target_daily_task else "",
                    "report_date": _active_task_report_date(target_daily_task),
                    "operation": operation,
                    "action_type": action_type,
                    "write_policy": write_policy,
                    "target_date": action_target.get("target_date") if isinstance(action_target, dict) else "",
                    "source_segment_index": getattr(action, "source_segment_index", 0),
                    "source_text_hash": getattr(action, "source_text_hash", ""),
                },
                payload={
                    "content": content,
                    "safety_flags": list(getattr(action, "safety_flags", []) or []),
                    "confidence": getattr(action, "confidence", 0.0),
                    "reason": getattr(action, "reason", ""),
                },
                links={"source_message_id": envelope.message_id},
                risk_level="low",
                reason="action-first daily segment",
            )
        )
    return effects


def _daily_task_for_write_target(
    daily_task: ActiveWorkflowTask | None,
    *,
    envelope: IncomingMessageEnvelope,
    action: Any,
    content: str,
) -> ActiveWorkflowTask | None:
    """Return the active daily task only when it is allowed to own this write.

    Historical daily context is still useful as memory, but after the 09:00
    cutoff it must not hijack a fresh write/edit into yesterday's report.
    Explicit historical edits are blocked later by the command layer.
    """

    if daily_task is None:
        return None
    report_date = _active_task_report_date(daily_task)
    if not _historical_active_daily_after_cutoff(report_date, envelope.received_at):
        return daily_task
    operation = str(getattr(action, "operation", "") or "")
    text = str(content or envelope.raw_text or "")
    if operation in {"query_current", "query_history", "begin_edit"}:
        return daily_task
    if _text_explicitly_targets_historical_daily(text):
        return daily_task
    return None


def _historical_active_daily_after_cutoff(report_date: str, received_at: Any) -> bool:
    current_date = _received_date(received_at)
    if current_date is None:
        return False
    if not _after_or_at_morning_cutoff(received_at):
        return False
    try:
        return date.fromisoformat(str(report_date or "")) < current_date
    except ValueError:
        return False


def _received_date(received_at: Any) -> date | None:
    value = getattr(received_at, "date", None)
    if callable(value):
        try:
            result = value()
            return result if isinstance(result, date) else None
        except Exception:
            return None
    return None


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


def _text_explicitly_targets_historical_daily(raw_text: str) -> bool:
    compact = _compact(raw_text)
    return _contains_any(
        compact,
        (
            "\u6628\u5929",
            "\u6628\u65e5",
            "\u524d\u5929",
            "\u5927\u524d\u5929",
            "\u4e0a\u4e00\u4efd\u65e5\u62a5",
            "\u4e0a\u6b21\u65e5\u62a5",
        ),
    )


def _dedupe_workflows(workflows: list[str]) -> list[str]:
    result: list[str] = []
    for workflow in workflows:
        if workflow and workflow not in result:
            result.append(workflow)
    return result


def _orphan_confirmation(envelope: IncomingMessageEnvelope) -> bool:
    if not _looks_like_confirmation(envelope.raw_text):
        return False
    if any(task.awaiting_confirmation for task in envelope.active_tasks):
        return False
    return not any(
        task.workflow == WORKFLOW_DAILY_REPORT and task.reply_candidate
        for task in envelope.active_tasks
    )


def _matched_workflows(
    *,
    daily_signal: float,
    monthly_signal: float,
    weekly_signal: float,
    case_signal: float,
    travel_signal: float,
    legal_research_signal: float,
    qa_signal: float,
    daily_context_signal: float,
    monthly_task: ActiveWorkflowTask | None,
    daily_task: ActiveWorkflowTask | None,
    weekly_task: ActiveWorkflowTask | None,
) -> list[str]:
    workflows: list[str] = []
    if monthly_signal >= 0.55 or (monthly_task and monthly_task.reply_candidate):
        workflows.append(WORKFLOW_MONTHLY_REPORT)
    if travel_signal >= 0.45:
        workflows.append(WORKFLOW_TRAVEL_COORDINATION)
    if case_signal >= 0.45:
        workflows.append(WORKFLOW_CASE_PROGRESS)
    if legal_research_signal >= 0.45:
        workflows.append(WORKFLOW_LEGAL_RESEARCH)
    if weekly_signal >= 0.45 or (weekly_task and weekly_signal >= 0.3):
        workflows.append(WORKFLOW_WEEKLY_REPORT)
    if (
        daily_signal >= 0.45
        or (daily_task and daily_signal >= 0.3)
        or (
            daily_task
            and daily_context_signal >= 0.35
            and _active_daily_context_can_claim(
                monthly_signal=monthly_signal,
                weekly_signal=weekly_signal,
                case_signal=case_signal,
                travel_signal=travel_signal,
                legal_research_signal=legal_research_signal,
                qa_signal=qa_signal,
            )
        )
        or (travel_signal >= 0.45 and daily_signal >= 0.3)
        or (case_signal >= 0.45 and daily_signal >= 0.2)
    ):
        workflows.append(WORKFLOW_DAILY_REPORT)
    if qa_signal >= 0.55 and not workflows:
        workflows.append(WORKFLOW_INTERNAL_QA)
    return workflows


def _primary_workflow(matched_workflows: list[str]) -> str:
    for workflow in (
        WORKFLOW_MONTHLY_REPORT,
        WORKFLOW_TRAVEL_COORDINATION,
        WORKFLOW_CASE_PROGRESS,
        WORKFLOW_WEEKLY_REPORT,
        WORKFLOW_DAILY_REPORT,
        WORKFLOW_LEGAL_RESEARCH,
        WORKFLOW_INTERNAL_QA,
        WORKFLOW_CHAT,
    ):
        if workflow in matched_workflows:
            return workflow
    return matched_workflows[0] if matched_workflows else WORKFLOW_UNKNOWN_OR_HELP


def _active_daily_context_can_claim(
    *,
    monthly_signal: float,
    weekly_signal: float,
    case_signal: float,
    travel_signal: float,
    legal_research_signal: float,
    qa_signal: float,
    help_signal: float = 0.0,
) -> bool:
    """Avoid letting an active daily draft steal stronger cross-workflow turns."""

    return (
        monthly_signal < 0.55
        and weekly_signal < 0.45
        and case_signal < 0.45
        and travel_signal < 0.45
        and legal_research_signal < 0.45
        and qa_signal < 0.55
        and help_signal < 0.75
    )


def _strong_non_daily_route(
    *,
    monthly_signal: float,
    weekly_signal: float,
    case_signal: float,
    travel_signal: float,
    legal_research_signal: float,
    qa_signal: float,
) -> tuple[str, float] | None:
    candidates = [
        (WORKFLOW_MONTHLY_REPORT, monthly_signal, 0.55),
        (WORKFLOW_TRAVEL_COORDINATION, travel_signal, 0.45),
        (WORKFLOW_CASE_PROGRESS, case_signal, 0.45),
        (WORKFLOW_WEEKLY_REPORT, weekly_signal, 0.45),
        (WORKFLOW_LEGAL_RESEARCH, legal_research_signal, 0.45),
        (WORKFLOW_INTERNAL_QA, qa_signal, 0.55),
    ]
    passing = [(workflow, signal) for workflow, signal, threshold in candidates if signal >= threshold]
    if not passing:
        return None
    return max(passing, key=lambda item: item[1])


def _planned_effects(envelope: IncomingMessageEnvelope, matched_workflows: list[str]) -> list[WorkflowEffect]:
    effects: list[WorkflowEffect] = []
    raw_text = str(envelope.raw_text or "")
    daily_task = _latest_active_task(envelope.active_tasks, WORKFLOW_DAILY_REPORT)
    daily_signal = _daily_report_signal(raw_text, received_at=envelope.received_at)
    daily_context_signal = _daily_context_signal(raw_text, daily_task)
    if WORKFLOW_TRAVEL_COORDINATION in matched_workflows:
        effects.append(
            WorkflowEffect(
                effect_type="upsert_travel_plan",
                target_system=WORKFLOW_TRAVEL_COORDINATION,
                target={"date_hint": _travel_date_hint(raw_text, received_at=envelope.received_at)},
                payload={"content": raw_text},
                risk_level="medium",
                requires_confirmation=True,
                reason="travel wording detected",
            )
        )
    if WORKFLOW_WEEKLY_REPORT in matched_workflows:
        effects.append(
            WorkflowEffect(
                effect_type="draft_weekly_report",
                target_system=WORKFLOW_WEEKLY_REPORT,
                target={"period_hint": "current_week"},
                payload={"content": raw_text},
                risk_level="low",
                reason="weekly-report wording detected",
            )
        )
    if WORKFLOW_CASE_PROGRESS in matched_workflows:
        effects.append(
            WorkflowEffect(
                effect_type="append_case_progress",
                target_system=WORKFLOW_CASE_PROGRESS,
                target={"matter_hint": _case_matter_hint(raw_text)},
                payload={"content": raw_text},
                risk_level="medium",
                requires_confirmation=True,
                reason="case-progress wording detected",
            )
        )
    if WORKFLOW_LEGAL_RESEARCH in matched_workflows:
        effects.append(
            WorkflowEffect(
                effect_type="run_legal_research",
                target_system=WORKFLOW_LEGAL_RESEARCH,
                target={"question_hint": "legal_research"},
                payload={"content": raw_text},
                risk_level="low",
                reason="legal-research wording detected",
            )
        )
    if WORKFLOW_DAILY_REPORT in matched_workflows:
        if daily_task and daily_task.awaiting_confirmation and _looks_like_confirmation(raw_text):
            effects.append(
                WorkflowEffect(
                    effect_type=EFFECT_CONFIRM_DAILY_REPORT,
                    target_system=WORKFLOW_DAILY_REPORT,
                    target={"task_id": daily_task.task_id},
                    risk_level="low",
                    reason=daily_task.reason or "active daily-report task is awaiting confirmation",
                )
            )
            return effects
        effect_type = EFFECT_ADD_DAILY_REPORT_ITEM
        reason = "daily-report wording detected"
        if daily_task and daily_context_signal >= 0.35 and not _looks_like_full_daily_report(raw_text):
            effect_type = EFFECT_LEGACY_DAILY_CONTEXT_ACTION
            reason = daily_task.reason or "active daily-report context accepted follow-up text"
        effects.append(
            WorkflowEffect(
                effect_type=effect_type,
                target_system=WORKFLOW_DAILY_REPORT,
                target={
                    "task_id": daily_task.task_id if daily_task else "",
                    "field": _daily_field_hint(raw_text),
                    "status": daily_task.status if daily_task else "",
                    "report_date": _active_task_report_date(daily_task),
                },
                payload={"content": raw_text},
                links={"source_message_id": envelope.message_id},
                risk_level="low",
                reason=reason,
            )
        )
    if WORKFLOW_MONTHLY_REPORT in matched_workflows:
        effects.append(
            WorkflowEffect(
                effect_type="capture_monthly_report_reply",
                target_system=WORKFLOW_MONTHLY_REPORT,
                target={"task_id": _active_task_id(envelope.active_tasks, WORKFLOW_MONTHLY_REPORT)},
                payload={"content": raw_text},
                risk_level="medium",
                reason="monthly-report wording detected",
            )
        )
    return effects


def _safety_for_effects(effects: list[WorkflowEffect], matched_workflows: list[str]) -> SafetyDecision:
    if not effects:
        return SafetyDecision(commit_policy="blocked", reason="no write effects planned")

    flags: list[str] = []
    safe_effects: list[str] = []
    pending_effects: list[str] = []
    blocked_effects: list[str] = []

    write_workflows = {effect.target_system for effect in effects}
    if len(write_workflows) > 1:
        flags.append("multi_workflow_write")

    for effect in effects:
        if effect.requires_confirmation or effect.risk_level in {"high", "medium"} and len(write_workflows) > 1:
            pending_effects.append(effect.effect_type)
        else:
            safe_effects.append(effect.effect_type)

    if any(effect.requires_confirmation for effect in effects) or flags:
        return SafetyDecision(
            commit_policy="needs_confirmation",
            flags=flags,
            safe_effects=safe_effects,
            pending_effects=pending_effects,
            blocked_effects=blocked_effects,
            reason="planned write crosses workflow boundary or requires confirmation",
        )

    return SafetyDecision(
        commit_policy="partial_allowed",
        flags=flags,
        safe_effects=safe_effects,
        pending_effects=pending_effects,
        blocked_effects=blocked_effects,
        reason="single low-risk workflow effect can be accepted by its workflow",
    )


def _entity_hints(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "")
    hints: dict[str, Any] = {}
    if "出差" in text:
        hints["travel"] = True
    if re.search(r"[A-Za-z0-9一二三四五六七八九十百千万]+案", text):
        hints["matter_mention"] = True
    return hints


def _daily_field_hint(raw_text: str) -> str:
    compact = _compact(raw_text)
    if any(marker in compact for marker in ("问题风险", "问题/风险", "风险", "困难")) and not any(
        marker in compact for marker in ("今日", "今天", "完成")
    ):
        return "problems"
    if any(marker in compact for marker in ("明日计划", "明天计划", "下步计划")) and not any(
        marker in compact for marker in ("今日", "今天", "完成")
    ):
        return "tomorrow_plan"
    return "today_work"


def _travel_date_hint(raw_text: str, *, received_at: datetime | None = None) -> str:
    relative_hint = date_hint_from_text(raw_text, received_at=received_at)
    if relative_hint != "unknown":
        return relative_hint
    compact = _compact(raw_text)
    if "明天" in compact or "明日" in compact:
        return "tomorrow"
    if "今天" in compact or "今日" in compact:
        return "today"
    if "下周" in compact:
        return "next_week"
    return "unknown"


def _case_matter_hint(raw_text: str) -> str:
    text = str(raw_text or "")
    if _looks_like_case_product_build_work(text):
        return ""
    if _contains_any(
        text,
        (
            "\u6848\u4ef6\u8fdb\u5c55",
            "\u6848\u4ef6\u6c47\u62a5",
            "\u6848\u4ef6\u6c9f\u901a",
            "\u6848\u4ef6\u6750\u6599",
            "\u6848\u4ef6\u8d44\u6599",
            "\u6848\u4ef6\u7ba1\u7406",
            "\u6848\u4ef6\u53f0\u8d26",
            "\u6848\u4ef6\u8282\u70b9",
            "\u6848\u4ef6\u4e8b\u9879",
            "\u6cd5\u52a1\u5c0f\u7fa4\u6848\u4ef6",
        ),
    ):
        return ""
    patterns = [
        r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}?\u6848\u4ef6)",
        r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}?\u6848)(?!\u4ef6)",
    ]
    if _contains_any(text, ("\u6cd5\u9662", "\u4e2d\u9662", "\u9ad8\u9662", "\u6267\u884c\u8fdb\u5c55", "\u5f00\u5ead")):
        patterns.append(r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}?\u4e8b\u9879)")
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = _clean_case_matter_hint(match.group(1))
        if _valid_case_matter_hint(candidate):
            return candidate
    return ""


def _clean_case_matter_hint(candidate: str) -> str:
    value = str(candidate or "").strip(" ：:，,。；;、")
    value = re.sub(r"^(?:\u4eca\u5929|\u4eca\u65e5|\u660e\u5929|\u660e\u65e5|\u6628\u5929|\u6628\u65e5|\u4e0b\u5468[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u5929]?)", "", value)
    for marker in ("\u6c9f\u901a", "\u5904\u7406", "\u8ddf\u8fdb", "\u63a8\u8fdb", "\u529e\u7406", "\u534f\u8c03", "\u5bf9\u63a5", "\u7814\u7a76", "\u6574\u7406", "\u8865\u5145", "\u5f00\u5ead"):
        if marker in value:
            tail = value.rsplit(marker, 1)[-1].strip(" ：:，,。；;、")
            if len(tail) >= 3:
                value = tail
    return value


def _valid_case_matter_hint(candidate: str) -> bool:
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
        if prefix in {
            "\u62df\u8bc9",
            "\u5f85\u8bc9",
            "\u8bc9\u8bbc",
            "\u88ab\u544a",
            "\u8bbe\u8ba1",
            "\u90e8\u5206",
            "\u76f8\u5173",
            "\u91cd\u70b9",
            "\u6240\u6709",
            "\u5168\u90e8",
            "\u4e00\u822c",
            "\u591a\u4e2a",
            "\u5404\u7c7b",
        }:
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


def _monthly_report_signal(raw_text: str, task: ActiveWorkflowTask | None) -> float:
    compact = _compact(raw_text)
    if not compact:
        return 0.0
    score = 0.15 if task else 0.0
    if task and task.reply_candidate:
        score += 0.75
    if task and task.awaiting_confirmation and _looks_like_confirmation(raw_text):
        score += 0.65
    monthly_markers = (
        "指标",
        "未完成原因",
        "存在问题",
        "下月目标",
        "行动方案",
        "绩效",
        "月报",
        "完成率",
    )
    score += min(0.5, sum(0.12 for marker in monthly_markers if marker in compact))
    if _contains_any(raw_text, ("\u8fd9\u4e2a\u6708", "\u672c\u6708", "\u6708\u5ea6", "\u6708")) and _contains_any(
        raw_text, ("\u6307\u6807", "\u7ee9\u6548", "\u5b8c\u6210\u60c5\u51b5", "\u5b8c\u6210\u7387", "\u76ee\u6807")
    ):
        score += 0.42
    if _contains_any(raw_text, ("\u6307\u6807\u5b8c\u6210\u60c5\u51b5", "\u6708\u62a5", "\u7ee9\u6548\u76ee\u6807", "\u4e0b\u6708\u76ee\u6807")):
        score += 0.2
    if re.search(r"(?:^|[\n\r])\s*(?:第)?\d+\s*(?:项|[.、．)])", raw_text or ""):
        score += 0.15
    return min(score, 1.0)


def _weekly_report_signal(raw_text: str, task: ActiveWorkflowTask | None = None) -> float:
    compact = _compact(raw_text)
    if not compact:
        return 0.0
    score = 0.12 if task else 0.0
    weekly_markers = (
        "本周完成",
        "本周工作",
        "本周总结",
        "周报",
        "下周计划",
        "下周重点",
        "本周",
        "下周",
    )
    score += min(0.72, sum(0.14 for marker in weekly_markers if marker in compact))
    if _contains_any(raw_text, ("\u5468\u62a5",)) and _contains_any(
        raw_text, ("\u751f\u6210", "\u5199", "\u6574\u7406", "\u6c47\u603b", "\u53d1")
    ):
        score += 0.36
    if _contains_any(raw_text, ("\u672c\u5468", "\u4e0b\u5468")) and _contains_any(
        raw_text, ("\u5b8c\u6210", "\u5de5\u4f5c", "\u8ba1\u5212", "\u91cd\u70b9")
    ):
        score += 0.18
    if any(marker in compact for marker in ("今日工作", "明日计划", "日报")):
        score -= 0.2
    if any(marker in compact for marker in ("未完成原因", "下月目标", "行动方案")):
        score -= 0.25
    return min(max(score, 0.0), 1.0)


def _travel_signal(raw_text: str) -> float:
    compact = _compact(raw_text)
    if not compact:
        return 0.0
    if _looks_like_travel_coordination_product_work(raw_text):
        return 0.0
    score = 0.0
    travel_markers = ("出差", "差旅", "去上海", "去北京", "去广州", "去深圳", "行程", "目的地")
    score += min(0.8, sum(0.22 for marker in travel_markers if marker in compact))
    if _contains_any(raw_text, ("\u51fa\u5dee", "\u5dee\u65c5", "\u884c\u7a0b")):
        score += 0.18
    if _contains_any(raw_text, ("\u51fa\u5dee", "\u5dee\u65c5")) and _contains_any(
        raw_text, ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5", "\u4e0b\u5468", "\u53bb")
    ):
        score += 0.18
    if any(marker in compact for marker in ("明天", "明日", "今天", "今日", "下周", "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周天")):
        score += 0.12
    return min(score, 1.0)


def _looks_like_travel_coordination_product_work(raw_text: str) -> bool:
    text = str(raw_text or "")
    if not _contains_any(
        text,
        (
            "\u51fa\u5dee\u534f\u540c",
            "\u51fa\u5dee\u7cfb\u7edf",
            "\u51fa\u5dee\u6a21\u5757",
            "\u51fa\u5dee\u529f\u80fd",
            "\u51fa\u5dee\u5de5\u5177",
            "\u5dee\u65c5\u7cfb\u7edf",
            "\u5dee\u65c5\u6a21\u5757",
        ),
    ):
        return False
    if _looks_like_concrete_trip(raw_text):
        return False
    return _contains_any(
        text,
        (
            "\u505a",
            "\u5f00\u53d1",
            "\u4f18\u5316",
            "\u5efa\u8bbe",
            "\u642d\u5efa",
            "\u4e0a\u7ebf",
            "\u6d4b\u8bd5",
            "\u6a21\u5757",
            "\u7cfb\u7edf",
            "\u529f\u80fd",
            "\u5de5\u5177",
        ),
    )


def _looks_like_concrete_trip(raw_text: str) -> bool:
    text = str(raw_text or "")
    if not _has_trip_destination(text):
        return False
    if re.search(r"(\u4eca\u5929|\u4eca\u65e5|\u660e\u5929|\u660e\u65e5|\u4e0b\u5468)?.{0,4}(\u53bb|\u8d74|\u5230).{1,16}(\u51fa\u5dee|\u5dee\u65c5|\u5f00\u5ead|\u76d6\u7ae0|\u8d70\u8bbf|\u5904\u7406|\u6c9f\u901a)", text):
        return True
    if re.search(r"(\u51fa\u5dee|\u5dee\u65c5).{0,10}(\u5357\u4eac|\u4e0a\u6d77|\u5317\u4eac|\u5e7f\u5dde|\u6df1\u5733|\u82cf\u5dde|\u626c\u5dde|\u5e38\u5dde|\u5609\u5174)", text):
        return True
    return False


def _has_trip_destination(raw_text: str) -> bool:
    return _contains_any(
        raw_text,
        (
            "\u5609\u5174\u5357\u6e56\u8857\u9053",
            "\u5357\u6e56\u8857\u9053",
            "\u5357\u4eac",
            "\u626c\u5dde",
            "\u5609\u5174",
            "\u5e38\u5dde",
            "\u82cf\u5dde",
            "\u4e0a\u6d77",
            "\u5317\u4eac",
            "\u5e7f\u5dde",
            "\u6df1\u5733",
        ),
    )


def _case_progress_signal(raw_text: str) -> float:
    compact = _compact(raw_text)
    if not compact:
        return 0.0
    if any(marker in compact for marker in ("案件汇报", "法务小群案件", "案件材料", "案件资料", "案件管理")):
        return 0.0
    matter_hint = _case_matter_hint(raw_text)
    if not matter_hint:
        return 0.0
    score = 0.0
    score += 0.34
    case_markers = (
        "案件",
        "开庭",
        "证据",
        "起诉",
        "诉讼",
        "调解",
        "执行",
        "判决",
        "保全",
        "承办",
        "案号",
        "项目争议",
        "合同争议",
    )
    score += min(0.62, sum(0.12 for marker in case_markers if marker in compact))
    if any(marker in compact for marker in ("法院", "中院", "高院", "案号", "执行进展", "开庭", "出庭")):
        score += 0.18
    if any(marker in compact for marker in ("处理", "跟进", "完成", "确认", "整理", "提交", "沟通")):
        score += 0.12
    if any(marker in compact for marker in ("案例", "裁判规则", "法律研究", "查一下")):
        score -= 0.2
    return min(max(score, 0.0), 1.0)


def _legal_research_signal(raw_text: str) -> float:
    compact = _compact(raw_text)
    if not compact:
        return 0.0
    score = 0.0
    research_markers = (
        "法律研究",
        "裁判规则",
        "最新案例",
        "类案",
        "法规",
        "法条",
        "司法解释",
        "判例",
        "检索",
        "研究一下",
    )
    score += min(0.7, sum(0.14 for marker in research_markers if marker in compact))
    if any(marker in compact for marker in ("帮我查", "查一下", "检索", "研究")):
        score += 0.2
    if any(marker in compact for marker in ("裁判规则", "最新案例", "司法解释", "法规", "法条")):
        score += 0.2
    if any(marker in compact for marker in ("今日工作", "明日计划", "本周完成", "未完成原因")):
        score -= 0.25
    return min(max(score, 0.0), 1.0)


def _internal_qa_signal(raw_text: str) -> float:
    compact = _compact(raw_text)
    if not compact:
        return 0.0
    score = _help_signal(raw_text)
    qa_markers = (
        "帮我查",
        "查一下",
        "怎么",
        "为什么",
        "能不能",
        "可不可以",
        "规则",
        "流程",
        "制度",
        "是什么",
        "裁判",
        "法律",
        "法规",
        "案例",
    )
    score += min(0.6, sum(0.1 for marker in qa_markers if marker in compact))
    if any(marker in compact for marker in ("帮我查", "查一下")) and any(
        marker in compact for marker in ("规则", "制度", "裁判", "法律", "法规", "案例")
    ):
        score += 0.25
    if any(marker in compact for marker in ("流程", "制度")) and any(marker in compact for marker in ("是什么", "怎么", "如何")):
        score += 0.35
    if any(marker in compact for marker in ("今日工作", "明日计划", "本周完成", "未完成原因", "下月目标")):
        score -= 0.25
    return min(max(score, 0.0), 1.0)


def _daily_context_signal(raw_text: str, task: ActiveWorkflowTask | None) -> float:
    if task is None:
        return 0.0
    compact = _compact(raw_text)
    if not compact:
        return 0.0
    if _active_daily_context_negative_signal(raw_text):
        return 0.0

    is_question = _looks_like_context_question(raw_text)
    small_talk_signal = _small_talk_signal(raw_text)
    score = 0.25 if task.reply_candidate else 0.0
    if task.awaiting_confirmation and _looks_like_confirmation(raw_text):
        score += 0.68

    context_markers = (
        "确认",
        "确认提交",
        "提交",
        "可以",
        "好",
        "好的",
        "嗯",
        "恩",
        "取消",
        "没问题",
        "没啥问题",
        "就这样",
        "暂无",
        "暂无问题",
        "无",
        "没有",
        "没有问题",
        "发我",
        "发我看",
        "看下",
        "给我看",
        "草稿",
        "当前日报",
        "日报草稿",
        "现在长啥样",
        "长啥样",
        "合并",
        "和并",
        "并",
        "修改",
        "改成",
        "调整",
        "删掉",
        "删除",
        "撤回",
        "补充",
        "继续",
        "清空",
        "全部删除",
        "第",
        "条",
        "项",
        "一到",
        "二到",
        "三到",
        "四到",
        "五到",
        "六到",
        "七到",
        "八到",
        "今日工作",
        "工作",
        "风险",
        "问题",
        "问题风险",
        "明日计划",
        "计划",
    )
    score += min(0.72, sum(0.12 for marker in context_markers if marker in compact))
    if re.search(r"(?:第?[一二三四五六七八九十\d]+(?:到|至|-)?[一二三四五六七八九十\d]*)(?:条|项)", str(raw_text or "")):
        score += 0.24
    if re.search(r"(?:\d+|[一二三四五六七八九十]+)\s*(?:-|到|至)\s*(?:\d+|[一二三四五六七八九十]+)", str(raw_text or "")):
        score += 0.24
    work_update_markers = (
        "用印",
        "合同",
        "归档",
        "沟通",
        "跟进",
        "开会",
        "案件",
        "材料",
        "会议",
        "整理",
        "审核",
        "处理",
        "推进",
        "发函",
        "邮寄",
        "盖章",
        "部署",
        "测试",
        "梳理",
        "更新",
        "统计",
        "安排",
        "调休",
        "技能",
        "评估",
        "法务",
        "支撑",
        "表单",
        "送达",
        "地址",
        "项目",
        "常规工作",
    )
    if any(marker in compact for marker in work_update_markers):
        score += 0.24
    if len(compact) >= 12:
        score += 0.16
    if compact in {
        "确认",
        "确认提交",
        "提交",
        "可以提交",
        "没问题",
        "没问题提交",
        "没啥问题",
        "没啥问题了",
        "暂无",
        "暂无问题",
        "无",
        "没有",
        "没有问题",
        "没啥",
        "取消",
        "好",
        "好的",
        "嗯",
        "恩",
        "是",
        "是的",
        "对",
        "对的",
        "确定",
        "\u597d\u4e86\u63d0\u4ea4",
        "\u597d\u63d0\u4ea4",
        "\u63d0\u4ea4\u5427",
        "ok",
        "OK",
        "交吧",
        "交",
        "交了",
        "已经说了啊",
        "不用了",
        "不必了",
    }:
        score += 0.3
    if task.reply_candidate and len(compact) >= 2 and not is_question and small_talk_signal < 0.45:
        score += 0.18
    if is_question:
        score -= 0.35
    if small_talk_signal >= 0.45:
        score -= 0.45
    if _daily_report_signal(raw_text) >= 0.45:
        score += 0.18
    if len(compact) >= 6 and not is_question:
        score += 0.14
    if len(compact) >= 10 and not is_question:
        score += 0.08
    return min(max(score, 0.0), 1.0)


def _active_daily_context_negative_signal(raw_text: str) -> bool:
    if _looks_like_bot_meta_question(raw_text):
        return True
    if _looks_like_lifestyle_question(raw_text):
        return True
    if _looks_like_short_non_work_reply(raw_text):
        return True
    return False


def _looks_like_bot_meta_question(raw_text: str) -> bool:
    return _contains_any(
        raw_text,
        (
            "\u4f60\u591a\u5927",
            "\u4f60\u51e0\u5c81",
            "\u4f60\u662f\u8c01",
            "\u4f60\u53eb\u4ec0\u4e48",
            "\u4f60\u80fd\u5e72\u561b",
            "\u4f60\u80fd\u505a\u4ec0\u4e48",
            "\u4f60\u90fd\u8bb0\u5f55",
            "\u4f60\u8bb0\u5f55",
            "\u8bb0\u5f55\u7684\u662f\u5565",
            "\u8bb0\u5f55\u7684\u662f\u4ec0\u4e48",
            "\u4f60\u5199\u7684\u662f\u5565",
            "\u4f60\u5199\u7684\u662f\u4ec0\u4e48",
            "agent2",
            "Agent2",
            "\u7070\u6d4b",
            "\u73b0\u5728\u662f\u5565",
            "\u73b0\u5728\u662f\u4ec0\u4e48",
            "\u73b0\u5728\u662f",
        ),
    )


def _looks_like_lifestyle_question(raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    if not text.strip():
        return False
    compact = _compact(text)
    work_markers = (
        "合同",
        "案件",
        "材料",
        "开庭",
        "法院",
        "用印",
        "印章",
        "函件",
        "台账",
        "项目",
        "系统",
        "流程",
        "月报",
        "周报",
        "日报",
        "客户",
        "供应商",
        "业务部门",
        "法务",
        "收款",
        "回款",
        "诉讼",
        "执行",
    )
    if any(marker in compact for marker in work_markers):
        return False
    life_markers = (
        "天气",
        "下雨",
        "下雪",
        "冷不冷",
        "热不热",
        "多少度",
        "穿啥",
        "穿什么",
        "穿哪件",
        "衣服",
        "外套",
        "短袖",
        "长袖",
        "出门",
        "打伞",
        "带伞",
        "会不会下雨",
        "吃啥",
        "吃什么",
        "喝啥",
        "喝什么",
        "吃饭",
        "早饭",
        "午饭",
        "晚饭",
        "火锅",
        "咖啡",
        "奶茶",
        "睡觉",
        "几点睡",
        "睡不着",
        "好困",
        "做梦",
        "健身",
        "跑步",
        "电影",
        "游戏",
        "打游戏",
        "洗车",
        "去哪玩",
        "周末去哪",
        "今天去哪",
        "明天去哪",
    )
    if not any(marker in compact for marker in life_markers):
        return False
    question_markers = ("?", "？", "啥", "什么", "咋", "怎么", "要不要", "能不能", "可不可以", "吗", "呢")
    return any(marker in text for marker in question_markers) or any(marker in compact for marker in question_markers)


def _looks_like_short_non_work_reply(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if len(compact) > 4:
        return False
    if _looks_like_confirmation(raw_text):
        return False
    if _daily_report_signal(raw_text) >= 0.35:
        return False
    allowed_markers = (
        "\u4f11\u5047",
        "\u8c03\u4f11",
        "\u5f00\u4f1a",
        "\u4f1a\u8bae",
        "\u76d6\u7ae0",
        "\u7528\u5370",
        "\u5408\u540c",
        "\u6848\u4ef6",
        "\u6750\u6599",
        "\u5f52\u6863",
        "\u5ba1\u6838",
        "\u51fd",
        "\u6ca1\u95ee\u9898",
        "\u6ca1\u5565",
        "\u6682\u65e0",
        "\u6ca1\u6709",
        "\u65e0",
        "\u53d1\u6211",
        "\u770b\u4e0b",
        "\u7ed9\u6211\u770b",
        "\u8349\u7a3f",
        "\u786e\u5b9a",
        "\u662f",
        "\u662f\u7684",
        "\u5bf9\u7684",
        "\u53ef\u4ee5",
        "\u55ef",
        "\u6069",
        "\u4ea4",
        "\u4ea4\u4e86",
        "\u4ea4\u5427",
        "\u597d",
        "ok",
        "\u5bf9",
        "\u5220",
        "\u6539",
        "\u5408\u5e76",
        "\u590d\u5236",
        "\u6e05\u7a7a",
        "\u64a4\u56de",
        "\u63d0\u4ea4",
    )
    return not _contains_any(raw_text, allowed_markers)


def _daily_report_signal(raw_text: str, *, received_at: datetime | None = None) -> float:
    compact = _compact(raw_text)
    if not compact:
        return 0.0
    score = 0.0
    daily_markers = (
        "今日工作",
        "今天工作",
        "今日完成",
        "今天完成",
        "问题风险",
        "问题/风险",
        "明日计划",
        "明天计划",
        "日报",
        "日志",
        "确认提交",
        "补交",
        "撤回",
        "修改",
        "修订",
        "更新",
        "清空",
        "昨天日报",
    )
    score += min(0.72, sum(0.12 for marker in daily_markers if marker in compact))
    if _looks_like_full_daily_report(raw_text):
        score += 0.35
    if _looks_like_completed_previous_plan(raw_text):
        score += 0.55
    if _looks_like_yesterday_makeup_daily_content(raw_text):
        score += 0.7
    if _looks_like_daily_copy_previous(raw_text):
        score += 0.6
    if _looks_like_daily_revoke(raw_text):
        score += 0.6
    if _looks_like_single_daily_section(raw_text):
        score += 0.55
    if _looks_like_daily_travel_plan(raw_text, received_at=received_at):
        score += 0.42
    if _looks_like_daily_work_update(raw_text):
        score += 0.35
    if _looks_like_standalone_daily_fragment(raw_text):
        score += 0.48
    if any(marker in compact for marker in ("今天", "今日", "明天", "明日")) and any(
        verb in compact
        for verb in (
            "完成",
            "处理",
            "跟进",
            "审核",
            "整理",
            "计划",
            "修订",
            "更新",
            "梳理",
            "沟通",
            "对接",
            "发送",
            "打印",
            "归档",
            "装订",
            "完善",
            "优化",
            "开发",
            "测试",
            "推进",
            "汇报",
            "讨论",
            "确认",
            "出具",
        )
    ):
        score += 0.22
    if any(marker in compact for marker in ("今天", "今日")) and any(verb in compact for verb in ("完成", "处理", "审核", "整理")):
        score += 0.25
    if any(marker in compact for marker in ("日报", "日志")) and any(
        verb in compact for verb in ("清空", "删除", "删掉", "撤回", "覆盖", "重写", "重新写")
    ):
        score += 0.45
    if any(marker in compact for marker in ("未完成原因", "下月目标", "行动方案")):
        score -= 0.28
    return min(max(score, 0.0), 1.0)


def _looks_like_daily_work_update(raw_text: str) -> bool:
    if _looks_like_context_question(raw_text):
        return False
    return (
        _contains_any(raw_text, ("\u4eca\u5929", "\u4eca\u65e5"))
        and _contains_any(
            raw_text,
            (
                "\u505a",
                "\u5b8c\u6210",
                "\u5904\u7406",
                "\u4f18\u5316",
                "\u5f00\u53d1",
                "\u5efa\u8bbe",
                "\u6d4b\u8bd5",
                "\u6574\u7406",
                "\u68b3\u7406",
                "\u6c9f\u901a",
                "\u5ba1\u6838",
            ),
        )
        and _contains_any(
            raw_text,
            (
                "\u7cfb\u7edf",
                "\u6a21\u5757",
                "\u529f\u80fd",
                "\u6d41\u7a0b",
                "\u5408\u540c",
                "\u6848\u4ef6",
                "\u9879\u76ee",
                "\u6570\u636e",
                "\u6750\u6599",
                "\u53f0\u8d26",
                "\u65e5\u62a5",
            ),
        )
    )


def _looks_like_yesterday_makeup_daily_content(raw_text: str) -> bool:
    text = str(raw_text or "")
    if not text.strip():
        return False
    if not any(marker in text for marker in ("\u6628\u5929", "\u6628\u65e5")):
        return False
    if not any(marker in text for marker in ("\u65e5\u62a5", "\u65e5\u5fd7")):
        return False
    if not any(marker in text for marker in ("\u8865\u4e00\u4e0b", "\u8865\u4e0b", "\u8865\u5199", "\u8865\u4e0a", "\u6ca1\u5199", "\u5fd8\u5199", "\u5fd8\u4e86\u5199")):
        return False
    return any(
        marker in text
        for marker in (
            "\u4e3b\u8981\u5de5\u4f5c",
            "\u5de5\u4f5c\u662f",
            "\u6574\u7406",
            "\u5904\u7406",
            "\u5ba1\u6838",
            "\u6c9f\u901a",
            "\u5f00\u4e86\u4e2a\u4f1a",
            "\u53f0\u8d26",
            "\u5408\u540c",
            "\u6848\u4ef6",
            "\u95ee\u9898\u662f",
            "\u98ce\u9669\u662f",
        )
    )


def _looks_like_standalone_daily_fragment(raw_text: str) -> bool:
    compact = _compact(raw_text)
    return compact in {
        "休假",
        "调休",
        "开会",
        "会议",
        "盖章",
        "用印",
        "合同审核",
        "审核合同",
        "案件沟通",
        "材料整理",
        "整理材料",
        "归档",
    }


def _looks_like_completed_previous_plan(raw_text: str) -> bool:
    return (
        _contains_any(raw_text, ("\u6628\u5929", "\u6628\u65e5", "\u524d\u4e00\u5929"))
        and _contains_any(
            raw_text,
            (
                "\u660e\u65e5\u8ba1\u5212",
                "\u660e\u5929\u8ba1\u5212",
                "\u8ba1\u5212",
                "\u5f85\u529e",
                "\u5b89\u6392",
                "\u4e8b\u9879",
            ),
        )
        and _contains_any(raw_text, ("\u5df2\u5b8c\u6210", "\u5b8c\u6210\u4e86", "\u5b8c\u6210", "\u505a\u5b8c", "\u641e\u5b9a"))
    )


def _looks_like_single_daily_section(raw_text: str) -> bool:
    return _contains_any(
        raw_text,
        (
            "\u95ee\u9898\u98ce\u9669",
            "\u95ee\u9898/\u98ce\u9669",
            "\u95ee\u9898\uff1a",
            "\u98ce\u9669\uff1a",
            "\u6682\u65e0\u660e\u663e\u95ee\u9898",
            "\u660e\u65e5\u8ba1\u5212",
            "\u660e\u5929\u8ba1\u5212",
        ),
    )


def _looks_like_daily_travel_plan(raw_text: str, *, received_at: datetime | None = None) -> bool:
    if _looks_like_concrete_trip(raw_text) and date_hint_from_text(raw_text, received_at=received_at) == "tomorrow":
        return True
    return _looks_like_concrete_trip(raw_text) and _contains_any(
        raw_text,
        ("\u4eca\u5929", "\u4eca\u65e5", "\u660e\u5929", "\u660e\u65e5", "\u8ba1\u5212", "\u62df"),
    )


def _looks_like_daily_copy_previous(raw_text: str) -> bool:
    compact = _compact(raw_text)
    repeat_markers = ("还是", "也是", "照旧", "一样", "同样", "那些事", "那些事情", "那几件事", "老样子")
    work_markers = ("今天", "今日", "工作", "做", "事", "内容")
    previous_markers = ("\u6628\u5929", "\u6628\u65e5", "\u524d\u4e00\u5929", "\u524d\u5929", "\u524d\u65e5", "\u5927\u524d\u5929")
    directional_repeat_markers = (
        "\u540c\u524d\u5929",
        "\u540c\u524d\u65e5",
        "\u540c\u6628\u5929",
        "\u540c\u6628\u65e5",
    )
    if (
        _contains_any(compact, previous_markers)
        and (_contains_any(compact, repeat_markers) or _contains_any(compact, directional_repeat_markers))
        and _contains_any(compact, work_markers)
    ):
        return True
    return _contains_any(
        raw_text,
        (
            "\u590d\u5236\u6628\u5929",
            "\u590d\u5236\u6628\u65e5",
            "\u590d\u5236\u524d\u5929",
            "\u590d\u5236\u524d\u65e5",
            "\u590d\u5236\u5927\u524d\u5929",
            "\u628a\u6628\u5929\u7684\u5e26\u8fc7\u6765",
            "\u628a\u6628\u65e5\u7684\u5e26\u8fc7\u6765",
            "\u628a\u524d\u5929\u7684\u5e26\u8fc7\u6765",
            "\u628a\u524d\u65e5\u7684\u5e26\u8fc7\u6765",
            "\u548c\u6628\u5929\u4e00\u6837",
            "\u548c\u6628\u65e5\u4e00\u6837",
            "\u548c\u524d\u5929\u4e00\u6837",
            "\u548c\u524d\u65e5\u4e00\u6837",
        ),
    )


def _looks_like_daily_revoke(raw_text: str) -> bool:
    return _contains_any(raw_text, ("\u64a4\u56de", "\u64a4\u9500", "\u53d6\u6d88\u63d0\u4ea4", "\u9000\u56de")) and _contains_any(
        raw_text, ("\u65e5\u62a5", "\u6628\u5929", "\u6628\u65e5", "\u4eca\u5929", "\u4eca\u65e5")
    )


def _help_signal(raw_text: str) -> float:
    compact = _compact(raw_text)
    if not compact:
        return 0.0
    if compact.endswith("?") or compact.endswith("？"):
        return 0.85
    question_markers = ("怎么", "为什么", "能不能", "可不可以", "查一下", "帮我看", "什么原因")
    score = sum(0.18 for marker in question_markers if marker in compact)
    if _contains_any(raw_text, ("\u5e2e\u6211\u67e5", "\u67e5\u4e00\u4e0b", "\u95ee\u4e0b", "\u987a\u4fbf\u95ee")):
        score += 0.55
    if _contains_any(raw_text, ("\u662f\u4ec0\u4e48", "\u600e\u4e48", "\u5982\u4f55", "\u80fd\u5426")):
        score += 0.35
    return min(0.85, score)


def _small_talk_signal(raw_text: str) -> float:
    compact = _compact(raw_text)
    if not compact:
        return 0.0
    score = 0.0
    chat_request_markers = (
        "聊聊",
        "聊会",
        "聊一会",
        "和我聊天",
        "和我聊",
        "跟我聊",
        "陪我聊",
        "说说话",
        "闲聊",
        "聊天",
    )
    if any(marker in compact for marker in chat_request_markers):
        score += 0.78
    vent_markers = (
        "妈的",
        "烦死",
        "好烦",
        "无语",
        "服了",
        "崩溃",
        "气死",
        "被气死",
        "太难用",
        "用不了",
        "不想测",
        "懒得测",
    )
    if any(marker in compact for marker in vent_markers):
        score += 0.66
    if _looks_like_lifestyle_question(raw_text):
        score += 0.72
    small_talk_markers = (
        "哈哈",
        "嘿嘿",
        "嗨",
        "你好",
        "早",
        "早上好",
        "晚上好",
        "天气",
        "咖啡",
        "吃了",
        "手抓饼",
        "吐槽",
        "无语",
    )
    score += min(0.7, sum(0.18 for marker in small_talk_markers if marker in compact))
    if any(marker in compact for marker in ("天气", "咖啡", "吃了", "手抓饼")):
        score += 0.42
    if len(compact) <= 3 and compact in {"嗨", "你好", "哈哈", "无语"}:
        score += 0.35
    if _contains_any(raw_text, ("\u54c8\u54c8", "\u563f\u563f")):
        score += 0.32
    if _contains_any(raw_text, ("\u673a\u5668\u4eba", "\u7cfb\u7edf", "\u4f60")) and _contains_any(
        raw_text,
        (
            "\u806a\u660e",
            "\u597d\u7528",
            "\u4e0d\u9519",
            "\u633a\u597d",
            "\u771f\u68d2",
            "\u8c22\u8c22",
            "\u8f9b\u82e6",
        ),
    ):
        score += 0.5
    return min(score, 1.0)


def _looks_like_context_question(raw_text: str) -> bool:
    text = str(raw_text or "")
    compact = _compact(text)
    if not compact:
        return False
    if text.rstrip().endswith(("?", "？", "吗", "嘛", "么")):
        return True
    question_scan = compact.replace("不怎么", "")
    return any(
        marker in question_scan
        for marker in (
            "怎么",
            "为什么",
            "能不能",
            "可不可以",
            "是不是",
            "啥意思",
            "什么原因",
            "什么",
            "啥",
            "咋",
            "咋样",
            "要不要",
            "会不会",
            "几点",
            "多少度",
            "冷不冷",
            "热不热",
        )
    )


def _looks_like_full_daily_report(raw_text: str) -> bool:
    text = str(raw_text or "")
    section_hits = 0
    patterns = (
        r"(?:^|[\n\r\s])(?:今日工作|今天工作|今日完成|今天完成|工作总结)\s*[:：]",
        r"(?:^|[\n\r\s])(?:问题[/／]风险|风险[/／]困难|问题风险|风险问题|问题|风险|困难)\s*[:：]",
        r"(?:^|[\n\r\s])(?:明日计划|明天计划|接下来计划|计划)\s*[:：]",
    )
    for pattern in patterns:
        if re.search(pattern, text):
            section_hits += 1
    return section_hits >= 2


def _looks_like_confirmation(raw_text: str) -> bool:
    compact = _compact(raw_text)
    return compact in {
        "确认",
        "确认提交",
        "提交",
        "提交了",
        "可以提交",
        "没问题",
        "没问题提交",
        "就这样",
        "对",
        "对的",
        "确定",
        "可以",
        "好",
        "好的",
        "嗯",
        "恩",
        "是",
        "是的",
        "交",
        "交了",
        "交吧",
        "\u597d\u4e86\u63d0\u4ea4",
        "\u597d\u63d0\u4ea4",
        "\u63d0\u4ea4\u5427",
        "ok",
    }


def _looks_like_candidate_focus_confirmation(raw_text: str) -> bool:
    compact = _compact(raw_text)
    return compact in {
        "对",
        "对的",
        "确定",
        "可以",
        "好",
        "好的",
        "嗯",
        "恩",
        "是",
        "是的",
        "没错",
        "就是这个",
        "就这个",
        "就这条",
        "这个",
        "这条",
        "ok",
    }


def _looks_like_daily_submit_reply(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if compact in {"\u597d\u4e86\u63d0\u4ea4", "\u597d\u63d0\u4ea4", "\u63d0\u4ea4\u5427"}:
        return True
    return compact in {
        "确认",
        "确认提交",
        "提交",
        "提交了",
        "可以提交",
        "确定",
        "是",
        "是的",
        "对",
        "对的",
        "交",
        "交了",
        "交吧",
        "ok",
    }


def _looks_like_usage_preference_instruction(raw_text: str) -> bool:
    compact = _compact(raw_text)
    if not compact:
        return False
    future_markers = ("下次", "以后", "往后", "后面", "以后我说", "下次我说")
    behavior_markers = ("就直接", "直接", "自动", "默认", "不用问", "不需要问")
    bot_action_markers = ("提交", "交", "确认", "清空", "撤回", "修改", "写日报", "发日报")
    return (
        any(marker in compact for marker in future_markers)
        and any(marker in compact for marker in behavior_markers)
        and any(marker in compact for marker in bot_action_markers)
    )


def _contains_any(raw_text: str, markers: tuple[str, ...]) -> bool:
    compact = _compact(raw_text)
    return any(_compact(marker) in compact for marker in markers)


def _compact(value: str) -> str:
    return re.sub(r"[\s\u3000，,。\.、；;：:！!？?（）()\[\]【】\"'“”‘’]+", "", str(value or "")).lower()


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
