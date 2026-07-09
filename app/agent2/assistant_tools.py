import json
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from app.agent2.assistant_responder import AssistantReply
from app.agent2.rag_qa import build_rag_qa_reply
from app.agent2.reply_composer import compose_assistant_reply
from app.utils.json import extract_json_object

if TYPE_CHECKING:
    from app.agent2.context_pack import Agent2ContextPack
    from app.llm.client import LLMClient


TOOL_REPLY_TYPES = {"internal_qa", "legal_research"}


@dataclass(frozen=True)
class AssistantToolReplyResult:
    text: str
    source: str
    fallback_used: bool
    error: str = ""


async def build_tool_assisted_reply(
    *,
    raw_text: str,
    assistant_reply: AssistantReply | None,
    llm_client: "LLMClient",
    context_pack: "Agent2ContextPack | None" = None,
) -> AssistantToolReplyResult:
    fallback = _fallback_text(raw_text=raw_text, assistant_reply=assistant_reply, context_pack=context_pack)
    if assistant_reply is None or assistant_reply.reply_type not in TOOL_REPLY_TYPES:
        return AssistantToolReplyResult(text=fallback, source="static", fallback_used=True)
    rag_reply = build_rag_qa_reply(
        raw_text=raw_text,
        context_pack=context_pack,
        reply_type=assistant_reply.reply_type,
    )
    if rag_reply is not None:
        return AssistantToolReplyResult(text=rag_reply.text, source=rag_reply.source, fallback_used=False)

    try:
        output = await llm_client.complete_json(
            system_prompt=_system_prompt(assistant_reply.reply_type),
            user_prompt=_user_prompt(raw_text=raw_text, assistant_reply=assistant_reply, context_pack=context_pack),
            model=_model_for_reply(llm_client, assistant_reply.reply_type),
            thinking_enabled=_thinking_for_reply(llm_client, assistant_reply.reply_type),
            timeout_seconds=_timeout_for_reply(llm_client, assistant_reply.reply_type),
            max_retries=0,
        )
        reply = _parse_reply_text(output)
        if not reply:
            return AssistantToolReplyResult(text=fallback, source="fallback_empty", fallback_used=True)
        reply_text = _ensure_non_daily_prefix(reply, assistant_reply.reply_type)
        return AssistantToolReplyResult(
            text=_ensure_case_count_details(reply_text, context_pack=context_pack),
            source="llm",
            fallback_used=False,
        )
    except (TimeoutError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        return AssistantToolReplyResult(
            text=fallback,
            source="fallback_error",
            fallback_used=True,
            error=exc.__class__.__name__,
        )


def _fallback_text(
    *,
    raw_text: str,
    assistant_reply: AssistantReply | None,
    context_pack: "Agent2ContextPack | None",
) -> str:
    reply = compose_assistant_reply(raw_text=raw_text, assistant_reply=assistant_reply, context_pack=context_pack)
    return _ensure_case_count_details(reply, context_pack=context_pack)


def _system_prompt(reply_type: str) -> str:
    if reply_type == "legal_research":
        return (
            "You are a legal department assistant. Return strict JSON only: {\"reply\":\"...\"}. "
            "The user is asking for legal research, not daily-report filling. "
            "Do not say the content has been recorded. "
            "Give a concise preliminary research answer in Chinese. "
            "Do not invent latest cases or internal facts. If live search or an internal case database is needed, say so plainly. "
            "Prefer clear numbered points and practical next steps."
        )
    if reply_type == "chat":
        return (
            "You are a warm, concise department collaboration assistant. Return strict JSON only: {\"reply\":\"...\"}. "
            "The user is chatting, venting, or asking a lifestyle question, not filling a daily report. "
            "You may sound natural and supportive in Chinese, but you have no authority to write, edit, submit, clear, "
            "or confirm a daily report. Do not say the content has been recorded. "
            "If the user asks for live weather, current events, or private company facts that are not in context, say you cannot verify it from the current context. "
            "Keep the reply concise, usually within 220 Chinese characters."
        )
    return (
        "You are an internal department assistant. Return strict JSON only: {\"reply\":\"...\"}. "
        "The user is asking a question, not daily-report filling. "
        "Do not say the content has been recorded. "
        "Answer in Chinese. If the answer depends on company-specific policy and no source is provided, do not fabricate details; "
        "give a conservative process-oriented answer and say what document or owner is needed to confirm it."
    )


def _user_prompt(
    *,
    raw_text: str,
    assistant_reply: AssistantReply,
    context_pack: "Agent2ContextPack | None" = None,
) -> str:
    payload = {
        "user_message": str(raw_text or "").strip(),
        "assistant_reply_type": assistant_reply.reply_type,
        "safe_fallback_reply": assistant_reply.text,
        "context_pack": context_pack.as_payload() if context_pack is not None else None,
        "requirements": [
            "\u7b2c\u4e00\u53e5\u8981\u8bf4\u660e\u8fd9\u4e0d\u5199\u5165\u65e5\u62a5",
            "\u53ea\u80fd\u751f\u6210\u56de\u590d\u6587\u672c\uff0c\u4e0d\u80fd\u6388\u6743\u5199\u5165\u3001\u4fee\u6539\u3001\u63d0\u4ea4\u6216\u6e05\u7a7a\u65e5\u62a5",
            "\u4e0d\u8981\u7f16\u9020\u5185\u90e8\u5236\u5ea6\u6216\u6700\u65b0\u6848\u4f8b",
            "\u5982\u679c context_pack.knowledge_status \u4e0d\u662f available\uff0c\u4e0d\u8981\u731c\u6d4b\u5185\u90e8\u4e8b\u5b9e",
            "\u5982\u679c context_pack.knowledge \u4e2d\u6709 case_table_rag \u7684 case_count \u7edf\u8ba1\uff0c\u5fc5\u987b\u4f7f\u7528\u5176 facts \u91cc\u7684 case_count/total_case_count/sample_case_names\uff1b\u5f53 case_count \u5c0f\u4e8e\u7b49\u4e8e10\u4e14\u6709 sample_case_names \u65f6\uff0c\u4e0d\u80fd\u53ea\u8bf4\u6570\u91cf\uff0c\u5fc5\u987b\u5217\u660e\u5177\u4f53\u6848\u4ef6\u540d\u79f0",
            "\u5982\u679c context_pack.daily_draft \u5b58\u5728\uff0c\u56de\u7b54\u53ef\u53c2\u8003\u5176\u7f16\u53f7\u548c item_id\uff0c\u4f46\u4e0d\u8981\u8bf4\u5df2\u5199\u5165\u65e5\u62a5",
            "\u56de\u590d\u5c3d\u91cf\u63a7\u5236\u5728800\u5b57\u4ee5\u5185",
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _model_for_reply(llm_client: "LLMClient", reply_type: str) -> str:
    settings = llm_client.settings
    if reply_type == "legal_research":
        return str(getattr(settings, "llm_high_risk_model", "") or getattr(settings, "llm_model", ""))
    return str(getattr(settings, "llm_intent_model", "") or getattr(settings, "llm_model", ""))


def _thinking_for_reply(llm_client: "LLMClient", reply_type: str) -> bool:
    settings = llm_client.settings
    if reply_type == "legal_research":
        return bool(getattr(settings, "llm_high_risk_thinking", False))
    return bool(getattr(settings, "llm_intent_thinking", False))


def _timeout_for_reply(llm_client: "LLMClient", reply_type: str) -> float:
    settings = llm_client.settings
    if reply_type == "legal_research":
        return max(30.0, float(getattr(settings, "llm_high_risk_timeout_seconds", 15.0) or 15.0))
    return float(getattr(settings, "llm_intent_timeout_seconds", 8.0) or 8.0)


def _parse_reply_text(output: str) -> str:
    data: Any = extract_json_object(output)
    if not isinstance(data, dict):
        return ""
    return str(data.get("reply") or "").strip()


def _ensure_non_daily_prefix(reply: str, reply_type: str) -> str:
    head = reply[:40]
    if reply.startswith(("\u8fd9\u53e5", "\u8fd9\u4e2a")) and (
        "\u4e0d\u5199\u5165\u65e5\u62a5" in head or "\u4e0d\u8bb0\u5165\u65e5\u62a5" in head
    ):
        return reply
    if reply_type == "legal_research":
        prefix = "\u8fd9\u53e5\u6211\u6309\u3010\u6cd5\u5f8b\u7814\u7a76\u3011\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002"
    elif reply_type == "chat":
        prefix = "\u8fd9\u53e5\u6211\u6309\u3010\u95f2\u804a\u3011\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002"
    else:
        prefix = "\u8fd9\u53e5\u6211\u6309\u3010\u5185\u90e8\u95ee\u7b54\u3011\u5904\u7406\uff0c\u4e0d\u5199\u5165\u65e5\u62a5\u3002"
    return f"{prefix}\n{reply}"


def _ensure_case_count_details(reply: str, *, context_pack: "Agent2ContextPack | None") -> str:
    facts = _small_case_count_facts(context_pack)
    if facts is None:
        return reply
    sample_names = _sample_case_names(facts)
    if not sample_names:
        return reply
    missing_names = [name for name in sample_names if name and name not in reply]
    if not missing_names:
        return reply

    count = _int_value(facts.get("case_count"))
    total_count = _int_value(facts.get("total_case_count"))
    scope = _case_count_scope_label(facts)
    detail_lines = [f"{index}. {name}" for index, name in enumerate(sample_names, start=1)]
    if str(count) not in reply:
        if total_count and total_count != count:
            intro = f"\u6839\u636e\u6848\u4ef6\u5e95\u8868\u7edf\u8ba1\uff0c{scope}\u4e3a{count}\u4ef6\uff08\u8be5\u8303\u56f4\u603b\u8ba1{total_count}\u4ef6\uff09\u3002"
        else:
            intro = f"\u6839\u636e\u6848\u4ef6\u5e95\u8868\u7edf\u8ba1\uff0c{scope}\u4e3a{count}\u4ef6\u3002"
        return "\n".join([reply.rstrip(), intro, "\u5177\u4f53\u6848\u4ef6\uff1a", *detail_lines])
    return "\n".join([reply.rstrip(), "\u5177\u4f53\u6848\u4ef6\uff1a", *detail_lines])


def _small_case_count_facts(context_pack: "Agent2ContextPack | None") -> dict[str, Any] | None:
    if context_pack is None:
        return None
    for evidence in getattr(context_pack, "knowledge", ()) or ():
        facts = getattr(evidence, "facts", None)
        if not isinstance(facts, dict) or "case_count" not in facts:
            continue
        count = _int_value(facts.get("case_count"))
        if count <= 0 or count > 10:
            continue
        return facts
    return None


def _sample_case_names(facts: dict[str, Any]) -> list[str]:
    values = facts.get("sample_case_names") or ()
    names: list[str] = []
    if isinstance(values, (list, tuple)):
        for value in values:
            name = str(value or "").strip()
            if name and name not in names:
                names.append(name)
    count = _int_value(facts.get("case_count"))
    return names[:count] if count > 0 else names


def _case_count_scope_label(facts: dict[str, Any]) -> str:
    assignee = str(facts.get("assignee_name") or "").strip()
    table_label = str(facts.get("table_label") or "").strip()
    unclosed_only = bool(facts.get("unclosed_only"))
    if assignee or table_label:
        scope = f"{assignee}{table_label}\u6848\u4ef6"
    else:
        scope = "\u5168\u90e8\u6848\u4ef6"
    if unclosed_only:
        return f"{scope}\u4e2d\u672a\u7ed3\u6848\u7684"
    return scope


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
