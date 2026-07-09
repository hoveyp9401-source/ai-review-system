from __future__ import annotations

from dataclasses import dataclass, field
import random
from typing import TYPE_CHECKING, Any

from app.workflows.intake import WORKFLOW_CHAT

if TYPE_CHECKING:
    from app.agent2.context_pack import Agent2ContextPack
    from app.agent2.personal_memory import PersonalMemoryProfile


_RANDOM = random.SystemRandom()


@dataclass(frozen=True)
class ChatCapabilityReply:
    """Read-only chat response for Agent2 conversational turns."""

    text: str
    reply_type: str = "chat"
    workflow: str = WORKFLOW_CHAT
    source: str = "static"
    write_effects: tuple[str, ...] = ()
    memory_notes: tuple[str, ...] = field(default_factory=tuple)

    def as_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "reply_type": self.reply_type,
            "workflow": self.workflow,
            "source": self.source,
            "write_effects": list(self.write_effects),
            "memory_notes": list(self.memory_notes),
        }


def build_chat_reply(
    raw_text: str,
    *,
    fallback: str = "",
    context_pack: "Agent2ContextPack | None" = None,
) -> ChatCapabilityReply:
    """Build a friendly read-only chat reply.

    Chat can read short-term context and user-scoped memory, but it never
    authorizes daily-report writes or other business side effects.
    """

    profile = _profile(context_pack)
    text = str(raw_text or "").strip()
    memory_hint = _memory_hint(profile)
    notes = tuple(str(note or "") for note in getattr(profile, "notes", ()) or ()) if profile else ()

    if _looks_like_system_feedback(text):
        reply = _join_reply(_system_feedback_reply(text, profile), memory_hint)
        return ChatCapabilityReply(text=reply, memory_notes=notes)

    if _looks_like_daily_meta_status(text):
        name = _friendly_name(profile)
        prefix = _address(name, "行，你说。")
        reply = _join_reply(
            prefix,
            _pick_variant(
                text,
                (
                    "这句本身不写入日报。你直接把今天做了什么、有什么问题、明天准备干啥发来，我帮你归类整理。",
                    "这句我先当状态回执，不写入日报。后面你说具体事项就行，我会按今日工作、问题/风险、明日计划拆好。",
                    "这句不写入日报。内容你丢过来，我不会把“写日报了”这几个字写进去，只会记录你后面说的具体工作。",
                    "好，你说呗。正经日报内容我来整理；这句只是开场，不写入日报。",
                    "收到，准备好了。你发具体内容，我来放到对应栏目里；这句不写入日报，也不入库。",
                ),
            ),
            memory_hint,
        )
        return ChatCapabilityReply(text=reply, source="static_random", memory_notes=notes)

    if _looks_like_absurd_or_vulgar_joke(text):
        name = _friendly_name(profile)
        prefix = _address(name, "这个我先当玩笑。")
        reply = _join_reply(
            prefix,
            _pick_variant(
                text,
                (
                    "这句不写入日报。正经说，今天干了啥、卡在哪、明天准备做什么，直接发我。",
                    "这句不写入日报。要写的话，说点能给领导看的：今日工作、问题/风险、明日计划。",
                    "这句我拦住，不写入日报也不改草稿。你把今天的正经事项丢过来，我帮你收拾成日报。",
                    "我懂你在开玩笑，但这句不写入日报，也不入库。来，说说今天具体推进了哪几件事。",
                    "这句不写入日报，也不算工作内容。要继续日报，直接说“今天完成了……”我来整理。",
                ),
            ),
            memory_hint,
        )
        return ChatCapabilityReply(text=reply, source="static_random", memory_notes=notes)

    if _looks_like_chat_request(text):
        name = _friendly_name(profile)
        prefix = _address(name, "可以说两句。")
        reply = _join_reply(
            prefix,
            _pick_variant(
                text,
                (
                    "但我不会把闲聊当任务处理。这句不写入日报；要继续办事，直接说日报、月报、案件、出差或要查的问题。",
                    "我先把这句当闲聊拦住，不写入日报。后面你直接发具体工作、问题风险、明日计划或查询内容就行。",
                    "这句只按闲聊处理，不写入日报，也不改任何数据。要落日报或查数据，直接说具体事项。",
                ),
            ),
            memory_hint,
        )
        return ChatCapabilityReply(text=reply, source="static_random", memory_notes=notes)

    if _looks_like_tired_or_frustrated(text):
        name = _friendly_name(profile)
        prefix = _address(name, _pick_variant(text, ("我听到了。", "懂，有点烦。", "收到，先稳一下。")))
        reply = _join_reply(
            prefix,
            _pick_variant(
                text,
                (
                    "这句不写入日报。要是系统哪一步不对，直接说问题；要落日报时，说具体工作、问题或明日计划。",
                    "我先不把它当工作内容，也不写入日报。你继续说要记录的事项，或者直接问要查的月报/案件数据。",
                    "这句我当反馈/情绪处理，不写入日报、不动草稿。后面发具体动作，我再按对应任务处理。",
                ),
            ),
            memory_hint,
        )
        return ChatCapabilityReply(text=reply, source="static_random", memory_notes=notes)

    if _looks_like_thanks(text):
        reply = _join_reply(
            _pick_variant(
                text,
                (
                    "收到，不客气。这句不写入日报；你继续说工作内容就行。",
                    "好，我知道了。这句只当闲聊，不写入日报。",
                    "没问题。这句不写入日报、不改草稿，你继续发要处理的事。",
                ),
            ),
            memory_hint,
        )
        return ChatCapabilityReply(text=reply, source="static_random", memory_notes=notes)

    if _looks_like_greeting(text):
        name = _friendly_name(profile)
        prefix = _address(name, _pick_variant(text, ("在呢。", "我在。", "来了。")))
        reply = _join_reply(
            prefix,
            _pick_variant(
                text,
                (
                    "这句我当闲聊处理，不写入日报。你可以继续直接说日报、月报、出差、案件或要查的问题。",
                    "这句不写入日报。要记录工作或查询数据，直接说具体内容就行。",
                    "我在。这句只作闲聊回执，不写入日报、不动草稿。",
                ),
            ),
            memory_hint,
        )
        return ChatCapabilityReply(text=reply, source="static_random", memory_notes=notes)

    reply = _join_reply(
        _pick_variant(
            text,
            (
                "这句我先当闲聊处理，不写入日报。要继续的话，直接说今日工作、问题风险、明日计划，或者问月报/案件数据。",
                "收到，这句不写入日报，也不改草稿。你后面直接发具体工作或查询问题就行。",
                "我先不把这句当任务执行，也不写入日报。要记录日报就说具体事项；要查询数据就直接问。",
                "这句只作闲聊回执，不写入日报、不写库。继续日报、月报、案件或出差事项时，直接说内容。",
            ),
        )
        or fallback,
        memory_hint,
    )
    return ChatCapabilityReply(text=reply, source="static_random", memory_notes=notes)


def _system_feedback_reply(text: str, profile: "PersonalMemoryProfile | None") -> str:
    name = _friendly_name(profile)
    if _contains_any(text, ("记录", "记住", "上下文", "历史", "memory", "Memory", "rag", "RAG")):
        prefix = f"{name}，我理解，你是在问我到底记了什么。" if name else "我理解，你是在问我到底记了什么。"
        return _join_reply(
            prefix,
            "这句我不写入日报。当前我只把活跃任务、日报草稿、最近上下文和你的个人填报习惯当作只读参考，用来判断下一句该进日报、月报、出差、案件还是闲聊。",
            "这些记忆不会因为一句闲聊直接改动，也不会授权我擅自写入日报。",
        )
    prefix = f"{name}，我懂，你这句更像是在反馈系统体验，不是日报内容。" if name else "我懂，你这句更像是在反馈系统体验，不是日报内容。"
    return _join_reply(
        prefix,
        "我先不写入日报；当前任务上下文会保留，不会因为这句反馈把正在填的日报/月报弄丢。",
    )


def _profile(context_pack: "Agent2ContextPack | None") -> "PersonalMemoryProfile | None":
    return getattr(context_pack, "personal_memory", None) if context_pack is not None else None


def _friendly_name(profile: "PersonalMemoryProfile | None") -> str:
    if profile is None:
        return ""
    name = str(getattr(profile, "display_name", "") or "").strip()
    if not name:
        return ""
    return name if len(name) <= 4 else ""


def _memory_hint(profile: "PersonalMemoryProfile | None") -> str:
    if profile is None:
        return ""
    notes = set(getattr(profile, "notes", ()) or ())
    if "user_has_previous_plan_rollover_habit" in notes:
        return "我也会继续按你的历史习惯处理“昨天计划已完成”这类说法。"
    if getattr(profile, "active_habits", ()):
        return "你的个人填报习惯会继续按当前账号单独保留。"
    return ""


def _join_reply(*parts: str) -> str:
    return "\n".join(part.strip() for part in parts if str(part or "").strip())


def _pick_variant(text: str, variants: tuple[str, ...]) -> str:
    if not variants:
        return ""
    return _RANDOM.choice(variants)


def _address(name: str, text: str) -> str:
    return f"{name}，{text}" if name else text


def _looks_like_system_feedback(text: str) -> bool:
    return _contains_any(
        text,
        (
            "agent",
            "Agent",
            "机器人",
            "系统",
            "灰测",
            "测试",
            "好蠢",
            "一坨",
            "识别",
            "回复",
            "记录",
            "上下文",
            "记忆",
        ),
    )


def _looks_like_daily_meta_status(text: str) -> bool:
    compact = "".join(str(text or "").split())
    return compact in {
        "写日报了",
        "我写日报了",
        "已经写日报了",
        "我已经写日报了",
        "填日报了",
        "我填日报了",
        "开始写日报",
        "我要写日报了",
        "准备写日报",
    }


def _looks_like_absurd_or_vulgar_joke(text: str) -> bool:
    compact = "".join(str(text or "").split())
    if len(compact) > 16:
        return False
    return _contains_any(
        compact,
        (
            "吃屎",
            "拉屎",
            "一坨屎",
            "傻逼",
            "滚",
        ),
    )


def _looks_like_tired_or_frustrated(text: str) -> bool:
    return _contains_any(
        text,
        (
            "妈的",
            "烦",
            "累",
            "崩溃",
            "无语",
            "服了",
            "气死",
            "被气死",
            "害怕",
            "失望",
            "懒得测",
            "不想测",
            "受不了",
            "太难用",
            "用不了",
        ),
    )


def _looks_like_chat_request(text: str) -> bool:
    compact = "".join(str(text or "").split())
    if compact in {
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
            "我想聊",
            "想聊",
            "聊聊",
            "聊会",
            "聊一会",
            "和我聊",
            "跟我聊",
            "陪我聊",
            "说说话",
            "闲聊",
            "聊天",
        ),
    )


def _looks_like_thanks(text: str) -> bool:
    return _contains_any(text, ("谢谢", "谢了", "辛苦", "辛苦了"))


def _looks_like_greeting(text: str) -> bool:
    return _contains_any(text, ("你好", "早上好", "在吗", "在不在", "早"))


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)
