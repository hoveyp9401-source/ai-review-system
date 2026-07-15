from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CaseFollowupGateDecision:
    create_task: bool
    send_message: bool
    allow_projection: bool
    reason_code: str


class CaseFollowupEffectGate:
    def __init__(
        self,
        *,
        enabled: bool,
        send_enabled: bool,
        projection_enabled: bool,
        tenant_allowlist: tuple[str, ...],
        user_allowlist: tuple[str, ...],
        trigger_allowlist: tuple[str, ...],
        user_daily_limit: int,
        case_daily_limit: int,
    ):
        self.enabled = enabled
        self.send_enabled = send_enabled
        self.projection_enabled = projection_enabled
        self.tenants = frozenset(tenant_allowlist)
        self.users = frozenset(user_allowlist)
        self.triggers = frozenset(trigger_allowlist)
        self.user_daily_limit = user_daily_limit
        self.case_daily_limit = case_daily_limit

    def decide(
        self,
        *,
        tenant_id: str,
        user_id: str,
        case_id: str,
        trigger_type: str,
        case_is_assigned: bool,
        user_messages_today: int,
        case_messages_today: int,
    ) -> CaseFollowupGateDecision:
        del case_id
        if not self.enabled:
            return CaseFollowupGateDecision(False, False, False, "followup_disabled")
        if tenant_id not in self.tenants:
            return CaseFollowupGateDecision(False, False, False, "tenant_not_allowed")
        if user_id not in self.users:
            return CaseFollowupGateDecision(False, False, False, "user_not_allowed")
        if not case_is_assigned:
            return CaseFollowupGateDecision(False, False, False, "case_not_assigned")
        if trigger_type not in self.triggers:
            return CaseFollowupGateDecision(False, False, False, "trigger_not_allowed")
        within_limits = (
            user_messages_today < self.user_daily_limit
            and case_messages_today < self.case_daily_limit
        )
        reason = "allowed" if within_limits else "daily_limit_reached"
        return CaseFollowupGateDecision(
            create_task=True,
            send_message=self.send_enabled and within_limits,
            allow_projection=self.projection_enabled,
            reason_code=reason,
        )
