from __future__ import annotations

import asyncio
from typing import Any

from app.repositories import progress_outbox_status_snapshot


def _format_dt(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else "")


async def collect_status(*, session: Any) -> dict[str, Any]:
    return await progress_outbox_status_snapshot(session)


def format_status(snapshot: dict[str, Any]) -> str:
    lines = ["PROGRESS_OUTBOX_STATUS_START"]
    for key in ("pending", "processing", "processed", "failed", "dead_letter"):
        lines.append(f"{key}={int(snapshot.get(key) or 0)}")
    lines.append(f"oldest_pending_at={_format_dt(snapshot.get('oldest_pending_at'))}")
    lines.append(f"latest_processed_at={_format_dt(snapshot.get('latest_processed_at'))}")
    recent_failed_error = str(snapshot.get("recent_failed_error") or "")
    lines.append(f"recent_failed_error={recent_failed_error[:300]}")
    lines.append("PROGRESS_OUTBOX_STATUS_END")
    return "\n".join(lines)


async def _run_cli() -> None:
    from app.db import AsyncSessionLocal, engine

    try:
        async with AsyncSessionLocal() as session:
            print(format_status(await collect_status(session=session)))
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_run_cli())


if __name__ == "__main__":
    main()
