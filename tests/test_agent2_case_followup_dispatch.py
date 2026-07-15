from datetime import datetime, timedelta, timezone

import pytest

from app.agent2.case_followup_dispatch import (
    FollowupDispatchState,
    accept_provider_receipt,
    build_followup_outbox_plan,
    build_followup_reminder_outbox_plan,
    settle_followup_answer,
)


NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)


def _scheduled():
    return FollowupDispatchState(
        followup_id="followup-1",
        tenant_id="tenant-a",
        user_id="user-1",
        conversation_id="conversation-1",
        case_id="case-1",
        case_name="南京工程款案",
        case_version=4,
        question_text="南京工程款案已经一周没有更新了。目前有什么新情况？",
        task_status="scheduled",
        message_status="scheduled",
        response_status="not_requested",
        expires_at=NOW + timedelta(days=7),
        version=1,
    )


def test_outbox_plan_is_stable_and_contains_only_bound_case_facts():
    first = build_followup_outbox_plan(_scheduled(), now=NOW)
    second = build_followup_outbox_plan(_scheduled(), now=NOW)

    assert first.notification_id == second.notification_id
    assert first.idempotency_key == second.idempotency_key
    assert first.payload["case_id"] == "case-1"
    assert first.payload["followup_id"] == "followup-1"
    assert first.payload["text"] == _scheduled().question_text
    assert first.state_after.message_status == "queued"


def test_provider_acceptance_requires_external_message_id_and_creates_pending():
    queued = build_followup_outbox_plan(_scheduled(), now=NOW).state_after

    accepted = accept_provider_receipt(
        queued, external_message_id="ding-task-1", now=NOW,
        expected_state_version=8,
    )

    assert accepted.state_after.message_status == "accepted_by_provider"
    assert accepted.state_after.response_status == "awaiting_input"
    assert accepted.state_after.task_status == "waiting_for_reply"
    assert accepted.pending.source_message_id == "ding-task-1"
    assert accepted.pending.expected_state_version == 8
    assert accepted.delivery_confirmed is False


def test_missing_provider_message_id_cannot_be_accepted_or_create_pending():
    queued = build_followup_outbox_plan(_scheduled(), now=NOW).state_after

    with pytest.raises(ValueError, match="provider message id"):
        accept_provider_receipt(
            queued, external_message_id="", now=NOW, expected_state_version=8
        )


def test_pending_is_consumed_only_after_committed_case_receipt():
    queued = build_followup_outbox_plan(_scheduled(), now=NOW).state_after
    accepted = accept_provider_receipt(
        queued, external_message_id="ding-task-1", now=NOW,
        expected_state_version=8,
    )

    unchanged = settle_followup_answer(
        accepted.state_after, accepted.pending, now=NOW,
        case_receipt_status="failed", case_actual_write=False,
    )
    assert unchanged.pending_after.status == "awaiting_input"
    assert unchanged.state_after.task_status == "waiting_for_reply"

    completed = settle_followup_answer(
        accepted.state_after, accepted.pending, now=NOW,
        case_receipt_status="executed", case_actual_write=True,
    )
    assert completed.pending_after.status == "consumed"
    assert completed.state_after.task_status == "answered"
    assert completed.state_after.response_status == "answered"


def test_reminder_has_stable_per_attempt_id_and_keeps_original_pending_state():
    queued = build_followup_outbox_plan(_scheduled(), now=NOW).state_after
    accepted = accept_provider_receipt(
        queued, external_message_id="ding-task-1", now=NOW,
        expected_state_version=8,
    ).state_after

    first = build_followup_reminder_outbox_plan(
        accepted, reminder_number=1, now=NOW + timedelta(days=2)
    )
    replay = build_followup_reminder_outbox_plan(
        accepted, reminder_number=1, now=NOW + timedelta(days=2)
    )

    assert first.idempotency_key == replay.idempotency_key
    assert first.payload["is_reminder"] == "true"
    assert first.state_after == accepted
