from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sys


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import one exact release and verify that all 74 live Agent2 "
            "controls match its model, prompt, and tool registry contract."
        )
    )
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--expected-model")
    return parser.parse_args()


async def _verify(args: argparse.Namespace) -> dict[str, object]:
    code_root = args.code_root.resolve(strict=True)
    if not code_root.is_dir() or code_root.is_symlink():
        raise AssertionError("code root must be a real release directory")
    os.chdir(code_root)
    sys.path.insert(0, str(code_root))

    from sqlalchemy import select

    from app.agent2.tool_calling.canary_config import (
        CANARY_MODEL_NAME,
        CANARY_THINKING_ENABLED,
        canary_prompt_sha256,
    )
    from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
    from app.agent2.tool_calling.registry import runtime_registry_contract_digest
    from app.config import get_settings
    from app.db import AsyncSessionLocal, engine
    from app.legal_daily_roster import (
        FORMAL_ROSTER_MEMBER_COUNT,
        load_formal_legal_daily_roster,
    )
    from app.utils.time import today_in_timezone

    settings = get_settings()
    dashboard_tenant_id = str(
        settings.legal_daily_dashboard_tenant_id or ""
    ).strip()
    if not dashboard_tenant_id:
        raise AssertionError("legal daily tenant is not configured")
    expected = {
        "registry_digest": runtime_registry_contract_digest(settings),
        "prompt_sha256": canary_prompt_sha256(),
        "model_name": CANARY_MODEL_NAME,
    }
    if settings.agent2_tool_call_canary_max_active_users != FORMAL_ROSTER_MEMBER_COUNT:
        raise AssertionError(
            "Agent2 active-user limit does not match the 74-person roster"
        )
    if args.expected_model and CANARY_MODEL_NAME != args.expected_model:
        raise AssertionError(
            f"imported model is {CANARY_MODEL_NAME}, expected {args.expected_model}"
        )
    try:
        async with AsyncSessionLocal() as session:
            roster = await load_formal_legal_daily_roster(
                session,
                tenant_id=dashboard_tenant_id,
                on_date=today_in_timezone(settings.timezone),
            )
            roster_user_ids = set(roster.user_ids)
            controls = list(
                (
                    await session.scalars(
                        select(ToolCallCanaryControl)
                        .where(ToolCallCanaryControl.user_id.in_(roster_user_ids))
                        .order_by(ToolCallCanaryControl.control_key)
                    )
                ).all()
            )
            await session.rollback()
    finally:
        await engine.dispose()
    if len(controls) != FORMAL_ROSTER_MEMBER_COUNT:
        raise AssertionError(
            f"control count is {len(controls)}, expected {FORMAL_ROSTER_MEMBER_COUNT}"
        )
    if len({row.user_id for row in controls}) != FORMAL_ROSTER_MEMBER_COUNT:
        raise AssertionError("Agent2 controls contain duplicate users")
    if {row.user_id for row in controls} != roster_user_ids:
        raise AssertionError("Agent2 controls do not exactly match the formal roster")
    runtime_tenant_ids = {row.tenant_id for row in controls}
    if len(runtime_tenant_ids) != 1:
        raise AssertionError("Agent2 controls do not share one runtime tenant")
    runtime_tenant_id = next(iter(runtime_tenant_ids))
    if not all(
        row.enabled
        and row.messages_enabled
        and row.runtime == "canary_execute"
        for row in controls
    ):
        raise AssertionError("not all controls are message-ready Agent2 controls")
    mismatches = [
        row.control_key
        for row in controls
        if row.registry_digest != expected["registry_digest"]
        or row.prompt_sha256 != expected["prompt_sha256"]
        or row.model_name != expected["model_name"]
    ]
    if mismatches:
        raise AssertionError(
            f"{len(mismatches)} controls do not match the imported release"
        )
    return {
        "status": "pass",
        "code_root": str(code_root),
        "dashboard_tenant_id": dashboard_tenant_id,
        "runtime_tenant_id": runtime_tenant_id,
        "control_count": len(controls),
        "active_user_limit": settings.agent2_tool_call_canary_max_active_users,
        "contract": expected,
        "thinking_enabled": CANARY_THINKING_ENABLED,
        "model_distribution": dict(Counter(row.model_name for row in controls)),
        "dingtalk_send_calls": 0,
    }


def main() -> None:
    args = _args()
    print(json.dumps(asyncio.run(_verify(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
