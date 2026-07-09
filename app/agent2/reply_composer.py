from __future__ import annotations

from typing import TYPE_CHECKING

from app.agent2.assistant_responder import AssistantReply
from app.agent2.chat_capability import build_chat_reply

if TYPE_CHECKING:
    from app.agent2.context_pack import Agent2ContextPack
    from app.agent2.personal_memory import PersonalMemoryProfile


def compose_assistant_reply(
    *,
    raw_text: str,
    assistant_reply: AssistantReply | None,
    context_pack: "Agent2ContextPack | None" = None,
) -> str:
    """Render a user-facing assistant reply without changing workflow effects."""

    if assistant_reply is None:
        return build_chat_reply(
            raw_text,
            fallback="这句我先不写入日报，请补充说明。",
            context_pack=context_pack,
        ).text
    reply_type = str(assistant_reply.reply_type or "")
    if reply_type in {"small_talk", "chat"}:
        return build_chat_reply(raw_text, fallback=assistant_reply.text, context_pack=context_pack).text
    if reply_type == "clarify_intent":
        return _soften_clarification(assistant_reply.text, context_pack=context_pack)
    return assistant_reply.text or "这句我先不写入日报，请补充说明。"


def _soften_clarification(text: str, *, context_pack: "Agent2ContextPack | None") -> str:
    profile = _profile(context_pack)
    name = _friendly_name(profile)
    if not text:
        text = "这句我先不写入日报。你是想记成工作内容，还是想让我按问题/研究来处理？"
    if name and not text.startswith(name):
        return f"{name}，{text}"
    return text


def _profile(context_pack: "Agent2ContextPack | None") -> "PersonalMemoryProfile | None":
    return getattr(context_pack, "personal_memory", None) if context_pack is not None else None


def _friendly_name(profile: "PersonalMemoryProfile | None") -> str:
    if profile is None:
        return ""
    name = str(getattr(profile, "display_name", "") or "").strip()
    if not name:
        return ""
    return name if len(name) <= 4 else ""
