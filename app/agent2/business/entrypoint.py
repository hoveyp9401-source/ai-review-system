from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.models import Agent2IdentityBinding, RouteControlAudit
from app.agent2.business.route_control import RouteControlRepository, RouteDecision, decide_route


@dataclass(frozen=True)
class Agent2EntrypointResolution:
    decision: RouteDecision
    binding: Agent2IdentityBinding | None


async def resolve_agent2_entrypoint(
    session: AsyncSession,
    *,
    settings: object,
    dingtalk_user_id: str,
    source_message_id: str,
) -> Agent2EntrypointResolution:
    if not bool(getattr(settings, "agent2_business_phase2_enabled", False)):
        return Agent2EntrypointResolution(
            RouteDecision("", "agent1", "agent2_business_phase2_disabled"),
            None,
        )
    allowed_tenants = parse_tenant_allowlist(
        getattr(settings, "agent2_business_tenant_ids", "")
    )
    if not allowed_tenants:
        return Agent2EntrypointResolution(
            RouteDecision("", "agent1", "no_test_tenant_allowlist"),
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
            RouteDecision("", "agent1", "identity_outside_agent2_test_tenants"),
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
    if control is not None:
        session.add(
            RouteControlAudit(
                tenant_id=binding.tenant_id,
                actor_user_id=binding.user_id,
                source_message_id=source_message_id,
                before_json={
                    "route_mode": control.route_mode,
                    "agent1_rollback_enabled": control.agent1_rollback_enabled,
                    "version": control.version,
                },
                after_json={
                    "resolved_route": decision.route,
                    "user_id": binding.user_id,
                },
                reason=f"route_decision:{decision.reason}",
            )
        )
        await session.flush()
    return Agent2EntrypointResolution(decision, binding)


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
