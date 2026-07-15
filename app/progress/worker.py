from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import uuid
from datetime import timedelta
from typing import Any, Callable

from app.config import get_settings
from app.repositories import (
    claim_pending_progress_outbox_events,
    mark_progress_outbox_failed,
    mark_progress_outbox_processed,
    recover_stale_progress_outbox_events,
)
from app.utils.time import now_in_timezone


logger = logging.getLogger(__name__)


def progress_worker_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "progress_enabled", False) and getattr(settings, "progress_worker_enabled", False))


async def process_progress_outbox_once(
    *,
    session_factory: Callable[[], Any],
    settings: Any,
    worker_id: str | None = None,
) -> int:
    if not progress_worker_enabled(settings):
        return 0
    worker_id = worker_id or _default_worker_id()
    now = now_in_timezone(getattr(settings, "timezone", "Asia/Shanghai"))
    batch_size = int(getattr(settings, "progress_worker_batch_size", 50) or 50)
    max_retries = int(getattr(settings, "progress_worker_max_retries", 5) or 5)
    stale_minutes = int(getattr(settings, "progress_worker_stale_lock_minutes", 10) or 10)

    async with session_factory() as session:
        recovered = await recover_stale_progress_outbox_events(
            session,
            stale_before=now - timedelta(minutes=max(1, stale_minutes)),
            now=now,
            limit=batch_size,
        )
        if recovered:
            logger.warning("progress worker recovered stale processing events count=%s", len(recovered))
        events = await claim_pending_progress_outbox_events(
            session,
            worker_id=worker_id,
            limit=batch_size,
            now=now,
        )
        await session.commit()

    processed = 0
    for event in events:
        async with session_factory() as session:
            try:
                event = await session.merge(event)
                await _process_event_noop(event)
                await mark_progress_outbox_processed(session, event, now=now_in_timezone(getattr(settings, "timezone", "Asia/Shanghai")))
                await session.commit()
                processed += 1
            except Exception as exc:
                await session.rollback()
                async with session_factory() as retry_session:
                    event = await retry_session.merge(event)
                    await mark_progress_outbox_failed(
                        retry_session,
                        event,
                        error_message=str(exc),
                        now=now_in_timezone(getattr(settings, "timezone", "Asia/Shanghai")),
                        max_retries=max_retries,
                    )
                    await retry_session.commit()
                logger.exception("progress outbox event failed id=%s", getattr(event, "id", None))
    return processed


async def _process_event_noop(event: Any) -> None:
    """Phase 1 placeholder.

    Future phases will convert outbox events into progress_intake_events and
    matter_update_candidates. Phase 1 deliberately performs no LLM calls,
    no matter matching, and no matter_updates writes.
    """


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4()}"


async def run_worker_loop(*, poll_seconds: float = 5.0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app.db import AsyncSessionLocal, engine

    settings = get_settings()
    try:
        while True:
            processed = await process_progress_outbox_once(
                session_factory=AsyncSessionLocal,
                settings=settings,
            )
            if processed:
                logger.info("progress worker processed=%s", processed)
            await asyncio.sleep(poll_seconds)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Progress Intake outbox worker.")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit.")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()

    async def _run() -> None:
        if args.once:
            from app.db import AsyncSessionLocal, engine

            settings = get_settings()
            try:
                processed = await process_progress_outbox_once(session_factory=AsyncSessionLocal, settings=settings)
                logger.info("progress worker once processed=%s", processed)
            finally:
                await engine.dispose()
            return
        await run_worker_loop(poll_seconds=args.poll_seconds)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
