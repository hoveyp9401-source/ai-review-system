from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select, text

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    CANARY_THINKING_ENABLED,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.canary_store import (
    ToolCallCanaryControl,
    ToolCallCanaryControlAudit,
)
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.legal_daily_roster import (
    FORMAL_CENTER_MEMBER_COUNT,
    FORMAL_CENTER_TEAM_CODE,
    FORMAL_CHILD_TEAM_NAMES,
    FORMAL_CONFIRMED_CENTER_DIRECT_MEMBER_NAMES,
    FORMAL_PARENT_DEPARTMENT,
    FORMAL_ROSTER_MEMBER_COUNT,
)


EXPECTED_MODEL = "deepseek-v4-flash"
BACKUP_SCHEMA_VERSION = 1
ACTOR = "codex-release"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Atomically align all 74 Agent2 controls with the V22 Flash "
            "runtime contract, or restore the previous contract."
        )
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("preflight", "activate", "check", "restore"),
    )
    parser.add_argument("--backup-path", type=Path, required=True)
    parser.add_argument("--release-key", required=True)
    parser.add_argument(
        "--reason",
        default="activate Agent2 V22 overnight semantics on DeepSeek V4 Flash",
    )
    return parser.parse_args()


def _control_mapping(control: ToolCallCanaryControl) -> dict[str, object]:
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


def _contract_mapping(row: dict[str, Any] | ToolCallCanaryControl) -> dict[str, str]:
    if isinstance(row, ToolCallCanaryControl):
        return {
            "registry_digest": row.registry_digest,
            "prompt_sha256": row.prompt_sha256,
            "model_name": row.model_name,
        }
    return {
        "registry_digest": str(row["registry_digest"]),
        "prompt_sha256": str(row["prompt_sha256"]),
        "model_name": str(row["model_name"]),
    }


def _same_contract(
    control: ToolCallCanaryControl,
    target: dict[str, str],
) -> bool:
    return _contract_mapping(control) == target


def _controls_sha256(controls: list[dict[str, object]]) -> str:
    canonical = json.dumps(
        controls,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary backup already exists: {temporary}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_backup(path: Path, *, release_key: str) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise AssertionError("unsupported control backup schema")
    if payload.get("release_key") != release_key:
        raise AssertionError("control backup belongs to a different release")
    rows = payload.get("controls")
    if not isinstance(rows, list) or len(rows) != FORMAL_ROSTER_MEMBER_COUNT:
        raise AssertionError("control backup does not contain exactly 74 rows")
    if any(not isinstance(row, dict) for row in rows):
        raise AssertionError("control backup contains an invalid row")
    typed_rows = [dict(row) for row in rows]
    if len({str(row.get("control_key") or "") for row in typed_rows}) != len(
        typed_rows
    ):
        raise AssertionError("control backup contains duplicate control keys")
    if _controls_sha256(typed_rows) != payload.get("controls_sha256"):
        raise AssertionError("control backup checksum mismatch")
    return payload


def _candidate_contract(settings: object) -> dict[str, str]:
    if CANARY_MODEL_NAME != EXPECTED_MODEL:
        raise AssertionError(
            f"candidate model is {CANARY_MODEL_NAME}, expected {EXPECTED_MODEL}"
        )
    if CANARY_THINKING_ENABLED is not True:
        raise AssertionError("V22 Flash thinking must be explicitly enabled")
    return {
        "registry_digest": runtime_registry_contract_digest(settings),
        "prompt_sha256": canary_prompt_sha256(),
        "model_name": CANARY_MODEL_NAME,
    }


def _distribution(controls: list[ToolCallCanaryControl]) -> dict[str, object]:
    return {
        "registry_digest": dict(
            Counter(row.registry_digest for row in controls)
        ),
        "prompt_sha256": dict(Counter(row.prompt_sha256 for row in controls)),
        "model_name": dict(Counter(row.model_name for row in controls)),
        "version_min": min(row.version for row in controls),
        "version_max": max(row.version for row in controls),
    }


async def _roster_user_ids(session: Any, tenant_id: str) -> set[str]:
    rows = (
        await session.execute(
            text(
                """
                SELECT
                    users.id::text AS user_id,
                    users.name AS user_name,
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
                  AND users.active IS TRUE
                  AND teams.department_name = :parent_department
                  AND (
                      teams.active IS TRUE
                      OR teams.code = :center_team_code
                  )
                ORDER BY users.id
                """
            ),
            {
                "tenant_id": tenant_id,
                "parent_department": FORMAL_PARENT_DEPARTMENT,
                "center_team_code": FORMAL_CENTER_TEAM_CODE,
            },
        )
    ).mappings().all()
    if len(rows) != FORMAL_ROSTER_MEMBER_COUNT:
        raise AssertionError(
            f"roster count is {len(rows)}, expected {FORMAL_ROSTER_MEMBER_COUNT}"
        )
    user_ids = {str(row["user_id"]) for row in rows}
    if len(user_ids) != FORMAL_ROSTER_MEMBER_COUNT:
        raise AssertionError("current roster contains duplicate memberships")
    child_names = {
        str(row["team_name"]) for row in rows if bool(row["team_active"])
    }
    if child_names != FORMAL_CHILD_TEAM_NAMES:
        raise AssertionError("the seven child departments changed")
    center_names = {
        str(row["user_name"])
        for row in rows
        if str(row["team_code"] or "") == FORMAL_CENTER_TEAM_CODE
        and not bool(row["team_active"])
    }
    if len(center_names) != FORMAL_CENTER_MEMBER_COUNT or not (
        FORMAL_CONFIRMED_CENTER_DIRECT_MEMBER_NAMES <= center_names
    ):
        raise AssertionError("center-direct roster changed")
    return user_ids


async def _locked_controls(
    session: Any,
    *,
    roster_user_ids: set[str],
) -> list[ToolCallCanaryControl]:
    controls = list(
        (
            await session.scalars(
                select(ToolCallCanaryControl)
                .where(ToolCallCanaryControl.user_id.in_(roster_user_ids))
                .order_by(ToolCallCanaryControl.control_key)
                .with_for_update()
            )
        ).all()
    )
    if len(controls) != FORMAL_ROSTER_MEMBER_COUNT:
        raise AssertionError(
            f"control count is {len(controls)}, expected {FORMAL_ROSTER_MEMBER_COUNT}"
        )
    if {row.user_id for row in controls} != roster_user_ids:
        raise AssertionError("Agent2 controls do not exactly match the roster")
    runtime_tenant_ids = {row.tenant_id for row in controls}
    if len(runtime_tenant_ids) != 1:
        raise AssertionError("Agent2 controls do not share one runtime tenant")
    return controls


def _validate_active_scope(
    controls: list[ToolCallCanaryControl],
    *,
    roster_user_ids: set[str],
) -> None:
    if {row.user_id for row in controls} != roster_user_ids:
        raise AssertionError("Agent2 controls do not match the formal roster")
    if not all(
        row.enabled
        and row.messages_enabled
        and row.runtime == "canary_execute"
        for row in controls
    ):
        raise AssertionError("not all 74 controls are active Agent2 controls")


def _target_by_key_from_backup(
    payload: dict[str, object],
) -> dict[str, dict[str, str]]:
    return {
        str(row["control_key"]): _contract_mapping(row)
        for row in payload["controls"]  # type: ignore[index]
    }


async def _audit_exists(session: Any, source_change_id: str) -> bool:
    return (
        await session.scalar(
            select(ToolCallCanaryControlAudit.audit_id).where(
                ToolCallCanaryControlAudit.source_change_id == source_change_id
            )
        )
        is not None
    )


async def main() -> None:
    args = _args()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.release_key) is None:
        raise ValueError("release key contains unsafe characters")
    settings = get_settings()
    dashboard_tenant_id = str(
        settings.legal_daily_dashboard_tenant_id or ""
    ).strip()
    if not dashboard_tenant_id:
        raise AssertionError("legal daily tenant is not configured")
    candidate = _candidate_contract(settings)
    backup_payload = (
        _load_backup(args.backup_path, release_key=args.release_key)
        if args.mode == "restore"
        else None
    )
    changed_count = 0
    audit_prefix = ""
    try:
        async with AsyncSessionLocal() as session:
            roster_user_ids = await _roster_user_ids(
                session,
                dashboard_tenant_id,
            )
            controls = await _locked_controls(
                session,
                roster_user_ids=roster_user_ids,
            )
            _validate_active_scope(
                controls,
                roster_user_ids=roster_user_ids,
            )
            runtime_tenant_id = controls[0].tenant_id
            before = [_control_mapping(row) for row in controls]

            if args.mode == "activate":
                if args.backup_path.exists():
                    existing = _load_backup(
                        args.backup_path,
                        release_key=args.release_key,
                    )
                    if existing.get("controls") != before:
                        raise AssertionError(
                            "existing backup no longer matches current controls"
                        )
                else:
                    _write_json_atomically(
                        args.backup_path,
                        {
                            "schema_version": BACKUP_SCHEMA_VERSION,
                            "release_key": args.release_key,
                            "created_at": datetime.now(UTC).isoformat(),
                            "dashboard_tenant_id": dashboard_tenant_id,
                            "runtime_tenant_id": runtime_tenant_id,
                            "candidate": {
                                **candidate,
                                "thinking_enabled": True,
                            },
                            "controls_sha256": _controls_sha256(before),
                            "controls": before,
                        },
                    )
                target_by_key = {
                    row.control_key: candidate for row in controls
                }
                audit_prefix = f"{args.release_key}:activate"
                reason = args.reason
            elif args.mode == "restore":
                assert backup_payload is not None
                if (
                    backup_payload.get("dashboard_tenant_id")
                    != dashboard_tenant_id
                ):
                    raise AssertionError("dashboard tenant backup mismatch")
                if (
                    backup_payload.get("runtime_tenant_id")
                    != runtime_tenant_id
                ):
                    raise AssertionError("runtime tenant backup mismatch")
                target_by_key = _target_by_key_from_backup(backup_payload)
                if set(target_by_key) != {row.control_key for row in controls}:
                    raise AssertionError("control backup scope does not match")
                backup_candidate = dict(backup_payload.get("candidate") or {})
                current_contracts = {
                    tuple(_contract_mapping(row).values()) for row in controls
                }
                backup_contracts = {
                    tuple(target_by_key[row.control_key].values())
                    for row in controls
                }
                candidate_tuple = tuple(candidate.values())
                if current_contracts not in ({candidate_tuple}, backup_contracts):
                    raise AssertionError(
                        "refusing to overwrite controls changed after activation"
                    )
                if any(
                    str(backup_candidate.get(key) or "") != value
                    for key, value in candidate.items()
                ):
                    raise AssertionError("backup candidate contract mismatch")
                audit_prefix = f"{args.release_key}:restore"
                reason = f"restore Agent2 controls after failed {args.release_key}"
            else:
                target_by_key = {
                    row.control_key: candidate for row in controls
                }
                reason = ""

            if args.mode == "preflight":
                distributions = _distribution(controls)
                if any(
                    len(distributions[field]) != 1
                    for field in ("registry_digest", "prompt_sha256", "model_name")
                ):
                    raise AssertionError("current 74 controls are not one contract")
                await session.rollback()
            elif args.mode == "check":
                if any(
                    not _same_contract(row, target_by_key[row.control_key])
                    for row in controls
                ):
                    raise AssertionError(
                        "74 controls do not match the V22 Flash contract"
                    )
                await session.rollback()
            else:
                for control in controls:
                    target = target_by_key[control.control_key]
                    if _same_contract(control, target):
                        continue
                    source_change_id = f"{audit_prefix}:{control.control_key}"
                    if await _audit_exists(session, source_change_id):
                        raise AssertionError(
                            f"source change already used: {source_change_id}"
                        )
                    before_row = _control_mapping(control)
                    control.registry_digest = target["registry_digest"]
                    control.prompt_sha256 = target["prompt_sha256"]
                    control.model_name = target["model_name"]
                    control.version += 1
                    control.changed_by = ACTOR
                    control.change_reason = reason
                    await session.flush()
                    session.add(
                        ToolCallCanaryControlAudit(
                            audit_id=uuid5(
                                NAMESPACE_URL,
                                f"agent2-v22-control-audit:{source_change_id}",
                            ),
                            control_key=control.control_key,
                            actor_user_id=ACTOR,
                            source_change_id=source_change_id,
                            before_json=before_row,
                            after_json=_control_mapping(control),
                            reason=reason,
                        )
                    )
                    changed_count += 1
                await session.commit()

        async with AsyncSessionLocal() as verification_session:
            roster_user_ids = await _roster_user_ids(
                verification_session,
                dashboard_tenant_id,
            )
            controls = await _locked_controls(
                verification_session,
                roster_user_ids=roster_user_ids,
            )
            _validate_active_scope(
                controls,
                roster_user_ids=roster_user_ids,
            )
            after = _distribution(controls)
            if args.mode == "preflight":
                target_verified = all(
                    len(after[field]) == 1
                    for field in ("registry_digest", "prompt_sha256", "model_name")
                )
            else:
                target_verified = all(
                    _same_contract(row, target_by_key[row.control_key])
                    for row in controls
                )
            if not target_verified:
                raise AssertionError("control contract verification failed")
            audit_count = (
                int(
                    await verification_session.scalar(
                        select(func.count(ToolCallCanaryControlAudit.audit_id)).where(
                            ToolCallCanaryControlAudit.source_change_id.like(
                                f"{audit_prefix}:%"
                            )
                        )
                    )
                    or 0
                )
                if audit_prefix
                else 0
            )
            if audit_prefix and audit_count != changed_count:
                raise AssertionError("control audit count does not match changes")
            await verification_session.rollback()
    finally:
        await engine.dispose()

    print(
        json.dumps(
            {
                "mode": args.mode,
                "release_key": args.release_key,
                "candidate": {
                    **candidate,
                    "thinking_enabled": CANARY_THINKING_ENABLED,
                },
                "dashboard_tenant_id": dashboard_tenant_id,
                "runtime_tenant_id": runtime_tenant_id,
                "roster_count": FORMAL_ROSTER_MEMBER_COUNT,
                "control_count": len(controls),
                "changed_count": changed_count,
                "audit_count": audit_count,
                "target_verified": target_verified,
                "after": after,
                "dingtalk_send_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
