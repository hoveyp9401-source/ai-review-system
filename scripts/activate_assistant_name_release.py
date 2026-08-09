from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select, text

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


LEGAL_CENTER = "法务合约中心"
EXPECTED_ROSTER_COUNT = 74


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("check", "activate", "restore"),
        required=True,
    )
    parser.add_argument("--registry-digest")
    parser.add_argument("--prompt-sha256")
    parser.add_argument("--model-name")
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
) -> dict[str, dict[str, int]]:
    return {
        "registry_digest": dict(
            Counter(row.registry_digest for row in controls)
        ),
        "prompt_sha256": dict(
            Counter(row.prompt_sha256 for row in controls)
        ),
        "model_name": dict(Counter(row.model_name for row in controls)),
        "version": {
            "min": min(row.version for row in controls),
            "max": max(row.version for row in controls),
        },
    }


def _validated_digest(value: str | None, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"invalid {label}")
    return normalized


async def main() -> None:
    args = _parse_args()
    settings = get_settings()
    tenant_id = str(settings.legal_daily_dashboard_tenant_id or "").strip()
    candidate = {
        "registry_digest": runtime_registry_contract_digest(settings),
        "prompt_sha256": canary_prompt_sha256(),
        "model_name": CANARY_MODEL_NAME,
    }
    if args.mode == "restore":
        target = {
            "registry_digest": _validated_digest(
                args.registry_digest, "registry digest"
            ),
            "prompt_sha256": _validated_digest(
                args.prompt_sha256, "prompt sha256"
            ),
            "model_name": str(args.model_name or "").strip(),
        }
        if not target["model_name"]:
            raise ValueError("invalid model name")
    else:
        target = candidate

    async with AsyncSessionLocal() as session:
        roster_user_ids = {
            str(row[0])
            for row in (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT users.id
                        FROM legal_daily_team_memberships memberships
                        JOIN users ON users.id = memberships.user_id
                        JOIN teams ON teams.id = memberships.team_id
                        WHERE memberships.tenant_id = :tenant_id
                          AND memberships.effective_from <= CURRENT_DATE
                          AND (memberships.effective_to IS NULL
                               OR memberships.effective_to >= CURRENT_DATE)
                          AND teams.department_name = :department_name
                          AND (teams.active IS TRUE OR teams.code = 'legal-center')
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "department_name": LEGAL_CENTER,
                    },
                )
            ).all()
        }
        if len(roster_user_ids) != EXPECTED_ROSTER_COUNT:
            raise AssertionError(
                f"roster count is {len(roster_user_ids)}, expected 74"
            )
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
        control_tenant_ids = {row.tenant_id for row in controls}
        if len(control_tenant_ids) != 1:
            raise AssertionError(
                f"controls span multiple tenants: {sorted(control_tenant_ids)}"
            )
        control_user_ids = {row.user_id for row in controls}
        if control_user_ids != roster_user_ids:
            raise AssertionError("control users do not match the 74-person roster")
        if not all(
            row.enabled
            and row.messages_enabled
            and row.runtime == "canary_execute"
            for row in controls
        ):
            raise AssertionError("not all controls are active Agent2 controls")

        before = _distribution(controls)
        changed_count = 0
        if args.mode != "check":
            reason = (
                "deploy explicit assistant/user role correction on Agent2"
                if args.mode == "activate"
                else "restore pre-role-correction Agent2 contract"
            )
            change_prefix = (
                "assistant-name-v15-20260807"
                if args.mode == "activate"
                else "assistant-name-v15-rollback-20260807"
            )
            for control in controls:
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
                control.registry_digest = target["registry_digest"]
                control.prompt_sha256 = target["prompt_sha256"]
                control.model_name = target["model_name"]
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

    async with AsyncSessionLocal() as session:
        controls = list(
            (
                await session.scalars(
                    select(ToolCallCanaryControl)
                    .order_by(ToolCallCanaryControl.control_key)
                )
            ).all()
        )
        after = _distribution(controls)
        audit_count = int(
            await session.scalar(
                select(func.count(ToolCallCanaryControlAudit.audit_id)).where(
                    ToolCallCanaryControlAudit.source_change_id.like(
                        "assistant-name-v15-%20260807:%"
                    )
                )
            )
            or 0
        )
        await session.rollback()

    await engine.dispose()
    print(
        json.dumps(
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "mode": args.mode,
                "candidate": candidate,
                "target": target,
                "roster_count": EXPECTED_ROSTER_COUNT,
                "control_count": len(controls),
                "changed_count": changed_count,
                "before": before,
                "after": after,
                "assistant_name_release_audit_count": audit_count,
                "dingtalk_send_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
