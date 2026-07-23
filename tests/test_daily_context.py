import asyncio
from datetime import date, datetime
from types import SimpleNamespace

from app.agent2.daily_state import PENDING_DAILY_CANDIDATE_KEY
from app.services.state_machine import STATUS_COLLECTING, STATUS_COMPLETED, STATUS_PENDING_CONFIRMATION
from app.workflows.daily_context import (
    build_live_daily_active_task,
    daily_active_task_from_report,
    daily_active_task_from_snapshot,
    load_live_daily_context,
    load_live_daily_report,
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


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _ExecuteResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _Scalars(self._values)


class _Session:
    def __init__(self, values):
        self._values = values

    async def execute(self, _statement):
        return _ExecuteResult(self._values)


class _SequenceSession:
    def __init__(self, *values):
        self._values = list(values)

    async def execute(self, _statement):
        return _ExecuteResult(self._values.pop(0))


def test_live_daily_context_never_promotes_an_old_collecting_report_to_today(monkeypatch):
    old_report = SimpleNamespace(
        id="old-report",
        status=STATUS_COLLECTING,
        report_date=date(2026, 7, 9),
        section_status={},
    )
    today_report = SimpleNamespace(
        id="today-report",
        status=STATUS_COLLECTING,
        report_date=date(2026, 7, 12),
        section_status={},
    )
    monkeypatch.setattr(
        "app.workflows.daily_context.now_in_timezone",
        lambda _timezone: datetime(2026, 7, 12, 19, 30),
    )

    report = asyncio.run(
        load_live_daily_report(
            _Session((old_report, today_report)),
            SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
            SimpleNamespace(timezone="Asia/Shanghai"),
        )
    )

    assert report is today_report


def test_live_daily_context_binds_a_before_cutoff_reply_to_the_recent_reminder_date(monkeypatch):
    reminder = SimpleNamespace(
        id="reminder-event-1",
        report_id=None,
        report_date=date(2026, 7, 14),
        backend_action="daily_report_reminder_sent",
        created_at=datetime(2026, 7, 14, 22, 0),
    )
    monkeypatch.setattr(
        "app.workflows.daily_context.now_in_timezone",
        lambda _timezone: datetime(2026, 7, 15, 6, 42),
    )

    context = asyncio.run(
        load_live_daily_context(
            _SequenceSession((), (reminder,), ()),
            SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
            SimpleNamespace(timezone="Asia/Shanghai"),
        )
    )

    assert context.report is None
    assert context.report_date == date(2026, 7, 14)
    assert context.source == "recent_reminder"
    assert context.active_task is not None
    assert context.active_task.metadata["report_date"] == "2026-07-14"


def test_live_daily_active_task_uses_the_recent_reminder_context(monkeypatch):
    reminder = SimpleNamespace(
        id="reminder-event-2",
        report_id=None,
        report_date=date(2026, 7, 14),
        backend_action="daily_report_reminder_sent",
        created_at=datetime(2026, 7, 14, 22, 0),
    )
    monkeypatch.setattr(
        "app.workflows.daily_context.now_in_timezone",
        lambda _timezone: datetime(2026, 7, 15, 6, 42),
    )

    task = asyncio.run(
        build_live_daily_active_task(
            _SequenceSession((), (reminder,), ()),
            SimpleNamespace(id="user-1", timezone="Asia/Shanghai"),
            SimpleNamespace(timezone="Asia/Shanghai"),
        )
    )

    assert task is not None
    assert task.workflow == WORKFLOW_DAILY_REPORT
    assert task.reply_candidate is True
    assert task.metadata["report_date"] == "2026-07-14"
