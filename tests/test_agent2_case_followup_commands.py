from datetime import datetime, timedelta, timezone

from app.agent2.case_followup_commands import (
    UpdateCaseFollowupPolicy,
    execute_policy_update,
)
from app.agent2.case_lifecycle_followup import CaseFollowupPolicySnapshot


def test_bulk_policy_does_not_override_case_manual_policy_without_explicit_force():
    current = CaseFollowupPolicySnapshot(
        policy_id="policy-1", tenant_id="tenant-a", case_id="case-1",
        assigned_user_id="user-1", enabled=True, cadence_type="daily",
        timezone="Asia/Shanghai", last_meaningful_progress_at=None,
        next_due_at=None, version=3, policy_source="case_manual_override",
    )
    command = UpdateCaseFollowupPolicy(
        command_id="command-1", tenant_id="tenant-a", case_id="case-1",
        assigned_user_id="user-1", expected_version=3,
        policy_source="bulk_assignment", cadence_type="weekly", enabled=True,
        force_manual_override=False, source_turn_id="turn-1",
        idempotency_key="policy:bulk:1",
    )

    blocked = execute_policy_update(
        current, command, actor_has_case_permission=True,
        now=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )

    assert blocked.status == "blocked"
    assert blocked.reason_code == "manual_override_preserved"
    assert blocked.actual_write is False
    assert blocked.after == current


def test_custom_interval_and_future_snooze_are_calculated_from_meaningful_progress():
    progress_at = datetime(2026, 7, 1, 9, tzinfo=timezone.utc)
    current = CaseFollowupPolicySnapshot(
        policy_id="policy-1", tenant_id="tenant-a", case_id="case-1",
        assigned_user_id="user-1", enabled=True, cadence_type="weekly",
        timezone="Asia/Shanghai", last_meaningful_progress_at=progress_at,
        next_due_at=None, version=1,
    )
    snoozed_until = datetime(2026, 7, 20, 9, tzinfo=timezone.utc)
    command = UpdateCaseFollowupPolicy(
        command_id="command-custom", tenant_id="tenant-a", case_id="case-1",
        assigned_user_id="user-1", expected_version=1,
        policy_source="case_manual_override", cadence_type="custom_interval",
        enabled=True, force_manual_override=False, source_turn_id="turn-custom",
        idempotency_key="policy:custom:1", custom_interval_days=10,
        snoozed_until=snoozed_until,
    )

    result = execute_policy_update(
        current, command, actor_has_case_permission=True,
        now=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )

    assert result.status == "executed"
    assert result.after.next_due_at == progress_at + timedelta(days=10)
    assert result.after.snoozed_until == snoozed_until
    assert result.after.policy_source == "case_manual_override"
