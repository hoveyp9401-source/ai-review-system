from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from app.agent2.case_lifecycle_followup import (
    CadenceType,
    CaseFollowupPolicySnapshot,
    calculate_next_due_at,
)


@dataclass(frozen=True)
class UpdateCaseFollowupPolicy:
    command_id: str
    tenant_id: str
    case_id: str
    assigned_user_id: str
    expected_version: int
    policy_source: str
    cadence_type: CadenceType
    enabled: bool
    force_manual_override: bool
    source_turn_id: str
    idempotency_key: str
    custom_interval_days: int | None = None
    business_days_only: bool | None = None
    snoozed_until: datetime | None = None
    hearing_reminders_enabled: bool | None = None
    stage_transition_enabled: bool | None = None
    node_transition_enabled: bool | None = None
    command_type: str = "update_case_followup_policy"


@dataclass(frozen=True)
class TriggerCaseFollowupNow:
    command_id: str
    tenant_id: str
    case_id: str
    assigned_user_id: str
    source_turn_id: str
    idempotency_key: str
    expected_policy_version: int | None = None
    command_type: str = "trigger_case_followup_now"


@dataclass(frozen=True)
class CancelCaseFollowupTask:
    command_id: str
    tenant_id: str
    case_id: str
    followup_id: str
    assigned_user_id: str
    expected_version: int
    source_turn_id: str
    idempotency_key: str
    command_type: str = "cancel_case_followup_task"


@dataclass(frozen=True)
class PolicyCommandExecution:
    status: str
    reason_code: str
    before: CaseFollowupPolicySnapshot
    after: CaseFollowupPolicySnapshot
    actual_write: bool


def execute_policy_update(
    current: CaseFollowupPolicySnapshot,
    command: UpdateCaseFollowupPolicy,
    *,
    actor_has_case_permission: bool,
    now: datetime,
) -> PolicyCommandExecution:
    if not actor_has_case_permission:
        return PolicyCommandExecution("blocked", "permission_denied", current, current, False)
    if (
        current.tenant_id != command.tenant_id
        or current.case_id != command.case_id
        or current.assigned_user_id != command.assigned_user_id
    ):
        return PolicyCommandExecution("blocked", "policy_scope_mismatch", current, current, False)
    if current.version != command.expected_version:
        return PolicyCommandExecution("blocked", "version_conflict", current, current, False)
    if (
        current.policy_source == "case_manual_override"
        and command.policy_source != "case_manual_override"
        and not command.force_manual_override
    ):
        return PolicyCommandExecution(
            "blocked", "manual_override_preserved", current, current, False
        )
    next_due_at = current.next_due_at
    if command.snoozed_until is not None and command.snoozed_until <= now:
        return PolicyCommandExecution("blocked", "snooze_must_be_future", current, current, False)
    if current.last_meaningful_progress_at is not None and command.cadence_type in {
        "daily", "weekly", "every_15_days", "monthly", "custom_interval"
    }:
        next_due_at = calculate_next_due_at(
            current.last_meaningful_progress_at,
            cadence_type=command.cadence_type,
            timezone_name=current.timezone,
            custom_interval_days=command.custom_interval_days,
            business_days_only=(
                current.business_days_only
                if command.business_days_only is None
                else command.business_days_only
            ),
        )
    after = replace(
        current,
        enabled=command.enabled,
        cadence_type=command.cadence_type,
        policy_source=command.policy_source,
        next_due_at=next_due_at,
        custom_interval_days=command.custom_interval_days,
        business_days_only=(
            current.business_days_only
            if command.business_days_only is None
            else command.business_days_only
        ),
        snoozed_until=command.snoozed_until,
        hearing_reminders_enabled=(
            current.hearing_reminders_enabled
            if command.hearing_reminders_enabled is None
            else command.hearing_reminders_enabled
        ),
        stage_transition_enabled=(
            current.stage_transition_enabled
            if command.stage_transition_enabled is None
            else command.stage_transition_enabled
        ),
        node_transition_enabled=(
            current.node_transition_enabled
            if command.node_transition_enabled is None
            else command.node_transition_enabled
        ),
        version=current.version + 1,
    )
    return PolicyCommandExecution("executed", "ok", current, after, True)
