from datetime import datetime, timedelta, timezone

from app.agent2.daily_commands import DailyCommand
from app.agent_core.daily_capability import run_daily_capability
from app.agent_core.execution_policy import (
    AuthorizedAction,
    ExecutionPolicy,
    authorize_capability_request,
)
from app.agent_core.types import DailySnapshot


def _daily_command(text: str, *, target_field: str = "today_work") -> DailyCommand:
    return DailyCommand(
        operation="fill",
        target_field=target_field,  # type: ignore[arg-type]
        content=[text],
        should_write=True,
        source_effect_type="daily_entry",
    )


def _policy(
    *,
    operation: str = "fill",
    target_fields: tuple[str, ...] = ("today_work",),
    write_policy: str = "dry_run",
    expires_at: datetime | None = None,
) -> ExecutionPolicy:
    return ExecutionPolicy(
        turn_id="turn-auth",
        plan_id="plan-auth",
        authorized_actions=[
            AuthorizedAction(
                authorization_id="auth-daily-1",
                plan_id="plan-auth",
                turn_id="turn-auth",
                workflow="daily_report",
                capability="daily_report",
                operation=operation,
                allowed_target_fields=target_fields,
                write_policy=write_policy,
                expires_at=expires_at,
                reason="test grant",
            )
        ],
    )


def test_daily_capability_rejects_write_without_authorization():
    result = run_daily_capability(
        turn_id="turn-auth",
        snapshot=DailySnapshot(),
        commands=[_daily_command("合同审核")],
    )

    assert result.after.today_work == []
    assert result.changed is False
    assert result.read_only is True
    assert result.operation_ledger[0].write_policy == "blocked"
    assert "authorization_denied" in result.operation_ledger[0].safety_flags


def test_daily_capability_allows_write_with_matching_authorization():
    result = run_daily_capability(
        turn_id="turn-auth",
        snapshot=DailySnapshot(),
        commands=[_daily_command("合同审核")],
        execution_policy=_policy(),
    )

    assert result.after.today_work == ["合同审核"]
    assert result.changed is True
    assert result.operation_ledger[0].authorization_id == "auth-daily-1"
    assert result.operation_ledger[0].write_policy == "dry_run"


def test_daily_capability_rejects_field_overreach():
    result = run_daily_capability(
        turn_id="turn-auth",
        snapshot=DailySnapshot(),
        commands=[_daily_command("客户未反馈", target_field="problems")],
        execution_policy=_policy(target_fields=("today_work",)),
    )

    assert result.after.problems == []
    assert result.operation_ledger[0].write_policy == "blocked"
    assert "field_not_authorized" in result.operation_ledger[0].safety_flags


def test_authorization_rejects_expired_grant():
    now = datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc)
    decision = authorize_capability_request(
        _policy(expires_at=now - timedelta(seconds=1)),
        turn_id="turn-auth",
        capability="daily_report",
        operation="fill",
        target_field="today_work",
        requested_write_policy="dry_run",
        now=now,
    )

    assert decision.allowed is False
    assert "authorization_expired" in decision.safety_flags


def test_sandbox_authorization_cannot_be_used_for_commit_like_write():
    decision = authorize_capability_request(
        _policy(write_policy="sandbox"),
        turn_id="turn-auth",
        capability="daily_report",
        operation="fill",
        target_field="today_work",
        requested_write_policy="dry_run",
    )

    assert decision.allowed is False
    assert "sandbox_write_policy_required" in decision.safety_flags


def test_authorization_continues_past_non_matching_sandbox_grant():
    policy = ExecutionPolicy(
        turn_id="turn-auth",
        plan_id="plan-auth",
        authorized_actions=[
            AuthorizedAction(
                authorization_id="auth-sandbox",
                plan_id="plan-auth",
                turn_id="turn-auth",
                workflow="daily_report",
                capability="daily_report",
                operation="fill",
                allowed_target_fields=("today_work",),
                write_policy="sandbox",
            ),
            AuthorizedAction(
                authorization_id="auth-dry-run",
                plan_id="plan-auth",
                turn_id="turn-auth",
                workflow="daily_report",
                capability="daily_report",
                operation="fill",
                allowed_target_fields=("today_work",),
                write_policy="dry_run",
            ),
        ],
    )

    decision = authorize_capability_request(
        policy,
        turn_id="turn-auth",
        capability="daily_report",
        operation="fill",
        target_field="today_work",
        requested_write_policy="dry_run",
    )

    assert decision.allowed is True
    assert decision.authorization_id == "auth-dry-run"
