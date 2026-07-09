from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any, Callable

from app.agent.action_plan import ActionPlan, PendingInteractionPlan
from app.agent.state_resolver import resolve_pending_interaction
from app.llm.extractor import LLMOutputError


DirectPlanResolver = Callable[[str, Any], tuple[str, ActionPlan] | None]
FallbackPlanBuilder = Callable[[str, Any], ActionPlan | None]


_PRE_LLM_DIRECT_BRANCHES = {
    "direct_query_current",
    "direct_unsubmit_report",
    "direct_reset_without_content",
    "direct_clear_current_report",
    "direct_current_pasted_report",
    "direct_repair_feedback",
    "direct_replace_current_report",
    "direct_pasted_reference_report",
    "direct_structured_multi_field_report",
    "direct_previous_plan_completion",
    "direct_simple_full_report",
    "direct_single_action_effects",
    "direct_numbered_work_items",
    "direct_asr_correction_work",
    "direct_not_replace_but_add",
    "direct_negative_replacement",
    "direct_delete_last_modified_item",
    "direct_last_unwritten_candidate",
    "direct_this_is_problem_without_candidate",
    "direct_set_section_empty",
    "direct_ordinal_delete",
    "direct_range_merge",
    "direct_problem_slot_answer",
    "direct_plan_slot_answer",
    "direct_tomorrow_plan_phrase",
    "direct_restore_snapshot",
    "direct_explicit_append",
    "direct_explicit_work_append",
    "direct_explicit_problem_append",
}

_PRE_LLM_PENDING_BRANCHES = {
    "select_append_target",
    "await_content_confirm_reprompt",
    "cancel_append_content",
    "accept_quality_candidate",
    "quality_candidate_missing",
    "query_pending_dated_report",
    "select_historical_edit_field",
    "query_historical_edit_field",
    "historical_edit_field_focus_reprompt",
    "historical_edit_replace_full_report",
    "query_current_edit_flow",
    "current_edit_field_focus_reprompt",
    "current_edit_replace_full_report",
    "historical_edit_delete_blocked",
}


@dataclass(frozen=True)
class DecisionRoute:
    source: str
    branch: str
    plan: ActionPlan | None
    meta: dict[str, Any]
    entered_llm: bool
    llm_seconds: float = 0.0
    error_message: str = ""

    @property
    def reason(self) -> str:
        if self.plan is None:
            return self.error_message
        return self.plan.reason or self.error_message


class DecisionRouter:
    """Choose one decision source before the report executor mutates state."""

    def __init__(self, report_agent: Any):
        self.report_agent = report_agent

    async def decide(
        self,
        *,
        raw_input: str,
        context: dict[str, Any],
        existing: Any,
        direct_plan_resolver: DirectPlanResolver,
        fallback_plan_builder: FallbackPlanBuilder,
    ) -> DecisionRoute:
        pending_interaction = context.get("pending_interaction")
        direct_plan = direct_plan_resolver(raw_input, existing)
        if direct_plan is not None:
            branch, plan = direct_plan
            if _can_use_direct_before_llm(branch, raw_input=raw_input) and not _should_defer_direct_for_context_pending(branch, raw_input, pending_interaction):
                return DecisionRoute(
                    source="direct_rule",
                    branch=branch,
                    plan=plan,
                    meta={"model": "direct_rule", "thinking": False, "timeout": False, "branch": branch},
                    entered_llm=False,
                )

        bypass_pending = _should_bypass_pending(raw_input, pending_interaction, context)
        if not bypass_pending:
            state_resolution = resolve_pending_interaction(raw_input, pending_interaction)
            if state_resolution is not None:
                plan = state_resolution.plan
                branch = state_resolution.branch
                if _can_use_pending_before_llm(branch):
                    return DecisionRoute(
                        source="pending_state",
                        branch=branch,
                        plan=plan,
                        meta={"model": "state_resolver", "thinking": False, "timeout": False, "branch": branch},
                        entered_llm=False,
                    )

            continuation_plan = _pending_continuation_reprompt(raw_input, pending_interaction)
            if continuation_plan is not None:
                return DecisionRoute(
                    source="pending_state",
                    branch="pending_continuation_reprompt",
                    plan=continuation_plan,
                    meta={"model": "state_resolver", "thinking": False, "timeout": False, "branch": "pending_continuation_reprompt"},
                    entered_llm=False,
                )

        llm_context = _llm_context_for_bypassed_pending(context, pending_interaction) if bypass_pending else context
        step_start = time.perf_counter()
        try:
            decision_result = await self.report_agent.decide_with_meta(raw_input=raw_input, context=llm_context)
        except LLMOutputError as exc:
            seconds = round(time.perf_counter() - step_start, 4)
            meta = dict(getattr(exc, "meta", {}) or {})
            fallback_plan = fallback_plan_builder(raw_input, existing)
            if fallback_plan is not None and _can_use_error_fallback(fallback_plan):
                fallback_meta = {
                    "model": str(meta.get("model") or "report-agent-error-fallback"),
                    "thinking": meta.get("thinking"),
                    "timeout": bool(meta.get("timeout")),
                    "fallback": "simple_report_fields",
                    "original_error": str(exc),
                }
                return DecisionRoute(
                    source="report_agent_error_fallback",
                    branch="report_agent_error_fallback",
                    plan=fallback_plan,
                    meta=fallback_meta,
                    entered_llm=True,
                    llm_seconds=seconds,
                    error_message=str(exc),
                )
            return DecisionRoute(
                source="report_agent_error",
                branch="report_agent_error",
                plan=None,
                meta=meta,
                entered_llm=True,
                llm_seconds=seconds,
                error_message=str(exc),
            )

        seconds = round(time.perf_counter() - step_start, 4)
        plan = decision_result.payload
        meta = dict(decision_result.meta or {})
        if bypass_pending:
            plan = _plan_clearing_bypassed_pending(plan, pending_interaction)
            meta["pending_bypassed"] = True
        return DecisionRoute(
            source="report_agent",
            branch="report_agent",
            plan=plan,
            meta=meta,
            entered_llm=True,
            llm_seconds=seconds,
        )


_INTERRUPTIBLE_PENDING_TYPES = {
    "awaiting_action_confirmation",
    "pending_batch_action",
    "pending_clarification",
    "awaiting_append_target",
    "awaiting_append_target_confirmation",
    "awaiting_append_content",
    "awaiting_content_quality_confirmation",
    "awaiting_dated_report_action",
}
_CONTEXT_ONLY_PENDING_TYPES = {"current_report_edit_flow", "historical_report_edit_flow"}

_CONFIRMATION_CONTINUATIONS = {
    "\u786e\u8ba4",
    "\u786e\u5b9a",
    "\u786e\u8ba4\u5220\u9664",
    "\u5220\u6389",
    "\u5220\u9664",
    "\u786e\u8ba4\u6e05\u7a7a",
    "\u55ef",
    "\u55ef\u55ef",
    "\u6069",
    "\u6069\u6069",
    "\u53ef\u4ee5",
    "\u5bf9",
    "\u662f",
    "\u662f\u7684",
    "\u597d",
    "\u597d\u7684",
    "\u6ca1\u9519",
    "\u6ca1\u95ee\u9898",
    "\u884c",
    "\u53d6\u6d88",
    "\u7b97\u4e86",
    "\u5148\u4e0d\u6539",
    "\u4e0d\u7528\u4e86",
    "\u4e0d\u6539\u4e86",
    "\u5c31\u8fd9\u4e2a",
    "\u5c31\u8fd9\u6837",
    "\u6309\u521a\u624d\u7684",
    "\u6309\u8fd9\u4e2a",
    "\u5c31\u8fd9\u4e48\u5199",
}
_SECTION_CONTINUATIONS = {
    "\u4eca\u65e5\u5de5\u4f5c",
    "\u4eca\u5929\u5de5\u4f5c",
    "\u95ee\u9898",
    "\u95ee\u9898\u98ce\u9669",
    "\u98ce\u9669\u95ee\u9898",
    "\u660e\u65e5\u8ba1\u5212",
    "\u660e\u5929\u8ba1\u5212",
}
_DATED_REPORT_ACTION_CONTINUATIONS = {
    "\u4fee\u6539",
    "\u6539",
    "\u5220\u9664",
    "\u5220",
    "\u67e5\u770b",
    "\u770b\u770b",
    "\u663e\u793a",
    "\u53d6\u6d88",
}
_NEW_EDIT_VERBS = (
    "\u91cd\u65b0\u6574\u7406",
    "\u91cd\u65b0\u751f\u6210",
    "\u91cd\u65b0\u5217",
    "\u91cd\u65b0\u5199",
    "\u91cd\u5199",
    "\u6539\u6210",
    "\u4fee\u6539",
    "\u5220\u9664",
    "\u5220\u6389",
    "\u79fb\u9664",
    "\u53bb\u6389",
    "\u6e05\u7a7a",
    "\u5408\u5e76",
    "\u548c\u5e76",
    "\u62c6\u5206",
    "\u4f18\u5316",
    "\u6da6\u8272",
    "\u66ff\u6362",
    "\u8c03\u6574",
    "\u8986\u76d6",
    "\u91cd\u65b0\u63d0\u4ea4",
    "\u91cd\u586b",
)
_NEW_EDIT_TARGETS = (
    "\u4eca\u65e5\u5de5\u4f5c",
    "\u4eca\u5929\u5de5\u4f5c",
    "\u95ee\u9898\u98ce\u9669",
    "\u98ce\u9669\u95ee\u9898",
    "\u95ee\u9898",
    "\u660e\u65e5\u8ba1\u5212",
    "\u660e\u5929\u8ba1\u5212",
    "\u65e5\u62a5",
    "\u7b2c",
    "\u6761",
)
_FULL_REPORT_SECTION_PATTERNS = (
    r"(?:^|[\n\r\s])(?:\u4eca\u65e5\u5de5\u4f5c|\u4eca\u5929\u5de5\u4f5c|\u4eca\u65e5\u5b8c\u6210|\u4eca\u65e5\u5b8c\u6210\u5de5\u4f5c|\u4eca\u5929\u5b8c\u6210|\u5df2\u5b8c\u6210|\u5b8c\u6210\u5de5\u4f5c|\u5de5\u4f5c\u603b\u7ed3)\s*[\uff1a:]",
    r"(?:^|[\n\r\s])(?:\u95ee\u9898[/\uff0f]\u98ce\u9669|\u98ce\u9669[/\uff0f]\u56f0\u96be|\u95ee\u9898\u98ce\u9669|\u98ce\u9669\u95ee\u9898|\u95ee\u9898|\u98ce\u9669|\u56f0\u96be|blocker)\s*[\uff1a:]",
    r"(?:^|[\n\r\s])(?:\u660e\u65e5\u8ba1\u5212|\u660e\u5929\u8ba1\u5212|\u660e\u65e5\u8981\u505a|\u660e\u5929\u8981\u505a|\u63a5\u4e0b\u6765\u8ba1\u5212|\u63a5\u4e0b\u6765\u8981\u505a|\u4eca\u65e5\u8ba1\u5212|\u8ba1\u5212)\s*[\uff1a:]",
)


def _should_bypass_pending(raw_input: str, pending_interaction: Any, context: dict[str, Any]) -> bool:
    if not isinstance(pending_interaction, dict):
        return False
    pending_type = str(pending_interaction.get("type") or "")
    if _is_explicit_pending_continuation(raw_input, pending_interaction):
        return False
    if pending_type in _CONTEXT_ONLY_PENDING_TYPES and _looks_like_full_report(raw_input):
        return False
    if _looks_like_full_report(raw_input):
        return True
    if _looks_like_new_edit_intent(raw_input):
        return True
    if _looks_like_new_structured_instruction(raw_input):
        return True
    return False


def _is_explicit_pending_continuation(raw_input: str, pending_interaction: dict[str, Any]) -> bool:
    compact = _compact_for_pending(raw_input)
    if not compact:
        return False
    pending_type = str(pending_interaction.get("type") or "")
    operation = str(pending_interaction.get("operation") or "")
    if compact in _CONFIRMATION_CONTINUATIONS:
        return True
    if pending_type in {"awaiting_action_confirmation", "pending_batch_action"}:
        if _is_confirming_pending_action(compact, operation):
            return True
    if pending_type == "awaiting_dated_report_action" and compact in _DATED_REPORT_ACTION_CONTINUATIONS:
        return True
    if compact in _SECTION_CONTINUATIONS:
        return True
    if _looks_like_short_index_answer(compact):
        return True
    if pending_type == "awaiting_append_content" and not _looks_like_full_report(raw_input) and not _looks_like_new_edit_intent(raw_input):
        return False
    return False


def _should_defer_direct_for_context_pending(branch: str, raw_input: str, pending_interaction: Any) -> bool:
    if not isinstance(pending_interaction, dict):
        return False
    pending_type = str(pending_interaction.get("type") or "")
    if pending_type not in _CONTEXT_ONLY_PENDING_TYPES:
        return False
    if branch not in {
        "direct_structured_multi_field_report",
        "direct_simple_full_report",
        "direct_replace_current_report",
        "direct_pasted_reference_report",
    }:
        return False
    return _looks_like_full_report(raw_input)


def _pending_continuation_reprompt(raw_input: str, pending_interaction: Any) -> ActionPlan | None:
    if not isinstance(pending_interaction, dict):
        return None
    if not _is_explicit_pending_continuation(raw_input, pending_interaction):
        return None
    pending_type = str(pending_interaction.get("type") or "")
    if pending_type in _CONTEXT_ONLY_PENDING_TYPES:
        return None
    return ActionPlan(
        intent="edit_draft",
        confidence="high",
        should_write=False,
        pending_interaction_to_set=PendingInteractionPlan.model_validate(pending_interaction),
        reply_to_user="I still need the specific pending detail, such as the section or item number.",
        reason="Kept explicit short continuation in pending state instead of routing to LLM.",
    )


def _is_confirming_pending_action(compact: str, operation: str) -> bool:
    if compact.startswith("\u786e\u8ba4\u5220\u9664") and any(marker in operation for marker in ("delete", "clear", "batch")):
        return True
    if compact.startswith("\u786e\u8ba4\u6e05\u7a7a") and any(marker in operation for marker in ("clear", "delete", "batch")):
        return True
    return False


def _looks_like_short_index_answer(compact: str) -> bool:
    if len(compact) > 16:
        return False
    if re.fullmatch(r"(?:\u7b2c)?\d+(?:\u6761)?", compact):
        return True
    if re.fullmatch(r"\d+[-\uff0d\u2014\u2013]\d+", compact):
        return True
    if re.fullmatch(r"\d+(?:[\u3001,\uff0c]\d+)+", compact):
        return True
    return False


def _looks_like_full_report(raw_input: str) -> bool:
    text = str(raw_input or "")
    hits = sum(1 for pattern in _FULL_REPORT_SECTION_PATTERNS if re.search(pattern, text))
    return hits >= 2


def _looks_like_new_edit_intent(raw_input: str) -> bool:
    compact = _compact_for_pending(raw_input)
    if not compact:
        return False
    if compact in _CONFIRMATION_CONTINUATIONS:
        return False
    has_verb = any(verb in compact for verb in _NEW_EDIT_VERBS)
    has_target = any(target in compact for target in _NEW_EDIT_TARGETS) or bool(re.search(r"\d+[-\uff0d\u2014\u2013]\d+", compact))
    if has_verb and has_target:
        return True
    if "\u6309" in compact and any(marker in compact for marker in ("\u5217\u51fa\u6765", "\u91cd\u65b0\u5217", "\u7f16\u53f7")):
        return True
    return False


def _looks_like_new_structured_instruction(raw_input: str) -> bool:
    text = str(raw_input or "")
    compact = _compact_for_pending(text)
    if not compact:
        return False
    if _looks_like_full_report(text):
        return True
    return bool(re.search(r"(?:^|[\n\r])\s*(?:\d+|[\uff08(]?\d+[\uff09)])\s*[\.\u3001\uff0e\uff09)]", text)) and any(
        marker in compact for marker in ("\u91cd\u65b0\u6574\u7406", "\u91cd\u65b0\u751f\u6210", "\u8986\u76d6", "\u65e5\u62a5")
    )


def _llm_context_for_bypassed_pending(context: dict[str, Any], pending_interaction: Any) -> dict[str, Any]:
    if not isinstance(pending_interaction, dict):
        return context
    pending_type = str(pending_interaction.get("type") or "")
    if pending_type in _CONTEXT_ONLY_PENDING_TYPES:
        return context
    next_context = dict(context)
    next_context.pop("pending_interaction", None)
    return next_context


def _plan_clearing_bypassed_pending(plan: ActionPlan, pending_interaction: Any) -> ActionPlan:
    if not isinstance(pending_interaction, dict):
        return plan
    pending_type = str(pending_interaction.get("type") or "")
    if pending_type not in _INTERRUPTIBLE_PENDING_TYPES:
        return plan
    if plan.clear_pending_interaction or plan.pending_interaction_to_set is not None:
        return plan
    payload = plan.model_dump()
    payload["clear_pending_interaction"] = True
    payload["reason"] = ((plan.reason or "") + " Bypassed stale pending state for new user intent.").strip()
    return ActionPlan.model_validate(payload)


def _compact_for_pending(value: str) -> str:
    return re.sub(r"[\s\u3000\uff0c,\u3002\.\u3001\uff1b;\uff1a:\uff01!\uff1f?\uff08\uff09()\u3010\u3011\[\]\"'\u201c\u201d\u2018\u2019]+", "", str(value or "").lower())


def _can_use_direct_before_llm(branch: str, *, raw_input: str = "") -> bool:
    if branch == "direct_ordinal_delete" and not _looks_like_delete_request(raw_input):
        return False
    return branch in _PRE_LLM_DIRECT_BRANCHES


def _looks_like_delete_request(raw_input: str) -> bool:
    compact = _compact_for_pending(raw_input)
    return any(token in compact for token in ("删除", "删掉", "去掉", "移除", "不要"))


def _can_use_pending_before_llm(branch: str) -> bool:
    if branch.startswith("confirm_") or branch.startswith("cancel_"):
        return True
    if branch.startswith("pending_clarification_"):
        return True
    return branch in _PRE_LLM_PENDING_BRANCHES


def _can_use_error_fallback(plan: ActionPlan) -> bool:
    if plan.intent != "fill_report":
        return False
    allowed_action_types = {"append_items", "replace_field"}
    return all(action.type in allowed_action_types for action in (plan.actions or []))
