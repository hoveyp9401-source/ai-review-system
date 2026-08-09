from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.canary_store import (
    ToolCallCanaryControl,
    ToolCallCanaryControlAudit,
)
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal, engine


EXPECTED_ROSTER_COUNT = 74
EXPECTED_CHILD_TEAMS = {
    "法务一部",
    "法务二部",
    "法务三部",
    "法务四部",
    "法务五部",
    "法务六部",
    "综合管理部",
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        required=True,
        choices=("check", "activate", "restore"),
    )
    parser.add_argument("--backup-path", type=Path, required=True)
    parser.add_argument(
        "--release-key",
        default="agent2-stability-v19-20260808",
        help="Unique release key used for append-only control audit IDs.",
    )
    parser.add_argument(
        "--reason",
        default=(
            "activate Agent2 managed-query, scheduler, and "
            "conversation stability release"
        ),
    )
    return parser.parse_args()


def _mapping(control: ToolCallCanaryControl) -> dict[str, object]:
    return {
        "control_key": control.control_key,
        "tenant_id": control.tenant_id,
        "user_id": control.user_id,
        "enabled": control.enabled,
        "runtime": control.runtime,
        "messages_enabled": control.messages_enabled,
        "registry_digest": control.registry_digest,
        "prompt_sha256": control.prompt_sha256,
        "model_name": control.model_name,
        "version": control.version,
        "changed_by": control.changed_by,
        "change_reason": control.change_reason,
    }


def _distribution(
    controls: list[ToolCallCanaryControl],
) -> dict[str, object]:
    return {
        "registry_digest": dict(
            Counter(row.registry_digest for row in controls)
        ),
        "prompt_sha256": dict(
            Counter(row.prompt_sha256 for row in controls)
        ),
        "model_name": dict(Counter(row.model_name for row in controls)),
        "version_min": min(row.version for row in controls),
        "version_max": max(row.version for row in controls),
    }


async def _roster_user_ids(session, tenant_id: str) -> set[str]:
    rows = (
        await session.execute(
            text(
                """
                SELECT
                    users.id::text AS user_id,
                    teams.name AS team_name,
                    teams.code AS team_code,
                    teams.active AS team_active
                FROM legal_daily_team_memberships memberships
                JOIN teams ON teams.id = memberships.team_id
                JOIN users ON users.id = memberships.user_id
                WHERE memberships.tenant_id = :tenant_id
                  AND memberships.effective_from <= CURRENT_DATE
                  AND (
                      memberships.effective_to IS NULL
                      OR memberships.effective_to >= CURRENT_DATE
                  )
                  AND (teams.active IS TRUE OR teams.code = 'legal-center')
                ORDER BY users.id
                """
            ),
            {"tenant_id": tenant_id},
        )
    ).mappings().all()
    if len(rows) != EXPECTED_ROSTER_COUNT:
        raise AssertionError(f"roster count is {len(rows)}, expected 74")
    user_ids = {str(row["user_id"]) for row in rows}
    if len(user_ids) != EXPECTED_ROSTER_COUNT:
        raise AssertionError("current roster contains duplicate memberships")
    child_team_names = {
        str(row["team_name"])
        for row in rows
        if bool(row["team_active"])
    }
    if child_team_names != EXPECTED_CHILD_TEAMS:
        raise AssertionError(sorted(child_team_names))
    center_direct = [
        row
        for row in rows
        if str(row["team_code"] or "") == "legal-center"
        and not bool(row["team_active"])
    ]
    if len(center_direct) != 2:
        raise AssertionError(f"center direct count is {len(center_direct)}")
    return user_ids


async def main() -> None:
    args = _args()
    settings = get_settings()
    tenant_id = str(settings.legal_daily_dashboard_tenant_id or "").strip()
    if not tenant_id:
        raise AssertionError("legal daily tenant is not configured")
    candidate = {
        "registry_digest": runtime_registry_contract_digest(settings),
        "prompt_sha256": canary_prompt_sha256(),
        "model_name": CANARY_MODEL_NAME,
    }
    backup_payload: dict[str, object] | None = None
    if args.mode == "restore":
        backup_payload = json.loads(args.backup_path.read_text(encoding="utf-8"))
    changed_count = 0
    try:
        async with AsyncSessionLocal() as session:
            roster_user_ids = await _roster_user_ids(session, tenant_id)
            controls = list(
                (
                    await session.scalars(
                        select(ToolCallCanaryControl)
                        .order_by(ToolCallCanaryControl.control_key)
                        .with_for_update()
                    )
                ).all()
            )
            if len(controls) != EXPECTED_ROSTER_COUNT:
                raise AssertionError(
                    f"control count is {len(controls)}, expected 74"
                )
            if {row.user_id for row in controls} != roster_user_ids:
                raise AssertionError("Agent2 controls do not match the roster")
            if not all(
                row.enabled
                and row.messages_enabled
                and row.runtime == "canary_execute"
                for row in controls
            ):
                raise AssertionError("not all controls are active Agent2 controls")

            before = [_mapping(row) for row in controls]
            if args.mode == "activate":
                if args.backup_path.exists():
                    existing_backup = json.loads(
                        args.backup_path.read_text(encoding="utf-8")
                    )
                    if existing_backup.get("controls") != before:
                        raise AssertionError("existing control backup does not match")
                else:
                    args.backup_path.parent.mkdir(parents=True, exist_ok=True)
                    args.backup_path.write_text(
                        json.dumps(
                            {
                                "created_at": datetime.now(UTC).isoformat(),
                                "candidate": candidate,
                                "controls": before,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                target_by_key = {
                    row.control_key: candidate for row in controls
                }
                change_prefix = args.release_key
                reason = args.reason
            elif args.mode == "restore":
                assert backup_payload is not None
                backup_rows = backup_payload.get("controls")
                if not isinstance(backup_rows, list):
                    raise AssertionError("control backup is invalid")
                target_by_key = {
                    str(row["control_key"]): {
                        "registry_digest": str(row["registry_digest"]),
                        "prompt_sha256": str(row["prompt_sha256"]),
                        "model_name": str(row["model_name"]),
                    }
                    for row in backup_rows
                    if isinstance(row, dict)
                }
                if set(target_by_key) != {row.control_key for row in controls}:
                    raise AssertionError("control backup scope does not match")
                change_prefix = f"{args.release_key}-rollback"
                reason = (
                    f"restore Agent2 control contract after {args.release_key} rollback"
                )
            else:
                target_by_key = {
                    row.control_key: candidate for row in controls
                }
                change_prefix = ""
                reason = ""

            if args.mode != "check":
                for control in controls:
                    target = target_by_key[control.control_key]
                    if (
                        control.registry_digest == target["registry_digest"]
                        and control.prompt_sha256 == target["prompt_sha256"]
                        and control.model_name == target["model_name"]
                    ):
                        continue
                    source_change_id = f"{change_prefix}:{control.control_key}"
                    existing_audit = await session.scalar(
                        select(ToolCallCanaryControlAudit.audit_id).where(
                            ToolCallCanaryControlAudit.source_change_id
                            == source_change_id
                        )
                    )
                    if existing_audit is not None:
                        raise AssertionError(
                            f"source change already used: {source_change_id}"
                        )
                    before_row = _mapping(control)
                    control.registry_digest = str(target["registry_digest"])
                    control.prompt_sha256 = str(target["prompt_sha256"])
                    control.model_name = str(target["model_name"])
                    control.version += 1
                    control.changed_by = "codex-release"
                    control.change_reason = reason
                    await session.flush()
                    session.add(
                        ToolCallCanaryControlAudit(
                            audit_id=uuid5(
                                NAMESPACE_URL,
                                f"agent2-control-audit:{source_change_id}",
                            ),
                            control_key=control.control_key,
                            actor_user_id="codex-release",
                            source_change_id=source_change_id,
                            before_json=before_row,
                            after_json=_mapping(control),
                            reason=reason,
                        )
                    )
                    changed_count += 1
                await session.commit()
            else:
                await session.rollback()

        async with AsyncSessionLocal() as verification_session:
            controls = list(
                (
                    await verification_session.scalars(
                        select(ToolCallCanaryControl).order_by(
                            ToolCallCanaryControl.control_key
                        )
                    )
                ).all()
            )
            after = _distribution(controls)
            if any(
                row.registry_digest
                != target_by_key[row.control_key]["registry_digest"]
                or row.prompt_sha256
                != target_by_key[row.control_key]["prompt_sha256"]
                or row.model_name
                != target_by_key[row.control_key]["model_name"]
                for row in controls
            ):
                raise AssertionError("control contract verification failed")
            await verification_session.rollback()
    finally:
        await engine.dispose()

    print(
        json.dumps(
            {
                "mode": args.mode,
                "candidate": candidate,
                "roster_count": EXPECTED_ROSTER_COUNT,
                "control_count": len(controls),
                "changed_count": changed_count,
                "after": after,
                "dingtalk_send_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
