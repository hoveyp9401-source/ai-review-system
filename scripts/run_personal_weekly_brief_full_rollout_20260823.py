from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
MANUAL_SATURDAY = datetime(2026, 8, 22, 9, 0, tzinfo=TIMEZONE)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "run", "reconcile"))
    parser.add_argument("--source-process-id", type=int, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


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


def _write_evidence(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


async def main() -> None:
    args = _args()
    if args.action != "inspect" and args.output is None:
        raise RuntimeError("rollout evidence path is required")
    if args.output is not None and (
        args.output.exists() or args.output.is_symlink()
    ):
        raise RuntimeError("rollout evidence path already exists")
    _load_process_environment(args.source_process_id)

    from sqlalchemy import select

    from app.agent2.personal_weekly_brief import derive_personal_weekly_brief_window
    from app.agent2.personal_weekly_brief_scope import (
        load_personal_weekly_brief_target_revalidation,
        load_personal_weekly_brief_targets,
    )
    from app.agent2.personal_weekly_brief_store import SqlPersonalWeeklyBriefStore
    from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
    from app.config import get_settings
    from app.db import AsyncSessionLocal, engine
    from app.llm.client import LLMClient
    from app.models import User
    from app.scheduler.runner import (
        PERSONAL_WEEKLY_BRIEF_TIMEZONE,
        run_personal_weekly_brief_generation_job,
        run_personal_weekly_brief_reconcile_job,
    )
    from app.services.dingtalk import DingTalkRobotClient

    get_settings.cache_clear()
    settings = get_settings()
    switches_open = bool(
        settings.agent2_personal_weekly_brief_enabled is True
        and settings.agent2_personal_weekly_brief_send_enabled is True
        and str(settings.agent2_personal_weekly_brief_tenant_id or "").strip()
    )
    if args.action != "inspect" and not switches_open:
        raise RuntimeError("personal weekly brief rollout switches are not open")
    tenant_values = tuple(
        value.strip()
        for value in str(
            settings.agent2_personal_weekly_brief_tenant_id
            or settings.agent2_weekly_plan_tenant_allowlist
            or ""
        ).split(",")
        if value.strip()
    )
    if len(tenant_values) != 1:
        raise RuntimeError("personal weekly brief runtime tenant is not unique")
    tenant_id = tenant_values[0]
    roster_tenant = str(settings.legal_daily_dashboard_tenant_id or tenant_id).strip()
    window = derive_personal_weekly_brief_window(
        MANUAL_SATURDAY,
        timezone_name=PERSONAL_WEEKLY_BRIEF_TIMEZONE,
    )

    async with AsyncSessionLocal() as session:
        targets = await load_personal_weekly_brief_targets(
            session,
            tenant_id=tenant_id,
            roster_tenant_id=roster_tenant,
            on_date=MANUAL_SATURDAY.date(),
            expected_model_name=CANARY_MODEL_NAME,
            robot_code=settings.dingtalk_robot_code,
        )
        revalidated = await load_personal_weekly_brief_target_revalidation(
            session,
            tenant_id=tenant_id,
            roster_tenant_id=roster_tenant,
            on_date=MANUAL_SATURDAY.date(),
            expected_model_name=CANARY_MODEL_NAME,
            frozen_targets=targets,
            robot_code=settings.dingtalk_robot_code,
        )
        rows_before = await SqlPersonalWeeklyBriefStore(session).load_for_week(
            tenant_id=tenant_id,
            week_start=window.week_start,
            limit=100,
        )
        users = tuple(
            (
                await session.scalars(
                    select(User).where(
                        User.id.in_([UUID(target.internal_user_id) for target in targets])
                    )
                )
            ).all()
        )
        names_by_user = {str(user.id): user.name for user in users}
        await session.rollback()
    blocked_names = {
        names_by_user.get(user_id, "未知用户"): reason
        for user_id, reason in revalidated.blocked_reasons.items()
    }
    if not (
        len(targets) == 74
        and len(revalidated.valid_targets) == 73
        and blocked_names == {"周星星": "direct_conversation_unavailable"}
    ):
        raise RuntimeError("personal weekly brief target scope is unsafe")

    before_statuses = Counter(row.status for row in rows_before)
    existing_pang = tuple(
        row for row in rows_before if names_by_user.get(row.owner_user_id) == "庞浩"
    )
    if args.action == "run" and not (
        len(rows_before) == 1
        and before_statuses == {"delivered": 1}
        and len(existing_pang) == 1
        and existing_pang[0].provider_message_id
    ):
        raise RuntimeError("personal weekly brief initial batch is not the one-user canary")
    existing_pang_provider = (
        existing_pang[0].provider_message_id if existing_pang else ""
    )

    if args.action != "inspect":
        _write_evidence(
            args.output,
            {
                "status": "started",
                "action": args.action,
                "week_start": window.week_start.isoformat(),
                "existing_rows": len(rows_before),
            },
        )

    result: dict[str, object] = {}
    llm_client = LLMClient(settings)
    robot = DingTalkRobotClient(settings)
    try:
        if args.action == "run":
            result = await run_personal_weekly_brief_generation_job(
                settings,
                llm_client=llm_client,
                robot=robot,
                now=MANUAL_SATURDAY,
            )
        if args.action in {"run", "reconcile"}:
            result["reconciled_deliveries_first"] = (
                await run_personal_weekly_brief_reconcile_job(
                    settings,
                    llm_client=llm_client,
                    robot=robot,
                    now=datetime.now(TIMEZONE),
                )
            )
            result["reconciled_deliveries_second"] = (
                await run_personal_weekly_brief_reconcile_job(
                    settings,
                    llm_client=llm_client,
                    robot=robot,
                    now=datetime.now(TIMEZONE),
                )
            )
    finally:
        await robot.close()
        await llm_client.close()

    async with AsyncSessionLocal() as session:
        rows_after = await SqlPersonalWeeklyBriefStore(session).load_for_week(
            tenant_id=tenant_id,
            week_start=window.week_start,
            limit=100,
        )
        await session.rollback()
    statuses = Counter(row.status for row in rows_after)
    failed_names = {
        names_by_user.get(row.owner_user_id, "未知用户"): row.last_error
        for row in rows_after
        if row.status in {"failed", "generation_failed"}
    }
    delivered = tuple(row for row in rows_after if row.status == "delivered")
    receipts_exact = sum(
        bool(
            (receipt := dict(row.delivery_receipt_json or {})).get(
                "delivery_verified"
            )
            is True
            and receipt.get("delivery_status") == "SUCCESS"
            and len(receipt.get("delivered_dingtalk_user_ids") or []) == 1
        )
        for row in delivered
    )
    pang_after = tuple(
        row for row in rows_after if names_by_user.get(row.owner_user_id) == "庞浩"
    )
    payload = {
        "status": "PASS",
        "action": args.action,
        "week_start": window.week_start.isoformat(),
        "formal_targets": len(targets),
        "valid_private_targets": len(revalidated.valid_targets),
        "blocked_names": blocked_names,
        "switches_open": switches_open,
        "before_statuses": dict(before_statuses),
        "after_statuses": dict(statuses),
        "delivered": len(delivered),
        "delivery_receipts_exact": receipts_exact,
        "context_recorded": sum(
            row.context_recorded_at is not None for row in rows_after
        ),
        "new_final_deliveries": max(0, len(delivered) - before_statuses["delivered"]),
        "existing_pang_unchanged": bool(
            len(pang_after) == 1
            and pang_after[0].provider_message_id == existing_pang_provider
        ),
        "failed_names": failed_names,
        "source_counts_by_status": {
            status: sorted(
                len((row.source_snapshot or {}).get("sources") or [])
                for row in rows_after
                if row.status == status
            )
            for status in sorted(statuses)
        },
        "job_result": result,
    }
    if args.action != "inspect" and not (
        len(rows_after) == 74
        and statuses == {"delivered": 73, "failed": 1}
        and receipts_exact == 73
        and payload["context_recorded"] == 72
        and payload["new_final_deliveries"] == 72
        and payload["existing_pang_unchanged"] is True
        and set(failed_names) == {"周星星"}
        and failed_names["周星星"].startswith(
            "pre_send_scope:direct_conversation_unavailable"
        )
    ):
        payload["status"] = "PARTIAL"
    if args.output is not None:
        _write_evidence(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    await engine.dispose()
    if args.action != "inspect" and payload["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
