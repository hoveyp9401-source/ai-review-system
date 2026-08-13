from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from dataclasses import replace

import pytest

from app.agent2.weekly_plan_domain import (
    add_weekly_plan_suggestion,
    create_weekly_plan,
    create_weekly_plan_batch,
    execute_weekly_plan_command,
    execute_weekly_plan_batch,
    weekly_plan_submission_timing,
)
from app.agent2.weekly_plan_models import WeeklyPlanCommand, WeeklyPlanRosterMember
from app.agent2.weekly_plan_suggestions import (
    SuggestionStatus,
    TrustedSourceKind,
    build_trusted_evidence,
)


NOW = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)


def _member(user_id: str = "user-a") -> WeeklyPlanRosterMember:
    return WeeklyPlanRosterMember(
        user_id=user_id,
        display_name="测试用户",
        department_id="department-a",
        department_name="测试部门",
        team_id="team-a",
        team_name="测试小组",
    )


def _evidence(text: str):
    return build_trusted_evidence(
        owner_user_id="user-a",
        source_kind=TrustedSourceKind.CONFIRMED_RECORD,
        source_ref="daily-report-item-1",
        source_version="1",
        evidence_text=text,
    )


def test_batch_and_plan_are_pinned_to_an_exact_monday_and_monday_through_saturday() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member(),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)

    assert batch.target_week_start == date(2026, 8, 17)
    assert [day.plan_date for day in plan.days] == [
        date(2026, 8, 17),
        date(2026, 8, 18),
        date(2026, 8, 19),
        date(2026, 8, 20),
        date(2026, 8, 21),
        date(2026, 8, 22),
    ]
    assert {day.state for day in plan.days} == {"unfilled"}

    with pytest.raises(ValueError, match="Monday"):
        create_weekly_plan_batch(
            tenant_id="tenant-a",
            target_week_start=date(2026, 8, 18),
            roster=(_member(),),
            created_at=NOW,
        )


def _command(plan, command_type: str, patch: dict, *, command_id: str = "command-1"):
    return WeeklyPlanCommand(
        command_id=command_id,
        command_type=command_type,
        tenant_id=plan.tenant_id,
        actor_user_id=plan.owner_user_id,
        plan_id=plan.plan_id,
        expected_version=plan.version,
        idempotency_key=f"idem-{command_id}",
        source_message_id=f"message-{command_id}",
        patch=patch,
    )


def test_submission_after_monday_deadline_is_derived_as_late_without_changing_status() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member(),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    current = plan
    for index, day in enumerate(plan.days):
        current = execute_weekly_plan_command(
            _command(
                current,
                "set_day_empty",
                {"plan_date": day.plan_date.isoformat()},
                command_id=f"late-empty-{index}",
            ),
            plan=current,
            executed_at=NOW,
        ).after

    deadline = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
    submitted = execute_weekly_plan_command(
        _command(current, "submit_plan", {}, command_id="late-submit"),
        plan=current,
        executed_at=deadline + timedelta(minutes=1),
    ).after

    assert submitted.status == "submitted"
    assert submitted.submitted_at == deadline + timedelta(minutes=1)
    assert weekly_plan_submission_timing(submitted, deadline_at=deadline) == "late"


def test_add_edit_move_delete_and_set_empty_keep_stable_items_and_explicit_day_states() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member(),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)

    added = execute_weekly_plan_command(
        _command(
            plan,
            "add_item",
            {
                "plan_date": "2026-08-17",
                "original_text": "整理甲项目材料",
                "source": "manual",
            },
        ),
        plan=plan,
        executed_at=NOW,
    )
    item_id = added.after.days[0].items[0].item_id
    assert added.receipt.status == "executed"
    assert added.after.days[0].state == "planned"
    assert added.after.days[0].items[0].original_text == "整理甲项目材料"

    edited = execute_weekly_plan_command(
        _command(
            added.after,
            "edit_item",
            {"item_id": item_id, "original_text": "整理甲项目补充材料"},
            command_id="command-2",
        ),
        plan=added.after,
        executed_at=NOW,
    )
    assert edited.after.days[0].items[0].item_id == item_id
    assert edited.after.days[0].items[0].original_text == "整理甲项目补充材料"

    moved = execute_weekly_plan_command(
        _command(
            edited.after,
            "move_item",
            {"item_id": item_id, "plan_date": "2026-08-19"},
            command_id="command-3",
        ),
        plan=edited.after,
        executed_at=NOW,
    )
    assert moved.after.days[0].state == "unfilled"
    assert moved.after.days[2].state == "planned"
    assert moved.after.days[2].items[0].item_id == item_id

    deleted = execute_weekly_plan_command(
        _command(
            moved.after,
            "delete_item",
            {"item_id": item_id},
            command_id="command-4",
        ),
        plan=moved.after,
        executed_at=NOW,
    )
    assert deleted.after.days[2].state == "unfilled"
    assert deleted.after.days[2].items == ()

    emptied = execute_weekly_plan_command(
        _command(
            deleted.after,
            "set_day_empty",
            {"plan_date": "2026-08-22"},
            command_id="command-5",
        ),
        plan=deleted.after,
        executed_at=NOW,
    )
    assert emptied.after.days[5].state == "explicitly_empty"
    assert emptied.after.days[5].items == ()


def test_suggestion_is_not_a_commitment_until_owner_accepts_it_for_an_exact_day() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member(),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    suggested = add_weekly_plan_suggestion(
        plan=plan,
        evidence=_evidence("继续跟进甲项目材料，补齐风险提示"),
        matter_excerpt="继续跟进甲项目材料",
        created_at=NOW,
        expires_at=NOW + timedelta(days=9),
    )

    assert suggested.suggestions[0].status is SuggestionStatus.AVAILABLE
    assert all(day.state == "unfilled" and not day.items for day in suggested.days)

    accepted = execute_weekly_plan_command(
        _command(
            suggested,
            "accept_suggestion",
            {
                "suggestion_id": suggested.suggestions[0].suggestion_id,
                "plan_date": "2026-08-20",
            },
            command_id="accept-1",
        ),
        plan=suggested,
        executed_at=NOW,
    )

    assert accepted.after.suggestions[0].status is SuggestionStatus.ACCEPTED
    assert accepted.after.days[3].state == "planned"
    assert accepted.after.days[3].items[0].original_text == "继续跟进甲项目材料"
    assert (
        accepted.after.days[3].items[0].source
        == "accepted_suggestion:confirmed_record"
    )
    assert (
        accepted.after.suggestions[0].accepted_item_id
        == accepted.after.days[3].items[0].item_id
    )


def test_rejecting_suggestion_records_decision_without_creating_plan_item() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member(),),
        created_at=NOW,
    )
    plan = add_weekly_plan_suggestion(
        plan=create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW),
        evidence=build_trusted_evidence(
            owner_user_id="user-a",
            source_kind=TrustedSourceKind.USER_ORIGINAL_MESSAGE,
            source_ref="source-message-2",
            source_version="1",
            evidence_text="继续跟进乙事项",
        ),
        matter_excerpt="继续跟进乙事项",
        created_at=NOW,
        expires_at=NOW + timedelta(days=9),
    )

    rejected = execute_weekly_plan_command(
        _command(
            plan,
            "reject_suggestion",
            {"suggestion_id": plan.suggestions[0].suggestion_id},
            command_id="reject-1",
        ),
        plan=plan,
        executed_at=NOW,
    )

    assert rejected.after.suggestions[0].status is SuggestionStatus.REJECTED
    assert all(not day.items for day in rejected.after.days)


@pytest.mark.parametrize(
    ("override", "executed_keys", "expected_reason"),
    [
        ({"tenant_id": "tenant-b"}, frozenset(), "tenant_mismatch"),
        ({"actor_user_id": "user-b"}, frozenset(), "owner_mismatch"),
        ({"expected_version": 99}, frozenset(), "version_conflict"),
        ({}, frozenset({"idem-command-1"}), "duplicate"),
    ],
)
def test_mutation_fails_closed_for_wrong_tenant_owner_version_or_duplicate(
    override: dict, executed_keys: frozenset[str], expected_reason: str
) -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member(),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    command = replace(
        _command(
            plan,
            "add_item",
            {
                "plan_date": "2026-08-17",
                "original_text": "整理材料",
                "source": "manual",
            },
        ),
        **override,
    )

    result = execute_weekly_plan_command(
        command,
        plan=plan,
        executed_at=NOW,
        executed_idempotency_keys=executed_keys,
    )

    assert result.receipt.status == (
        "duplicate" if expected_reason == "duplicate" else "blocked"
    )
    assert result.receipt.reason_code == expected_reason
    assert result.receipt.actual_write is False
    assert result.after == plan
    assert result.audit_event is None


def test_preview_is_read_only_and_submit_requires_every_day_to_be_resolved() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member(),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    preview = execute_weekly_plan_command(
        _command(plan, "preview_plan", {}), plan=plan, executed_at=NOW
    )
    assert preview.receipt.reason_code == "read_only"
    assert preview.receipt.actual_write is False
    assert preview.after == plan

    blocked = execute_weekly_plan_command(
        _command(plan, "submit_plan", {}, command_id="submit-1"),
        plan=plan,
        executed_at=NOW,
    )
    assert blocked.receipt.reason_code == "unresolved_days"
    assert blocked.after == plan

    resolved = replace(
        plan,
        days=tuple(replace(day, state="explicitly_empty") for day in plan.days),
        status="pending_confirmation",
    )
    submitted = execute_weekly_plan_command(
        _command(resolved, "submit_plan", {}, command_id="submit-2"),
        plan=resolved,
        executed_at=NOW,
    )
    assert submitted.receipt.status == "executed"
    assert submitted.after.status == "submitted"
    assert submitted.after.submitted_at == NOW


def test_all_resolved_draft_waits_for_confirmation_and_submitted_plan_stays_editable() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member(),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    current = plan
    for index, day in enumerate(plan.days):
        current = execute_weekly_plan_command(
            _command(
                current,
                "set_day_empty",
                {"plan_date": day.plan_date.isoformat()},
                command_id=f"empty-{index}",
            ),
            plan=current,
            executed_at=NOW,
        ).after
    assert current.status == "pending_confirmation"

    submitted = execute_weekly_plan_command(
        _command(current, "submit_plan", {}, command_id="submit-once"),
        plan=current,
        executed_at=NOW,
    ).after
    revised_at = NOW + timedelta(hours=1)
    revised = execute_weekly_plan_command(
        _command(
            submitted,
            "add_item",
            {
                "plan_date": "2026-08-17",
                "original_text": "补充甲事项",
                "source": "manual",
            },
            command_id="revision-1",
        ),
        plan=submitted,
        executed_at=revised_at,
    )
    assert revised.receipt.status == "executed"
    assert revised.after.status == "submitted"
    assert revised.after.submitted_at == NOW
    assert revised.after.updated_at == revised_at
    assert revised.audit_event is not None


def test_submitted_revision_cannot_leave_a_previously_resolved_day_unfilled() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member(),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    current = plan
    for index, day in enumerate(plan.days):
        command_type = "add_item" if index == 0 else "set_day_empty"
        patch = (
            {
                "plan_date": day.plan_date.isoformat(),
                "original_text": "周一事项",
                "source": "manual",
            }
            if index == 0
            else {"plan_date": day.plan_date.isoformat()}
        )
        current = execute_weekly_plan_command(
            _command(
                current,
                command_type,
                patch,
                command_id=f"resolve-{index}",
            ),
            plan=current,
            executed_at=NOW,
        ).after
    submitted = execute_weekly_plan_command(
        _command(current, "submit_plan", {}, command_id="submit"),
        plan=current,
        executed_at=NOW,
    ).after
    item_id = submitted.days[0].items[0].item_id

    deletion = execute_weekly_plan_command(
        _command(
            submitted,
            "delete_item",
            {"item_id": item_id},
            command_id="delete-last-item",
        ),
        plan=submitted,
        executed_at=NOW + timedelta(hours=1),
    )

    assert deletion.receipt.status == "blocked"
    assert deletion.receipt.reason_code == "submitted_revision_unresolved_days"
    assert deletion.after == submitted

    moved = execute_weekly_plan_command(
        _command(
            submitted,
            "move_item",
            {"item_id": item_id, "plan_date": "2026-08-18"},
            command_id="move-last-item",
        ),
        plan=submitted,
        executed_at=NOW + timedelta(hours=1),
    )
    assert moved.receipt.status == "blocked"
    assert moved.receipt.reason_code == "submitted_revision_unresolved_days"
    assert moved.after == submitted


def test_submitted_delete_and_set_empty_batch_uses_final_state_and_failed_batch_is_zero_write() -> None:
    batch = create_weekly_plan_batch(
        tenant_id="tenant-a",
        target_week_start=date(2026, 8, 17),
        roster=(_member(),),
        created_at=NOW,
    )
    plan = create_weekly_plan(batch=batch, owner_user_id="user-a", created_at=NOW)
    current = plan
    for index, day in enumerate(plan.days):
        operation = "add_item" if index == 0 else "set_day_empty"
        patch = (
            {
                "plan_date": day.plan_date.isoformat(),
                "original_text": "周一事项",
                "source": "manual",
            }
            if index == 0
            else {"plan_date": day.plan_date.isoformat()}
        )
        current = execute_weekly_plan_command(
            _command(current, operation, patch, command_id=f"resolve-{index}"),
            plan=current,
            executed_at=NOW,
        ).after
    submitted = execute_weekly_plan_command(
        _command(current, "submit_plan", {}, command_id="submit-batch"),
        plan=current,
        executed_at=NOW,
    ).after
    item_id = submitted.days[0].items[0].item_id
    delete = _command(
        submitted, "delete_item", {"item_id": item_id}, command_id="delete-batch"
    )
    set_empty = _command(
        submitted,
        "set_day_empty",
        {"plan_date": "2026-08-17"},
        command_id="empty-batch",
    )
    succeeded = execute_weekly_plan_batch(
        (delete, set_empty), plan=submitted, executed_at=NOW + timedelta(hours=1)
    )
    assert succeeded.receipt.status == "executed"
    assert succeeded.after.status == "submitted"
    assert succeeded.after.days[0].state == "explicitly_empty"
    assert succeeded.after.version == submitted.version + 1

    invalid_second = _command(
        submitted,
        "edit_item",
        {"item_id": "missing-item", "original_text": "不会落库"},
        command_id="invalid-second",
    )
    failed = execute_weekly_plan_batch(
        (delete, invalid_second),
        plan=submitted,
        executed_at=NOW + timedelta(hours=1),
    )
    assert failed.receipt.status == "blocked"
    assert failed.after == submitted
    assert failed.receipt.actual_write is False
    assert failed.audit_event is None

    replay = execute_weekly_plan_batch(
        (delete, set_empty),
        plan=submitted,
        executed_at=NOW + timedelta(hours=1),
        executed_idempotency_keys=frozenset({delete.idempotency_key}),
    )
    assert replay.receipt.status == "duplicate"
    assert replay.receipt.actual_write is False
    assert replay.after == submitted
    assert replay.audit_event is None
