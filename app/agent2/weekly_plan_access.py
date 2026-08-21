"""Fail-closed access decisions for the Agent2 weekly-plan domain."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

MAX_WEEKLY_PLAN_USERS = 74


class WeeklyPlanAccessAction(str, Enum):
    READ = "read"
    WRITE = "write"
    SEND = "send"


@dataclass(frozen=True)
class WeeklyPlanAccessDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class WeeklyPlanAccessPolicy:
    enabled: bool = False
    write_enabled: bool = False
    send_enabled: bool = False
    tenant_allowlist: frozenset[str] = frozenset()
    user_allowlist: frozenset[str] = frozenset()
    send_user_allowlist: frozenset[str] = frozenset()

    def decide(
        self,
        *,
        action: WeeklyPlanAccessAction,
        tenant_id: str,
        user_id: str,
        conversation_kind: str,
    ) -> WeeklyPlanAccessDecision:
        if not isinstance(action, WeeklyPlanAccessAction):
            return _deny("weekly_plan_action_invalid")
        if self.enabled is not True:
            return _deny("weekly_plan_disabled")
        if not all(
            isinstance(value, frozenset)
            and all(_is_stable_internal_id(item) for item in value)
            for value in (
                self.tenant_allowlist,
                self.user_allowlist,
                self.send_user_allowlist,
            )
        ):
            return _deny("weekly_plan_allowlist_invalid")
        if len(self.tenant_allowlist) > 1:
            return _deny("weekly_plan_single_canary_scope_required")
        if len(self.user_allowlist) > MAX_WEEKLY_PLAN_USERS:
            return _deny("weekly_plan_user_scope_too_large")
        if len(self.send_user_allowlist) > MAX_WEEKLY_PLAN_USERS:
            return _deny("weekly_plan_send_scope_too_large")
        if not self.send_user_allowlist.issubset(self.user_allowlist):
            return _deny("weekly_plan_send_scope_not_enabled")
        if not isinstance(tenant_id, str) or not tenant_id:
            return _deny("weekly_plan_tenant_id_missing")
        if not isinstance(user_id, str) or not user_id:
            return _deny("weekly_plan_user_id_missing")
        if tenant_id not in self.tenant_allowlist:
            return _deny("weekly_plan_tenant_not_allowlisted")
        if user_id not in self.user_allowlist:
            return _deny("weekly_plan_user_not_allowlisted")
        if conversation_kind != "direct":
            return _deny("weekly_plan_direct_conversation_required")
        if (
            action is WeeklyPlanAccessAction.WRITE
            and self.write_enabled is not True
        ):
            return _deny("weekly_plan_write_disabled")
        if action is WeeklyPlanAccessAction.SEND:
            if self.send_enabled is not True:
                return _deny("weekly_plan_send_disabled")
            if user_id not in self.send_user_allowlist:
                return _deny("weekly_plan_send_user_not_allowlisted")
        return WeeklyPlanAccessDecision(True, "allowed")


def _deny(reason: str) -> WeeklyPlanAccessDecision:
    return WeeklyPlanAccessDecision(False, reason)


def _is_stable_internal_id(value: object) -> bool:
    """Reject display names and ambiguous text at this deterministic boundary.

    Identity resolution happens before this policy.  The policy accepts only the
    non-empty, whitespace-free ASCII identifiers produced by that identity layer;
    it intentionally has no display-name parameter or name matching fallback.
    """

    return (
        isinstance(value, str)
        and 1 <= len(value) <= 255
        and value.isascii()
        and not any(character.isspace() for character in value)
    )


__all__ = [
    "WeeklyPlanAccessAction",
    "WeeklyPlanAccessDecision",
    "WeeklyPlanAccessPolicy",
]
