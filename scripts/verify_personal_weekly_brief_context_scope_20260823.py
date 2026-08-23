from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent2.personal_weekly_brief_scope import (
    load_personal_weekly_brief_target_revalidation,
    load_personal_weekly_brief_targets,
)
from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
from app.config import get_settings
from app.db import AsyncSessionLocal, engine


async def main() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    runtime_values = tuple(
        value.strip()
        for value in str(
            settings.agent2_personal_weekly_brief_tenant_id
            or settings.agent2_weekly_plan_tenant_allowlist
            or ""
        ).split(",")
        if value.strip()
    )
    runtime_tenant = runtime_values[0] if len(runtime_values) == 1 else ""
    roster_tenant = str(
        settings.legal_daily_dashboard_tenant_id or runtime_tenant
    ).strip()
    if not runtime_tenant or not roster_tenant:
        raise RuntimeError("personal weekly brief tenant is missing")
    observed_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    async with AsyncSessionLocal() as session:
        frozen = await load_personal_weekly_brief_targets(
            session,
            tenant_id=runtime_tenant,
            roster_tenant_id=roster_tenant,
            on_date=observed_date,
            expected_model_name=CANARY_MODEL_NAME,
            robot_code=settings.dingtalk_robot_code,
        )
        revalidated = await load_personal_weekly_brief_target_revalidation(
            session,
            tenant_id=runtime_tenant,
            roster_tenant_id=roster_tenant,
            on_date=observed_date,
            expected_model_name=CANARY_MODEL_NAME,
            frozen_targets=frozen,
            robot_code=settings.dingtalk_robot_code,
        )
        await session.rollback()
    blocked_names = {
        target.display_name: revalidated.blocked_reasons[target.internal_user_id]
        for target in frozen
        if target.internal_user_id in revalidated.blocked_reasons
    }
    payload = {
        "status": "PASS",
        "formal_targets": len(frozen),
        "real_private_conversations": sum(
            bool(target.conversation_id) for target in frozen
        ),
        "valid_targets": len(revalidated.valid_targets),
        "blocked_targets": len(revalidated.blocked_reasons),
        "blocked_names": blocked_names,
        "generation_enabled": settings.agent2_personal_weekly_brief_enabled,
        "send_enabled": settings.agent2_personal_weekly_brief_send_enabled,
    }
    if not (
        payload["formal_targets"] == 74
        and payload["real_private_conversations"] == 73
        and payload["valid_targets"] == 73
        and payload["blocked_targets"] == 1
        and blocked_names == {"周星星": "direct_conversation_unavailable"}
        and payload["generation_enabled"] is False
        and payload["send_enabled"] is False
    ):
        payload["status"] = "FAIL"
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    await engine.dispose()
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
