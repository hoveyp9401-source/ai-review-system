from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ai_review_agent2_recent_context")


def compact_message_text(text: str) -> str:
    return "".join(str(text or "").split())


def webhook_event_text(event: Any) -> str:
    payload = getattr(event, "payload", None) or {}
    if not isinstance(payload, dict):
        return ""
    text_payload = payload.get("text")
    if isinstance(text_payload, dict) and text_payload.get("content"):
        return str(text_payload.get("content") or "").strip()
    content = payload.get("content")
    if isinstance(content, dict) and content.get("recognition"):
        return str(content.get("recognition") or "").strip()
    for key in ("raw_input", "raw_text", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def looks_like_recent_case_context_text(text: str) -> bool:
    value = str(text or "")
    if "案" not in value and "被告" not in value:
        return False
    return any(
        marker in value
        for marker in (
            "有多少",
            "多少",
            "几件",
            "几个",
            "统计",
            "原告",
            "被告",
            "存量",
            "新增",
            "下降率",
            "同比",
            "环比",
            "团队",
            "部门",
            "法务",
        )
    )


async def load_recent_case_context_messages(
    *,
    session: Any,
    dingtalk_user_id: str,
    current_message_id: str = "",
    current_text: str = "",
    limit: int = 8,
) -> list[dict[str, str]]:
    if not dingtalk_user_id:
        return []
    try:
        from sqlalchemy import desc, select

        from app.models import WebhookEvent

        rows = (
            await session.execute(
                select(WebhookEvent)
                .where(WebhookEvent.dingtalk_user_id == dingtalk_user_id)
                .order_by(desc(WebhookEvent.received_at))
                .limit(max(1, limit) + 4)
            )
        ).scalars().all()
    except Exception as exc:
        logger.info("skipped recent case context lookup: %s", exc)
        return []

    result: list[dict[str, str]] = []
    current_compact = compact_message_text(current_text)
    for row in rows:
        if current_message_id and str(getattr(row, "external_message_id", "") or "") == current_message_id:
            continue
        text = webhook_event_text(row)
        if not text or compact_message_text(text) == current_compact:
            continue
        if not looks_like_recent_case_context_text(text):
            continue
        result.append({"text": text, "received_at": str(getattr(row, "received_at", "") or "")})
        if len(result) >= limit:
            break
    return result
