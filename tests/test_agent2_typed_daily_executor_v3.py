from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy.dialects import postgresql

from app.agent2.typed_daily_commands import TypedDailyCommand
from app.agent2.admission_hashes import compute_admission_claim_hashes
from app.agent2.business.contracts import BusinessCommandError
from app.agent2.typed_daily_executor import (
    TypedDailyExecutionContext,
    build_typed_daily_snapshot,
    execute_typed_agent2_daily_commands as _execute_typed_agent2_daily_commands,
)


async def execute_typed_agent2_daily_commands(*args, **kwargs):
    kwargs.setdefault("execution_authority", "authenticated_admin_command")
    return await _execute_typed_agent2_daily_commands(*args, **kwargs)


class _ReceiptSession:
    def __init__(self, receipts=()):
        self.statements = []
        self.receipts = tuple(receipts)

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(rowcount=1)

    async def scalars(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: list(self.receipts))

    async def flush(self):
        return None


class _AuthoritativeTicketStore:
    def __init__(self, *, reject_code: str = ""):
        self.reject_code = reject_code
        self.requests = []
        self.consumptions = []
        self.replays = []

    async def acquire(self, request):
        self.requests.append(request)
        if self.reject_code:
            raise BusinessCommandError(
                self.reject_code,
                "admission",
                "authoritative Ticket rejected the command",
            )
        return SimpleNamespace(ticket_id=request.admission_ticket["ticket_id"])

    async def consume(self, lease, *, receipt, consumed_at):
        self.consumptions.append((lease, receipt, consumed_at))

    async def validate_consumed_execution_replay(self, request, *, receipt_id):
        self.replays.append((request, receipt_id))


def _admitted_daily_append(*, user, report_date, version=0, item="完成合同审核", suffix="1"):
    report_id = uuid5(
        NAMESPACE_URL,
        f"agent2-daily-report:{user.id}:{report_date.isoformat()}",
    )
    action_id = f"daily-action-{suffix}"
    operation = "capture_daily_event"
    object_ref = {
        "object_type": "daily_report",
        "stable_id": str(report_id),
        "version": version,
    }
    authority_scope = {
        "field": "today_work",
        "raw_fact": item,
    }
    segment_hash = "a" * 64
    fact_hash, command_hash = compute_admission_claim_hashes(
        action_id=action_id,
        operation=operation,
        segment_text_sha256=segment_hash,
        domain="report",
        object_ref=object_ref,
        authority_scope=authority_scope,
        allowed_changed_fields=("section", "items"),
    )
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    ticket = {
        "ticket_id": str(uuid5(NAMESPACE_URL, f"daily-ticket-{suffix}")),
        "tenant_id": "sandbox-agent2-phase2-20260711",
        "user_id": str(user.id),
        "conversation_id": "daily-conversation",
        "source_message_id": "daily-message",
        "action_id": action_id,
        "segment_id": f"daily-segment-{suffix}",
        "segment_text_sha256": segment_hash,
        "domain": "report",
        "operation": operation,
        "object_ref": object_ref,
        "expected_conversation_state_version": 3,
        "authority_scope": authority_scope,
        "allowed_changed_fields": ["section", "items"],
        "fact_claims_sha256": fact_hash,
        "authorized_command_sha256": command_hash,
        "ticket_status": "issued",
        "executor_revalidation_required": True,
        "proves_business_write": False,
        "contract_version": "agent2.domain_admission.v1",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    return TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, f"daily-command-{suffix}"),
        decision_id=uuid5(NAMESPACE_URL, f"daily-decision-{suffix}"),
        sub_decision_id=uuid5(NAMESPACE_URL, f"daily-subdecision-{suffix}"),
        command_type="append_item",
        report_id=report_id,
        report_version=version,
        target_item_ids=(),
        patch={"field": "today_work", "items": [item]},
        idempotency_key=f"daily-message:daily:{suffix}",
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id=action_id,
        admission_operation=operation,
    )


def test_typed_executor_persists_structured_command_without_natural_language(monkeypatch):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "typed-executor-user"),
        team_id=uuid5(NAMESPACE_URL, "typed-executor-team"),
        timezone="Asia/Shanghai",
    )
    report_date = date(2026, 7, 10)
    snapshot = build_typed_daily_snapshot(user=user, report_date=report_date, report=None)
    decision_id = uuid5(NAMESPACE_URL, "typed-executor-decision")
    sub_decision_id = uuid5(NAMESPACE_URL, "typed-executor-sub-decision")
    command = TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "typed-executor-command"),
        decision_id=decision_id,
        sub_decision_id=sub_decision_id,
        command_type="append_item",
        report_id=snapshot.report_id,
        report_version=0,
        target_item_ids=(),
        patch={"field": "today_work", "items": ["完成合同审核"]},
        idempotency_key="typed-executor-message:append",
    )
    captured: dict = {}
    session = _ReceiptSession()

    async def fake_lock(session, user_id, target_date):
        captured["lock"] = (user_id, target_date)

    async def fake_get_report(session, user_id, target_date):
        return None

    async def fake_upsert(session, **kwargs):
        captured["upsert"] = kwargs
        return SimpleNamespace(
            id=kwargs["report_id_override"],
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
        )

    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", fake_upsert)

    result = asyncio.run(
        execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=(command,),
            execution_context=TypedDailyExecutionContext(
                report_date=report_date,
                source="agent2_v3_test",
                source_text_hash="a" * 64,
                tenant_id="sandbox-alpha",
            ),
            settings=SimpleNamespace(timezone="Asia/Shanghai"),
        )
    )

    assert result.report_saved is True
    assert result.today_work == ["完成合同审核"]
    assert result.today_work[0] in result.message
    assert report_date.isoformat() in result.message
    assert captured["upsert"]["raw_input"] == ""
    assert captured["upsert"]["llm_payload"]["source_text_hash"] == "a" * 64
    assert captured["upsert"]["llm_payload"]["typed_commands"] == [command.as_dict()]
    assert "完成合同审核" not in captured["upsert"]["raw_input"]
    assert result.command_results[0]["receipt_id"]
    receipt_sql = str(session.statements[-1].compile(dialect=postgresql.dialect()))
    assert "agent2_daily_command_receipts" in receipt_sql


def test_completed_content_edit_preserves_auto_submission_metadata(monkeypatch):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "completed-owner-edit-user"),
        team_id=uuid5(NAMESPACE_URL, "completed-owner-edit-team"),
        timezone="Asia/Shanghai",
    )
    report_date = date(2026, 8, 11)
    report_id = uuid5(NAMESPACE_URL, "completed-owner-edit-report")
    submitted_at = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
    existing = SimpleNamespace(
        id=report_id,
        user_id=user.id,
        team_id=user.team_id,
        report_date=report_date,
        status="completed",
        today_work=["review contract"],
        problems=[],
        tomorrow_plan=["follow up"],
        section_status={
            "_agent2_report_version": 4,
            "_draft_item_ids": {
                "today_work": ["tw-1"],
                "problems": [],
                "tomorrow_plan": ["tp-1"],
            },
        },
        confirmation_type="auto_submitted_timeout",
        confirmed_by_user=False,
        pending_confirmation_at=None,
        auto_submit_at=None,
        submitted_at=submitted_at,
    )
    command = TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "completed-owner-edit-command"),
        decision_id=uuid5(NAMESPACE_URL, "completed-owner-edit-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, "completed-owner-edit-subdecision"),
        command_type="edit_item",
        report_id=report_id,
        report_version=4,
        target_item_ids=("tw-1",),
        patch={"replacement": "review final contract"},
        idempotency_key="completed-owner-edit:daily:0",
    )
    captured: dict = {}
    session = _ReceiptSession()

    async def fake_lock(*args, **kwargs):
        return None

    async def fake_get_report(*args, **kwargs):
        return existing

    async def fake_upsert(session, **kwargs):
        captured.update(kwargs)
        existing.today_work = list(kwargs["today_work"])
        existing.problems = list(kwargs["problems"])
        existing.tomorrow_plan = list(kwargs["tomorrow_plan"])
        return existing

    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", fake_upsert)

    result = asyncio.run(
        execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=(command,),
            execution_context=TypedDailyExecutionContext(
                report_date=report_date,
                source="agent2_v3_test",
                source_text_hash="f" * 64,
                tenant_id="tenant-test",
                allow_completed_content_mutation=True,
            ),
            settings=SimpleNamespace(timezone="Asia/Shanghai"),
        )
    )

    assert result.status == "completed"
    assert result.today_work == ["review final contract"]
    assert captured["preserve_existing_submission"] is True
    assert captured["confirmation_type"] == "auto_submitted_timeout"
    assert captured["confirmed_by_user"] is False
    assert captured["pending_confirmation_at"] is None
    assert captured["auto_submit_at"] is None


def test_typed_executor_rejects_multi_target_single_item_command_before_db_access():
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "typed-executor-shape-user"),
        team_id=uuid5(NAMESPACE_URL, "typed-executor-shape-team"),
        timezone="Asia/Shanghai",
    )
    report_date = date(2026, 7, 10)
    command = TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "typed-executor-shape-command"),
        decision_id=uuid5(NAMESPACE_URL, "typed-executor-shape-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, "typed-executor-shape-sub-decision"),
        command_type="delete_item",
        report_id=uuid5(NAMESPACE_URL, "typed-executor-shape-report"),
        report_version=3,
        target_item_ids=("item-1", "item-2"),
        patch={},
        idempotency_key="typed-executor-shape:delete",
    )

    with pytest.raises(ValueError, match="exactly one target"):
        asyncio.run(
            execute_typed_agent2_daily_commands(
                object(),
                user=user,
                commands=(command,),
                execution_context=TypedDailyExecutionContext(
                    report_date=report_date,
                    source="agent2_v3_test",
                    source_text_hash="b" * 64,
                ),
                settings=SimpleNamespace(timezone="Asia/Shanghai"),
            )
        )


def test_semantic_typed_daily_executor_rejects_mutation_without_ticket_before_db():
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "typed-executor-authority-user"),
        team_id=uuid5(NAMESPACE_URL, "typed-executor-authority-team"),
        timezone="Asia/Shanghai",
    )
    command = TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "typed-executor-authority-command"),
        decision_id=uuid5(NAMESPACE_URL, "typed-executor-authority-decision"),
        sub_decision_id=uuid5(
            NAMESPACE_URL, "typed-executor-authority-sub-decision"
        ),
        command_type="append_item",
        report_id=uuid5(NAMESPACE_URL, "typed-executor-authority-report"),
        report_version=0,
        target_item_ids=(),
        patch={"field": "today_work", "items": ["完成合同审核"]},
        idempotency_key="typed-executor-authority:append",
    )

    with pytest.raises(BusinessCommandError) as exc_info:
        asyncio.run(
            execute_typed_agent2_daily_commands(
                object(),
                user=user,
                commands=(command,),
                execution_context=TypedDailyExecutionContext(
                    report_date=date(2026, 7, 14),
                    source="agent2_v3_test",
                    source_text_hash="c" * 64,
                    tenant_id="tenant-test",
                ),
                settings=SimpleNamespace(timezone="Asia/Shanghai"),
                execution_authority="semantic_ticket",
            )
        )

    assert exc_info.value.code == "admission_ticket_required"


def test_typed_query_uses_command_report_date_and_never_upserts(monkeypatch):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "typed-query-user"),
        team_id=uuid5(NAMESPACE_URL, "typed-query-team"),
        timezone="Asia/Shanghai",
    )
    requested_date = date(2026, 7, 9)
    report = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "typed-query-report"),
        user_id=user.id,
        report_date=requested_date,
        status="completed",
        today_work=["historical work"],
        problems=["historical risk"],
        tomorrow_plan=["historical plan"],
        section_status={"_agent2_report_version": 4},
    )
    command = TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "typed-query-command"),
        decision_id=uuid5(NAMESPACE_URL, "typed-query-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, "typed-query-sub-decision"),
        command_type="query_report",
        report_id=report.id,
        report_version=4,
        target_item_ids=(),
        patch={"report_date": requested_date.isoformat()},
        idempotency_key="typed-query-message:query",
    )
    captured: dict = {"upsert": 0, "lock": 0}
    session = _ReceiptSession()

    async def fake_get_report(session, user_id, target_date):
        captured["get_report"] = (user_id, target_date)
        return report

    async def forbidden_upsert(*args, **kwargs):
        captured["upsert"] += 1
        raise AssertionError("read-only query must not upsert")

    async def forbidden_lock(*args, **kwargs):
        captured["lock"] += 1
        raise AssertionError("read-only query must not take a write lock")

    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", forbidden_upsert)
    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", forbidden_lock)

    result = asyncio.run(
        execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=(command,),
            execution_context=TypedDailyExecutionContext(
                report_date=date(2026, 7, 10),
                source="agent2_v3_test",
                source_text_hash="c" * 64,
                tenant_id="sandbox-alpha",
            ),
            settings=SimpleNamespace(timezone="Asia/Shanghai"),
        )
    )

    assert captured["get_report"] == (user.id, requested_date)
    assert captured["upsert"] == 0
    assert captured["lock"] == 0
    assert result.read_only is True
    assert result.report_saved is False
    assert result.report_date == requested_date
    assert result.today_work == ["historical work"]
    assert "historical work" in result.message
    assert "historical risk" in result.message
    assert "historical plan" in result.message
    assert result.command_results[0]["actual_write"] is False
    assert result.command_results[0]["receipt_id"]
    receipt_sql = str(session.statements[-1].compile(dialect=postgresql.dialect()))
    assert "agent2_daily_command_receipts" in receipt_sql


def test_typed_duplicate_persists_receipt_without_upserting_report(monkeypatch):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "typed-duplicate-user"),
        team_id=uuid5(NAMESPACE_URL, "typed-duplicate-team"),
        timezone="Asia/Shanghai",
    )
    report_date = date(2026, 7, 10)
    report = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "typed-duplicate-report"),
        user_id=user.id,
        report_date=report_date,
        status="collecting",
        today_work=["完成合同审核"],
        problems=[],
        tomorrow_plan=[],
        section_status={"_agent2_report_version": 1},
    )
    command = TypedDailyCommand(
        command_id=uuid5(NAMESPACE_URL, "typed-duplicate-command"),
        decision_id=uuid5(NAMESPACE_URL, "typed-duplicate-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, "typed-duplicate-sub-decision"),
        command_type="append_item",
        report_id=report.id,
        report_version=1,
        target_item_ids=(),
        patch={"field": "today_work", "items": ["完成合同审核"]},
        idempotency_key="typed-duplicate-message:daily:1",
    )
    session = _ReceiptSession(
        receipts=(
            SimpleNamespace(
                user_id=user.id,
                command_id=command.command_id,
                decision_id=command.decision_id,
                sub_decision_id=command.sub_decision_id,
                command_type=command.command_type,
                idempotency_key="typed-duplicate-message:daily:1",
                status="executed",
                reason_code="exact_target",
            ),
        )
    )
    captured = {"upsert": 0}

    async def fake_lock(*args, **kwargs):
        return None

    async def fake_get_report(*args, **kwargs):
        return report

    async def forbidden_upsert(*args, **kwargs):
        captured["upsert"] += 1
        raise AssertionError("duplicate replay must not upsert the report")

    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", forbidden_upsert)

    result = asyncio.run(
        execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=(command,),
            execution_context=TypedDailyExecutionContext(
                report_date=report_date,
                source="agent2_v3_test",
                source_text_hash="d" * 64,
                tenant_id="sandbox-alpha",
            ),
            settings=SimpleNamespace(timezone="Asia/Shanghai"),
        )
    )

    assert captured["upsert"] == 0
    assert result.report_saved is False
    assert result.command_results[0]["validation_status"] == "duplicate"
    assert result.command_results[0]["status"] == "duplicate"
    assert result.command_results[0]["receipt_id"]


def test_daily_executor_acquires_and_consumes_one_authoritative_ticket_per_mutation(
    monkeypatch,
):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "daily-authority-user"),
        team_id=uuid5(NAMESPACE_URL, "daily-authority-team"),
        timezone="Asia/Shanghai",
    )
    report_date = date(2026, 7, 14)
    commands = (
        _admitted_daily_append(
            user=user,
            report_date=report_date,
            version=0,
            item="完成合同审核",
            suffix="one",
        ),
        _admitted_daily_append(
            user=user,
            report_date=report_date,
            version=1,
            item="提交补充材料",
            suffix="two",
        ),
    )
    session = _ReceiptSession()
    store = _AuthoritativeTicketStore()
    captured = {"upserts": 0}

    async def fake_lock(*args, **kwargs):
        return None

    async def fake_get_report(*args, **kwargs):
        return None

    async def fake_upsert(session, **kwargs):
        captured["upserts"] += 1
        return SimpleNamespace(
            id=kwargs["report_id_override"],
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
        )

    async def fake_sync(*args, **kwargs):
        return SimpleNamespace(status="completed", reason_code="")

    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", fake_upsert)
    monkeypatch.setattr(
        "app.agent2.typed_daily_executor.sync_focused_report_task",
        fake_sync,
    )

    result = asyncio.run(
        execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=commands,
            execution_context=TypedDailyExecutionContext(
                report_date=report_date,
                source="agent2_v3_test",
                source_text_hash="e" * 64,
                tenant_id="sandbox-agent2-phase2-20260711",
                conversation_id="daily-conversation",
                source_turn_id="daily-message",
                execution_started_at=datetime(2026, 7, 14, 9, 0, 1, tzinfo=UTC),
                conversation_state_version=3,
            ),
            settings=SimpleNamespace(timezone="Asia/Shanghai"),
            admission_ticket_store=store,
            execution_authority="semantic_ticket",
        )
    )

    assert captured["upserts"] == 1
    assert result.today_work == ["完成合同审核", "提交补充材料"]
    assert [request.object_ref["version"] for request in store.requests] == [0, 1]
    assert [request.receipt_kind for request in store.requests] == [
        "daily_report",
        "daily_report",
    ]
    assert len(store.consumptions) == 2
    assert [item[1].status for item in store.consumptions] == ["executed", "executed"]
    assert [item[1].actual_write for item in store.consumptions] == [True, True]
    assert len({item[1].receipt_id for item in store.consumptions}) == 2


@pytest.mark.parametrize(
    "reason_code",
    [
        "admission_ticket_not_found",
        "admission_ticket_expired",
        "admission_ticket_inactive",
        "admission_ticket_authority_mismatch",
    ],
)
def test_daily_executor_authoritative_ticket_rejection_is_zero_write(
    monkeypatch,
    reason_code,
):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, f"daily-authority-reject-user:{reason_code}"),
        team_id=uuid5(NAMESPACE_URL, "daily-authority-reject-team"),
        timezone="Asia/Shanghai",
    )
    report_date = date(2026, 7, 14)
    command = _admitted_daily_append(
        user=user,
        report_date=report_date,
        item="完成合同审核",
        suffix=reason_code,
    )
    store = _AuthoritativeTicketStore(reject_code=reason_code)
    session = _ReceiptSession()
    captured = {"upserts": 0}

    async def fake_lock(*args, **kwargs):
        return None

    async def fake_get_report(*args, **kwargs):
        return None

    async def forbidden_upsert(*args, **kwargs):
        captured["upserts"] += 1
        raise AssertionError("authoritative Ticket rejection must be zero-write")

    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", forbidden_upsert)

    result = asyncio.run(
        execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=(command,),
            execution_context=TypedDailyExecutionContext(
                report_date=report_date,
                source="agent2_v3_test",
                source_text_hash="f" * 64,
                tenant_id="sandbox-agent2-phase2-20260711",
                conversation_id="daily-conversation",
                source_turn_id="daily-message",
                execution_started_at=datetime(2026, 7, 14, 9, 0, 1, tzinfo=UTC),
                conversation_state_version=3,
            ),
            settings=SimpleNamespace(timezone="Asia/Shanghai"),
            admission_ticket_store=store,
            execution_authority="semantic_ticket",
        )
    )

    assert captured["upserts"] == 0
    assert result.report_saved is False
    assert result.command_results[0]["status"] == "blocked"
    assert result.command_results[0]["reason"] == reason_code
    assert result.command_results[0]["actual_write"] is False
    assert len(store.requests) == 1
    assert store.consumptions == []


def test_daily_duplicate_returns_prior_receipt_before_ticket_reacquisition(monkeypatch):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "daily-authority-duplicate-user"),
        team_id=uuid5(NAMESPACE_URL, "daily-authority-duplicate-team"),
        timezone="Asia/Shanghai",
    )
    report_date = date(2026, 7, 14)
    command = _admitted_daily_append(
        user=user,
        report_date=report_date,
        version=1,
        item="完成合同审核",
        suffix="duplicate",
    )
    command = replace(
        command,
        admission_ticket={
            **command.admission_ticket,
            "issued_at": datetime(2026, 7, 14, 8, 0, tzinfo=UTC).isoformat(),
            "expires_at": datetime(2026, 7, 14, 8, 5, tzinfo=UTC).isoformat(),
        },
    )
    report = SimpleNamespace(
        id=command.report_id,
        user_id=user.id,
        report_date=report_date,
        status="collecting",
        today_work=["完成合同审核"],
        problems=[],
        tomorrow_plan=[],
        section_status={"_agent2_report_version": 1},
    )
    prior_receipt = SimpleNamespace(
        receipt_id=uuid5(NAMESPACE_URL, "daily-authority-duplicate-receipt"),
        user_id=user.id,
        command_id=command.command_id,
        decision_id=command.decision_id,
        sub_decision_id=command.sub_decision_id,
        command_type=command.command_type,
        idempotency_key=command.idempotency_key,
        status="executed",
        reason_code="exact_target",
        audit_json={
            "_execution_scope": {
                "conversation_id": "daily-conversation",
                "source_channel": "agent2_v3_test",
                "source_turn_id": "daily-message",
                "scope_sha256": hashlib.sha256(
                    "\x1f".join(
                        (
                            "sandbox-agent2-phase2-20260711",
                            "daily-conversation",
                            "agent2_v3_test",
                            "daily-message",
                        )
                    ).encode("utf-8")
                ).hexdigest(),
            }
        },
    )
    session = _ReceiptSession(receipts=(prior_receipt,))
    store = _AuthoritativeTicketStore()

    async def fake_lock(*args, **kwargs):
        return None

    async def fake_get_report(*args, **kwargs):
        return report

    async def forbidden_upsert(*args, **kwargs):
        raise AssertionError("duplicate command must not upsert")

    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", forbidden_upsert)

    result = asyncio.run(
        execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=(command,),
            execution_context=TypedDailyExecutionContext(
                report_date=report_date,
                source="agent2_v3_test",
                source_text_hash="1" * 64,
                tenant_id="sandbox-agent2-phase2-20260711",
                conversation_id="daily-conversation",
                source_turn_id="daily-message",
                execution_started_at=datetime(2026, 7, 14, 9, 0, 1, tzinfo=UTC),
                conversation_state_version=3,
            ),
            settings=SimpleNamespace(timezone="Asia/Shanghai"),
            admission_ticket_store=store,
            execution_authority="semantic_ticket",
        )
    )

    assert result.command_results[0]["status"] == "duplicate"
    assert result.command_results[0]["actual_write"] is False
    assert store.requests == []
    assert store.consumptions == []
    assert len(store.replays) == 1


def test_daily_duplicate_idempotency_collision_fails_closed(monkeypatch):
    user = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "daily-idempotency-collision-user"),
        team_id=uuid5(NAMESPACE_URL, "daily-idempotency-collision-team"),
        timezone="Asia/Shanghai",
    )
    report_date = date(2026, 7, 14)
    command = _admitted_daily_append(
        user=user,
        report_date=report_date,
        version=1,
        suffix="collision",
    )
    report = SimpleNamespace(
        id=command.report_id,
        user_id=user.id,
        report_date=report_date,
        status="collecting",
        today_work=["完成合同审核"],
        problems=[],
        tomorrow_plan=[],
        section_status={"_agent2_report_version": 1},
    )
    conflicting_receipt = SimpleNamespace(
        user_id=user.id,
        command_id=uuid5(NAMESPACE_URL, "different-command"),
        decision_id=command.decision_id,
        sub_decision_id=command.sub_decision_id,
        command_type=command.command_type,
        idempotency_key=command.idempotency_key,
        status="executed",
        reason_code="exact_target",
    )
    session = _ReceiptSession(receipts=(conflicting_receipt,))
    store = _AuthoritativeTicketStore()

    async def fake_lock(*args, **kwargs):
        return None

    async def fake_get_report(*args, **kwargs):
        return report

    async def forbidden_upsert(*args, **kwargs):
        raise AssertionError("idempotency collision must be zero-write")

    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", forbidden_upsert)

    result = asyncio.run(
        execute_typed_agent2_daily_commands(
            session,
            user=user,
            commands=(command,),
            execution_context=TypedDailyExecutionContext(
                report_date=report_date,
                source="agent2_v3_test",
                source_text_hash="2" * 64,
                tenant_id="sandbox-agent2-phase2-20260711",
                conversation_id="daily-conversation",
                source_turn_id="daily-message",
                execution_started_at=datetime(2026, 7, 14, 9, 0, 1, tzinfo=UTC),
                conversation_state_version=3,
            ),
            settings=SimpleNamespace(timezone="Asia/Shanghai"),
            admission_ticket_store=store,
            execution_authority="semantic_ticket",
        )
    )

    assert result.command_results[0]["reason"] == "idempotency_collision"
    assert result.command_results[0]["actual_write"] is False
    assert store.requests == []
    assert store.consumptions == []
