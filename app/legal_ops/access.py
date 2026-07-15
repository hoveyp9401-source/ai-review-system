from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import Agent2IdentityBinding
from app.legal_ops.auth import SandboxPrincipal


@dataclass(frozen=True)
class LivePrincipalScope:
    tenant_id: str
    user_id: str
    allowed_case_ids: tuple[str, ...] | None
    writable_case_ids: tuple[str, ...] | None = None
    binding: Agent2IdentityBinding | None = None


async def load_live_principal_scope(
    session: AsyncSession,
    principal: SandboxPrincipal,
) -> LivePrincipalScope:
    """Resolve live authorization from PostgreSQL; credential claims only identify the actor."""
    if principal.has_role("tenant_admin", "system_admin"):
        return LivePrincipalScope(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            allowed_case_ids=None,
        )
    binding = await session.scalar(
        select(Agent2IdentityBinding).where(
            Agent2IdentityBinding.tenant_id == principal.tenant_id,
            Agent2IdentityBinding.user_id == principal.user_id,
            Agent2IdentityBinding.active.is_(True),
        )
    )
    if binding is None:
        raise PermissionError("active Legal Ops identity binding required")
    raw_case_ids = (binding.permission_scope_json or {}).get("allowed_case_ids", [])
    allowed_case_ids: list[str] = []
    for value in raw_case_ids if isinstance(raw_case_ids, list) else ():
        try:
            parsed = str(UUID(str(value)))
        except (TypeError, ValueError):
            continue
        if parsed not in allowed_case_ids:
            allowed_case_ids.append(parsed)
    raw_writable_case_ids = (binding.permission_scope_json or {}).get(
        "writable_case_ids"
    )
    writable_case_ids: list[str] | None = (
        [] if isinstance(raw_writable_case_ids, list) else None
    )
    for value in raw_writable_case_ids if isinstance(raw_writable_case_ids, list) else ():
        try:
            parsed = str(UUID(str(value)))
        except (TypeError, ValueError):
            continue
        if writable_case_ids is not None and parsed not in writable_case_ids:
            writable_case_ids.append(parsed)
    return LivePrincipalScope(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        allowed_case_ids=tuple(allowed_case_ids),
        writable_case_ids=(
            tuple(writable_case_ids) if writable_case_ids is not None else None
        ),
        binding=binding,
    )
