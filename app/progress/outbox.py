from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Callable

from app.repositories import create_progress_outbox_event_once


logger = logging.getLogger(__name__)


def progress_outbox_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "progress_enabled", False) and getattr(settings, "progress_outbox_enabled", False))


def progress_outbox_allowed_for_user(settings: Any, user: Any) -> bool:
    if not progress_outbox_enabled(settings):
        return False
    user_ids = _csv_set(getattr(settings, "progress_outbox_user_ids", ""))
    team_ids = _csv_set(getattr(settings, "progress_outbox_team_ids", ""))
    if not user_ids and not team_ids:
        return True
    user_id = str(getattr(user, "id", "") or "")
    team_id = str(getattr(user, "team_id", "") or "")
    return bool((user_id and user_id in user_ids) or (team_id and team_id in team_ids))


def _csv_set(value: Any) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def raw_text_hash(raw_text: str) -> str:
    return hashlib.sha256((raw_text or "").encode("utf-8")).hexdigest()


def build_progress_outbox_idempotency_key(
    *,
    event_type: str,
    source_type: str,
    source_id: str,
    report_id: str | None,
    text_hash: str,
) -> str:
    raw = json.dumps(
        {
            "event_type": event_type,
            "source_type": source_type,
            "source_id": source_id,
            "report_id": report_id or "",
            "raw_text_hash": text_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"progress-outbox:{digest}"


async def enqueue_daily_report_outbox_best_effort(
    *,
    session_factory: Callable[[], Any],
    settings: Any,
    user: Any,
    result: Any,
    raw_text: str,
    source: str,
    source_id: str,
    event_type: str = "daily_report_processed",
) -> None:
    if not progress_outbox_allowed_for_user(settings, user):
        return
    if not getattr(result, "report_saved", False) or not getattr(result, "report_id", None):
        return

    text_hash = raw_text_hash(raw_text)
    report_id_text = str(getattr(result, "report_id", "") or "")
    idempotency_key = build_progress_outbox_idempotency_key(
        event_type=event_type,
        source_type=source,
        source_id=source_id,
        report_id=report_id_text,
        text_hash=text_hash,
    )
    payload = {
        "source": source,
        "source_id": source_id,
        "report_id": report_id_text,
        "report_date": getattr(result, "report_date", None).isoformat() if getattr(result, "report_date", None) else "",
        "raw_text_len": len(raw_text or ""),
        "raw_text_hash": text_hash,
        "status": getattr(result, "status", ""),
        "reply_kind": getattr(result, "reply_kind", ""),
        "report_saved": bool(getattr(result, "report_saved", False)),
        "confirmation_type": getattr(result, "confirmation_type", ""),
        "confirmed_by_user": bool(getattr(result, "confirmed_by_user", False)),
        "today_work_count": len(getattr(result, "today_work", None) or []),
        "problems_count": len(getattr(result, "problems", None) or []),
        "tomorrow_plan_count": len(getattr(result, "tomorrow_plan", None) or []),
        "quality_warning": getattr(result, "quality_warning", None),
    }

    session = None
    try:
        async with session_factory() as session:
            await create_progress_outbox_event_once(
                session,
                event_type=event_type,
                source_type=source,
                source_id=source_id,
                user_id=getattr(user, "id", None),
                team_id=getattr(user, "team_id", None),
                report_id=uuid.UUID(report_id_text),
                report_date=getattr(result, "report_date", None),
                payload_json=payload,
                raw_text_hash=text_hash,
                idempotency_key=idempotency_key,
            )
            await session.commit()
    except Exception:
        if session is not None and hasattr(session, "rollback"):
            try:
                await session.rollback()
            except Exception:
                logger.exception("progress outbox rollback failed")
        logger.exception(
            "progress outbox enqueue failed source=%s source_id=%s report_id=%s",
            source,
            source_id,
            report_id_text,
        )
