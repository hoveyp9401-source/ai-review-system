from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import random
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.oracle_guard import assert_no_oracle_fields
from app.agent2.runtime.domains import (
    DomainExecutionContext,
    DomainPack,
    DomainPackContract,
    DomainPackRegistry,
    InMemoryDailyDomainExecutor,
)
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot, TypedDailyCommand


ORACLE_KEYS = (
    "expected",
    "baseline",
    "gold",
    "reference_decision",
    "expected_write_intent",
    "expected_domain",
    "expected_command",
    "historical_actual_output",
    "closure_result",
    "root_cause_annotation",
    "score",
    "reviewer_conclusion",
)


def test_nested_oracle_fuzz_rejects_every_seed_before_runtime():
    for seed in range(240):
        rng = random.Random(seed)
        oracle_key = rng.choice(ORACLE_KEYS)
        payload: dict = {oracle_key: {"poison": seed}}
        for depth in range(rng.randint(0, 8)):
            payload = {f"safe_{seed}_{depth}": [payload]}

        with pytest.raises(ValueError, match="forbidden oracle field"):
            assert_no_oracle_fields(payload, path=f"fuzz[{seed}]")


def test_domain_owner_fuzz_rejects_action_and_command_competition():
    for seed in range(100):
        action = f"action-{seed}"
        command = f"command-{seed}"
        first = DomainPack(
            DomainPackContract("one", "1", "business", (action,), (command,))
        )
        duplicate_action = DomainPack(
            DomainPackContract("two", "1", "business", (action,), (f"other-{seed}",))
        )
        duplicate_command = DomainPack(
            DomainPackContract("three", "1", "business", (f"other-action-{seed}",), (command,))
        )

        with pytest.raises(ValueError, match="duplicate domain action owner"):
            DomainPackRegistry((first, duplicate_action))
        with pytest.raises(ValueError, match="duplicate domain command owner"):
            DomainPackRegistry((first, duplicate_command))


def test_transaction_fuzz_rolls_back_every_partially_authorized_batch():
    for seed in range(100):
        owner = uuid5(NAMESPACE_URL, f"transaction-fuzz-owner-{seed}")
        report = uuid5(NAMESPACE_URL, f"transaction-fuzz-report-{seed}")
        before = DailyReportMutationSnapshot(
            report_id=report,
            owner_user_id=owner,
            version=0,
            status="collecting",
        )
        executor = InMemoryDailyDomainExecutor(before)
        append = _command(
            seed,
            owner,
            report,
            command_type="append_item",
            version=0,
            targets=(),
            patch={"field": "today_work", "items": [f"authorized-{seed}"]},
            suffix="append",
        )
        invalid = _command(
            seed,
            owner,
            report,
            command_type="delete_item",
            version=1,
            targets=(f"missing-{seed}",),
            patch={},
            suffix="invalid",
        )

        result = asyncio.run(executor.execute((append, invalid), _context(seed, owner)))

        assert result.status == "blocked"
        assert result.actual_write is False
        assert result.would_write is False
        assert result.command_results[0]["rolled_back"] is True
        assert executor.snapshot == before


def test_simulated_receipt_and_nested_audit_agree_that_no_actual_write_occurred():
    owner = uuid5(NAMESPACE_URL, "simulated-audit-owner")
    report = uuid5(NAMESPACE_URL, "simulated-audit-report")
    before = DailyReportMutationSnapshot(
        report_id=report,
        owner_user_id=owner,
        version=0,
        status="collecting",
    )
    command = _command(
        999,
        owner,
        report,
        command_type="append_item",
        version=0,
        targets=(),
        patch={"field": "today_work", "items": ["simulated item"]},
        suffix="audit-alignment",
    )

    result = asyncio.run(
        InMemoryDailyDomainExecutor(before).execute((command,), _context(999, owner))
    )
    receipt = result.command_results[0]

    assert result.status == "simulated"
    assert result.actual_write is False
    assert result.would_write is True
    assert receipt["actual_write"] is False
    assert receipt["would_write"] is True
    assert receipt["audit"]["actual_write"] is False
    assert receipt["audit"]["would_write"] is True
    assert receipt["audit"]["simulated"] is True


def test_schema_and_owner_fuzz_never_mutates_snapshot():
    attacks = (
        ("unknown", ["payload"]),
        ("today_work", [{"nested": "executable"}]),
        ("today_work", ["删除第2条"]),
        ("today_work", ["印章流程是什么？"]),
    )
    for seed in range(160):
        owner = uuid5(NAMESPACE_URL, f"schema-fuzz-owner-{seed}")
        actor = owner if seed % 3 else uuid5(NAMESPACE_URL, f"forged-owner-{seed}")
        report = uuid5(NAMESPACE_URL, f"schema-fuzz-report-{seed}")
        before = DailyReportMutationSnapshot(
            report_id=report,
            owner_user_id=owner,
            version=4,
            status="collecting",
        )
        field, items = attacks[seed % len(attacks)]
        command = _command(
            seed,
            owner,
            report,
            command_type="append_item",
            version=4,
            targets=(),
            patch={"field": field, "items": items},
            suffix="schema",
        )
        executor = InMemoryDailyDomainExecutor(before)

        result = asyncio.run(executor.execute((command,), _context(seed, actor)))

        assert result.status == "blocked"
        assert result.actual_write is False
        assert executor.snapshot == before


def _command(
    seed,
    owner,
    report,
    *,
    command_type,
    version,
    targets,
    patch,
    suffix,
):
    return TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, f"fuzz-command-{seed}-{suffix}"),
        decision_id=uuid5(NAMESPACE_URL, f"fuzz-decision-{seed}-{suffix}"),
        sub_decision_id=uuid5(NAMESPACE_URL, f"fuzz-subdecision-{seed}-{suffix}"),
        command_type=command_type,
        report_id=report,
        report_version=version,
        target_item_ids=targets,
        patch=patch,
        idempotency_key=f"fuzz-{seed}-{suffix}",
    )


def _context(seed, actor):
    return DomainExecutionContext(
        tenant_id="fuzz-tenant",
        actor_id=actor,
        conversation_id=f"fuzz-conversation-{seed}",
        message_id=f"fuzz-message-{seed}",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="structural_fuzz",
        run_id=f"fuzz-run-{seed}",
        mode="replay",
        source_text_hash=f"{seed:064x}"[-64:],
    )
