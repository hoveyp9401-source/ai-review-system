from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.workflows.intake import (
    EFFECT_ADD_DAILY_REPORT_ITEM,
    EFFECT_CONFIRM_DAILY_REPORT,
    EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
    WORKFLOW_CHAT,
    WORKFLOW_DAILY_REPORT,
    WORKFLOW_UNKNOWN_OR_HELP,
    RoutingPlan,
)


WorkflowIntakeMode = Literal["observe_only", "protective_gate", "strict_gate"]
GateReplyType = Literal["none", "clarify", "confirm", "inform_blocked", "not_supported"]

MODE_OBSERVE_ONLY = "observe_only"
MODE_PROTECTIVE_GATE = "protective_gate"
MODE_STRICT_GATE = "strict_gate"

DAILY_EFFECT = EFFECT_ADD_DAILY_REPORT_ITEM
DAILY_LEGACY_EFFECTS = {
    EFFECT_ADD_DAILY_REPORT_ITEM,
    EFFECT_CONFIRM_DAILY_REPORT,
    EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
}


@dataclass(frozen=True)
class GateDecision:
    """Execution-facing decision for legacy daily-report intake."""

    mode: WorkflowIntakeMode
    allow_legacy_daily: bool
    block_legacy_daily: bool
    reply_type: GateReplyType = "none"
    safe_effects: list[str] = field(default_factory=list)
    pending_effects: list[str] = field(default_factory=list)
    blocked_effects: list[str] = field(default_factory=list)
    need_confirmation: bool = False
    need_clarification: bool = False
    reason: str = ""
    audit_tags: list[str] = field(default_factory=list)
    reply_text: str = ""

    def as_observation(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "allow_legacy_daily": self.allow_legacy_daily,
            "block_legacy_daily": self.block_legacy_daily,
            "reply_type": self.reply_type,
            "safe_effects": list(self.safe_effects),
            "pending_effects": list(self.pending_effects),
            "blocked_effects": list(self.blocked_effects),
            "need_confirmation": self.need_confirmation,
            "need_clarification": self.need_clarification,
            "reason": self.reason,
            "audit_tags": list(self.audit_tags),
            "has_reply_text": bool(self.reply_text),
        }


def normalize_workflow_intake_mode(value: Any) -> WorkflowIntakeMode:
    text = str(value or "").strip().lower()
    if text in {MODE_OBSERVE_ONLY, MODE_PROTECTIVE_GATE, MODE_STRICT_GATE}:
        return text  # type: ignore[return-value]
    return MODE_OBSERVE_ONLY


def build_gate_decision(plan: RoutingPlan, *, mode: Any = MODE_OBSERVE_ONLY) -> GateDecision:
    normalized_mode = normalize_workflow_intake_mode(mode)
    effect_types = [effect.effect_type for effect in plan.effects]

    if normalized_mode == MODE_OBSERVE_ONLY:
        return GateDecision(
            mode=normalized_mode,
            allow_legacy_daily=True,
            block_legacy_daily=False,
            safe_effects=list(plan.safety_decision.safe_effects),
            pending_effects=list(plan.safety_decision.pending_effects),
            blocked_effects=list(plan.safety_decision.blocked_effects),
            reason="observe_only does not change legacy daily behavior",
            audit_tags=["observe_only"],
        )

    if normalized_mode == MODE_STRICT_GATE:
        return GateDecision(
            mode=normalized_mode,
            allow_legacy_daily=False,
            block_legacy_daily=True,
            reply_type="not_supported",
            safe_effects=list(plan.safety_decision.safe_effects),
            pending_effects=list(plan.safety_decision.pending_effects),
            blocked_effects=effect_types,
            need_clarification=True,
            reason="strict_gate interface exists but is not enabled for legacy daily execution",
            audit_tags=["strict_gate_not_enabled"],
            reply_text="当前严格模式暂未启用，我先不写入日报。请联系管理员确认处理方式。",
        )

    if "orphan_confirmation" in plan.safety_decision.flags:
        return GateDecision(
            mode=normalized_mode,
            allow_legacy_daily=False,
            block_legacy_daily=True,
            reply_type="clarify",
            safe_effects=[],
            pending_effects=[],
            blocked_effects=effect_types,
            need_clarification=True,
            reason="confirmation-like message has no active pending context",
            audit_tags=["orphan_confirmation"],
            reply_text="你是要确认哪一项？请说明要提交或修改的日期和内容。",
        )

    daily_effects = [effect_type for effect_type in effect_types if effect_type in DAILY_LEGACY_EFFECTS]
    if not daily_effects:
        return GateDecision(
            mode=normalized_mode,
            allow_legacy_daily=False,
            block_legacy_daily=True,
            reply_type=_blocked_reply_type(plan),
            safe_effects=[],
            pending_effects=list(plan.safety_decision.pending_effects),
            blocked_effects=effect_types,
            need_clarification=plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP,
            reason="message has no daily-report write effect",
            audit_tags=["no_daily_effect", plan.primary_workflow],
            reply_text=_blocked_non_daily_message(plan),
        )

    if _should_confirm_destructive_daily_action(plan):
        return GateDecision(
            mode=normalized_mode,
            allow_legacy_daily=False,
            block_legacy_daily=True,
            reply_type="confirm",
            safe_effects=[],
            pending_effects=daily_effects,
            blocked_effects=[effect for effect in effect_types if effect not in DAILY_LEGACY_EFFECTS],
            need_confirmation=True,
            reason="destructive daily operation requires confirmation before legacy execution",
            audit_tags=["destructive_daily_operation"],
            reply_text="这看起来是删除、清空、撤回或覆盖类操作。为避免误删，我先不执行。请明确日期和操作内容后再确认。",
        )

    secondary_effects = [effect for effect in effect_types if effect not in DAILY_LEGACY_EFFECTS]
    if secondary_effects:
        if _can_allow_daily_with_sidecar_effects(plan, secondary_effects):
            return GateDecision(
                mode=normalized_mode,
                allow_legacy_daily=True,
                block_legacy_daily=False,
                reply_type="none",
                safe_effects=daily_effects,
                pending_effects=secondary_effects,
                blocked_effects=[],
                need_confirmation=False,
                reason="daily write is allowed while non-daily sidecar effects stay in their own sandbox or workflow",
                audit_tags=["daily_allowed_with_sidecar_effects", *secondary_effects],
            )
        return GateDecision(
            mode=normalized_mode,
            allow_legacy_daily=False,
            block_legacy_daily=True,
            reply_type="confirm",
            safe_effects=[],
            pending_effects=effect_types,
            blocked_effects=[],
            need_confirmation=True,
            reason="multi-effect message cannot be silently handled by legacy daily executor",
            audit_tags=["multi_effect_pending", *secondary_effects],
            reply_text=_multi_effect_message(effect_types),
        )

    if _has_non_daily_or_chatter_segment(plan):
        if _can_allow_daily_with_non_daily_segments(plan, daily_effects):
            return GateDecision(
                mode=normalized_mode,
                allow_legacy_daily=True,
                block_legacy_daily=False,
                reply_type="none",
                safe_effects=daily_effects,
                pending_effects=[],
                blocked_effects=[],
                need_confirmation=False,
                reason="daily segment is safe to write while non-daily segment is handled separately",
                audit_tags=["daily_allowed_with_non_daily_segments"],
            )
        return GateDecision(
            mode=normalized_mode,
            allow_legacy_daily=False,
            block_legacy_daily=True,
            reply_type="confirm",
            safe_effects=[],
            pending_effects=daily_effects,
            blocked_effects=[],
            need_confirmation=True,
            reason="segmented message contains non-daily or chatter content",
            audit_tags=["multi_segment_pending"],
            reply_text=_multi_segment_message(plan),
        )

    if plan.confidence < 0.45:
        return GateDecision(
            mode=normalized_mode,
            allow_legacy_daily=False,
            block_legacy_daily=True,
            reply_type="clarify",
            safe_effects=[],
            pending_effects=daily_effects,
            blocked_effects=[],
            need_clarification=True,
            reason="daily effect confidence is too low for legacy write",
            audit_tags=["low_confidence_daily"],
            reply_text="这句我还不能确定要写到哪一天、哪一栏。请说明是今日工作、问题风险还是明日计划。",
        )

    return GateDecision(
        mode=normalized_mode,
        allow_legacy_daily=True,
        block_legacy_daily=False,
        reply_type="none",
        safe_effects=daily_effects,
        pending_effects=[],
        blocked_effects=[],
        reason="daily-only message can enter legacy daily executor",
        audit_tags=[
            "daily_only_allowed",
            *(
                ["daily_destructive_delegated"]
                if _has_destructive_daily_text(plan) and _can_delegate_destructive_daily_action(plan)
                else []
            ),
        ],
    )


def _blocked_reply_type(plan: RoutingPlan) -> GateReplyType:
    if plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP:
        return "clarify"
    return "inform_blocked"


def _blocked_non_daily_message(plan: RoutingPlan) -> str:
    if plan.primary_workflow == WORKFLOW_UNKNOWN_OR_HELP:
        return "这句我还不能确定是不是日报内容，我先不写入日报。请说明你要记录到日报，还是只是咨询/讨论。"
    return "我识别这不是日报内容，先不写入日报。对应能力会交给相应工作流处理。"


def _multi_effect_message(effect_types: list[str]) -> str:
    labels = [_effect_label(effect_type) for effect_type in effect_types]
    joined = "\n".join(f"{index}. {label}" for index, label in enumerate(labels, start=1))
    return (
        "我识别到这句话可能需要同步到多个位置：\n"
        f"{joined}\n\n"
        "当前多工作流执行器还未接管，我先不让旧日报流程静默只写日报。"
        "如果你只想记录到日报，请明确说“仅写入日报”。"
    )


def _has_non_daily_or_chatter_segment(plan: RoutingPlan) -> bool:
    if len(plan.segments) <= 1:
        return False
    for segment in plan.segments:
        if segment.intent == "small_talk":
            return True
        if any(workflow != WORKFLOW_DAILY_REPORT for workflow in segment.matched_workflows):
            return True
        if segment.primary_workflow not in {WORKFLOW_DAILY_REPORT, WORKFLOW_UNKNOWN_OR_HELP}:
            return True
    return False


def _can_allow_daily_with_sidecar_effects(plan: RoutingPlan, secondary_effects: list[str]) -> bool:
    allowed_sidecars = {
        "upsert_travel_plan",
        "append_case_progress",
        "run_legal_research",
        "draft_weekly_report",
    }
    if not secondary_effects or any(effect not in allowed_sidecars for effect in secondary_effects):
        return False
    return any(effect.target_system == WORKFLOW_DAILY_REPORT for effect in plan.effects)


def _can_allow_daily_with_non_daily_segments(plan: RoutingPlan, daily_effects: list[str]) -> bool:
    if not daily_effects:
        return False
    allowed_segment_workflows = {
        "internal_qa",
        "legal_research",
        "travel_coordination",
        "case_progress",
        "weekly_report",
        WORKFLOW_UNKNOWN_OR_HELP,
    }
    for segment in plan.segments:
        if segment.intent == "small_talk":
            continue
        for workflow in segment.matched_workflows:
            if workflow != WORKFLOW_DAILY_REPORT and workflow not in allowed_segment_workflows:
                return False
    return True


def _multi_segment_message(plan: RoutingPlan) -> str:
    labels: list[str] = []
    for segment in plan.segments:
        if segment.intent == "small_talk":
            labels.append("闲聊")
            continue
        if segment.matched_workflows:
            labels.extend(_workflow_label(workflow) for workflow in segment.matched_workflows)
    unique_labels: list[str] = []
    for label in labels:
        if label not in unique_labels:
            unique_labels.append(label)
    joined = "、".join(unique_labels) or "多段内容"
    return f"我识别到这条消息里可能混有{joined}，先不让旧日报流程静默整段写入。请确认哪些内容只写入日报。"


def _workflow_label(workflow: str) -> str:
    return {
        WORKFLOW_DAILY_REPORT: "日报",
        "monthly_report": "月报",
        "weekly_report": "周报",
        "case_progress": "案件进展",
        "travel_coordination": "出差协同",
        "legal_research": "法律研究",
        "internal_qa": "内部问答",
        WORKFLOW_CHAT: "闲聊",
        WORKFLOW_UNKNOWN_OR_HELP: "待确认内容",
    }.get(workflow, workflow)


def _effect_label(effect_type: str) -> str:
    return {
        DAILY_EFFECT: "日报",
        EFFECT_CONFIRM_DAILY_REPORT: "日报确认",
        EFFECT_LEGACY_DAILY_CONTEXT_ACTION: "日报上下文回复",
        "draft_weekly_report": "周报",
        "capture_monthly_report_reply": "月报",
        "upsert_travel_plan": "出差协同",
        "append_case_progress": "案件进展",
        "run_legal_research": "法律研究",
    }.get(effect_type, effect_type)


def _has_destructive_daily_text(plan: RoutingPlan) -> bool:
    text = "".join(str(effect.payload.get("content") or "") for effect in plan.effects)
    compact = (
        text.replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
        .replace("，", "")
        .replace(",", "")
        .replace("。", "")
    )
    return any(marker in compact for marker in ("清空", "删除", "删掉", "撤回", "覆盖", "重新写", "重写"))


def _should_confirm_destructive_daily_action(plan: RoutingPlan) -> bool:
    return False


def _can_delegate_destructive_daily_action(plan: RoutingPlan) -> bool:
    """Let the daily workflow own its own confirmation chain.

    The gate should block standalone destructive-looking text from falling into
    the legacy daily executor. When the router already tied the text to an
    active daily task, the daily workflow has the state needed to ask for or
    consume confirmation; duplicating that state in the gate would be a step
    back toward a monolithic intent handler.
    """

    if not plan.effects:
        return False
    for effect in plan.effects:
        if effect.effect_type not in DAILY_LEGACY_EFFECTS:
            return False
        if effect.target_system != WORKFLOW_DAILY_REPORT:
            return False
        if effect.effect_type == EFFECT_LEGACY_DAILY_CONTEXT_ACTION and effect.target.get("task_id"):
            return True
    return False
