from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent2.business.models import Agent2IdentityBinding
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.weekly_plan_access import (
    WeeklyPlanAccessAction,
    WeeklyPlanAccessPolicy,
)
from app.agent2.weekly_plan_domain import _stable_id
from app.agent2.weekly_plan_store import (
    _audits,
    _batches,
    _days,
    _items,
    _plans,
    _receipts,
    _roster,
    _suggestions,
)
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.legal_daily_roster import load_formal_legal_daily_roster
from app.scheduler.runner import register_weekly_plan_jobs

EXPECTED_USERS = 74
ENV_PATH = Path("/home/ai_review_tunnel/ai-review-system/.env")
ENV_KEYS = (
    "AGENT2_WEEKLY_PLAN_ENABLED",
    "AGENT2_WEEKLY_PLAN_WRITE_ENABLED",
    "AGENT2_WEEKLY_PLAN_SEND_ENABLED",
    "AGENT2_WEEKLY_PLAN_TENANT_ALLOWLIST",
    "AGENT2_WEEKLY_PLAN_USER_ALLOWLIST",
    "AGENT2_WEEKLY_PLAN_SEND_USER_ALLOWLIST",
    "WEEKLY_PLAN_COLLECTION_OPEN_HOUR",
    "WEEKLY_PLAN_COLLECTION_OPEN_MINUTE",
    "WEEKLY_PLAN_REMINDER_HOUR",
    "WEEKLY_PLAN_REMINDER_MINUTE",
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_env(path: Path) -> tuple[bytes, dict[str, str]]:
    encoded = path.read_bytes()
    values: dict[str, str] = {}
    for raw_line in encoded.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in ENV_KEYS:
            if key in values:
                raise RuntimeError(f"duplicate weekly-plan env key: {key}")
            values[key] = value
    missing = set(ENV_KEYS) - set(values)
    if missing:
        raise RuntimeError(f"missing weekly-plan env keys: {sorted(missing)}")
    return encoded, values


def _write_env(path: Path, replacements: dict[str, str]) -> dict[str, object]:
    encoded, current = _read_env(path)
    if set(replacements) != set(ENV_KEYS):
        raise RuntimeError("weekly-plan env replacement is incomplete")
    lines = encoded.decode("utf-8").splitlines(keepends=True)
    replaced: set[str] = set()
    output: list[str] = []
    for raw_line in lines:
        stripped = raw_line.rstrip("\r\n")
        key = stripped.split("=", 1)[0] if "=" in stripped else ""
        if key not in replacements:
            output.append(raw_line)
            continue
        newline = "\r\n" if raw_line.endswith("\r\n") else "\n"
        if not raw_line.endswith(("\n", "\r")):
            newline = ""
        output.append(f"{key}={replacements[key]}{newline}")
        replaced.add(key)
    if replaced != set(replacements):
        raise RuntimeError("weekly-plan env replacement did not cover every key")
    updated = "".join(output).encode("utf-8")
    temporary = path.with_name(f".{path.name}.weekly-plan-{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    os.chmod(path, 0o600)
    return {
        "before_sha256": _sha256(encoded),
        "after_sha256": _sha256(updated),
        "changed": current != replacements,
    }


async def _exact_scope(session) -> tuple[str, tuple[str, ...], dict[str, Agent2IdentityBinding]]:
    settings = get_settings()
    tenant_id = str(settings.agent2_weekly_plan_tenant_allowlist or "").strip()
    legal_tenant_id = str(settings.legal_daily_dashboard_tenant_id or "").strip()
    if not tenant_id or not legal_tenant_id:
        raise RuntimeError("weekly-plan or formal-roster tenant is missing")
    formal = await load_formal_legal_daily_roster(
        session,
        tenant_id=legal_tenant_id,
        on_date=datetime.now(ZoneInfo(settings.timezone)).date(),
    )
    formal_user_ids = set(formal.user_ids)
    if len(formal_user_ids) != EXPECTED_USERS:
        raise RuntimeError(f"expected {EXPECTED_USERS} formal users")
    bindings = list(
        (
            await session.scalars(
                select(Agent2IdentityBinding).where(
                    Agent2IdentityBinding.tenant_id == tenant_id,
                    Agent2IdentityBinding.active.is_(True),
                )
            )
        ).all()
    )
    by_user_id = {str(binding.user_id): binding for binding in bindings}
    if set(by_user_id) != formal_user_ids:
        raise RuntimeError("weekly-plan identity bindings do not match the formal 74")
    dingtalk_ids = {
        str(binding.dingtalk_user_id).strip() for binding in bindings
    }
    if "" in dingtalk_ids or len(dingtalk_ids) != EXPECTED_USERS:
        raise RuntimeError("weekly-plan DingTalk identities are incomplete or duplicated")
    controls = list(
        (
            await session.scalars(
                select(ToolCallCanaryControl).where(
                    ToolCallCanaryControl.tenant_id == tenant_id,
                    ToolCallCanaryControl.enabled.is_(True),
                    ToolCallCanaryControl.messages_enabled.is_(True),
                )
            )
        ).all()
    )
    if {str(control.user_id) for control in controls} != formal_user_ids:
        raise RuntimeError("Agent2 controls do not match the formal 74")
    return tenant_id, tuple(sorted(formal_user_ids)), by_user_id


async def _batch_snapshot(session, *, tenant_id: str, target_week_start: date) -> dict[str, object]:
    batch = (
        await session.execute(
            select(_batches).where(
                _batches.c.tenant_id == tenant_id,
                _batches.c.target_week_start == target_week_start,
            )
        )
    ).mappings().one_or_none()
    if batch is None:
        return {
            "batch_id": "",
            "roster_user_ids": [],
            "plan_owner_user_ids": [],
            "plan_state_sha256": _sha256(b"[]"),
        }
    batch_id = batch["batch_id"]
    roster_user_ids = tuple(
        (
            await session.scalars(
                select(_roster.c.user_id)
                .where(
                    _roster.c.tenant_id == tenant_id,
                    _roster.c.batch_id == batch_id,
                )
                .order_by(_roster.c.user_id)
            )
        ).all()
    )
    plan_owner_user_ids = tuple(
        (
            await session.scalars(
                select(_plans.c.owner_user_id)
                .where(
                    _plans.c.tenant_id == tenant_id,
                    _plans.c.batch_id == batch_id,
                )
                .order_by(_plans.c.owner_user_id)
            )
        ).all()
    )
    plan_ids = tuple(
        (
            await session.scalars(
                select(_plans.c.plan_id)
                .where(
                    _plans.c.tenant_id == tenant_id,
                    _plans.c.batch_id == batch_id,
                )
                .order_by(_plans.c.plan_id)
            )
        ).all()
    )
    state_rows: list[dict[str, object]] = []
    table_specs = (
        (_plans, _plans.c.plan_id, _plans.c.batch_id == batch_id),
        (_days, _days.c.day_id, _days.c.plan_id.in_(plan_ids)),
        (_items, _items.c.item_id, _items.c.plan_id.in_(plan_ids)),
        (
            _suggestions,
            _suggestions.c.suggestion_id,
            _suggestions.c.plan_id.in_(plan_ids),
        ),
        (_receipts, _receipts.c.receipt_id, _receipts.c.plan_id.in_(plan_ids)),
        (_audits, _audits.c.audit_id, _audits.c.plan_id.in_(plan_ids)),
    )
    for table, order_column, condition in table_specs:
        rows = (
            await session.execute(
                select(table)
                .where(table.c.tenant_id == tenant_id, condition)
                .order_by(order_column)
            )
        ).mappings().all()
        state_rows.append(
            {
                "table": table.name,
                "rows": [dict(row) for row in rows],
            }
        )
    plan_state = json.dumps(
        state_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return {
        "batch_id": str(batch_id),
        "roster_user_ids": list(roster_user_ids),
        "plan_owner_user_ids": list(plan_owner_user_ids),
        "plan_state_sha256": _sha256(plan_state),
    }


async def _expand_existing_batch(
    session,
    *,
    tenant_id: str,
    user_ids: tuple[str, ...],
    bindings: dict[str, Agent2IdentityBinding],
    target_week_start: date,
) -> tuple[int, str]:
    batch = (
        await session.execute(
            select(_batches)
            .where(
                _batches.c.tenant_id == tenant_id,
                _batches.c.target_week_start == target_week_start,
            )
            .with_for_update()
        )
    ).mappings().one_or_none()
    if batch is None:
        return 0, ""
    existing = set(
        (
            await session.scalars(
                select(_roster.c.user_id).where(
                    _roster.c.tenant_id == tenant_id,
                    _roster.c.batch_id == batch["batch_id"],
                )
            )
        ).all()
    )
    if not existing.issubset(user_ids):
        raise RuntimeError("existing weekly-plan roster is outside the formal 74")
    inserted = 0
    for user_id in user_ids:
        if user_id in existing:
            continue
        binding = bindings[user_id]
        result = await session.execute(
            pg_insert(_roster)
            .values(
                roster_member_id=UUID(
                    _stable_id(
                        "weekly-plan-roster",
                        str(batch["batch_id"]),
                        user_id,
                    )
                ),
                tenant_id=tenant_id,
                batch_id=batch["batch_id"],
                user_id=user_id,
                display_name=str(binding.display_name or ""),
                department_id=str(binding.department_id or ""),
                department_name="",
                team_id=str(binding.team_id or ""),
                team_name="",
                created_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    _roster.c.tenant_id,
                    _roster.c.batch_id,
                    _roster.c.user_id,
                ]
            )
        )
        inserted += int(result.rowcount or 0)
    final_roster = set(
        (
            await session.scalars(
                select(_roster.c.user_id).where(
                    _roster.c.tenant_id == tenant_id,
                    _roster.c.batch_id == batch["batch_id"],
                )
            )
        ).all()
    )
    if final_roster != set(user_ids):
        raise RuntimeError("weekly-plan batch did not expand to the formal 74")
    return inserted, str(batch["batch_id"])


def _write_backup(path: Path, payload: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
    return {
        "path": str(path),
        "bytes": len(encoded),
        "sha256": _sha256(encoded),
    }


def _load_backup(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "agent2.weekly-plan-full-rollout.20260821.v1":
        raise RuntimeError("invalid weekly-plan rollout backup")
    return payload


async def backup(path: Path, *, target_week_start: date) -> None:
    env_encoded, env_values = _read_env(ENV_PATH)
    async with AsyncSessionLocal() as session:
        tenant_id, user_ids, _bindings = await _exact_scope(session)
        batch = await _batch_snapshot(
            session,
            tenant_id=tenant_id,
            target_week_start=target_week_start,
        )
    payload = {
        "schema_version": "agent2.weekly-plan-full-rollout.20260821.v1",
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "target_week_start": target_week_start.isoformat(),
        "tenant_id": tenant_id,
        "formal_user_count": len(user_ids),
        "formal_user_ids_sha256": _sha256(",".join(user_ids).encode("utf-8")),
        "env_sha256": _sha256(env_encoded),
        "env_values": env_values,
        "batch": batch,
    }
    result = _write_backup(path, payload)
    print(json.dumps({"action": "backup", "user_count": len(user_ids), "batch_roster_count": len(batch["roster_user_ids"]), "batch_plan_count": len(batch["plan_owner_user_ids"]), "backup": result}, sort_keys=True))


async def apply(path: Path, *, target_week_start: date) -> None:
    backup_payload = _load_backup(path)
    if backup_payload["target_week_start"] != target_week_start.isoformat():
        raise RuntimeError("backup target week does not match")
    env_encoded, env_values = _read_env(ENV_PATH)
    if _sha256(env_encoded) != backup_payload["env_sha256"] or env_values != backup_payload["env_values"]:
        raise RuntimeError("weekly-plan env changed after backup")
    inserted = 0
    async with AsyncSessionLocal() as session:
        tenant_id, user_ids, bindings = await _exact_scope(session)
        if tenant_id != backup_payload["tenant_id"]:
            raise RuntimeError("weekly-plan tenant changed after backup")
        inserted, _batch_id = await _expand_existing_batch(
            session,
            tenant_id=tenant_id,
            user_ids=user_ids,
            bindings=bindings,
            target_week_start=target_week_start,
        )
        await session.commit()
    scope = ",".join(user_ids)
    env_result = _write_env(
        ENV_PATH,
        {
            "AGENT2_WEEKLY_PLAN_ENABLED": "true",
            "AGENT2_WEEKLY_PLAN_WRITE_ENABLED": "true",
            "AGENT2_WEEKLY_PLAN_SEND_ENABLED": "true",
            "AGENT2_WEEKLY_PLAN_TENANT_ALLOWLIST": tenant_id,
            "AGENT2_WEEKLY_PLAN_USER_ALLOWLIST": scope,
            "AGENT2_WEEKLY_PLAN_SEND_USER_ALLOWLIST": scope,
            "WEEKLY_PLAN_COLLECTION_OPEN_HOUR": "15",
            "WEEKLY_PLAN_COLLECTION_OPEN_MINUTE": "0",
            "WEEKLY_PLAN_REMINDER_HOUR": "15",
            "WEEKLY_PLAN_REMINDER_MINUTE": "0",
        },
    )
    print(json.dumps({"action": "apply", "user_count": len(user_ids), "inserted_roster_members": inserted, "env": env_result}, sort_keys=True))


async def smoke(*, target_week_start: date) -> None:
    async with AsyncSessionLocal() as session:
        tenant_id, user_ids, bindings = await _exact_scope(session)
        before = await _batch_snapshot(
            session,
            tenant_id=tenant_id,
            target_week_start=target_week_start,
        )
        inserted, batch_id = await _expand_existing_batch(
            session,
            tenant_id=tenant_id,
            user_ids=user_ids,
            bindings=bindings,
            target_week_start=target_week_start,
        )
        during = await _batch_snapshot(
            session,
            tenant_id=tenant_id,
            target_week_start=target_week_start,
        )
        if batch_id and len(during["roster_user_ids"]) != EXPECTED_USERS:
            raise RuntimeError("weekly-plan rollout smoke did not reach 74 users")
        if during["plan_state_sha256"] != before["plan_state_sha256"]:
            raise RuntimeError("weekly-plan rollout smoke changed an existing plan")
        await session.rollback()
    async with AsyncSessionLocal() as session:
        tenant_id, _user_ids, _bindings = await _exact_scope(session)
        restored = await _batch_snapshot(
            session,
            tenant_id=tenant_id,
            target_week_start=target_week_start,
        )
    if restored != before:
        raise RuntimeError("weekly-plan rollout smoke left database residue")
    print(json.dumps({"action": "smoke", "inserted_then_rolled_back": inserted, "before_roster_count": len(before["roster_user_ids"]), "during_roster_count": len(during["roster_user_ids"]), "restored": True}, sort_keys=True))


async def restore(path: Path, *, target_week_start: date) -> None:
    payload = _load_backup(path)
    original = payload["batch"]
    original_ids = set(original["roster_user_ids"])
    tenant_id = str(payload["tenant_id"])
    fallback_disable = False
    removed = 0
    batch_id = str(original["batch_id"] or "")
    if batch_id:
        async with AsyncSessionLocal() as session:
            added_ids = set(
                (
                    await session.scalars(
                        select(_roster.c.user_id).where(
                            _roster.c.tenant_id == tenant_id,
                            _roster.c.batch_id == UUID(batch_id),
                            _roster.c.user_id.not_in(original_ids),
                        )
                    )
                ).all()
            )
            added_plan_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(_plans)
                    .where(
                        _plans.c.tenant_id == tenant_id,
                        _plans.c.batch_id == UUID(batch_id),
                        _plans.c.owner_user_id.in_(added_ids),
                    )
                )
                or 0
            )
            if added_plan_count:
                fallback_disable = True
            elif added_ids:
                result = await session.execute(
                    delete(_roster).where(
                        _roster.c.tenant_id == tenant_id,
                        _roster.c.batch_id == UUID(batch_id),
                        _roster.c.user_id.in_(added_ids),
                    )
                )
                removed = int(result.rowcount or 0)
            await session.commit()
    else:
        async with AsyncSessionLocal() as session:
            new_batch_exists = (
                await session.scalar(
                    select(_batches.c.batch_id).where(
                        _batches.c.tenant_id == tenant_id,
                        _batches.c.target_week_start == target_week_start,
                    )
                )
            ) is not None
            await session.rollback()
        # Never delete a batch created after rollout: it may already contain a
        # real user's plan.  Disable Weekly Plan on rollback and preserve data.
        fallback_disable = new_batch_exists
    replacements = dict(payload["env_values"])
    if fallback_disable:
        replacements["AGENT2_WEEKLY_PLAN_ENABLED"] = "false"
        replacements["AGENT2_WEEKLY_PLAN_WRITE_ENABLED"] = "false"
        replacements["AGENT2_WEEKLY_PLAN_SEND_ENABLED"] = "false"
    env_result = _write_env(ENV_PATH, replacements)
    print(json.dumps({"action": "restore", "removed_roster_members": removed, "fallback_disabled": fallback_disable, "env": env_result}, sort_keys=True))


async def verify(*, target_week_start: date) -> None:
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        tenant_id, user_ids, _bindings = await _exact_scope(session)
        batch = await _batch_snapshot(
            session,
            tenant_id=tenant_id,
            target_week_start=target_week_start,
        )
    configured = tuple(
        sorted(
            value
            for value in str(settings.agent2_weekly_plan_user_allowlist).split(",")
            if value
        )
    )
    send_configured = tuple(
        sorted(
            value
            for value in str(settings.agent2_weekly_plan_send_user_allowlist).split(",")
            if value
        )
    )
    if (
        settings.agent2_weekly_plan_enabled is not True
        or settings.agent2_weekly_plan_write_enabled is not True
        or settings.agent2_weekly_plan_send_enabled is not True
        or configured != user_ids
        or send_configured != user_ids
        or settings.weekly_plan_collection_open_hour != 15
        or settings.weekly_plan_collection_open_minute != 0
        or settings.weekly_plan_reminder_hour != 15
        or settings.weekly_plan_reminder_minute != 0
    ):
        raise RuntimeError("weekly-plan full-rollout settings are not exact")
    if batch["batch_id"] and set(batch["roster_user_ids"]) != set(user_ids):
        raise RuntimeError("weekly-plan current batch roster is not the formal 74")
    policy = WeeklyPlanAccessPolicy(
        enabled=True,
        write_enabled=True,
        send_enabled=True,
        tenant_allowlist=frozenset({tenant_id}),
        user_allowlist=frozenset(user_ids),
        send_user_allowlist=frozenset(user_ids),
    )
    for user_id in user_ids:
        for action in WeeklyPlanAccessAction:
            decision = policy.decide(
                action=action,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_kind="direct",
            )
            if not decision.allowed:
                raise RuntimeError(f"weekly-plan access denied: {action.value}")
    outsider = policy.decide(
        action=WeeklyPlanAccessAction.WRITE,
        tenant_id=tenant_id,
        user_id="outside-formal-roster",
        conversation_kind="direct",
    )
    if outsider.allowed:
        raise RuntimeError("weekly-plan outsider unexpectedly allowed")
    jobs: list[dict[str, object]] = []

    class _Scheduler:
        def add_job(self, func, trigger, **kwargs) -> None:
            jobs.append({"func": func, "trigger": trigger, **kwargs})

    async def _noop() -> None:
        return None

    registered = register_weekly_plan_jobs(
        _Scheduler(),
        settings=settings,
        open_job=_noop,
        reminder_job=_noop,
        reminder_reconcile_job=_noop,
        snapshot_job=_noop,
    )
    if registered != (
        "agent2_weekly_plan_collection_open",
        "agent2_weekly_plan_reminder_enqueue",
        "agent2_weekly_plan_reminder_reconcile",
        "agent2_weekly_plan_monday_snapshot",
    ):
        raise RuntimeError("weekly-plan scheduler jobs are incomplete")
    if [str(job["trigger"]) for job in jobs[:2]] != [
        "cron[day_of_week='fri', hour='15', minute='0']",
        "cron[day_of_week='fri', hour='15', minute='0']",
    ]:
        raise RuntimeError("weekly-plan Friday schedule is not 15:00")
    print(json.dumps({"action": "verify", "user_count": len(user_ids), "read_write_send": "74/74", "batch_roster_count": len(batch["roster_user_ids"]), "batch_plan_count": len(batch["plan_owner_user_ids"]), "registered_jobs": len(registered), "friday_reminder": "15:00"}, sort_keys=True))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("backup", "apply", "restore", "verify", "smoke"))
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--target-week-start", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    if args.action in {"backup", "apply", "restore"} and args.backup_path is None:
        parser.error("--backup-path is required")
    if args.action == "backup":
        await backup(args.backup_path, target_week_start=args.target_week_start)
    elif args.action == "apply":
        await apply(args.backup_path, target_week_start=args.target_week_start)
    elif args.action == "restore":
        await restore(args.backup_path, target_week_start=args.target_week_start)
    elif args.action == "smoke":
        await smoke(target_week_start=args.target_week_start)
    else:
        await verify(target_week_start=args.target_week_start)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
