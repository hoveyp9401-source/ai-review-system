from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any

from app.workflows.intake import (
    EFFECT_ADD_DAILY_REPORT_ITEM,
    EFFECT_CONFIRM_DAILY_REPORT,
    EFFECT_LEGACY_DAILY_CONTEXT_ACTION,
    WORKFLOW_DAILY_REPORT,
)


DAILY_FIELDS = ("today_work", "problems", "tomorrow_plan")
ALL_TARGET_FIELDS = ("*",)


@dataclass(frozen=True)
class AuthorizedAction:
    """A narrow authorization ticket for one capability operation."""

    authorization_id: str
    plan_id: str
    turn_id: str
    workflow: str
    capability: str
    operation: str
    allowed_target_fields: tuple[str, ...] = ALL_TARGET_FIELDS
    write_policy: str = "dry_run"
    task_id: str = ""
    source_effect_type: str = ""
    requires_confirmation: bool = False
    expires_at: datetime | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "authorization_id": self.authorization_id,
            "plan_id": self.plan_id,
            "turn_id": self.turn_id,
            "workflow": self.workflow,
            "capability": self.capability,
            "operation": self.operation,
            "allowed_target_fields": list(self.allowed_target_fields),
            "write_policy": self.write_policy,
            "task_id": self.task_id,
            "source_effect_type": self.source_effect_type,
            "requires_confirmation": self.requires_confirmation,
            "expires_at": self.expires_at.isoformat() if self.expires_at else "",
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    write_policy: str = "blocked"
    authorization_id: str = ""
    plan_id: str = ""
    safety_flags: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "write_policy": self.write_policy,
            "authorization_id": self.authorization_id,
            "plan_id": self.plan_id,
            "safety_flags": list(self.safety_flags),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExecutionPolicy:
    """Turn-scoped authorization tokens derived from the first routing layer."""

    turn_id: str
    plan_id: str
    authorized_actions: list[AuthorizedAction] = field(default_factory=list)
    default_write_policy: str = "blocked"

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "plan_id": self.plan_id,
            "default_write_policy": self.default_write_policy,
            "authorized_actions": [action.as_dict() for action in self.authorized_actions],
        }


def build_execution_policy(
    *,
    turn_id: str,
    routing_plan: Any,
    now: datetime | None = None,
    ttl_seconds: int = 600,
) -> ExecutionPolicy:
    """Build capability authorization tickets from a routing plan's effects."""

    issued_at = now or datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    plan_id = _plan_id(turn_id, routing_plan)
    actions: list[AuthorizedAction] = []
    for index, effect in enumerate(getattr(routing_plan, "effects", []) or [], start=1):
        for operation in _operations_for_effect(effect):
            write_policy = _write_policy_for_effect(effect)
            actions.append(
                AuthorizedAction(
                    authorization_id=_authorization_id(plan_id, effect, operation, index),
                    plan_id=plan_id,
                    turn_id=turn_id,
                    workflow=str(getattr(effect, "target_system", "") or ""),
                    capability=str(getattr(effect, "target_system", "") or ""),
                    operation=operation,
                    allowed_target_fields=_allowed_target_fields(effect),
                    write_policy=write_policy,
                    task_id=str((getattr(effect, "target", {}) or {}).get("task_id") or getattr(routing_plan, "task_id", "") or ""),
                    source_effect_type=str(getattr(effect, "effect_type", "") or ""),
                    requires_confirmation=bool(getattr(effect, "requires_confirmation", False)),
                    expires_at=expires_at,
                    reason=str(getattr(effect, "reason", "") or getattr(routing_plan, "reason", "") or ""),
                )
            )
    return ExecutionPolicy(turn_id=turn_id, plan_id=plan_id, authorized_actions=actions)


def authorize_capability_request(
    policy: ExecutionPolicy | None,
    *,
    turn_id: str,
    capability: str,
    operation: str,
    target_field: str = "",
    task_id: str = "",
    requested_write_policy: str = "dry_run",
    now: datetime | None = None,
) -> AuthorizationDecision:
    """Authorize one capability operation against a turn-scoped policy."""

    if policy is None:
        return _deny("missing_execution_policy", "capability request has no execution policy")
    if policy.turn_id != turn_id:
        return _deny("turn_id_mismatch", "authorization was issued for a different turn", plan_id=policy.plan_id)

    operation_matches = [
        action
        for action in policy.authorized_actions
        if action.capability == capability and _operation_matches(action.operation, operation)
    ]
    if not operation_matches:
        return _deny("operation_not_authorized", "operation is outside the routing plan", plan_id=policy.plan_id)

    current_time = now or datetime.now(timezone.utc)
    unexpired = [action for action in operation_matches if not _is_expired(action, current_time)]
    if not unexpired:
        return _deny("authorization_expired", "authorization token expired", plan_id=policy.plan_id)

    task_matches = [
        action
        for action in unexpired
        if not action.task_id or not task_id or action.task_id == task_id
    ]
    if not task_matches:
        return _deny("task_id_mismatch", "authorization belongs to another task", plan_id=policy.plan_id)

    field_matches = [action for action in task_matches if _target_field_allowed(action, target_field)]
    if not field_matches:
        return _deny("field_not_authorized", "target field is outside the authorization", plan_id=policy.plan_id)

    saw_sandbox_mismatch = False
    for action in field_matches:
        if action.write_policy == "sandbox" and requested_write_policy != "sandbox":
            saw_sandbox_mismatch = True
            continue
        if action.write_policy not in {"sandbox", requested_write_policy} and requested_write_policy != "read_only":
            continue
        return AuthorizationDecision(
            allowed=True,
            write_policy=action.write_policy,
            authorization_id=action.authorization_id,
            plan_id=action.plan_id,
            reason=action.reason or "authorized by routing plan",
        )
    if saw_sandbox_mismatch:
        return _deny(
            "sandbox_write_policy_required",
            "sandbox authorization cannot be used for a commit-like write",
            plan_id=policy.plan_id,
        )
    return _deny("write_policy_not_authorized", "requested write policy is outside the authorization", plan_id=policy.plan_id)


def _operations_for_effect(effect: Any) -> tuple[str, ...]:
    effect_type = str(getattr(effect, "effect_type", "") or "")
    if effect_type == EFFECT_ADD_DAILY_REPORT_ITEM:
        return ("fill",)
    if effect_type == EFFECT_CONFIRM_DAILY_REPORT:
        return ("confirm",)
    if effect_type == EFFECT_LEGACY_DAILY_CONTEXT_ACTION:
        return ("fill", "edit", "clear", "revoke", "copy_previous", "complete_previous_plan", "confirm")
    if effect_type == "upsert_travel_plan":
        return ("upsert_travel_plan",)
    if effect_type == "append_case_progress":
        return ("append_case_progress",)
    if effect_type == "capture_monthly_report_reply":
        return ("capture_reply",)
    if effect_type == "confirm_monthly_report_submission":
        return ("confirm_submission",)
    return (effect_type,) if effect_type else ()


def _write_policy_for_effect(effect: Any) -> str:
    target_system = str(getattr(effect, "target_system", "") or "")
    if target_system == WORKFLOW_DAILY_REPORT:
        return "dry_run"
    if target_system == "monthly_report":
        return "dry_run"
    if bool(getattr(effect, "requires_confirmation", False)):
        return "sandbox"
    return "read_only"


def _allowed_target_fields(effect: Any) -> tuple[str, ...]:
    target_system = str(getattr(effect, "target_system", "") or "")
    target = getattr(effect, "target", {}) or {}
    if target_system != WORKFLOW_DAILY_REPORT:
        return ALL_TARGET_FIELDS
    field_value = str(target.get("field") or "").strip()
    if field_value in DAILY_FIELDS:
        return (field_value,)
    return DAILY_FIELDS


def _operation_matches(grant_operation: str, requested_operation: str) -> bool:
    return grant_operation == "*" or grant_operation == requested_operation


def _target_field_allowed(action: AuthorizedAction, target_field: str) -> bool:
    if "*" in action.allowed_target_fields:
        return True
    if not target_field or target_field in {"all", "none"}:
        return True
    return target_field in action.allowed_target_fields


def _is_expired(action: AuthorizedAction, now: datetime) -> bool:
    return action.expires_at is not None and action.expires_at <= now


def _deny(flag: str, reason: str, *, plan_id: str = "") -> AuthorizationDecision:
    return AuthorizationDecision(
        allowed=False,
        write_policy="blocked",
        plan_id=plan_id,
        safety_flags=["authorization_denied", flag],
        reason=reason,
    )


def _plan_id(turn_id: str, routing_plan: Any) -> str:
    effect_types = ",".join(str(getattr(effect, "effect_type", "") or "") for effect in getattr(routing_plan, "effects", []) or [])
    raw = f"{turn_id}:{getattr(routing_plan, 'primary_workflow', '')}:{effect_types}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _authorization_id(plan_id: str, effect: Any, operation: str, index: int) -> str:
    raw = f"{plan_id}:{index}:{getattr(effect, 'target_system', '')}:{getattr(effect, 'effect_type', '')}:{operation}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
