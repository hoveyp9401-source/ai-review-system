from __future__ import annotations

import json
from typing import Any

from app.agent2.assistant_responder import AssistantReply
from app.agent2.assistant_tools import build_tool_assisted_reply
from app.agent2.semantic_interpreter_v3 import report_type_from_meta_opening
from app.utils.json import extract_json_object
from app.workflows.intake import WORKFLOW_CHAT, WORKFLOW_INTERNAL_QA, WORKFLOW_LEGAL_RESEARCH


_INTENT_REPLY_TYPES = (
    ("internal_query", "internal_qa", WORKFLOW_INTERNAL_QA),
    ("legal_query", "legal_research", WORKFLOW_LEGAL_RESEARCH),
    ("chat", "chat", WORKFLOW_CHAT),
)


def has_bound_confirmation_pending(decision: Any) -> bool:
    update = getattr(decision, "context_update", None)
    return getattr(update, "bind_pending", None) is not None


def pending_lifecycle_reply(decision: Any) -> str:
    """Describe a trusted lifecycle-only result without claiming a business write."""

    update = getattr(decision, "context_update", None)
    invalidated_ids = tuple(
        getattr(update, "invalidated_pending_ids", ()) or ()
    )
    reason = str(
        getattr(update, "pending_invalidation_reason", "") or ""
    ).strip()
    if invalidated_ids and reason == "cancelled_by_user":
        return "好的，已取消这次待确认操作，原操作不会执行。"
    return ""


def has_pending_lifecycle_update(decision: Any) -> bool:
    update = getattr(decision, "context_update", None)
    return bool(tuple(getattr(update, "invalidated_pending_ids", ()) or ()))


def append_cognitive_clarification(reply: str, decision: Any) -> str:
    clarification = getattr(decision, "clarification_need", None)
    question = str(getattr(clarification, "question", "") or "").strip()
    base = str(reply or "").strip()
    if not question or question in base:
        return base
    return f"{base}\n\n{question}" if base else question


async def build_cognitive_side_reply_v3(
    *,
    decision: Any,
    llm_client: Any,
    context_pack: Any | None = None,
) -> str:
    """Compose a read-only reply from Agent2 semantic segments, never Shadow/Agent1."""

    segments = tuple(getattr(decision, "segments", ()) or ())
    for report_type, intent in (
        ("daily", "daily_report_exit"),
        ("weekly", "weekly_report_exit"),
        ("monthly", "monthly_report_exit"),
    ):
        if any(intent in tuple(getattr(segment, "intents", ()) or ()) for segment in segments):
            label = {"daily": "日报", "weekly": "周报", "monthly": "月报"}[report_type]
            return f"已退出{label}填写。{label}内容和提交状态都没有改变。"
    for report_type, intent in (
        ("daily", "daily_report"),
        ("weekly", "weekly_report"),
        ("monthly", "monthly_report"),
    ):
        matching = [
            str(getattr(segment, "text", "") or "").strip()
            for segment in segments
            if intent in tuple(getattr(segment, "intents", ()) or ())
            and str(getattr(segment, "text", "") or "").strip()
        ]
        if matching and report_type_from_meta_opening("\n".join(matching)) == report_type:
            return _report_opening_reply(report_type)
    for intent, reply_type, workflow in _INTENT_REPLY_TYPES:
        matching = [
            str(getattr(segment, "text", "") or "").strip()
            for segment in segments
            if intent in tuple(getattr(segment, "intents", ()) or ())
            and str(getattr(segment, "text", "") or "").strip()
        ]
        if not matching:
            continue
        side_text = "\n".join(dict.fromkeys(matching))
        fallback = {
            "chat": "我看到了你这部分消息。",
            "internal_qa": "这部分按内部问答处理；没有可靠来源时不会编造内部制度。",
            "legal_research": "这部分按法律研究处理；需要先核对可用事实和权威来源。",
        }[reply_type]
        if reply_type == "chat":
            return await _build_chat_reply_v3(
                side_text=side_text,
                llm_client=llm_client,
                fallback=fallback,
                context_pack=context_pack,
            )
        result = await build_tool_assisted_reply(
            raw_text=side_text,
            assistant_reply=AssistantReply(
                reply_type=reply_type,
                workflow=workflow,
                text=fallback,
            ),
            llm_client=llm_client,
            context_pack=context_pack,
        )
        return result.text
    return ""


def _report_opening_reply(report_type: str) -> str:
    if report_type == "daily":
        return (
            "已进入日报。你直接发今天具体完成的工作、遇到的问题或明天的计划。"
            "这句不会写进日报，只有后续具体内容才会落档；每次更新后我会返回当前完整日报。"
        )
    if report_type == "weekly":
        return (
            "已进入周报。请发本周完成、风险问题和下周计划；"
            "这句不会写进周报，后续内容会留在当前报告上下文。"
        )
    return (
        "已进入月报。请发本月完成、关键指标、风险问题和下月计划；"
        "这句不会写进月报，后续内容会留在当前报告上下文。"
    )


async def _build_chat_reply_v3(
    *,
    side_text: str,
    llm_client: Any,
    fallback: str,
    context_pack: Any | None,
) -> str:
    settings = getattr(llm_client, "settings", None)
    try:
        output = await llm_client.complete_json(
            system_prompt=(
                "You are Agent2's read-only chat reply composer. Return strict JSON only: "
                "{\"reply\":\"...\"}. Reply naturally and concisely in Chinese to only the supplied "
                "chat segment. Do not claim any business write, notification, or database action."
            ),
            user_prompt=json.dumps(
                {
                    "chat_segment": side_text,
                    "context": context_pack.as_payload() if context_pack is not None else None,
                },
                ensure_ascii=False,
            ),
            model=str(getattr(settings, "llm_intent_model", "") or getattr(settings, "llm_model", "")),
            thinking_enabled=bool(getattr(settings, "llm_intent_thinking", False)),
            timeout_seconds=float(getattr(settings, "llm_intent_timeout_seconds", 8.0) or 8.0),
            max_retries=0,
        )
        payload = extract_json_object(output)
        reply = str(payload.get("reply") or "").strip() if isinstance(payload, dict) else ""
        if reply:
            return reply
    except (TimeoutError, ValueError, KeyError, TypeError, RuntimeError):
        pass
    return fallback
