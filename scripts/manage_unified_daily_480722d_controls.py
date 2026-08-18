from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.canary_store import (
    ToolCallCanaryControl,
    ToolCallCanaryControlRepository,
    _control_mapping,
)
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal, engine


ACTOR = "codex-agent2-context-followup-fe3fe22"
UPDATE_REASON = (
    "Deploy contextual Daily replacement and updated snapshot fix fe3fe22 on 2026-08-18"
)
EXPECTED_CONTROLS = 74


def _snapshot(row: ToolCallCanaryControl) -> dict[str, object]:
    return {
        "control_id": str(row.control_id),
        "control_key": row.control_key,
        "tenant_id": row.tenant_id,
        "user_id": row.user_id,
        "enabled": row.enabled,
        "runtime": row.runtime,
        "messages_enabled": row.messages_enabled,
        "registry_digest": row.registry_digest,
        "prompt_sha256": row.prompt_sha256,
        "model_name": row.model_name,
        "version": row.version,
        "changed_by": row.changed_by,
        "change_reason": row.change_reason,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


async def _rows(session, *, lock: bool = False):
    statement = select(ToolCallCanaryControl).order_by(
        ToolCallCanaryControl.control_key
    )
    if lock:
        statement = statement.with_for_update()
    return list((await session.scalars(statement)).all())


def _write_backup(path: Path, payload: dict) -> dict[str, object]:
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
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _load_backup(path: Path) -> dict[str, dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    controls = payload.get("controls")
    if not isinstance(controls, list) or len(controls) != EXPECTED_CONTROLS:
        raise RuntimeError("invalid full-control backup")
    by_id = {str(item["control_id"]): item for item in controls}
    if len(by_id) != EXPECTED_CONTROLS:
        raise RuntimeError("control backup contains duplicate IDs")
    return by_id


async def backup(path: Path) -> None:
    async with AsyncSessionLocal() as session:
        rows = await _rows(session)
    if len(rows) != EXPECTED_CONTROLS:
        raise RuntimeError(f"expected 74 controls, got {len(rows)}")
    payload = {
        "schema_version": "agent2.context-followup-fe3fe22.controls-backup.v1",
        "controls": [_snapshot(row) for row in rows],
        "count": len(rows),
    }
    print(
        json.dumps(
            {
                "action": "backup",
                "count": len(rows),
                "enabled": sum(row.enabled for row in rows),
                "messages_enabled": sum(row.messages_enabled for row in rows),
                "backup": _write_backup(path, payload),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


async def update(path: Path) -> None:
    expected = _load_backup(path)
    settings = get_settings()
    registry_digest = runtime_registry_contract_digest(settings)
    prompt_digest = canary_prompt_sha256()
    async with AsyncSessionLocal() as session:
        rows = await _rows(session, lock=True)
        if {str(row.control_id) for row in rows} != set(expected):
            raise RuntimeError("control set changed after backup")
        repository = ToolCallCanaryControlRepository(session)
        for row in rows:
            before_expected = expected[str(row.control_id)]
            for field in (
                "control_key",
                "tenant_id",
                "user_id",
                "enabled",
                "runtime",
                "messages_enabled",
                "registry_digest",
                "prompt_sha256",
                "model_name",
                "version",
            ):
                if getattr(row, field) != before_expected[field]:
                    raise RuntimeError(
                        f"control changed after backup: {row.control_key}:{field}"
                    )
            before = _control_mapping(row)
            row.registry_digest = registry_digest
            row.prompt_sha256 = prompt_digest
            row.version += 1
            row.changed_by = ACTOR
            row.change_reason = UPDATE_REASON
            await session.flush()
            repository._add_audit(
                control=row,
                before=before,
                actor_user_id=ACTOR,
                source_change_id=(
                    f"agent2-context-followup-fe3fe22:{row.control_id}:{uuid4()}"
                ),
                reason=UPDATE_REASON,
            )
        await session.commit()
    print(
        json.dumps(
            {
                "action": "update",
                "count": len(rows),
                "registry_digest": registry_digest,
                "prompt_sha256": prompt_digest,
                "model_name": CANARY_MODEL_NAME,
            },
            sort_keys=True,
        )
    )


async def restore(path: Path) -> None:
    expected = _load_backup(path)
    async with AsyncSessionLocal() as session:
        rows = await _rows(session, lock=True)
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
            row.change_reason = "Rollback Agent2 context followup fe3fe22 control alignment"
            await session.flush()
            repository._add_audit(
                control=row,
                before=before,
                actor_user_id=ACTOR,
                source_change_id=(
                    f"agent2-context-followup-fe3fe22-rollback:{row.control_id}:{uuid4()}"
                ),
                reason=row.change_reason,
            )
        await session.commit()
    print(json.dumps({"action": "restore", "count": len(rows)}))


async def verify() -> None:
    settings = get_settings()
    expected_registry = runtime_registry_contract_digest(settings)
    expected_prompt = canary_prompt_sha256()
    async with AsyncSessionLocal() as session:
        rows = await _rows(session)
    mismatches = [
        row.control_key
        for row in rows
        if row.registry_digest != expected_registry
        or row.prompt_sha256 != expected_prompt
        or row.model_name != CANARY_MODEL_NAME
    ]
    result = {
        "action": "verify",
        "count": len(rows),
        "enabled": sum(row.enabled for row in rows),
        "messages_enabled": sum(row.messages_enabled for row in rows),
        "mismatch_count": len(mismatches),
        "registry_digest": expected_registry,
        "prompt_sha256": expected_prompt,
        "model_name": CANARY_MODEL_NAME,
    }
    print(json.dumps(result, sort_keys=True))
    if (
        len(rows) != EXPECTED_CONTROLS
        or result["enabled"] != EXPECTED_CONTROLS
        or result["messages_enabled"] != EXPECTED_CONTROLS
        or mismatches
    ):
        raise RuntimeError("Agent2 full-control alignment failed")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("backup", "update", "restore", "verify"))
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
        await verify()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
