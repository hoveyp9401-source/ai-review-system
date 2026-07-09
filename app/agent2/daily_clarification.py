from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re

from app.agent2.context_pack import Agent2ContextPack, DailyDraftItemFrame
from app.agent2.daily_state import FIELD_LABELS, PENDING_DAILY_CANDIDATE_KEY


@dataclass(frozen=True)
class DailyClarificationCandidate:
    item: DailyDraftItemFrame
    reason: str
    score: float


@dataclass(frozen=True)
class DailyCandidateClarification:
    reply_text: str
    pending_payload: dict[str, object]


def build_daily_candidate_clarification_reply(
    *,
    raw_text: str,
    context_pack: Agent2ContextPack | None,
) -> str:
    clarification = build_daily_candidate_clarification(
        raw_text=raw_text,
        context_pack=context_pack,
    )
    return clarification.reply_text if clarification is not None else ""


def build_daily_candidate_clarification(
    *,
    raw_text: str,
    context_pack: Agent2ContextPack | None,
) -> DailyCandidateClarification | None:
    text = str(raw_text or "").strip()
    if not text or context_pack is None or context_pack.daily_draft is None:
        return None
    if _looks_like_non_daily_chatter(text):
        return None
    if not _looks_like_short_ambiguous_daily_fragment(text):
        return None
    candidate = _best_candidate(text, list(context_pack.daily_draft.items))
    if candidate is None:
        return None
    item = candidate.item
    if candidate.reason == "ordinal":
        reply_text = (
            "我没直接改日报。\n"
            f"你刚才只说「{text}」，我只能定位到当前草稿第 {item.global_index} 项：\n"
            f"【{item.field_label}{item.field_index}】{item.text}\n\n"
            "要处理这条，直接继续说具体改法，比如“改成……”或“删掉”。"
        )
    else:
        reply_text = (
            "我没直接改日报，怕把草稿改错。\n"
            f"你刚才说「{text}」，当前草稿里最接近的是：\n"
            f"【{item.field_label}{item.field_index}】{item.text}\n\n"
            "要改这条，直接说“改成……”或“把里面的 XX 改成 XX”；如果是新内容，把完整句发我。"
        )
    return DailyCandidateClarification(
        reply_text=reply_text,
        pending_payload={
            "type": "daily_candidate_focus",
            "field": item.field,
            "field_label": item.field_label,
            "field_index": item.field_index,
            "global_index": item.global_index,
            "item_id": item.item_id,
            "text": item.text,
            "source_text": text,
            "reason": candidate.reason,
            "score": round(candidate.score, 4),
        },
    )


def build_pending_daily_candidate_focus_reply(
    *,
    raw_text: str,
    context_pack: Agent2ContextPack | None,
) -> str:
    if context_pack is None:
        return ""
    text = str(raw_text or "").strip()
    if not _looks_like_focus_confirmation(text):
        return ""
    pending = _pending_candidate_from_context(context_pack)
    if pending is None:
        return ""
    label = pending.get("field_label") or _field_label(str(pending.get("field") or ""))
    index = pending.get("field_index") or pending.get("item_index") or ""
    content = str(pending.get("text") or "").strip()
    if not content:
        return ""
    return (
        "我先只定位到这条，没有提交或修改日报。\n"
        f"【{label}{index}】{content}\n\n"
        "要改这条，继续说具体改法，比如“括号删掉”“改成……”；如果是要提交日报，请说“确认提交日报”。"
    )


def _pending_candidate_from_context(context_pack: Agent2ContextPack) -> dict[str, object] | None:
    for action in context_pack.recent_actions:
        if action.action_type != "pending_daily_candidate":
            continue
        return {
            "field": action.field,
            "field_label": _field_label(action.field),
            "field_index": action.item_index,
            "item_index": action.item_index,
            "item_id": action.item_id,
            "text": action.text,
        }
    return None


def _looks_like_focus_confirmation(text: str) -> bool:
    return _compact(text) in {
        "对",
        "对的",
        "是",
        "是的",
        "嗯",
        "恩",
        "好",
        "好的",
        "可以",
        "确定",
        "没错",
        "就是这个",
        "就这个",
        "就这条",
        "这个",
        "这条",
    }


def _field_label(field: str) -> str:
    return FIELD_LABELS.get(field, "")


def _looks_like_short_ambiguous_daily_fragment(text: str) -> bool:
    compact = _compact(text)
    if not compact or len(compact) > 12:
        return False
    if any(marker in compact for marker in ("改成", "改为", "删掉", "删除", "去掉", "清空", "今日工作", "明日计划", "问题风险")):
        return False
    return True


def _looks_like_non_daily_chatter(text: str) -> bool:
    compact = _compact(text)
    if compact in {
        "哈哦",
        "哎",
        "唉",
        "额",
        "呃",
        "哦",
        "哦哦",
        "嗯",
        "嗯嗯",
    }:
        return True
    if any(marker in compact for marker in ("吃屎", "拉屎", "一坨屎", "傻逼")):
        return True
    if any(marker in compact for marker in ("穿啥", "穿什么", "吃啥", "吃什么")):
        return True
    return False


def _best_candidate(text: str, items: list[DailyDraftItemFrame]) -> DailyClarificationCandidate | None:
    if not items:
        return None
    ordinal = _ordinal_reference(text)
    if ordinal and 1 <= ordinal <= len(items):
        return DailyClarificationCandidate(item=items[ordinal - 1], reason="ordinal", score=1.0)

    scored: list[DailyClarificationCandidate] = []
    for item in items:
        score = _item_similarity(text, item.text)
        if score >= 0.34:
            scored.append(DailyClarificationCandidate(item=item, reason="fuzzy", score=score))
    if not scored:
        return None
    scored.sort(key=lambda candidate: candidate.score, reverse=True)
    if len(scored) >= 2 and scored[0].score - scored[1].score < 0.08:
        return None
    return scored[0]


def _item_similarity(raw_text: str, item_text: str) -> float:
    query = _compact(raw_text)
    target = _compact(_strip_parenthetical(item_text))
    if not query or not target:
        return 0.0
    direct = SequenceMatcher(None, query, target).ratio()
    longest = _longest_common_substring_len(query, target)
    coverage = longest / max(len(query), 1)
    containment = 0.0
    if query in target:
        containment = 1.0
    elif any(len(piece) >= 2 and piece in target for piece in _query_pieces(query)):
        containment = 0.55
    return max(direct, coverage, containment)


def _query_pieces(query: str) -> list[str]:
    return [query[start : start + size] for size in range(min(4, len(query)), 1, -1) for start in range(0, len(query) - size + 1)]


def _longest_common_substring_len(left: str, right: str) -> int:
    best = 0
    for i in range(len(left)):
        for j in range(i + 1, len(left) + 1):
            if j - i <= best:
                continue
            if left[i:j] in right:
                best = j - i
    return best


def _ordinal_reference(text: str) -> int:
    compact = _compact(text)
    if compact in {"第一", "第一个", "第一条", "第一项", "1", "1条", "第1", "第1条"}:
        return 1
    if compact in {"第二", "第二个", "第二条", "第二项", "2", "2条", "第2", "第2条"}:
        return 2
    if compact in {"第三", "第三个", "第三条", "第三项", "3", "3条", "第3", "第3条"}:
        return 3
    match = re.fullmatch(r"第?([0-9])(?:个|条|项)?", compact)
    if match:
        return int(match.group(1))
    return 0


def _strip_parenthetical(text: str) -> str:
    return re.sub(r"[（(][^（）()]+[）)]", "", str(text or ""))


def _compact(text: str) -> str:
    return re.sub(r"[\s\u3000:：,，.。;；!！?？()（）\[\]【】\"'“”‘’]+", "", str(text or "")).lower()
