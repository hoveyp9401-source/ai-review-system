from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import Agent2IdentityBinding
from app.legal_ops.auth import SandboxPrincipal

ROLE_LABELS = {
    "data_viewer": "数据查看者",
    "performance_data_maintainer": "绩效数据维护人员",
    "case_data_maintainer": "案件数据维护人员",
    "data_publisher": "数据发布管理员",
    "system_admin": "系统管理员",
    "tenant_admin": "系统管理员",
}

CAPABILITY_ROLES = {
    "view": set(ROLE_LABELS),
    "performance_upload": {
        "performance_data_maintainer",
        "system_admin",
        "tenant_admin",
    },
    "case_upload": {
        "case_data_maintainer",
        "system_admin",
        "tenant_admin",
    },
    "publish": {"data_publisher", "system_admin", "tenant_admin"},
    "system_admin": {"system_admin", "tenant_admin"},
}


@dataclass(frozen=True)
class IntakeAccess:
    tenant_id: str
    user_id: str
    roles: tuple[str, ...]

    @property
    def capabilities(self) -> dict[str, bool]:
        role_set = set(self.roles)
        return {
            capability: bool(role_set.intersection(roles))
            for capability, roles in CAPABILITY_ROLES.items()
        }

    def require(self, capability: str) -> None:
        if not self.capabilities.get(capability, False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "permission_denied",
                    "message": "当前账号没有执行此操作的权限",
                },
            )

    def payload(self) -> dict:
        return {
            "user_name": self.user_id,
            "roles": [
                {"code": role, "label": ROLE_LABELS.get(role, "已授权角色")}
                for role in self.roles
            ],
            "capabilities": self.capabilities,
        }


async def resolve_intake_access(
    session: AsyncSession,
    principal: SandboxPrincipal,
    *,
    live_mode: bool,
) -> IntakeAccess:
    principal_roles = tuple(role for role in principal.role_ids if role in ROLE_LABELS)
    if set(principal_roles).intersection({"tenant_admin", "system_admin"}):
        return IntakeAccess(principal.tenant_id, principal.user_id, principal_roles)

    binding = await session.scalar(
        select(Agent2IdentityBinding).where(
            Agent2IdentityBinding.tenant_id == principal.tenant_id,
            Agent2IdentityBinding.user_id == principal.user_id,
            Agent2IdentityBinding.active.is_(True),
        )
    )
    if binding is None:
        if live_mode:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "identity_binding_missing",
                    "message": "当前账号尚未配置数据接入中心权限",
                },
            )
        roles = principal_roles
    else:
        roles = tuple(
            str(role) for role in binding.role_ids if str(role) in ROLE_LABELS
        )
    access = IntakeAccess(principal.tenant_id, principal.user_id, roles)
    access.require("view")
    return access
