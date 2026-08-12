from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import RouteControlAudit, TenantRouteControl


RouteMode = Literal["agent2_primary"]
ResolvedRoute = Literal["agent2_primary", "blocked"]


@dataclass(frozen=True)
class RouteDecision:
    tenant_id: str
    route: ResolvedRoute
    reason: str
    agent1_rollback_enabled: bool = False


def decide_route(
    control: TenantRouteControl | None, *, tenant_id: str, user_id: str
) -> RouteDecision:
    if control is None or control.tenant_id != tenant_id:
        return RouteDecision(tenant_id, "blocked", "no_tenant_cutover_control")
    if control.route_mode == "agent2_canary":
        if user_id in set(control.canary_user_ids or []):
            return RouteDecision(tenant_id, "agent2_primary", "canary_user", control.agent1_rollback_enabled)
        return RouteDecision(tenant_id, "blocked", "outside_canary", control.agent1_rollback_enabled)
    if control.route_mode == "agent2_primary":
        return RouteDecision(
            tenant_id,
            "agent2_primary",
            "tenant_route_mode:agent2_primary",
            control.agent1_rollback_enabled,
        )
    return RouteDecision(
        tenant_id,
        "blocked",
        f"agent2_only_rejected_route_mode:{control.route_mode}",
        control.agent1_rollback_enabled,
    )


def decide_agent2_failure(
    decision: RouteDecision, *, explicit_rollback_requested: bool
) -> RouteDecision:
    if decision.route == "blocked":
        return decision
    return RouteDecision(
        decision.tenant_id,
        "blocked",
        "agent2_failure_no_agent1_fallback",
        decision.agent1_rollback_enabled,
    )


class RouteControlRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, tenant_id: str) -> TenantRouteControl | None:
        return await self.session.scalar(
            select(TenantRouteControl).where(TenantRouteControl.tenant_id == tenant_id)
        )

    async def change(
        self,
        *,
        tenant_id: str,
        route_mode: RouteMode,
        canary_user_ids: tuple[str, ...],
        agent1_rollback_enabled: bool,
        actor_user_id: str,
        source_message_id: str,
        reason: str,
        expected_version: int | None,
    ) -> TenantRouteControl:
        control = await self.session.scalar(
            select(TenantRouteControl)
            .where(TenantRouteControl.tenant_id == tenant_id)
            .with_for_update()
        )
        before: dict = {}
        if control is None:
            if expected_version not in (None, 0):
                raise ValueError("route_control_version_conflict")
            control = TenantRouteControl(
                tenant_id=tenant_id,
                route_mode=route_mode,
                canary_user_ids=list(canary_user_ids),
                agent1_rollback_enabled=agent1_rollback_enabled,
                version=1,
                changed_by=actor_user_id,
                change_reason=reason,
            )
            self.session.add(control)
        else:
            if expected_version is None or expected_version != control.version:
                raise ValueError("route_control_version_conflict")
            before = _control_mapping(control)
            control.route_mode = route_mode
            control.canary_user_ids = list(canary_user_ids)
            control.agent1_rollback_enabled = agent1_rollback_enabled
            control.version += 1
            control.changed_by = actor_user_id
            control.change_reason = reason

        await self.session.flush()
        self.session.add(
            RouteControlAudit(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                source_message_id=source_message_id,
                before_json=before,
                after_json=_control_mapping(control),
                reason=reason,
            )
        )
        await self.session.flush()
        return control


def _control_mapping(control: TenantRouteControl) -> dict:
    return {
        "tenant_id": control.tenant_id,
        "route_mode": control.route_mode,
        "canary_user_ids": list(control.canary_user_ids or []),
        "agent1_rollback_enabled": control.agent1_rollback_enabled,
        "version": control.version,
        "changed_by": control.changed_by,
        "change_reason": control.change_reason,
    }
