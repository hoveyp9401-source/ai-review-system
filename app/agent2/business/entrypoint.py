from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.models import Agent2IdentityBinding, RouteControlAudit
from app.agent2.business.route_control import RouteControlRepository, RouteDecision, decide_route


@dataclass(frozen=True)
class Agent2EntrypointResolution:
    decision: RouteDecision
    binding: Agent2IdentityBinding | None
    route_audit_pending: bool = False


RuntimeOwner = Literal["agent2_primary", "blocked"]


def decide_runtime_owner(
    decision: RouteDecision,
) -> RuntimeOwner:
    """Allow only the formal Agent2 runtime to own a business message."""

    if decision.route == "agent2_primary":
        return "agent2_primary"
    return "blocked"


async def resolve_agent2_entrypoint(
    session: AsyncSession,
    *,
    settings: object,
    dingtalk_user_id: str,
    source_message_id: str,
) -> Agent2EntrypointResolution:
    if not bool(getattr(settings, "agent2_business_phase2_enabled", False)):
        return Agent2EntrypointResolution(
            RouteDecision("", "blocked", "agent2_business_phase2_disabled"),
            None,
        )
    allowed_tenants = parse_tenant_allowlist(
        getattr(settings, "agent2_business_tenant_ids", "")
    )
    if not allowed_tenants:
        return Agent2EntrypointResolution(
            RouteDecision("", "blocked", "agent2_business_tenant_allowlist_missing"),
            None,
        )
    bindings = (
        await session.scalars(
            select(Agent2IdentityBinding)
            .where(
                Agent2IdentityBinding.dingtalk_user_id == dingtalk_user_id,
                Agent2IdentityBinding.tenant_id.in_(allowed_tenants),
                Agent2IdentityBinding.active.is_(True),
            )
            .limit(2)
        )
    ).all()
    if not bindings:
        return Agent2EntrypointResolution(
            RouteDecision("", "blocked", "agent2_identity_binding_missing"),
            None,
        )
    if len(bindings) != 1:
        return Agent2EntrypointResolution(
            RouteDecision("", "blocked", "ambiguous_cross_tenant_identity_binding"),
            None,
        )
    binding = bindings[0]
    control = await RouteControlRepository(session).get(binding.tenant_id)
    decision = decide_route(
        control,
        tenant_id=binding.tenant_id,
        user_id=binding.user_id,
    )
    session.add(
        RouteControlAudit(
            tenant_id=binding.tenant_id,
            actor_user_id=binding.user_id,
            source_message_id=source_message_id,
            before_json=(
                {
                    "route_mode": control.route_mode,
                    "agent1_rollback_enabled": control.agent1_rollback_enabled,
                    "version": control.version,
                }
                if control is not None
                else {
                    "route_mode": "missing",
                    "agent1_rollback_enabled": False,
                    "version": 0,
                }
            ),
            after_json={
                "resolved_route": decision.route,
                "user_id": binding.user_id,
            },
            reason=f"route_decision:{decision.reason}",
        )
    )
    await session.flush()
    return Agent2EntrypointResolution(
        decision,
        binding,
        route_audit_pending=True,
    )


async def persist_runtime_owner_claim(
    session: AsyncSession,
    resolution: Agent2EntrypointResolution,
) -> bool:
    """Durably record the selected runtime before any business execution.

    The route audit is intentionally committed at the orchestration boundary:
    a later LLM, repository, or reply failure must not erase the evidence of
    which runtime owned the provider message.
    """

    if not bool(getattr(resolution, "route_audit_pending", False)):
        return False
    await session.commit()
    return True


def build_business_command_context(
    binding: Agent2IdentityBinding,
    *,
    source_message_id: str,
    source_channel: str,
    occurred_at: datetime,
    conversation_id: str = "",
) -> BusinessCommandContext:
    scope = binding.permission_scope_json or {}
    raw_case_ids = scope.get("allowed_case_ids", [])
    allowed_case_ids = tuple(
        str(value).strip()
        for value in raw_case_ids
        if str(value).strip()
    ) if isinstance(raw_case_ids, (list, tuple)) else ()
    raw_writable_case_ids = scope.get("writable_case_ids")
    writable_case_ids = (
        tuple(
            str(value).strip()
            for value in raw_writable_case_ids
            if str(value).strip()
        )
        if isinstance(raw_writable_case_ids, (list, tuple))
        else None
    )
    return BusinessCommandContext(
        tenant_id=binding.tenant_id,
        company_id=binding.company_id,
        department_id=binding.department_id,
        team_id=binding.team_id,
        actor_user_id=binding.user_id,
        actor_role_ids=tuple(str(value) for value in (binding.role_ids or []) if str(value)),
        allowed_case_ids=allowed_case_ids,
        writable_case_ids=writable_case_ids,
        source_message_id=source_message_id,
        source_channel=source_channel,
        occurred_at=occurred_at,
        conversation_id=conversation_id,
    )


def parse_tenant_allowlist(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str):
        raw = ",".join(str(value) for value in (raw or ()))
    return tuple(
        dict.fromkeys(
            value.strip()
            for value in raw.replace("\n", ",").replace(";", ",").split(",")
            if value.strip()
        )
    )
