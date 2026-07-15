from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import (
    Agent2Case,
    Agent2IdentityBinding,
    CaseFollowupPolicy,
    CaseFollowupTask,
)


@dataclass(frozen=True)
class BulkFollowupFilter:
    case_type: str = ""
    stage: str = ""
    node: str = ""
    assigned_user_id: str = ""
    risk_level: str = ""
    has_hearing_date: bool | None = None
    cadence_type: str = ""
    waiting_for_reply: bool | None = None


@dataclass(frozen=True)
class BulkFollowupChange:
    cadence_type: str
    enabled: bool
    custom_interval_days: int | None = None
    hearing_reminders_enabled: bool | None = None
    stage_transition_enabled: bool | None = None
    node_transition_enabled: bool | None = None
    force_manual_override: bool = False


async def preview_bulk_followup_change(
    session: AsyncSession,
    *,
    tenant_id: str,
    filters: BulkFollowupFilter,
    change: BulkFollowupChange,
) -> dict[str, Any]:
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
    assigned: dict[str, set[str]] = {
        item.user_id: {
            str(value)
            for value in (item.permission_scope_json or {}).get("allowed_case_ids", [])
            if value
        }
        for item in bindings
    }
    all_assigned = set().union(*assigned.values()) if assigned else set()
    cases = list(
        (
            await session.scalars(
                select(Agent2Case).where(
                    Agent2Case.tenant_id == tenant_id,
                    Agent2Case.case_id.in_(tuple(_uuid_values(all_assigned))),
                )
            )
        ).all()
    ) if all_assigned else []
    policies = list(
        (
            await session.scalars(
                select(CaseFollowupPolicy).where(
                    CaseFollowupPolicy.tenant_id == tenant_id
                )
            )
        ).all()
    )
    policy_by_scope = {
        (str(item.case_id), item.assigned_user_id): item for item in policies
    }
    waiting_case_ids = {
        str(value)
        for value in (
            await session.scalars(
                select(CaseFollowupTask.case_id).where(
                    CaseFollowupTask.tenant_id == tenant_id,
                    CaseFollowupTask.task_status == "waiting_for_reply",
                    CaseFollowupTask.response_status == "awaiting_input",
                )
            )
        ).all()
    }
    rows: list[dict[str, Any]] = []
    for case in cases:
        source = dict(case.source_json or {})
        owner = case.owner_user_id
        if str(case.case_id) not in assigned.get(owner, set()):
            continue
        policy = policy_by_scope.get((str(case.case_id), owner))
        stage = str(source.get("major_stage") or source.get("stage") or "")
        node = str(source.get("minor_stage") or source.get("node") or "")
        risk = str(source.get("risk_level") or source.get("risk") or "")
        hearing_date = str(source.get("hearing_date") or "")
        waiting = str(case.case_id) in waiting_case_ids
        if filters.case_type and case.case_type != filters.case_type:
            continue
        if filters.stage and stage != filters.stage:
            continue
        if filters.node and node != filters.node:
            continue
        if filters.assigned_user_id and owner != filters.assigned_user_id:
            continue
        if filters.risk_level and risk != filters.risk_level:
            continue
        if filters.has_hearing_date is not None and bool(hearing_date) != filters.has_hearing_date:
            continue
        current_cadence = policy.cadence_type if policy is not None else "event_only"
        if filters.cadence_type and current_cadence != filters.cadence_type:
            continue
        if filters.waiting_for_reply is not None and waiting != filters.waiting_for_reply:
            continue
        manual_preserved = bool(
            policy is not None
            and policy.policy_source == "case_manual_override"
            and not change.force_manual_override
        )
        rows.append({
            "case_id": str(case.case_id), "case_name": case.case_name,
            "case_number": case.case_number, "case_type": case.case_type,
            "stage": stage, "node": node, "risk_level": risk,
            "assigned_user_id": owner, "case_version": case.version,
            "waiting_for_reply": waiting,
            "before": {
                "policy_id": str(policy.policy_id) if policy is not None else "",
                "version": policy.version if policy is not None else 0,
                "policy_source": policy.policy_source if policy is not None else "tenant_default",
                "cadence_type": current_cadence,
                "enabled": policy.enabled if policy is not None else False,
            },
            "after": {
                "cadence_type": (
                    current_cadence if manual_preserved else change.cadence_type
                ),
                "enabled": (
                    policy.enabled if manual_preserved and policy is not None else change.enabled
                ),
            },
            "will_change": not manual_preserved,
            "skip_reason": "manual_override_preserved" if manual_preserved else "",
        })
    rows.sort(key=lambda item: (item["assigned_user_id"], item["case_number"], item["case_id"]))
    material = json.dumps(
        {"tenant_id": tenant_id, "rows": rows, "change": change.__dict__},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return {
        "preview_id": sha256(material.encode("utf-8")).hexdigest(),
        "tenant_id": tenant_id,
        "matched_count": len(rows),
        "change_count": sum(bool(item["will_change"]) for item in rows),
        "preserved_manual_override_count": sum(
            item["skip_reason"] == "manual_override_preserved" for item in rows
        ),
        "items": rows,
    }


def _uuid_values(values: set[str]):
    from uuid import UUID

    for value in values:
        try:
            yield UUID(value)
        except (TypeError, ValueError):
            continue
