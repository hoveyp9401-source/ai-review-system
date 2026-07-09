from types import SimpleNamespace

from app.agent2.daily_state import PENDING_DAILY_CANDIDATE_KEY
from app.services.state_machine import STATUS_COLLECTING, STATUS_COMPLETED, STATUS_PENDING_CONFIRMATION
from app.workflows.daily_context import (
    daily_active_task_from_report,
    daily_active_task_from_snapshot,
    replay_context_before_message,
    update_replay_daily_context,
)
from app.workflows.intake import WORKFLOW_DAILY_REPORT


def test_daily_active_task_from_pending_confirmation_report():
    task = daily_active_task_from_report(
        SimpleNamespace(
            id="report-1",
            status=STATUS_PENDING_CONFIRMATION,
            report_date="2026-07-01",
            section_status={},
        )
    )

    assert task is not None
    assert task.workflow == WORKFLOW_DAILY_REPORT
    assert task.task_id == "report-1"
    assert task.reply_candidate is True
    assert task.awaiting_confirmation is True


def test_daily_active_task_keeps_pending_candidate_without_awaiting_confirmation():
    task = daily_active_task_from_report(
        SimpleNamespace(
            id="report-candidate",
            status=STATUS_COLLECTING,
            report_date="2026-07-05",
            section_status={
                PENDING_DAILY_CANDIDATE_KEY: {
                    "field": "today_work",
                    "field_index": 2,
                    "item_id": "tw-2",
                    "text": "进行上海机载项目评审",
                }
            },
        )
    )

    assert task is not None
    assert task.reply_candidate is True
    assert task.awaiting_confirmation is False
    assert PENDING_DAILY_CANDIDATE_KEY in task.metadata["pending_keys"]


def test_replay_context_tracks_active_daily_and_clears_completed_report():
    collecting = SimpleNamespace(
        legacy_action="report_update",
        legacy_status="",
        legacy_report_id="report-2",
        report_date="2026-07-01",
        after_snapshot={
            "report_id": "report-2",
            "report_date": "2026-07-01",
            "status": STATUS_COLLECTING,
        },
    )
    context = update_replay_daily_context(None, collecting)
    task = replay_context_before_message(context)

    assert task is not None
    assert task.workflow == WORKFLOW_DAILY_REPORT
    assert task.reply_candidate is True

    completed = SimpleNamespace(
        legacy_action="confirm_submit",
        legacy_status="",
        legacy_report_id="report-2",
        report_date="2026-07-01",
        after_snapshot={
            "report_id": "report-2",
            "report_date": "2026-07-01",
            "status": STATUS_COMPLETED,
        },
    )

    assert update_replay_daily_context(context, completed) is None


def test_daily_active_task_from_snapshot_uses_existing_report_draft():
    task = daily_active_task_from_snapshot(
        {
            "report_id": "report-3",
            "report_date": "2026-07-01",
            "status": STATUS_COLLECTING,
            "today_work_count": 2,
            "problems_count": 0,
            "tomorrow_plan_count": 1,
        }
    )

    assert task is not None
    assert task.task_id == "report-3"
    assert task.reply_candidate is True
