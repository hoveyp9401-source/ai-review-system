from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SandboxPrincipal:
    tenant_id: str
    user_id: str
    role_ids: tuple[str, ...]
    company_ids: tuple[str, ...] = ()
    department_ids: tuple[str, ...] = ()
    team_ids: tuple[str, ...] = ()

    def has_role(self, *roles: str) -> bool:
        return bool(set(self.role_ids).intersection(roles))


class PrincipalDirectory:
    """Resolve credentials to scope on the server; request scope is never authoritative."""

    def __init__(self, entries: dict[str, SandboxPrincipal] | None = None):
        self._entries = dict(entries or {})

    @classmethod
    def from_json(cls, raw: str) -> "PrincipalDirectory":
        if not raw.strip():
            return cls()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("legal ops principals must be a JSON object")
        entries: dict[str, SandboxPrincipal] = {}
        for token, item in payload.items():
            if not isinstance(item, dict):
                raise ValueError("principal entry must be an object")
            entries[str(token)] = _principal_from_mapping(item)
        return cls(entries)

    @classmethod
    def single_admin(cls, *, token: str, tenant_id: str, user_id: str = "sandbox-admin") -> "PrincipalDirectory":
        if not token.strip():
            return cls()
        return cls(
            {
                token: SandboxPrincipal(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    role_ids=("tenant_admin",),
                )
            }
        )

    def authenticate(self, token: str) -> SandboxPrincipal:
        supplied = str(token or "").strip()
        for expected, principal in self._entries.items():
            if supplied and hmac.compare_digest(supplied, expected):
                return principal
        raise PermissionError("invalid sandbox credential")

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": principal.tenant_id,
                "user_id": principal.user_id,
                "role_ids": list(principal.role_ids),
                "company_ids": list(principal.company_ids),
                "department_ids": list(principal.department_ids),
                "team_ids": list(principal.team_ids),
            }
            for principal in self._entries.values()
        ]


def _principal_from_mapping(item: dict[str, Any]) -> SandboxPrincipal:
    tenant_id = str(item.get("tenant_id") or "").strip()
    user_id = str(item.get("user_id") or "").strip()
    if not tenant_id or not user_id:
        raise ValueError("principal tenant_id and user_id are required")
    return SandboxPrincipal(
        tenant_id=tenant_id,
        user_id=user_id,
        role_ids=tuple(str(value) for value in item.get("role_ids") or ()),
        company_ids=tuple(str(value) for value in item.get("company_ids") or ()),
        department_ids=tuple(str(value) for value in item.get("department_ids") or ()),
        team_ids=tuple(str(value) for value in item.get("team_ids") or ()),
    )
