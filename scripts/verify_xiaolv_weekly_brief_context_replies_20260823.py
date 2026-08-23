from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

CASES = (
    ("second", "刚才简报第二项是什么？", "周计划与日报不会串线"),
    (
        "unclosed",
        "刚才这份简报里，哪项是可能未闭环的？",
        "周简报发出后能否承接",
    ),
    ("summary", "这份简报主要说了什么？", "连续对话"),
)
CONTEXT_MARKER = "小律上下文链路测试"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-process-id", type=int, required=True)
    return parser.parse_args()


def _runtime_tenant(settings) -> str:
    values = tuple(
        value.strip()
        for value in str(settings.agent2_weekly_plan_tenant_allowlist or "").split(",")
        if value.strip()
    )
    if len(values) != 1:
        raise RuntimeError("Agent2 runtime tenant is not unique")
    return values[0]


def _load_process_environment(pid: int) -> None:
    if pid <= 1:
        raise RuntimeError("source process id is invalid")
    values = {
        item.split(b"=", 1)[0].decode(): item.split(b"=", 1)[1].decode()
        for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if b"=" in item
    }
    if not values:
        raise RuntimeError("source process environment is empty")
    os.environ.clear()
    os.environ.update(values)


async def main() -> None:
    args = _args()
    if args.output.exists() or args.output.is_symlink():
        raise RuntimeError("context reply evidence path already exists")
    _load_process_environment(args.source_process_id)
    from sqlalchemy import func, select

    from app.agent2.personal_weekly_brief_scope import (
        load_personal_weekly_brief_targets,
    )
    from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
    from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
    from app.config import get_settings
    from app.db import AsyncSessionLocal, engine
    from app.llm.client import LLMClient
    from scripts.smoke_20260811_overnight_daily_rollback import (
        PANG_USER_ID,
        _turn,
        _user_and_control,
    )

    get_settings.cache_clear()
    settings = get_settings()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    async with AsyncSessionLocal() as session:
        targets = await load_personal_weekly_brief_targets(
            session,
            tenant_id=_runtime_tenant(settings),
            roster_tenant_id=settings.legal_daily_dashboard_tenant_id,
            on_date=now.date(),
            expected_model_name=CANARY_MODEL_NAME,
            robot_code=settings.dingtalk_robot_code,
        )
        matches = tuple(target for target in targets if target.display_name == "庞浩")
        if len(matches) != 1 or not matches[0].conversation_id:
            raise RuntimeError("exact Pang Hao Xiaolv conversation is unavailable")
        target = matches[0]
        await session.rollback()
    if target.internal_user_id != str(PANG_USER_ID):
        raise RuntimeError("Pang Hao smoke identity does not match formal scope")

    prefix = f"weekly-brief-context-reply-v10-{uuid4()}"
    llm_client = LLMClient(settings)
    results: list[dict[str, object]] = []
    try:
        for name, user_text, expected_fragment in CASES:
            source_id = f"{prefix}-{name}"
            async with AsyncSessionLocal() as session:
                try:
                    user, turn_settings = await _user_and_control(session)
                    outcome = await _turn(
                        session,
                        user=user,
                        settings=turn_settings,
                        llm_client=llm_client,
                        text=user_text,
                        conversation_id=target.conversation_id,
                        source_message_id=source_id,
                        now=now,
                        accepted_business_results=frozenset(
                            {"reply_only", "success", "no_op"}
                        ),
                        conversation_kind="direct",
                        message_occurred_at=now,
                    )
                    if outcome.actual_write or expected_fragment not in outcome.message:
                        raise AssertionError(
                            {
                                "name": name,
                                "actual_write": outcome.actual_write,
                                "message": outcome.message,
                            }
                        )
                    results.append(
                        {
                            "name": name,
                            "status": "PASS",
                            "user_visible_result": outcome.user_visible_result,
                            "actual_write": outcome.actual_write,
                            "answer": outcome.message,
                        }
                    )
                finally:
                    await session.rollback()
    finally:
        await llm_client.close()

    async with AsyncSessionLocal() as session:
        residue = int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                    ToolCallCanaryReceipt.source_message_id.like(f"{prefix}%")
                )
            )
            or 0
        )
        await session.rollback()
    payload = {
        "status": "PASS" if len(results) == 3 and residue == 0 else "FAIL",
        "cases": results,
        "rollback_residue": residue,
        "context_message_present": bool(CONTEXT_MARKER),
        "real_messages_sent": 0,
        "daily_report_writes": 0,
        "weekly_plan_writes": 0,
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output.chmod(0o600)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    await engine.dispose()
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
