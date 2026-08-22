from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.scheduler import runner
from app.scheduler.runner import (
    PERSONAL_WEEKLY_BRIEF_MODEL_CONCURRENCY,
    PERSONAL_WEEKLY_BRIEF_SEND_CONCURRENCY,
    recover_personal_weekly_brief_rows,
    run_bounded_personal_weekly_brief_model_batch,
    run_bounded_personal_weekly_brief_send_batch,
    run_streaming_personal_weekly_brief_batch,
    stage_personal_weekly_brief_target_batch,
    run_personal_weekly_brief_dispatch_job,
    run_personal_weekly_brief_generation_job,
    run_personal_weekly_brief_reconcile_job,
)
from app.agent2.personal_weekly_brief_store import PersonalWeeklyBriefRecord


NOW = datetime(2026, 8, 22, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _brief_record(index: int, *, status: str = "generated") -> PersonalWeeklyBriefRecord:
    return PersonalWeeklyBriefRecord(
        brief_id=f"00000000-0000-4000-8000-{index:012d}",
        tenant_id="tenant-a",
        owner_user_id=f"owner-{index}",
        conversation_id=f"conversation-{index}",
        week_start=date(2026, 8, 17),
        week_end=date(2026, 8, 21),
        snapshot_at=NOW,
        source_snapshot={"sources": []},
        source_fingerprint="a" * 64,
        content_json={"trace": {}},
        message_text="脱敏简报",
        llm_model="deepseek-v4-flash",
        status=status,
        idempotency_key=f"idempotency-{index}",
        created_at=NOW,
        updated_at=NOW,
    )


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
        llm_client=object(),
        robot=object(),
        now=NOW,
    )

    assert generated == {
        "staged": 0,
        "snapshot_failed": 0,
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
async def test_model_batch_has_fixed_concurrency_and_keeps_handled_owner_failure() -> None:
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
                return RuntimeError("one redacted user failed")
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


@pytest.mark.asyncio
async def test_model_batch_propagates_unexpected_database_failure() -> None:
    async def worker(value: int) -> int:
        if value == 3:
            raise SQLAlchemyError("isolated database unavailable")
        await asyncio.sleep(0.01)
        return value

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        await run_bounded_personal_weekly_brief_model_batch(
            tuple(range(12)),
            worker=worker,
        )


@pytest.mark.asyncio
async def test_model_batch_cancels_remaining_work_after_system_failure() -> None:
    started: list[int] = []
    completed: list[int] = []
    cancelled: list[int] = []

    async def worker(value: int) -> int:
        started.append(value)
        if value == 0:
            await asyncio.sleep(0.01)
            raise SQLAlchemyError("whole database unavailable")
        try:
            await asyncio.sleep(0.2)
            completed.append(value)
            return value
        except asyncio.CancelledError:
            cancelled.append(value)
            raise

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        await run_bounded_personal_weekly_brief_model_batch(
            tuple(range(74)),
            worker=worker,
        )

    await asyncio.sleep(0.02)
    assert {0, 1, 2, 3}.issubset(set(started))
    assert len(started) <= 5
    assert completed == []
    assert set(cancelled) == set(started) - {0}


@pytest.mark.asyncio
async def test_generation_start_database_failure_propagates_without_owner_failure_record(
    monkeypatch,
) -> None:
    failure_recorded = False

    class _Session:
        async def commit(self):
            raise AssertionError("generation start should fail before commit")

    class _Sessions:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return None

    class _Store:
        def __init__(self, _session):
            pass

        async def record_generation_started(self, **_kwargs):
            raise SQLAlchemyError("generation database unavailable")

        async def record_generation_failure(self, **_kwargs):
            nonlocal failure_recorded
            failure_recorded = True
            raise AssertionError("system failure must not become an owner failure")

    monkeypatch.setattr(runner, "AsyncSessionLocal", _Sessions())
    monkeypatch.setattr(runner, "SqlPersonalWeeklyBriefStore", _Store)

    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        await runner._generate_personal_weekly_brief_record(
            row=_brief_record(1, status="snapshot_ready"),
            target=SimpleNamespace(display_name="虚构用户甲"),
            tenant_id="tenant-a",
            model_pipeline=object(),
            generator=object(),
        )

    assert failure_recorded is False


@pytest.mark.asyncio
async def test_send_batch_propagates_overall_scope_failure_and_cancels_remaining() -> None:
    from app.agent2.personal_weekly_brief_scope import (
        PersonalWeeklyBriefOverallScopeError,
    )

    started: list[int] = []

    async def worker(value: int) -> int:
        started.append(value)
        if value == 1:
            raise PersonalWeeklyBriefOverallScopeError("formal scope changed")
        await asyncio.sleep(0.05)
        return value

    with pytest.raises(PersonalWeeklyBriefOverallScopeError, match="scope changed"):
        await run_bounded_personal_weekly_brief_send_batch(
            tuple(range(74)),
            worker=worker,
        )

    assert len(started) < 74


@pytest.mark.asyncio
async def test_snapshot_batch_isolates_one_owner_failure_and_continues_other_73() -> None:
    class _Savepoint:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _Session:
        def begin_nested(self):
            return _Savepoint()

    class _Service:
        def __init__(self) -> None:
            self.staged: list[int] = []
            self.failed: list[int] = []

        async def stage_target(self, *, target, **_kwargs):
            if target.index == 17:
                raise ValueError("redacted owner source exceeded limit")
            self.staged.append(target.index)
            return SimpleNamespace(status="snapshot_ready", owner_user_id=target.index)

        async def stage_failure(self, *, target, error_code, **_kwargs):
            self.failed.append(target.index)
            assert error_code == "snapshot_error:ValueError"
            return SimpleNamespace(status="generation_failed", owner_user_id=target.index)

    service = _Service()
    rows = await stage_personal_weekly_brief_target_batch(
        session=_Session(),
        targets=tuple(SimpleNamespace(index=index) for index in range(74)),
        snapshot_service=service,
        week_start=date(2026, 8, 17),
        snapshot_at=NOW,
    )

    assert len(rows) == 74
    assert service.failed == [17]
    assert len(service.staged) == 73
    assert sum(row.status == "generation_failed" for row in rows) == 1


@pytest.mark.asyncio
async def test_snapshot_batch_propagates_database_failure_and_stops() -> None:
    class _Savepoint:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _Session:
        def begin_nested(self):
            return _Savepoint()

    class _Service:
        async def stage_target(self, **_kwargs):
            raise SQLAlchemyError("isolated database failure")

        async def stage_failure(self, **_kwargs):
            raise AssertionError("database failure must not become an owner failure")

    with pytest.raises(SQLAlchemyError, match="isolated database failure"):
        await stage_personal_weekly_brief_target_batch(
            session=_Session(),
            targets=(SimpleNamespace(index=0), SimpleNamespace(index=1)),
            snapshot_service=_Service(),
            week_start=date(2026, 8, 17),
            snapshot_at=NOW,
        )


@pytest.mark.asyncio
async def test_streaming_batch_sends_first_owner_before_all_generation_finishes() -> None:
    generated_count = 0
    first_send_generated_count: int | None = None
    active_sends = 0
    maximum_sends = 0

    async def generate(index: int):
        nonlocal generated_count
        await asyncio.sleep(0.01 if index == 0 else 0.08)
        generated_count += 1
        return _brief_record(index)

    async def send(row: PersonalWeeklyBriefRecord):
        nonlocal first_send_generated_count, active_sends, maximum_sends
        if first_send_generated_count is None:
            first_send_generated_count = generated_count
        active_sends += 1
        maximum_sends = max(maximum_sends, active_sends)
        try:
            await asyncio.sleep(0.02)
            return row
        finally:
            active_sends -= 1

    generated, sent = await run_streaming_personal_weekly_brief_batch(
        tuple(range(8)),
        generation_worker=generate,
        send_worker=send,
    )

    assert len(generated) == 8
    assert len(sent) == 8
    assert first_send_generated_count is not None
    assert first_send_generated_count < 8
    assert maximum_sends <= PERSONAL_WEEKLY_BRIEF_SEND_CONCURRENCY == 2


@pytest.mark.asyncio
async def test_five_minute_recovery_handles_generation_failed_failed_and_claimed() -> None:
    generation_failed = _brief_record(1, status="generation_failed")
    failed = _brief_record(2, status="failed")
    stale_claimed = _brief_record(3, status="failed")
    calls: list[str] = []

    class _Store:
        async def fail_stale_claims(self, **_kwargs):
            calls.append("claimed_timeout")
            return (stale_claimed,)

        async def load_for_status(self, *, status, **_kwargs):
            return {
                "generation_failed": (generation_failed,),
                "failed": (failed,),
            }[status]

        async def requeue_generation_failure(self, **_kwargs):
            calls.append("generation_requeued")
            return _brief_record(1, status="snapshot_ready")

        async def requeue_recoverable_failure(self, **_kwargs):
            calls.append("delivery_requeued")
            return _brief_record(2, status="generated")

    result = await recover_personal_weekly_brief_rows(
        store=_Store(),
        tenant_id="tenant-a",
        now=NOW,
        send_enabled=True,
    )

    assert calls == [
        "claimed_timeout",
        "generation_requeued",
        "delivery_requeued",
    ]
    assert result["generation_requeued"][0].status == "snapshot_ready"
    assert result["delivery_requeued"][0].status == "generated"
    assert result["stale_claims_failed"][0].status == "failed"


@pytest.mark.asyncio
async def test_pre_send_latest_identity_change_blocks_only_that_owner(monkeypatch) -> None:
    row = _brief_record(1)
    observed: dict[str, object] = {}

    class _Session:
        async def commit(self):
            observed["committed"] = True

    class _Sessions:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return None

        def __call__(self):
            return self

    async def _revalidate(*_args, **_kwargs):
        return SimpleNamespace(
            valid_targets={},
            blocked_reasons={row.owner_user_id: "target_changed_after_snapshot"},
        )

    class _Store:
        def __init__(self, _session):
            pass

        async def record_pre_send_block(self, **kwargs):
            observed["blocked"] = kwargs
            return PersonalWeeklyBriefRecord(
                **{
                    **row.__dict__,
                    "status": "failed",
                    "last_error": "pre_send_scope:target_changed_after_snapshot",
                }
            )

    monkeypatch.setattr(runner, "AsyncSessionLocal", _Sessions())
    monkeypatch.setattr(
        runner,
        "load_personal_weekly_brief_target_revalidation",
        _revalidate,
    )
    monkeypatch.setattr(runner, "SqlPersonalWeeklyBriefStore", _Store)

    blocked = await runner._dispatch_personal_weekly_brief_record(
        _settings(
            agent2_personal_weekly_brief_enabled=True,
            agent2_personal_weekly_brief_send_enabled=True,
            agent2_personal_weekly_brief_tenant_id="tenant-a",
        ),
        robot=object(),
        row=row,
        frozen_targets=(),
        now=NOW,
    )

    assert blocked.status == "failed"
    assert observed["blocked"]["reason"] == "target_changed_after_snapshot"
    assert observed["committed"] is True


@pytest.mark.asyncio
async def test_pre_send_overall_roster_set_change_stops_before_send(monkeypatch) -> None:
    class _Sessions:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

        def __call__(self):
            return self

    async def _revalidate(*_args, **_kwargs):
        raise RuntimeError("personal weekly brief formal roster set changed after snapshot")

    monkeypatch.setattr(runner, "AsyncSessionLocal", _Sessions())
    monkeypatch.setattr(
        runner,
        "load_personal_weekly_brief_target_revalidation",
        _revalidate,
    )

    with pytest.raises(RuntimeError, match="formal roster set changed"):
        await runner._dispatch_personal_weekly_brief_record(
            _settings(
                agent2_personal_weekly_brief_enabled=True,
                agent2_personal_weekly_brief_send_enabled=True,
                agent2_personal_weekly_brief_tenant_id="tenant-a",
            ),
            robot=object(),
            row=_brief_record(1),
            frozen_targets=(),
            now=NOW,
        )
