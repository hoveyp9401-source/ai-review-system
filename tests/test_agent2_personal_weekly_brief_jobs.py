from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.scheduler import runner
from app.scheduler.runner import (
    PERSONAL_WEEKLY_BRIEF_MODEL_CONCURRENCY,
    run_bounded_personal_weekly_brief_model_batch,
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


def test_weekly_batch_is_not_wired_into_daily_chat_request_paths() -> None:
    webhook = Path("app/api/webhook.py").read_text(encoding="utf-8")
    stream = Path("app/stream_runner.py").read_text(encoding="utf-8")

    assert "run_personal_weekly_brief_generation_job" not in webhook
    assert "run_personal_weekly_brief_generation_job" not in stream
    assert "Agent2PersonalWeeklyBriefModelPipeline" not in webhook
    assert "Agent2PersonalWeeklyBriefModelPipeline" not in stream


@pytest.mark.asyncio
async def test_model_batch_has_fixed_concurrency_and_isolates_one_user_failure() -> None:
    active = 0
    maximum_active = 0
    completed: list[int] = []

    async def worker(value: int) -> int:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await asyncio.sleep(0.01)
            if value == 17:
                raise RuntimeError("one redacted user failed")
            completed.append(value)
            return value
        finally:
            active -= 1

    results = await run_bounded_personal_weekly_brief_model_batch(
        tuple(range(74)),
        worker=worker,
    )

    assert PERSONAL_WEEKLY_BRIEF_MODEL_CONCURRENCY == 4
    assert maximum_active == 4
    assert len(results) == 74
    assert isinstance(results[17], RuntimeError)
    assert len(completed) == 73
