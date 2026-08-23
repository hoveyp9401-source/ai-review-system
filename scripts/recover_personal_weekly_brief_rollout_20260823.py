from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
SATURDAY = datetime(2026, 8, 22, 9, 0, tzinfo=TIMEZONE)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "prepare", "run"))
    parser.add_argument("--source-process-id", type=int, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--backup", type=Path)
    return parser.parse_args()


def _load_process_environment(pid: int) -> None:
    values = {
        item.split(b"=", 1)[0].decode(): item.split(b"=", 1)[1].decode()
        for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if b"=" in item
    }
    if pid <= 1 or not values:
        raise RuntimeError("source process environment is unavailable")
    os.environ.clear()
    os.environ.update(values)
    tenants = tuple(
        value.strip()
        for value in values.get(
            "AGENT2_WEEKLY_PLAN_TENANT_ALLOWLIST",
            "",
        ).split(",")
        if value.strip()
    )
    if len(tenants) != 1:
        raise RuntimeError("runtime tenant is not unique")
    os.environ["AGENT2_PERSONAL_WEEKLY_BRIEF_ENABLED"] = "true"
    os.environ["AGENT2_PERSONAL_WEEKLY_BRIEF_SEND_ENABLED"] = "true"
    os.environ["AGENT2_PERSONAL_WEEKLY_BRIEF_TENANT_ID"] = tenants[0]


def _write(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


async def main() -> None:
    args = _args()
    if args.action != "inspect" and args.output is None:
        raise RuntimeError("recovery output path is required")
    if args.action == "prepare" and args.backup is None:
        raise RuntimeError("recovery backup path is required")
    for path in (args.output, args.backup):
        if path is not None and (path.exists() or path.is_symlink()):
            raise RuntimeError(f"recovery path exists: {path}")
    _load_process_environment(args.source_process_id)

    from sqlalchemy import select, update

    from app.agent2.personal_weekly_brief_store import (
        SqlPersonalWeeklyBriefStore,
        _briefs,
        _recovery_append,
    )
    from app.config import get_settings
    from app.db import AsyncSessionLocal, engine
    from app.llm.client import LLMClient
    from app.models import User
    from app.scheduler.runner import (
        run_personal_weekly_brief_generation_job,
        run_personal_weekly_brief_reconcile_job,
    )
    from app.services.dingtalk import DingTalkRobotClient

    get_settings.cache_clear()
    settings = get_settings()
    tenant_id = str(settings.agent2_personal_weekly_brief_tenant_id or "").strip()
    week_start = SATURDAY.date() - __import__("datetime").timedelta(days=5)
    if not tenant_id or week_start.isoformat() != "2026-08-17":
        raise RuntimeError("recovery scope is invalid")

    async def load_rows(session):
        return await SqlPersonalWeeklyBriefStore(session).load_for_week(
            tenant_id=tenant_id,
            week_start=week_start,
            limit=100,
        )

    async with AsyncSessionLocal() as session:
        rows_before = await load_rows(session)
        users = tuple(
            (
                await session.scalars(
                    select(User).where(
                        User.id.in_(
                            [__import__("uuid").UUID(row.owner_user_id) for row in rows_before]
                        )
                    )
                )
            ).all()
        )
        names = {str(user.id): user.name for user in users}
        raw_backup = list(
            (
                await session.execute(
                    select(_briefs).where(
                        _briefs.c.tenant_id == tenant_id,
                        _briefs.c.week_start == week_start,
                    )
                )
            ).mappings().all()
        )
        await session.rollback()
    before_counts = Counter(row.status for row in rows_before)

    if args.action == "prepare":
        if before_counts != {
            "delivered": 1,
            "delivery_pending": 4,
            "generated": 18,
            "generating": 2,
            "generation_failed": 49,
        }:
            raise RuntimeError("weekly brief recovery state changed before prepare")
        _write(args.backup, raw_backup)
        changed_at = datetime.now(TIMEZONE)
        async with AsyncSessionLocal() as session:
            interrupted = await session.execute(
                update(_briefs)
                .where(
                    _briefs.c.tenant_id == tenant_id,
                    _briefs.c.week_start == week_start,
                    _briefs.c.status == "generating",
                )
                .values(
                    status="generation_failed",
                    last_error="generation_interrupted_before_v26",
                    failed_at=changed_at,
                    retry_count=_briefs.c.retry_count + 1,
                    recovery_json=_recovery_append(
                        kind="generation_interrupted",
                        reason="previous rollout worker stopped before v26",
                        changed_at=changed_at,
                    ),
                    updated_at=changed_at,
                )
            )
            if interrupted.rowcount != 2:
                raise RuntimeError("interrupted generation count changed")
            requeued = await session.execute(
                update(_briefs)
                .where(
                    _briefs.c.tenant_id == tenant_id,
                    _briefs.c.week_start == week_start,
                    _briefs.c.status == "generation_failed",
                )
                .values(
                    status="snapshot_ready",
                    generation_started_at=None,
                    generated_at=None,
                    failed_at=None,
                    last_error="",
                    retry_count=0,
                    recovery_json=_recovery_append(
                        kind="generation_release_requeued",
                        reason="retry with reviewed compact-source v26 pipeline",
                        changed_at=changed_at,
                    ),
                    updated_at=changed_at,
                )
            )
            if requeued.rowcount != 51:
                raise RuntimeError("release requeue count changed")
            await session.commit()

    result: dict[str, object] = {}
    if args.action == "run":
        if before_counts != {
            "delivered": 1,
            "delivery_pending": 4,
            "generated": 18,
            "snapshot_ready": 51,
        }:
            raise RuntimeError("weekly brief recovery state changed before run")
        _write(
            args.output,
            {"status": "started", "before_counts": dict(before_counts)},
        )
        llm_client = LLMClient(settings)
        robot = DingTalkRobotClient(settings)
        try:
            result["reconcile_before"] = (
                await run_personal_weekly_brief_reconcile_job(
                    settings,
                    llm_client=llm_client,
                    robot=robot,
                    now=datetime.now(TIMEZONE),
                )
            )
            result["generation"] = await run_personal_weekly_brief_generation_job(
                settings,
                llm_client=llm_client,
                robot=robot,
                now=SATURDAY,
            )
            result["reconcile_after_first"] = (
                await run_personal_weekly_brief_reconcile_job(
                    settings,
                    llm_client=llm_client,
                    robot=robot,
                    now=datetime.now(TIMEZONE),
                )
            )
            result["reconcile_after_second"] = (
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
        rows_after = await load_rows(session)
        await session.rollback()
    after_counts = Counter(row.status for row in rows_after)
    failed_names = {
        names.get(row.owner_user_id, "未知用户"): row.last_error
        for row in rows_after
        if row.status in {"failed", "generation_failed"}
    }
    delivered = tuple(row for row in rows_after if row.status == "delivered")
    payload = {
        "status": "PASS",
        "action": args.action,
        "before_counts": dict(before_counts),
        "after_counts": dict(after_counts),
        "delivered": len(delivered),
        "context_recorded": sum(
            row.context_recorded_at is not None for row in rows_after
        ),
        "failed_names": failed_names,
        "job_result": result,
        "database_backup_written": args.action == "prepare",
    }
    if args.action == "prepare" and after_counts != {
        "delivered": 1,
        "delivery_pending": 4,
        "generated": 18,
        "snapshot_ready": 51,
    }:
        payload["status"] = "FAIL"
    if args.action == "run" and not (
        after_counts == {"delivered": 73, "failed": 1}
        and payload["context_recorded"] == 72
        and set(failed_names) == {"周星星"}
    ):
        payload["status"] = "PARTIAL"
    if args.output is not None:
        _write(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    await engine.dispose()
    if payload["status"] not in {"PASS", "PARTIAL"}:
        raise SystemExit(1)
    if args.action == "run" and payload["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
