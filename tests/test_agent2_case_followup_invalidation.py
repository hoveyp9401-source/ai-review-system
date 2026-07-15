from dataclasses import dataclass

from app.agent2.case_followup_invalidation import (
    plan_meaningful_progress_invalidation,
)


@dataclass(frozen=True)
class _Task:
    followup_id: str
    trigger_type: str
    question_type: str
    task_status: str


def test_meaningful_progress_cancels_only_stale_cadence_followups():
    tasks = (
        _Task("current", "fixed_cadence", "meaningful_progress", "waiting_for_reply"),
        _Task("scheduled", "fixed_cadence", "meaningful_progress", "scheduled"),
        _Task("waiting", "fixed_cadence", "meaningful_progress", "waiting_for_reply"),
        _Task("hearing", "hearing_proximity", "hearing_readiness", "scheduled"),
        _Task("answered", "fixed_cadence", "meaningful_progress", "answered"),
    )

    plan = plan_meaningful_progress_invalidation(
        tasks, current_followup_id="current"
    )

    assert plan.cancel_task_ids == ("scheduled", "waiting")
    assert plan.cancel_pending_for_task_ids == ("waiting",)
    assert plan.cancel_unsent_outbox_for_task_ids == ("scheduled",)


def test_progress_without_a_current_followup_still_invalidates_old_cadence_question():
    plan = plan_meaningful_progress_invalidation(
        (_Task("old", "fixed_cadence", "meaningful_progress", "queued"),),
        current_followup_id="",
    )

    assert plan.cancel_task_ids == ("old",)
    assert plan.cancel_pending_for_task_ids == ()
    assert plan.cancel_unsent_outbox_for_task_ids == ("old",)
