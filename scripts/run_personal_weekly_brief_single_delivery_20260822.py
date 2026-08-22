"""Generate and deliver one explicitly approved personal weekly-brief canary."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.agent2.personal_weekly_brief_delivery import (
    DingTalkPersonalWeeklyBriefTransport,
    PersonalWeeklyBriefDispatcher,
    PersonalWeeklyBriefRecipient,
)
from app.agent2.personal_weekly_brief_scope import (
    load_personal_weekly_brief_target_revalidation,
)
from app.agent2.personal_weekly_brief_store import SqlPersonalWeeklyBriefStore
from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
from app.config import Settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.scheduler import runner
from app.services.dingtalk import DingTalkRobotClient


TIMEZONE = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = "agent2.personal_weekly_brief.single_delivery.v1"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="【周工作简报测试】")
    return parser.parse_args()


def _one_csv(raw: object) -> str:
    values = tuple(
        value.strip() for value in str(raw or "").split(",") if value.strip()
    )
    if len(values) != 1:
        raise RuntimeError("Agent2 runtime tenant is not unique")
    return values[0]


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.output.exists() or args.output.is_symlink():
        raise RuntimeError("single-delivery evidence path already exists")
    base = Settings()
    runtime_tenant = _one_csv(base.agent2_weekly_plan_tenant_allowlist)
    roster_tenant = str(base.legal_daily_dashboard_tenant_id or "").strip()
    if not roster_tenant:
        raise RuntimeError("formal roster tenant is missing")
    os.environ["AGENT2_PERSONAL_WEEKLY_BRIEF_ENABLED"] = "true"
    os.environ["AGENT2_PERSONAL_WEEKLY_BRIEF_SEND_ENABLED"] = "true"
    os.environ["AGENT2_PERSONAL_WEEKLY_BRIEF_TENANT_ID"] = runtime_tenant
    settings = Settings()
    now = datetime.now(TIMEZONE)
    window = runner.derive_personal_weekly_brief_window(
        now,
        timezone_name=runner.PERSONAL_WEEKLY_BRIEF_TIMEZONE,
    )

    async with AsyncSessionLocal() as session:
        count = int(
            await session.scalar(
                text("SELECT count(*) FROM public.agent2_personal_weekly_briefs")
            )
            or 0
        )
        await session.rollback()
    if count != 0:
        raise RuntimeError("weekly brief table is not empty before one-user test")

    captured: dict[str, object] = {}
    original_stage = runner.stage_personal_weekly_brief_target_batch
    original_generate = runner._generate_personal_weekly_brief_record

    async def stage_one(*, session, targets, snapshot_service, **kwargs):
        matches = tuple(
            target for target in targets if target.display_name == args.recipient_name
        )
        if len(targets) != 74 or len(matches) != 1:
            raise RuntimeError("single-delivery recipient scope is not exact")
        captured["targets"] = targets
        captured["target"] = matches[0]
        return await original_stage(
            session=session,
            targets=matches,
            snapshot_service=snapshot_service,
            **kwargs,
        )

    async def generate_labeled(**kwargs):
        generated = await original_generate(**kwargs)
        if generated.status != "generated":
            return generated
        message = f"{args.label}\n{generated.message_text}"
        if len(message) > 4000:
            raise RuntimeError("labeled weekly brief is too long")
        async with AsyncSessionLocal() as session:
            changed = await session.execute(
                text(
                    """
                    UPDATE public.agent2_personal_weekly_briefs
                    SET message_text = :message, updated_at = :changed_at
                    WHERE tenant_id = :tenant_id
                      AND brief_id = CAST(:brief_id AS uuid)
                      AND owner_user_id = :owner_user_id
                      AND status = 'generated'
                    RETURNING brief_id
                    """
                ),
                {
                    "message": message,
                    "changed_at": datetime.now(TIMEZONE),
                    "tenant_id": generated.tenant_id,
                    "brief_id": generated.brief_id,
                    "owner_user_id": generated.owner_user_id,
                },
            )
            if changed.scalar_one_or_none() is None:
                raise RuntimeError("test label did not target generated row")
            await session.commit()
        async with AsyncSessionLocal() as session:
            labeled = await SqlPersonalWeeklyBriefStore(
                session
            ).load_by_owner_week(
                tenant_id=generated.tenant_id,
                owner_user_id=generated.owner_user_id,
                week_start=generated.week_start,
            )
        if labeled is None or labeled.message_text != message:
            raise RuntimeError("labeled weekly brief read-back failed")
        return labeled

    runner.stage_personal_weekly_brief_target_batch = stage_one
    runner._generate_personal_weekly_brief_record = generate_labeled
    robot = DingTalkRobotClient(settings)
    result = await runner.run_personal_weekly_brief_generation_job(
        settings,
        llm_client=LLMClient(settings),
        robot=robot,
        now=now,
    )
    target = captured.get("target")
    targets = captured.get("targets")
    if target is None or not isinstance(targets, tuple):
        raise RuntimeError("single-delivery target was not captured")

    async with AsyncSessionLocal() as session:
        row = await SqlPersonalWeeklyBriefStore(session).load_by_owner_week(
            tenant_id=runtime_tenant,
            owner_user_id=target.internal_user_id,
            week_start=window.week_start,
        )
    if row is None:
        raise RuntimeError("single-delivery row is missing")
    recipient = PersonalWeeklyBriefRecipient(
        tenant_id=target.tenant_id,
        internal_user_id=target.internal_user_id,
        dingtalk_user_id=target.dingtalk_user_id,
        conversation_id=target.conversation_id,
    )
    transport = DingTalkPersonalWeeklyBriefTransport(robot)
    for _ in range(6):
        if row.status != "delivery_pending":
            break
        await asyncio.sleep(2)
        async with AsyncSessionLocal() as session:
            latest_scope = await load_personal_weekly_brief_target_revalidation(
                session,
                tenant_id=runtime_tenant,
                roster_tenant_id=roster_tenant,
                on_date=datetime.now(TIMEZONE).date(),
                expected_model_name=CANARY_MODEL_NAME,
                frozen_targets=targets,
            )
            if target.internal_user_id not in latest_scope.valid_targets:
                raise RuntimeError("single-delivery recipient changed")
            current = await SqlPersonalWeeklyBriefStore(
                session
            ).load_by_owner_week(
                tenant_id=runtime_tenant,
                owner_user_id=target.internal_user_id,
                week_start=window.week_start,
            )
            dispatcher = PersonalWeeklyBriefDispatcher(
                store=SqlPersonalWeeklyBriefStore(session),
                transport=transport,
                tenant_id=runtime_tenant,
                allowed_user_ids=frozenset({target.internal_user_id}),
                clock=lambda: datetime.now(TIMEZONE),
            )
            row = await dispatcher.reconcile_pending(
                row=current,
                recipient=recipient,
                changed_at=datetime.now(TIMEZONE),
            )
            await session.commit()
        if row.status == "delivered":
            await runner._record_personal_weekly_brief_context(
                tenant_id=runtime_tenant,
                roster_tenant_id=roster_tenant,
                row=row,
                frozen_targets=targets,
            )

    async with AsyncSessionLocal() as session:
        final_row = await SqlPersonalWeeklyBriefStore(session).load_by_owner_week(
            tenant_id=runtime_tenant,
            owner_user_id=target.internal_user_id,
            week_start=window.week_start,
        )
        total_rows = int(
            await session.scalar(
                text("SELECT count(*) FROM public.agent2_personal_weekly_briefs")
            )
            or 0
        )
        await session.rollback()
    receipt = dict(final_row.delivery_receipt_json or {})
    delivered_ids = receipt.get("delivered_dingtalk_user_ids") or []
    if not (
        result.get("staged") == 1
        and result.get("generated") == 1
        and final_row.status == "delivered"
        and receipt.get("delivery_verified") is True
        and receipt.get("delivery_status") == "SUCCESS"
        and delivered_ids == [target.dingtalk_user_id]
        and final_row.context_recorded_at is not None
        and total_rows == 1
    ):
        raise RuntimeError("single weekly brief final delivery was not verified")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "recipient_name": args.recipient_name,
        "formal_target_count": len(targets),
        "brief_rows_total": total_rows,
        "final_status": final_row.status,
        "delivery_verified": True,
        "delivered_recipient_count": len(delivered_ids),
        "context_recorded": True,
        "message_sha256": hashlib.sha256(final_row.message_text.encode()).hexdigest(),
        "provider_reference_sha256": hashlib.sha256(
            final_row.provider_message_id.encode()
        ).hexdigest(),
        "service_generation_switch_changed": False,
        "service_send_switch_changed": False,
    }


async def _main() -> None:
    args = _args()
    payload = await _run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(args.output, 0o600)
    print(json.dumps({"status": payload["status"], "output": str(args.output)}))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
