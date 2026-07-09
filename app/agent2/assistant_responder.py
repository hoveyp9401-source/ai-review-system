from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent2.chat_capability import build_chat_reply
from app.workflows.gate import GateDecision
from app.workflows.intake import (
    EFFECT_ADD_DAILY_REPORT_ITEM,
    EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
    WORKFLOW_CHAT,
    WORKFLOW_INTERNAL_QA,
    WORKFLOW_LEGAL_RESEARCH,
    WORKFLOW_MONTHLY_REPORT,
    WORKFLOW_UNKNOWN_OR_HELP,
    RoutingPlan,
)


@dataclass(frozen=True)
class AssistantReply:
    reply_type: str
    workflow: str
    text: str

    def as_observation(self) -> dict[str, Any]:
        return {
            "reply_type": self.reply_type,
            "workflow": self.workflow,
            "text_chars": len(self.text),
            "has_text": bool(self.text),
        }


def build_assistant_reply(
    *,
    raw_text: str,
    plan: RoutingPlan,
    gate_decision: GateDecision,
) -> AssistantReply | None:
    """Build a read-only assistant reply for non-daily turns.

    This layer never authorizes a daily write. It only replaces the generic
    "not writing daily" gate text with a useful assistant response.
    """

    if not gate_decision.block_legacy_daily:
        return _build_side_assistant_reply(raw_text=raw_text, plan=plan, gate_decision=gate_decision)
    if gate_decision.need_confirmation or "orphan_confirmation" in gate_decision.audit_tags:
        return None

    workflow = str(plan.primary_workflow or "")
    action_types = _action_types(plan)
    raw = str(raw_text or "").strip()

    if workflow == WORKFLOW_LEGAL_RESEARCH or "run_legal_research" in {effect.effect_type for effect in plan.effects}:
        return AssistantReply(
            reply_type="legal_research",
            workflow=WORKFLOW_LEGAL_RESEARCH,
            text=_legal_research_reply(raw),
        )

    if workflow == WORKFLOW_INTERNAL_QA:
        return AssistantReply(
            reply_type="internal_qa",
            workflow=WORKFLOW_INTERNAL_QA,
            text=_internal_qa_reply(raw),
        )

    if workflow == WORKFLOW_MONTHLY_REPORT and "monthly_status_query" in action_types:
        return AssistantReply(
            reply_type="monthly_status_query",
            workflow=WORKFLOW_MONTHLY_REPORT,
            text=_monthly_status_query_reply(raw),
        )

    if workflow == WORKFLOW_CHAT and action_types.intersection({"small_talk", "assistant_feedback"}):
        return AssistantReply(
            reply_type="chat",
            workflow=WORKFLOW_CHAT,
            text=build_chat_reply(raw, fallback=_small_talk_reply(raw)).text,
        )

    if workflow == WORKFLOW_UNKNOWN_OR_HELP and action_types.intersection({"small_talk", "assistant_feedback"}):
        return AssistantReply(
            reply_type="chat",
            workflow=WORKFLOW_CHAT,
            text=build_chat_reply(raw, fallback=_small_talk_reply(raw)).text,
        )

    if workflow == WORKFLOW_UNKNOWN_OR_HELP and "disambiguation_required" in action_types:
        return AssistantReply(
            reply_type="clarify_intent",
            workflow=WORKFLOW_UNKNOWN_OR_HELP,
            text=(
                "\u8fd9\u53e5\u6211\u5148\u4e0d\u5199\u5165\u65e5\u62a5\u3002\u4f60\u662f\u60f3\u628a\u5b83\u8bb0\u6210\u4eca\u65e5\u5de5\u4f5c\uff0c"
                "\u8fd8\u662f\u8981\u6211\u6309\u95ee\u9898/\u7814\u7a76\u6765\u5904\u7406\uff1f"
            ),
        )

    return None


def _build_side_assistant_reply(
    *,
    raw_text: str,
    plan: RoutingPlan,
    gate_decision: GateDecision,
) -> AssistantReply | None:
    if not gate_decision.allow_legacy_daily:
        return None
    if not _has_daily_write_effect(plan):
        return None
    side_type = _side_assistant_reply_type(plan)
    if side_type == "legal_research":
        return AssistantReply(
            reply_type="legal_research",
            workflow=WORKFLOW_LEGAL_RESEARCH,
            text=_legal_research_reply(raw_text),
        )
    if side_type == "internal_qa":
        return AssistantReply(
            reply_type="internal_qa",
            workflow=WORKFLOW_INTERNAL_QA,
            text=_internal_qa_reply(raw_text),
        )
    return None


def _has_daily_write_effect(plan: RoutingPlan) -> bool:
    daily_write_effects = {EFFECT_ADD_DAILY_REPORT_ITEM, EFFECT_LEGACY_DAILY_CONTEXT_ACTION}
    if any(effect.effect_type in daily_write_effects for effect in plan.effects):
        return True
    return any(any(effect in daily_write_effects for effect in segment.effect_types) for segment in plan.segments)


def _side_assistant_reply_type(plan: RoutingPlan) -> str:
    action_types = _action_types(plan)
    if WORKFLOW_LEGAL_RESEARCH in plan.matched_workflows or "legal_research" in action_types:
        return "legal_research"
    if WORKFLOW_INTERNAL_QA in plan.matched_workflows or "internal_qa" in action_types:
        return "internal_qa"
    for segment in sorted(plan.segments, key=lambda item: item.index):
        if segment.primary_workflow == WORKFLOW_LEGAL_RESEARCH or WORKFLOW_LEGAL_RESEARCH in segment.matched_workflows:
            return "legal_research"
        if "run_legal_research" in segment.effect_types:
            return "legal_research"
        if segment.primary_workflow == WORKFLOW_INTERNAL_QA or WORKFLOW_INTERNAL_QA in segment.matched_workflows:
            return "internal_qa"
    return ""


def _action_types(plan: RoutingPlan) -> set[str]:
    action_plan = plan.entities.get("user_action_plan") if isinstance(plan.entities, dict) else None
    if isinstance(action_plan, dict):
        return {str(action_type or "") for action_type in action_plan.get("action_types") or []}
    actions = list(getattr(action_plan, "actions", []) or [])
    return {str(getattr(action, "action_type", "") or "") for action in actions}


def _small_talk_reply(raw_text: str) -> str:
    if _contains_any(raw_text, ("agent2", "Agent2", "\u7070\u6d4b", "\u73b0\u5728\u662f")):
        return (
            "\u73b0\u5728\u4f60\u8fd9\u8fb9\u6309 Agent2 \u7070\u6d4b\u8def\u5f84\u5904\u7406\u3002"
            "\u8fd9\u53e5\u6211\u5f53\u7cfb\u7edf\u72b6\u6001/\u95f2\u804a\u95ee\u9898\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002"
        )
    if _contains_any(raw_text, ("\u8f9b\u82e6", "\u8c22\u8c22", "\u8c22\u4e86")):
        return "\u6536\u5230\uff0c\u4e0d\u5ba2\u6c14\u3002\u8fd9\u53e5\u6211\u4e0d\u4f1a\u5199\u5165\u65e5\u62a5\uff0c\u4f60\u7ee7\u7eed\u8bf4\u5de5\u4f5c\u6216\u8981\u67e5\u7684\u4e8b\u5c31\u884c\u3002"
    if _contains_any(raw_text, ("\u5496\u5561", "\u65e9\u4e0a", "\u4f60\u597d")):
        return "\u5728\u5462\u3002\u8fd9\u53e5\u6211\u5f53\u95f2\u804a\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002\u4f60\u53ef\u4ee5\u7ee7\u7eed\u76f4\u63a5\u8bf4\u65e5\u62a5\u3001\u51fa\u5dee\u3001\u6848\u4ef6\u6216\u8981\u67e5\u7684\u95ee\u9898\u3002"
    return "\u6211\u5148\u628a\u8fd9\u53e5\u5f53\u95f2\u804a/\u53cd\u9988\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002\u8981\u8bb0\u5de5\u4f5c\u7684\u8bdd\uff0c\u628a\u5177\u4f53\u52a8\u4f5c\u8bf4\u51fa\u6765\u5c31\u884c\uff0c\u6bd4\u5982\u201c\u4eca\u5929\u63a5\u5f85\u4f9b\u5e94\u5546\u201d\u6216\u201c\u660e\u5929\u53bb\u5357\u4eac\u5f00\u5ead\u201d\u3002"


def _internal_qa_reply(raw_text: str) -> str:
    if _contains_any(raw_text, ("\u5370\u7ae0", "\u7528\u5370", "\u501f\u7ae0")):
        return (
            "\u8fd9\u4e2a\u6211\u6309\u5185\u90e8\u95ee\u7b54\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002\n"
            "\u3010\u5370\u7ae0/\u7528\u5370\u6d41\u7a0b\u3011\u5efa\u8bae\u6309\u8fd9\u4e2a\u987a\u5e8f\u786e\u8ba4\uff1a\n"
            "1. \u660e\u786e\u7528\u5370\u4e8b\u9879\u3001\u5408\u540c/\u51fd\u4ef6\u7248\u672c\u548c\u7528\u5370\u4e3b\u4f53\uff1b\n"
            "2. \u8d70\u7528\u5370/\u501f\u7ae0\u5ba1\u6279\uff0c\u540c\u6b65\u9644\u5bf9\u5e94\u6750\u6599\uff1b\n"
            "3. \u5ba1\u6279\u901a\u8fc7\u540e\u5230\u5370\u7ae0\u7ba1\u7406\u4eba\u5904\u7528\u5370\uff0c\u7559\u5b58\u53f0\u8d26\u548c\u626b\u63cf\u4ef6\u3002\n"
            "\u82e5\u662f\u7d27\u6025\u7528\u5370\uff0c\u5148\u627e\u7efc\u5408\u7ba1\u7406\u90e8\u786e\u8ba4\u53ef\u5426\u8d70\u52a0\u6025\u3002"
        )
    return (
        "\u8fd9\u4e2a\u6211\u6309\u5185\u90e8\u95ee\u7b54\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002"
        "\u76ee\u524d\u8fd8\u6ca1\u6709\u63a5\u5165\u5b8c\u6574\u5185\u90e8\u8d44\u6599\u5e93\uff0c\u4f60\u53ef\u4ee5\u628a\u5236\u5ea6\u6216\u6d41\u7a0b\u6587\u4ef6\u53d1\u6211\uff0c\u6211\u4f1a\u6309\u8d44\u6599\u5e93\u53e3\u5f84\u6574\u7406\u7b54\u590d\u3002"
    )


def _monthly_status_query_reply(raw_text: str) -> str:
    return (
        "\u8fd9\u53e5\u6211\u6309\u3010\u6708\u62a5\u72b6\u6001\u67e5\u8be2\u3011\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002"
        "\u6211\u4f1a\u8d70\u6708\u62a5\u586b\u62a5\u8bb0\u5f55\u67e5\u8be2\uff0c\u4e0d\u4f1a\u628a\u8fd9\u7c7b\u8bdd\u8bb0\u6210\u4eca\u65e5\u5de5\u4f5c\u3002"
    )


def _legal_research_reply(raw_text: str) -> str:
    topic = _clean_topic(raw_text)
    research_topic = topic or "\u8be5\u6cd5\u5f8b\u95ee\u9898"
    if _contains_any(raw_text, ("\u4f18\u5148\u53d7\u507f\u6743", "\u5efa\u8bbe\u5de5\u7a0b\u4ef7\u6b3e")):
        return (
            "\u8fd9\u53e5\u6211\u6309\u3010\u6cd5\u5f8b\u7814\u7a76\u3011\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002\n"
            "\u3010\u521d\u6b65\u7814\u7a76\u53e3\u5f84\u3011\u5efa\u8bbe\u5de5\u7a0b\u4ef7\u6b3e\u4f18\u5148\u53d7\u507f\u6743\u7684\u6838\u5fc3\u901a\u5e38\u770b\u4e09\u4ef6\u4e8b\uff1a\n"
            "1. \u6743\u5229\u57fa\u7840\uff1a\u662f\u5426\u5c5e\u4e8e\u5efa\u8bbe\u5de5\u7a0b\u4ef7\u6b3e\uff0c\u4e0d\u662f\u666e\u901a\u501f\u6b3e\u3001\u8d28\u4fdd\u8d23\u4efb\u6216\u7eaf\u635f\u5bb3\u8d54\u507f\uff1b\n"
            "2. \u8303\u56f4\u8fb9\u754c\uff1a\u4f18\u5148\u8303\u56f4\u901a\u5e38\u805a\u7126\u5de5\u7a0b\u6298\u4ef7/\u62cd\u5356\u4ef7\u6b3e\uff0c\u5229\u606f\u3001\u8fdd\u7ea6\u91d1\u7b49\u9700\u5355\u72ec\u770b\u88c1\u5224\u53e3\u5f84\uff1b\n"
            "3. \u671f\u9650\u4e0e\u884c\u4f7f\uff1a\u91cd\u70b9\u6838\u5bf9\u5de5\u7a0b\u7ae3\u5de5/\u7ed3\u7b97/\u5e94\u4ed8\u8282\u70b9\u53ca\u662f\u5426\u5728\u6cd5\u5b9a\u671f\u95f4\u5185\u4e3b\u5f20\u3002\n"
            "\u3010\u4e0b\u4e00\u6b65\u3011\u771f\u6b63\u51fa\u6b63\u5f0f\u610f\u89c1\u524d\uff0c\u8981\u628a\u6700\u9ad8\u6cd5\u53f8\u6cd5\u89e3\u91ca\u3001\u5f53\u5730\u9ad8\u9662\u53e3\u5f84\u548c\u8fd1\u671f\u7c7b\u6848\u88c1\u5224\u4e00\u8d77\u6838\u4e00\u904d\u3002"
        )
    return (
        "\u8fd9\u53e5\u6211\u6309\u3010\u6cd5\u5f8b\u7814\u7a76\u3011\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002\n"
        f"\u3010\u7814\u7a76\u4e3b\u9898\u3011{research_topic}\n"
        "\u3010\u5efa\u8bae\u8def\u5f84\u3011\n"
        "1. \u5148\u786e\u5b9a\u6cd5\u5f8b\u5173\u7cfb\u548c\u8bf7\u6c42\u6743\u57fa\u7840\uff1b\n"
        "2. \u518d\u627e\u6700\u9ad8\u6cd5\u53f8\u6cd5\u89e3\u91ca\u3001\u6307\u5bfc\u6027\u6848\u4f8b\u548c\u5730\u65b9\u9ad8\u9662\u53c2\u8003\u53e3\u5f84\uff1b\n"
        "3. \u6700\u540e\u628a\u76f8\u540c\u4e8b\u5b9e\u7ed3\u6784\u7684\u7c7b\u6848\u88c1\u5224\u62c6\u6210\u201c\u652f\u6301/\u4e0d\u652f\u6301/\u533a\u5206\u6761\u4ef6\u201d\u3002\n"
        "\u76ee\u524d\u8fd8\u6ca1\u6709\u63a5\u5165\u6b63\u5f0f\u6848\u4f8b\u5e93\u68c0\u7d22\uff0c\u6211\u5148\u7ed9\u4f60\u7814\u7a76\u6846\u67b6\uff1b\u63a5\u4e0b\u6765\u53ef\u4ee5\u628a\u68c0\u7d22/RAG\u63a5\u6210\u5de5\u5177\u3002"
    )


def _clean_topic(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    for marker in (
        "\u5e2e\u6211",
        "\u67e5\u4e00\u4e0b",
        "\u7814\u7a76\u4e00\u4e0b",
        "\u770b\u4e00\u4e0b",
        "\u6700\u65b0",
        "\u88c1\u5224\u89c2\u70b9",
        "\u88c1\u5224\u89c4\u5219",
        "\u5417",
        "\uff1f",
        "?",
    ):
        text = text.replace(marker, "")
    return text.strip("\u3002\uff0c, ;\uff1b")


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)
