from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agent2.weekly_plan_models import WeeklyPlanRosterMember
from app.scheduler import runner
from app.scheduler.runner import (
    register_weekly_plan_jobs,
    run_weekly_plan_collection_open_job,
    run_weekly_plan_history_suggestion_refresh_job,
    run_weekly_plan_reminder_maintenance_job,
)


class _Scheduler:
    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []

    def add_job(self, func, trigger, **kwargs) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})


def _settings(**overrides):
    values = {
        "timezone": "Asia/Shanghai",
        "agent2_weekly_plan_enabled": False,
        "agent2_weekly_plan_write_enabled": False,
        "agent2_weekly_plan_send_enabled": False,
        "agent2_weekly_plan_tenant_allowlist": "",
        "agent2_weekly_plan_user_allowlist": "",
        "agent2_weekly_plan_send_user_allowlist": "",
        "weekly_plan_collection_open_hour": 16,
        "weekly_plan_collection_open_minute": 0,
        "weekly_plan_reminder_hour": 15,
        "weekly_plan_reminder_minute": 0,
        "weekly_plan_snapshot_hour": 9,
        "weekly_plan_snapshot_minute": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _noop():
    return None


def test_weekly_plan_jobs_are_not_registered_when_feature_is_closed() -> None:
    scheduler = _Scheduler()

    registered = register_weekly_plan_jobs(
        scheduler,
        settings=_settings(),
        open_job=_noop,
        reminder_job=_noop,
        reminder_reconcile_job=_noop,
        snapshot_job=_noop,
    )

    assert registered == ()
    assert scheduler.jobs == []


def test_weekly_plan_jobs_fail_closed_without_exact_canary_scope() -> None:
    for settings in (
        _settings(agent2_weekly_plan_enabled=True),
        _settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
            agent2_weekly_plan_tenant_allowlist="tenant-a",
        ),
        _settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
            agent2_weekly_plan_tenant_allowlist="tenant-a",
            agent2_weekly_plan_user_allowlist="测试用户甲",
        ),
        _settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
            agent2_weekly_plan_tenant_allowlist="tenant-a,tenant-b",
            agent2_weekly_plan_user_allowlist="stable-user-id",
        ),
    ):
        scheduler = _Scheduler()
        assert register_weekly_plan_jobs(
            scheduler,
            settings=settings,
            open_job=_noop,
            reminder_job=_noop,
            reminder_reconcile_job=_noop,
            snapshot_job=_noop,
        ) == ()
        assert scheduler.jobs == []


def test_single_canary_registers_open_reminder_and_snapshot_without_sender() -> None:
    scheduler = _Scheduler()

    registered = register_weekly_plan_jobs(
        scheduler,
        settings=_settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
            agent2_weekly_plan_send_enabled=True,
            agent2_weekly_plan_tenant_allowlist="tenant-a",
            agent2_weekly_plan_user_allowlist="stable-user-id",
            agent2_weekly_plan_send_user_allowlist="stable-user-id",
        ),
        open_job=_noop,
        reminder_job=_noop,
        reminder_reconcile_job=_noop,
        snapshot_job=_noop,
    )

    assert registered == (
        "agent2_weekly_plan_collection_open",
        "agent2_weekly_plan_reminder_enqueue",
        "agent2_weekly_plan_reminder_reconcile",
        "agent2_weekly_plan_monday_snapshot",
    )
    assert [job["id"] for job in scheduler.jobs] == list(registered)
    assert [str(job["trigger"]) for job in scheduler.jobs] == [
        "cron[day_of_week='fri', hour='16', minute='0']",
        "cron[day_of_week='sun', hour='15', minute='0']",
        "interval[0:05:00]",
        "cron[day_of_week='mon', hour='9', minute='0']",
    ]
    assert all(job["max_instances"] == 1 for job in scheduler.jobs)
    assert all(job["coalesce"] is True for job in scheduler.jobs)
    assert all("send" not in job["id"] for job in scheduler.jobs)


def test_reminder_enqueue_is_not_registered_when_send_scope_is_closed() -> None:
    scheduler = _Scheduler()

    registered = register_weekly_plan_jobs(
        scheduler,
        settings=_settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
            agent2_weekly_plan_send_enabled=False,
            agent2_weekly_plan_tenant_allowlist="tenant-a",
            agent2_weekly_plan_user_allowlist="stable-user-id",
            agent2_weekly_plan_send_user_allowlist="",
        ),
        open_job=_noop,
        reminder_job=_noop,
        reminder_reconcile_job=_noop,
        snapshot_job=_noop,
    )

    assert registered == (
        "agent2_weekly_plan_collection_open",
        "agent2_weekly_plan_monday_snapshot",
    )
    assert [job["id"] for job in scheduler.jobs] == list(registered)


def test_two_user_canary_registers_open_and_snapshot_but_no_send_jobs() -> None:
    scheduler = _Scheduler()

    registered = register_weekly_plan_jobs(
        scheduler,
        settings=_settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
            agent2_weekly_plan_send_enabled=False,
            agent2_weekly_plan_tenant_allowlist="tenant-a",
            agent2_weekly_plan_user_allowlist="user-b,user-a",
            agent2_weekly_plan_send_user_allowlist="",
        ),
        open_job=_noop,
        reminder_job=_noop,
        reminder_reconcile_job=_noop,
        snapshot_job=_noop,
    )

    assert registered == (
        "agent2_weekly_plan_collection_open",
        "agent2_weekly_plan_monday_snapshot",
    )
    assert [job["id"] for job in scheduler.jobs] == list(registered)


def test_weekly_plan_scheduler_rejects_three_user_canary_scope() -> None:
    scheduler = _Scheduler()

    registered = register_weekly_plan_jobs(
        scheduler,
        settings=_settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
            agent2_weekly_plan_tenant_allowlist="tenant-a",
            agent2_weekly_plan_user_allowlist="user-a,user-b,user-c",
        ),
        open_job=_noop,
        reminder_job=_noop,
        reminder_reconcile_job=_noop,
        snapshot_job=_noop,
    )

    assert registered == ()
    assert scheduler.jobs == []


@pytest.mark.asyncio
async def test_two_user_open_job_freezes_both_configured_members_in_stable_order(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _Session:
        async def commit(self) -> None:
            captured["committed"] = True

    class _Sessions:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return None

        def __call__(self):
            return self

    async def _member(_session, *, tenant_id, user_id):
        return WeeklyPlanRosterMember(
            user_id=user_id,
            display_name=user_id,
            department_id="department-a",
            team_id="team-a",
        )

    class _Orchestrator:
        def __init__(self, _store) -> None:
            pass

        async def open_collection(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(runner, "AsyncSessionLocal", _Sessions())
    monkeypatch.setattr(runner, "_load_weekly_plan_canary_member", _member)
    monkeypatch.setattr(runner, "SqlWeeklyPlanStore", lambda _session: object())
    monkeypatch.setattr(
        runner,
        "SqlWeeklyPlanCollectionOrchestrator",
        _Orchestrator,
    )

    await run_weekly_plan_collection_open_job(
        _settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
            agent2_weekly_plan_tenant_allowlist="tenant-a",
            agent2_weekly_plan_user_allowlist="user-b,user-a",
        ),
        now=datetime(2026, 8, 14, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert [member.user_id for member in captured["source_roster"]] == [
        "user-a",
        "user-b",
    ]
    assert captured["canary_user_ids"] == frozenset({"user-a", "user-b"})
    assert captured["committed"] is True


@pytest.mark.asyncio
async def test_history_refresh_is_fail_closed_when_weekly_plan_is_disabled(
    monkeypatch,
) -> None:
    entered = False

    class _Sessions:
        def __call__(self):
            nonlocal entered
            entered = True
            raise AssertionError("disabled refresh must not open the database")

    monkeypatch.setattr(runner, "AsyncSessionLocal", _Sessions())

    await run_weekly_plan_history_suggestion_refresh_job(
        _settings(),
        llm_client=object(),
        now=datetime(2026, 8, 14, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert entered is False


@pytest.mark.asyncio
async def test_recurring_reminder_maintenance_recovers_dispatch_before_reconcile(
    monkeypatch,
) -> None:
    calls: list[tuple[str, datetime]] = []
    observed_at = datetime(2026, 8, 16, 15, 20, tzinfo=ZoneInfo("Asia/Shanghai"))

    async def _dispatch(settings, *, robot, now):
        calls.append(("dispatch", now))

    async def _reconcile(settings, *, robot, now):
        calls.append(("reconcile", now))

    monkeypatch.setattr(runner, "run_weekly_plan_reminder_dispatch_job", _dispatch)
    monkeypatch.setattr(runner, "run_weekly_plan_reminder_reconcile_job", _reconcile)

    await run_weekly_plan_reminder_maintenance_job(
        _settings(),
        robot=object(),
        now=observed_at,
    )

    assert calls == [("dispatch", observed_at), ("reconcile", observed_at)]
