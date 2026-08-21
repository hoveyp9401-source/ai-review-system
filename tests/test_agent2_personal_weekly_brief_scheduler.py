from __future__ import annotations

from types import SimpleNamespace

from app.config import Settings
from app.scheduler.runner import register_personal_weekly_brief_jobs


class _Scheduler:
    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []

    def add_job(self, func, trigger, **kwargs) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})


def _settings(**changes):
    values = {
        "timezone": "Asia/Shanghai",
        "agent2_personal_weekly_brief_enabled": False,
        "agent2_personal_weekly_brief_send_enabled": False,
        "agent2_personal_weekly_brief_tenant_id": "",
        "personal_weekly_brief_hour": 9,
        "personal_weekly_brief_minute": 0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _noop():
    return None


def test_personal_weekly_brief_switches_are_closed_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.agent2_personal_weekly_brief_enabled is False
    assert settings.agent2_personal_weekly_brief_send_enabled is False
    assert settings.agent2_personal_weekly_brief_tenant_id == ""


def test_closed_feature_registers_no_job() -> None:
    scheduler = _Scheduler()

    registered = register_personal_weekly_brief_jobs(
        scheduler,
        settings=_settings(),
        generation_job=_noop,
        reconciliation_job=_noop,
    )

    assert registered == ()
    assert scheduler.jobs == []


def test_generation_is_scheduled_for_saturday_0900_without_send_worker() -> None:
    scheduler = _Scheduler()

    registered = register_personal_weekly_brief_jobs(
        scheduler,
        settings=_settings(
            agent2_personal_weekly_brief_enabled=True,
            agent2_personal_weekly_brief_tenant_id="tenant-a",
        ),
        generation_job=_noop,
        reconciliation_job=_noop,
    )

    assert registered == ("agent2_personal_weekly_brief_generate",)
    assert str(scheduler.jobs[0]["trigger"]) == (
        "cron[day_of_week='sat', hour='9', minute='0']"
    )
    assert scheduler.jobs[0]["max_instances"] == 1
    assert scheduler.jobs[0]["coalesce"] is True


def test_schedule_is_fixed_to_shanghai_0900_even_if_unrelated_settings_differ() -> None:
    scheduler = _Scheduler()

    register_personal_weekly_brief_jobs(
        scheduler,
        settings=_settings(
            timezone="UTC",
            personal_weekly_brief_hour=18,
            personal_weekly_brief_minute=45,
            agent2_personal_weekly_brief_enabled=True,
            agent2_personal_weekly_brief_tenant_id="tenant-a",
        ),
        generation_job=_noop,
        reconciliation_job=_noop,
    )

    trigger = scheduler.jobs[0]["trigger"]
    assert str(trigger) == "cron[day_of_week='sat', hour='9', minute='0']"
    assert str(trigger.timezone) == "Asia/Shanghai"


def test_send_worker_is_registered_only_with_separate_send_switch() -> None:
    scheduler = _Scheduler()

    registered = register_personal_weekly_brief_jobs(
        scheduler,
        settings=_settings(
            agent2_personal_weekly_brief_enabled=True,
            agent2_personal_weekly_brief_send_enabled=True,
            agent2_personal_weekly_brief_tenant_id="tenant-a",
        ),
        generation_job=_noop,
        reconciliation_job=_noop,
    )

    assert registered == (
        "agent2_personal_weekly_brief_generate",
        "agent2_personal_weekly_brief_reconcile",
    )
    assert "interval[0:05:00]" == str(scheduler.jobs[1]["trigger"])


def test_invalid_or_multi_tenant_scope_registers_no_job() -> None:
    for tenant_id in ("", "tenant-a,tenant-b", " tenant-a", "测试租户"):
        scheduler = _Scheduler()

        assert register_personal_weekly_brief_jobs(
            scheduler,
            settings=_settings(
                agent2_personal_weekly_brief_enabled=True,
                agent2_personal_weekly_brief_tenant_id=tenant_id,
            ),
            generation_job=_noop,
            reconciliation_job=_noop,
        ) == ()
        assert scheduler.jobs == []
