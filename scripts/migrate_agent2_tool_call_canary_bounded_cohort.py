from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import func, select, text

from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.config import get_settings
from app.db import AsyncSessionLocal
from scripts.enable_agent2_tool_call_canary import (
    validate_enable_environment,
)


LEGACY_INDEX_NAME = "agent2_tool_call_one_enabled_idx"
LEGACY_INDEX_REQUIRED_PARTS = (
    "CREATE UNIQUE INDEX agent2_tool_call_one_enabled_idx",
    "agent2_tool_call_canary_controls",
    "WHERE (enabled = true)",
)


def validate_legacy_index_definition(definition: str) -> None:
    normalized = " ".join(str(definition).split())
    if not all(part in normalized for part in LEGACY_INDEX_REQUIRED_PARTS):
        raise ValueError("tool_call_canary_legacy_index_definition_mismatch")


async def _run(settings: Any) -> dict[str, object]:
    max_active = int(
        settings.agent2_tool_call_canary_max_active_users
    )
    if not 2 <= max_active <= 74:
        raise ValueError("bounded cohort limit must be between 2 and 74")
    async with AsyncSessionLocal() as session:
        try:
            active_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ToolCallCanaryControl)
                    .where(ToolCallCanaryControl.enabled.is_(True))
                )
                or 0
            )
            if active_count > max_active:
                raise ValueError(
                    "tool_call_canary_active_count_exceeds_cohort_limit"
                )
            index_definition = await session.scalar(
                text(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename =
                          'agent2_tool_call_canary_controls'
                      AND indexname = :index_name
                    """
                ),
                {"index_name": LEGACY_INDEX_NAME},
            )
            changed = index_definition is not None
            if index_definition is not None:
                validate_legacy_index_definition(str(index_definition))
                await session.execute(
                    text(
                        "DROP INDEX agent2_tool_call_one_enabled_idx"
                    )
                )
            remaining = await session.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename =
                          'agent2_tool_call_canary_controls'
                      AND indexname = :index_name
                    """
                ),
                {"index_name": LEGACY_INDEX_NAME},
            )
            route_index = await session.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename =
                          'agent2_tool_call_canary_controls'
                      AND indexname =
                          'agent2_tool_call_canary_route_idx'
                    """
                )
            )
            if int(remaining or 0) != 0 or int(route_index or 0) != 1:
                raise ValueError(
                    "tool_call_canary_bounded_cohort_index_gate_failed"
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return {
        "changed": changed,
        "legacy_index": LEGACY_INDEX_NAME,
        "legacy_index_definition": index_definition,
        "legacy_index_remaining": int(remaining or 0),
        "route_index_present": int(route_index or 0) == 1,
        "active_count": active_count,
        "max_active_users": max_active,
    }


def main() -> int:
    settings = get_settings()
    validate_enable_environment(settings)
    print(
        json.dumps(
            asyncio.run(_run(settings)),
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
