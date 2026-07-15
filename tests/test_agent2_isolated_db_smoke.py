from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from app.agent2.runtime.domains import DomainExecutionContext, InMemoryDailyDomainExecutor
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot, TypedDailyCommand


OWNER = uuid5(NAMESPACE_URL, "isolated-db-smoke-owner")
REPORT = uuid5(NAMESPACE_URL, "isolated-db-smoke-report")


def _snapshot() -> DailyReportMutationSnapshot:
    return DailyReportMutationSnapshot(
        report_id=REPORT,
        owner_user_id=OWNER,
        version=0,
        status="collecting",
    )


def _context(actor_id: UUID = OWNER) -> DomainExecutionContext:
    return DomainExecutionContext(
        tenant_id="isolated-test-tenant",
        actor_id=actor_id,
        conversation_id="isolated-conversation",
        message_id="isolated-message",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="isolated_transaction_simulator",
        run_id="isolated-run",
        mode="replay",
        source_text_hash="a" * 64,
    )


def _append(*, version: int = 0, key: str = "append-1", field: str = "today_work"):
    return TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, f"isolated-command:{key}"),
        decision_id=uuid5(NAMESPACE_URL, f"isolated-decision:{key}"),
        sub_decision_id=uuid5(NAMESPACE_URL, f"isolated-subdecision:{key}"),
        command_type="append_item",
        report_id=REPORT,
        report_version=version,
        target_item_ids=(),
        patch={"field": field, "items": ["完成合同审核"]},
        idempotency_key=key,
    )


def _delete_missing(*, version: int = 1):
    return TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "isolated-delete-missing"),
        decision_id=uuid5(NAMESPACE_URL, "isolated-delete-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, "isolated-delete-subdecision"),
        command_type="delete_item",
        report_id=REPORT,
        report_version=version,
        target_item_ids=("missing-item",),
        patch={},
        idempotency_key="delete-missing",
    )


def test_isolated_smoke_typed_command_has_simulated_effect_and_exact_receipt():
    executor = InMemoryDailyDomainExecutor(_snapshot())

    result = asyncio.run(executor.execute((_append(),), _context()))
    receipt = result.command_results[0]

    assert result.status == "simulated"
    assert result.actual_write is False
    assert result.would_write is True
    assert executor.snapshot.today_work == ("完成合同审核",)
    assert receipt["typed_command"] == _append().as_dict()
    assert receipt["validation_status"] == "authorized"
    assert receipt["simulated"] is True


def test_isolated_smoke_executor_interface_rejects_raw_text():
    executor = InMemoryDailyDomainExecutor(_snapshot())

    with pytest.raises(TypeError, match="TypedDailyCommand only"):
        asyncio.run(executor.execute(("今天完成合同审核",), _context()))


@pytest.mark.parametrize(
    ("command", "context", "reason"),
    [
        (_append(field="unknown_field"), _context(), "forbidden_payload"),
        (_append(version=9), _context(), "version_conflict"),
        (_append(), _context(uuid5(NAMESPACE_URL, "wrong-owner")), "forbidden_payload"),
    ],
)
def test_isolated_smoke_invalid_write_is_blocked_without_effect(command, context, reason):
    executor = InMemoryDailyDomainExecutor(_snapshot())

    result = asyncio.run(executor.execute((command,), context))

    assert result.status == "blocked"
    assert result.actual_write is False
    assert result.would_write is False
    assert result.command_results[0]["reason"] == reason
    assert executor.snapshot == _snapshot()


def test_isolated_smoke_batch_rolls_back_prior_authorized_command():
    executor = InMemoryDailyDomainExecutor(_snapshot())

    result = asyncio.run(executor.execute((_append(), _delete_missing()), _context()))

    assert result.status == "blocked"
    assert result.actual_write is False
    assert result.would_write is False
    assert result.command_results[0]["validation_status"] == "authorized"
    assert result.command_results[0]["rolled_back"] is True
    assert result.command_results[1]["reason"] == "target_not_found"
    assert executor.snapshot == _snapshot()


def test_isolated_smoke_duplicate_request_is_idempotent():
    executor = InMemoryDailyDomainExecutor(_snapshot())
    command = _append(key="stable-duplicate")

    first = asyncio.run(executor.execute((command,), _context()))
    after_first = executor.snapshot
    second = asyncio.run(executor.execute((command,), _context()))

    assert first.would_write is True
    assert second.status == "duplicate"
    assert second.command_results[0]["validation_status"] == "duplicate"
    assert second.command_results[0]["reason"] == "duplicate_message"
    assert executor.snapshot == after_first
    assert executor.snapshot.today_work == ("完成合同审核",)
