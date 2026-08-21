from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from app.agent2.tool_calling.canary_store import (
    ToolCallCanaryControlRepository,
    _control_mapping,
)
from app.db import AsyncSessionLocal, engine
from scripts import manage_unified_daily_480722d_controls as base


ACTOR = "codex-agent2-weekly-plan-b6bfb95"
UPDATE_REASON = (
    "Deploy Agent2 Weekly Work Plan hardening b6bfb95 on 2026-08-21"
)
RESTORE_REASON = (
    "Rollback Agent2 Weekly Work Plan hardening b6bfb95 controls"
)


async def backup(path: Path) -> None:
    async with AsyncSessionLocal() as session:
        rows = await base._rows(session)
    if len(rows) != base.EXPECTED_CONTROLS:
        raise RuntimeError(f"expected 74 controls, got {len(rows)}")
    payload = {
        "schema_version": "agent2.weekly-plan-b6bfb95.controls-backup.v1",
        "controls": [base._snapshot(row) for row in rows],
        "count": len(rows),
    }
    print(
        json.dumps(
            {
                "action": "backup",
                "count": len(rows),
                "enabled": sum(row.enabled for row in rows),
                "messages_enabled": sum(row.messages_enabled for row in rows),
                "backup": base._write_backup(path, payload),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


async def update(path: Path) -> None:
    base.ACTOR = ACTOR
    base.UPDATE_REASON = UPDATE_REASON
    await base.update(path)


async def restore(path: Path) -> None:
    expected = base._load_backup(path)
    async with AsyncSessionLocal() as session:
        rows = await base._rows(session, lock=True)
        if {str(row.control_id) for row in rows} != set(expected):
            raise RuntimeError("control set changed; refusing restore")
        repository = ToolCallCanaryControlRepository(session)
        for row in rows:
            target = expected[str(row.control_id)]
            before = _control_mapping(row)
            row.enabled = bool(target["enabled"])
            row.runtime = str(target["runtime"])
            row.messages_enabled = bool(target["messages_enabled"])
            row.registry_digest = str(target["registry_digest"])
            row.prompt_sha256 = str(target["prompt_sha256"])
            row.model_name = str(target["model_name"])
            row.version += 1
            row.changed_by = ACTOR
            row.change_reason = RESTORE_REASON
            await session.flush()
            repository._add_audit(
                control=row,
                before=before,
                actor_user_id=ACTOR,
                source_change_id=(
                    f"agent2-weekly-plan-b6bfb95-rollback:"
                    f"{row.control_id}:{uuid4()}"
                ),
                reason=RESTORE_REASON,
            )
        await session.commit()
    print(json.dumps({"action": "restore", "count": len(rows)}))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("backup", "update", "restore", "verify"),
    )
    parser.add_argument("--backup-path", type=Path)
    args = parser.parse_args()
    if args.action in {"backup", "update", "restore"} and args.backup_path is None:
        parser.error("--backup-path is required")
    if args.action == "backup":
        await backup(args.backup_path)
    elif args.action == "update":
        await update(args.backup_path)
    elif args.action == "restore":
        await restore(args.backup_path)
    else:
        await base.verify()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
