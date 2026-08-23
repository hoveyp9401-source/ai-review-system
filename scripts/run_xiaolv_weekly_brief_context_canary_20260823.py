from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

MESSAGE_TEXT = """【小律上下文链路测试】
庞总，这是只发给你的测试简报：
1. 本周完成：测试日报长文本整理链路。
2. 本周计划进展：测试周计划与日报不会串线。
3. 可能未闭环：测试周简报发出后能否承接你的回复。

这条消息只用于验证连续对话，不会写入日报或周计划。"""
SOURCE_MESSAGE_ID = "personal-weekly-brief-context-canary:v10:pang-20260823"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-process-id", type=int, required=True)
    return parser.parse_args()


def _one_runtime_tenant(settings) -> str:
    values = tuple(
        value.strip()
        for value in str(
            settings.agent2_personal_weekly_brief_tenant_id
            or settings.agent2_weekly_plan_tenant_allowlist
            or ""
        ).split(",")
        if value.strip()
    )
    if len(values) != 1:
        raise RuntimeError("Agent2 runtime tenant is not unique")
    return values[0]


def _write_evidence(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


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
        raise RuntimeError("context canary evidence path already exists")
    _write_evidence(
        args.output,
        {"status": "started", "source_message_id": SOURCE_MESSAGE_ID},
    )
    _load_process_environment(args.source_process_id)
    from sqlalchemy import select

    from app.agent2.personal_weekly_brief_scope import (
        load_personal_weekly_brief_targets,
    )
    from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
    from app.agent2.tool_calling.outbound_context import (
        record_verified_outbound_context_message,
    )
    from app.config import get_settings
    from app.db import AsyncSessionLocal, engine
    from app.models import User
    from app.services.dingtalk import DingTalkRobotClient

    get_settings.cache_clear()
    settings = get_settings()
    runtime_tenant = _one_runtime_tenant(settings)
    roster_tenant = str(settings.legal_daily_dashboard_tenant_id or "").strip()
    if not roster_tenant:
        raise RuntimeError("formal roster tenant is missing")
    observed_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    async with AsyncSessionLocal() as session:
        targets = await load_personal_weekly_brief_targets(
            session,
            tenant_id=runtime_tenant,
            roster_tenant_id=roster_tenant,
            on_date=observed_at.date(),
            expected_model_name=CANARY_MODEL_NAME,
            robot_code=settings.dingtalk_robot_code,
        )
        matches = tuple(target for target in targets if target.display_name == "庞浩")
        if len(targets) != 74 or len(matches) != 1 or not matches[0].conversation_id:
            raise RuntimeError("exact Pang Hao Xiaolv target is unavailable")
        target = matches[0]
        user = await session.scalar(
            select(User).where(
                User.id == UUID(target.internal_user_id),
                User.active.is_(True),
                User.dingtalk_user_id == target.dingtalk_user_id,
            )
        )
        if user is None:
            raise RuntimeError("Pang Hao user identity changed")
        await session.rollback()

    robot = DingTalkRobotClient(settings)
    sent_at = datetime.now(UTC)
    try:
        receipt = await robot.send_robot_direct_text_verified(
            user_ids=[target.dingtalk_user_id],
            text=MESSAGE_TEXT,
        )
    finally:
        await robot.close()
    delivered_ids = tuple(receipt.get("deliveryRecipientUserIds") or ())
    if not (
        receipt.get("deliveryVerified") is True
        and str(receipt.get("deliveryStatus") or "").upper() == "SUCCESS"
        and delivered_ids == (target.dingtalk_user_id,)
    ):
        raise RuntimeError("Xiaolv final delivery was not confirmed for exact recipient")

    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(
                User.id == UUID(target.internal_user_id),
                User.active.is_(True),
                User.dingtalk_user_id == target.dingtalk_user_id,
            )
        )
        if user is None:
            raise RuntimeError("Pang Hao user identity changed after delivery")
        recorded = await record_verified_outbound_context_message(
            session,
            user=user,
            conversation_id=target.conversation_id,
            message_text=MESSAGE_TEXT,
            source_message_id=SOURCE_MESSAGE_ID,
            delivery_receipt=receipt,
            sent_at=sent_at,
        )
        await session.commit()

    reference = str(receipt.get("processQueryKey") or "").strip()
    payload = {
        "status": "PASS",
        "recipient_count": 1,
        "recipient_name": target.display_name,
        "delivery_verified": True,
        "context_created": recorded.created,
        "context_event_id": str(recorded.event.id),
        "conversation_sha256": hashlib.sha256(
            target.conversation_id.encode("utf-8")
        ).hexdigest(),
        "provider_sha256": hashlib.sha256(reference.encode("utf-8")).hexdigest(),
        "message_sha256": hashlib.sha256(MESSAGE_TEXT.encode("utf-8")).hexdigest(),
        "daily_report_writes": 0,
        "weekly_plan_writes": 0,
    }
    _write_evidence(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
