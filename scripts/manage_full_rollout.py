from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select, text

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
from app.db import AsyncSessionLocal
from app.models import User


ROLLOUT_COUNT = 74
CHILD_ROSTER_COUNT = 72
PARENT_DEPARTMENT = "\u6cd5\u52a1\u5408\u7ea6\u4e2d\u5fc3"
CENTER_LEVEL_TEAM_CODE = "legal-center"
EXPECTED_CENTER_LEVEL_MEMBERS = frozenset(
    {"\u8d75\u536b\u4e2d", "\u6731\u4f73\u4f73"}
)
EXPECTED_CHILD_TEAMS = frozenset(
    {
        "\u6cd5\u52a1\u4e00\u90e8",
        "\u6cd5\u52a1\u4e8c\u90e8",
        "\u6cd5\u52a1\u4e09\u90e8",
        "\u6cd5\u52a1\u56db\u90e8",
        "\u6cd5\u52a1\u4e94\u90e8",
        "\u6cd5\u52a1\u516d\u90e8",
        "\u7efc\u5408\u7ba1\u7406\u90e8",
    }
)
ACTOR = "codex-full-rollout"
REASON = "Enable the verified 74-person legal-center Agent2 rollout"


async def plan() -> dict[str, object]:
    settings = get_settings()
    dashboard_tenant_id = str(settings.legal_daily_dashboard_tenant_id or "").strip()
    async with AsyncSessionLocal() as session:
        roster = await _roster(session, tenant_id=dashboard_tenant_id)
        controls = await _controls_for_roster(session, roster=roster)
    _validate_roster(roster)
    runtime_tenant_id = _runtime_tenant_id(controls)
    controls_by_user = {row.user_id: row for row in controls}
    return {
        "action": "plan",
        "simulation_only": True,
        "database_writes": 0,
        "dingtalk_send_calls": 0,
        "dashboard_tenant_id": dashboard_tenant_id,
        "runtime_tenant_id": runtime_tenant_id,
        "roster_count": len(roster),
        "child_department_member_count": sum(
            bool(row["team_active"]) for row in roster
        ),
        "center_level_member_count": sum(
            str(row["team_code"]) == CENTER_LEVEL_TEAM_CODE for row in roster
        ),
        "currently_active_users": sum(bool(row["active"]) for row in roster),
        "users_to_activate": sum(not bool(row["active"]) for row in roster),
        "existing_controls": len(controls),
        "controls_to_create": sum(
            str(row["user_id"]) not in controls_by_user for row in roster
        ),
        "controls_to_open": sum(
            str(row["user_id"]) not in controls_by_user
            or not controls_by_user[str(row["user_id"])].enabled
            or not controls_by_user[str(row["user_id"])].messages_enabled
            for row in roster
        ),
        "configured_active_user_limit": (
            settings.agent2_tool_call_canary_max_active_users
        ),
        "required_active_user_limit": ROLLOUT_COUNT,
        "ready_to_apply": (
            settings.agent2_tool_call_canary_max_active_users == ROLLOUT_COUNT
        ),
    }


async def apply(*, backup_path: Path, confirmed_count: int) -> dict[str, object]:
    if confirmed_count != ROLLOUT_COUNT:
        raise ValueError(f"--confirm-count must be exactly {ROLLOUT_COUNT}")
    if backup_path.exists():
        raise FileExistsError(f"backup already exists: {backup_path}")
    settings = get_settings()
    if settings.agent2_tool_call_canary_max_active_users != ROLLOUT_COUNT:
        raise RuntimeError(
            f"Agent2 active-user limit must be {ROLLOUT_COUNT} before rollout"
        )
    dashboard_tenant_id = str(settings.legal_daily_dashboard_tenant_id or "").strip()
    registry_digest = runtime_registry_contract_digest(settings)
    prompt_digest = canary_prompt_sha256()

    async with AsyncSessionLocal() as session:
        try:
            roster = await _roster(
                session,
                tenant_id=dashboard_tenant_id,
                lock=True,
            )
            _validate_roster(roster)
            user_ids = [UUID(str(row["user_id"])) for row in roster]
            users = list(
                (
                    await session.scalars(
                        select(User).where(User.id.in_(user_ids)).with_for_update()
                    )
                ).all()
            )
            controls = await _controls_for_roster(
                session,
                roster=roster,
                lock=True,
            )
            runtime_tenant_id = _runtime_tenant_id(controls)
            backup = {
                "dashboard_tenant_id": dashboard_tenant_id,
                "runtime_tenant_id": runtime_tenant_id,
                "users": {
                    str(user.id): {"active": bool(user.active)} for user in users
                },
                "controls": {row.user_id: _control_mapping(row) for row in controls},
            }
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text(
                json.dumps(backup, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            for user in users:
                user.active = True
            repository = ToolCallCanaryControlRepository(session)
            controls_by_user = {row.user_id: row for row in controls}
            created = 0
            updated = 0
            for user_id in sorted(str(user.id) for user in users):
                row = controls_by_user.get(user_id)
                if row is None:
                    row = ToolCallCanaryControl(
                        control_key=f"full-rollout:{user_id}",
                        tenant_id=runtime_tenant_id,
                        user_id=user_id,
                        enabled=True,
                        runtime="canary_execute",
                        messages_enabled=True,
                        registry_digest=registry_digest,
                        prompt_sha256=prompt_digest,
                        model_name=CANARY_MODEL_NAME,
                        version=1,
                        changed_by=ACTOR,
                        change_reason=REASON,
                    )
                    session.add(row)
                    await session.flush()
                    repository._add_audit(
                        control=row,
                        before={},
                        actor_user_id=ACTOR,
                        source_change_id=f"full-rollout-create:{user_id}:{uuid4()}",
                        reason=REASON,
                    )
                    created += 1
                    continue
                before = _control_mapping(row)
                row.enabled = True
                row.runtime = "canary_execute"
                row.messages_enabled = True
                row.registry_digest = registry_digest
                row.prompt_sha256 = prompt_digest
                row.model_name = CANARY_MODEL_NAME
                row.version += 1
                row.changed_by = ACTOR
                row.change_reason = REASON
                repository._add_audit(
                    control=row,
                    before=before,
                    actor_user_id=ACTOR,
                    source_change_id=f"full-rollout-update:{user_id}:{uuid4()}",
                    reason=REASON,
                )
                updated += 1
            await session.flush()
            enabled_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ToolCallCanaryControl)
                    .where(
                        ToolCallCanaryControl.tenant_id == runtime_tenant_id,
                        ToolCallCanaryControl.enabled.is_(True),
                        ToolCallCanaryControl.messages_enabled.is_(True),
                    )
                )
                or 0
            )
            if enabled_count != ROLLOUT_COUNT:
                raise RuntimeError(
                    f"expected {ROLLOUT_COUNT} message-ready controls, "
                    f"got {enabled_count}"
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return {
        "action": "apply",
        "activated_users": ROLLOUT_COUNT,
        "controls_created": created,
        "controls_updated": updated,
        "message_ready_controls": enabled_count,
        "backup_path": str(backup_path),
        "dingtalk_send_calls": 0,
    }


async def rollback(*, backup_path: Path) -> dict[str, object]:
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    runtime_tenant_id = str(backup["runtime_tenant_id"])
    user_state = dict(backup["users"])
    control_state = dict(backup["controls"])
    async with AsyncSessionLocal() as session:
        try:
            users = list(
                (
                    await session.scalars(
                        select(User)
                        .where(User.id.in_([UUID(value) for value in user_state]))
                        .with_for_update()
                    )
                ).all()
            )
            for user in users:
                user.active = bool(user_state[str(user.id)]["active"])
            controls = await _controls(
                session,
                tenant_id=runtime_tenant_id,
                lock=True,
            )
            repository = ToolCallCanaryControlRepository(session)
            for row in controls:
                before = _control_mapping(row)
                target = control_state.get(row.user_id)
                if target is None:
                    row.enabled = False
                    row.messages_enabled = False
                    reason = "Disable control created by full rollout rollback"
                else:
                    row.enabled = bool(target["enabled"])
                    row.runtime = str(target["runtime"])
                    row.messages_enabled = bool(target["messages_enabled"])
                    row.registry_digest = str(target["registry_digest"])
                    row.prompt_sha256 = str(target["prompt_sha256"])
                    row.model_name = str(target["model_name"])
                    reason = "Restore control state before full rollout"
                row.version += 1
                row.changed_by = ACTOR
                row.change_reason = reason
                repository._add_audit(
                    control=row,
                    before=before,
                    actor_user_id=ACTOR,
                    source_change_id=f"full-rollout-rollback:{row.user_id}:{uuid4()}",
                    reason=reason,
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return {
        "action": "rollback",
        "restored_users": len(users),
        "reviewed_controls": len(controls),
        "dingtalk_send_calls": 0,
    }


async def _roster(session, *, tenant_id: str, lock: bool = False):
    suffix = " FOR UPDATE OF users" if lock else ""
    return (
        (
            await session.execute(
                text(
                    """
                SELECT users.id::text AS user_id, users.name, users.active,
                       users.dingtalk_user_id, users.team_id::text AS user_team_id,
                       memberships.team_id::text AS team_id,
                       memberships.data_complete,
                       teams.code AS team_code,
                       teams.name AS team_name,
                       teams.department_name,
                       teams.active AS team_active
                FROM legal_daily_team_memberships memberships
                JOIN users ON users.id = memberships.user_id
                JOIN teams ON teams.id = memberships.team_id
                WHERE memberships.tenant_id = :tenant_id
                  AND memberships.effective_from <= CURRENT_DATE
                  AND (
                      memberships.effective_to IS NULL
                      OR memberships.effective_to >= CURRENT_DATE
                  )
                  AND teams.department_name = :parent_department
                  AND (
                      teams.active IS TRUE
                      OR teams.code = :center_level_team_code
                  )
                ORDER BY users.id
                """
                    + suffix
                ),
                {
                    "tenant_id": tenant_id,
                    "parent_department": PARENT_DEPARTMENT,
                    "center_level_team_code": CENTER_LEVEL_TEAM_CODE,
                },
            )
        )
        .mappings()
        .all()
    )


async def _controls(session, *, tenant_id: str, lock: bool = False):
    statement = (
        select(ToolCallCanaryControl)
        .where(ToolCallCanaryControl.tenant_id == tenant_id)
        .order_by(ToolCallCanaryControl.user_id)
    )
    if lock:
        statement = statement.with_for_update()
    return list((await session.scalars(statement)).all())


async def _controls_for_roster(session, *, roster, lock: bool = False):
    user_ids = [str(row["user_id"]) for row in roster]
    statement = (
        select(ToolCallCanaryControl)
        .where(ToolCallCanaryControl.user_id.in_(user_ids))
        .order_by(ToolCallCanaryControl.user_id)
    )
    if lock:
        statement = statement.with_for_update()
    return list((await session.scalars(statement)).all())


def _runtime_tenant_id(controls) -> str:
    tenant_ids = {str(row.tenant_id) for row in controls}
    if len(tenant_ids) != 1:
        raise RuntimeError(
            "existing rollout cohort must resolve to exactly one runtime tenant"
        )
    return tenant_ids.pop()


def _validate_roster(rows) -> None:
    if len(rows) != ROLLOUT_COUNT:
        raise RuntimeError(
            f"expected {ROLLOUT_COUNT} roster members, got {len(rows)}"
        )
    if len({str(row["user_id"]) for row in rows}) != ROLLOUT_COUNT:
        raise RuntimeError("roster contains duplicate current memberships")
    if any(not str(row["dingtalk_user_id"] or "").strip() for row in rows):
        raise RuntimeError("roster contains a member without DingTalk identity")
    if len({str(row["dingtalk_user_id"]).strip() for row in rows}) != ROLLOUT_COUNT:
        raise RuntimeError("roster contains duplicate DingTalk identities")
    if any(str(row["user_team_id"]) != str(row["team_id"]) for row in rows):
        raise RuntimeError("roster contains a user whose system team is out of sync")
    if any(not bool(row["data_complete"]) for row in rows):
        raise RuntimeError("roster contains incomplete organization data")

    child_rows = [row for row in rows if bool(row["team_active"])]
    center_rows = [
        row
        for row in rows
        if str(row["team_code"]) == CENTER_LEVEL_TEAM_CODE
        and not bool(row["team_active"])
    ]
    if len(child_rows) != CHILD_ROSTER_COUNT:
        raise RuntimeError(
            f"expected {CHILD_ROSTER_COUNT} members in seven child departments, "
            f"got {len(child_rows)}"
        )
    child_team_names = {str(row["team_name"]) for row in child_rows}
    if child_team_names != EXPECTED_CHILD_TEAMS:
        raise RuntimeError(
            f"unexpected child departments: {sorted(child_team_names)!r}"
        )
    if len({str(row["team_id"]) for row in child_rows}) != len(
        EXPECTED_CHILD_TEAMS
    ):
        raise RuntimeError("child-department roster must resolve to seven teams")
    center_names = {str(row["name"]) for row in center_rows}
    if center_names != EXPECTED_CENTER_LEVEL_MEMBERS:
        raise RuntimeError(
            f"unexpected center-level members: {sorted(center_names)!r}"
        )
    if len(center_rows) != len(EXPECTED_CENTER_LEVEL_MEMBERS):
        raise RuntimeError("center-level roster contains duplicate memberships")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply", "rollback"))
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--confirm-count", type=int, default=0)
    args = parser.parse_args()
    if args.action in {"apply", "rollback"} and args.backup_path is None:
        parser.error("--backup-path is required")
    if args.action == "plan":
        result = await plan()
    elif args.action == "apply":
        result = await apply(
            backup_path=args.backup_path,
            confirmed_count=args.confirm_count,
        )
    else:
        result = await rollback(backup_path=args.backup_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
