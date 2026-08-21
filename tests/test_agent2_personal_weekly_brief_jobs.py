from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.scheduler import runner
from app.scheduler.runner import (
    run_personal_weekly_brief_dispatch_job,
    run_personal_weekly_brief_generation_job,
    run_personal_weekly_brief_reconcile_job,
)


NOW = datetime(2026, 8, 22, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _settings(**changes):
    values = {
        "timezone": "Asia/Shanghai",
        "agent2_personal_weekly_brief_enabled": False,
        "agent2_personal_weekly_brief_send_enabled": False,
        "agent2_personal_weekly_brief_tenant_id": "",
    }
    values.update(changes)
    return SimpleNamespace(**values)


class _ForbiddenSessions:
    def __call__(self):
        raise AssertionError("closed switch must not open the database")


@pytest.mark.asyncio
async def test_all_jobs_fail_closed_before_database_or_transport_access(monkeypatch) -> None:
    monkeypatch.setattr(runner, "AsyncSessionLocal", _ForbiddenSessions())

    generated = await run_personal_weekly_brief_generation_job(
        _settings(),
        llm_client=object(),
        robot=object(),
        now=NOW,
    )
    dispatched = await run_personal_weekly_brief_dispatch_job(
        _settings(),
        robot=object(),
        now=NOW,
    )
    reconciled = await run_personal_weekly_brief_reconcile_job(
        _settings(),
        robot=object(),
        now=NOW,
    )

    assert generated == {
        "staged": 0,
        "generated": 0,
        "generation_failed": 0,
        "dispatched": 0,
    }
    assert dispatched == 0
    assert reconciled == 0


@pytest.mark.asyncio
async def test_generation_refuses_non_saturday_even_if_directly_called() -> None:
    friday = datetime(2026, 8, 21, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    with pytest.raises(
        ValueError,
        match="personal_weekly_brief_generation_requires_saturday",
    ):
        await run_personal_weekly_brief_generation_job(
            _settings(
                agent2_personal_weekly_brief_enabled=True,
                agent2_personal_weekly_brief_tenant_id="tenant-a",
            ),
            llm_client=object(),
            robot=object(),
            now=friday,
        )


def test_main_scheduler_wires_generation_and_reconciliation_registration() -> None:
    source = Path("app/scheduler/runner.py").read_text(encoding="utf-8")

    assert "register_personal_weekly_brief_jobs(" in source
    assert "run_personal_weekly_brief_generation_job(" in source
    assert "run_personal_weekly_brief_reconcile_job(" in source
