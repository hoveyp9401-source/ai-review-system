from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeStateTransition,
)
from app.agent2.selection_pending import SelectionPendingAudit
from app.agent2.workflow_audit import persist_selection_settlement_audit


RUNTIME = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "agent2"
    / "cognitive_runtime_v3.py"
)


def test_receipt_backed_selection_settlement_audit_is_persisted() -> None:
    """The success audit returned by settle_selection cannot be discarded."""

    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "finalize_cognitive_core_v3_execution"
    )
    settlement_names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "settle_selection"
        ):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        settlement_names.update(
            target.id for target in targets if isinstance(target, ast.Name)
        )

    assert settlement_names, (
        "finalizer discards SelectionSettlement by reading only `.pending_after`; "
        "its receipt-linked audit can never be persisted"
    )
    persisted_audit_calls = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        has_settlement_audit = any(
            isinstance(child, ast.Attribute)
            and child.attr == "audit"
            and isinstance(child.value, ast.Name)
            and child.value.id in settlement_names
            for child in ast.walk(call)
        )
        passes_same_session = any(
            isinstance(child, ast.Name) and child.id == "session"
            for child in ast.walk(call)
        )
        if has_settlement_audit and passes_same_session:
            persisted_audit_calls.append(call)

    assert persisted_audit_calls, (
        "receipt-backed SelectionSettlement.audit is not persisted on the same "
        "session/transaction as Pending consumption"
    )


@pytest.mark.asyncio
async def test_settlement_audit_persister_records_receipt_without_business_text() -> None:
    class Session:
        def __init__(self) -> None:
            self.events: list[object] = []
            self.flushed = False

        def add(self, event: object) -> None:
            self.events.append(event)

        async def flush(self) -> None:
            self.flushed = True

    session = Session()
    now = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)
    audit = SelectionPendingAudit(
        pending_id="pending-1",
        tenant_id="tenant-1",
        user_id="00000000-0000-0000-0000-000000000001",
        conversation_id="conversation-1",
        source_turn_id="message-answer",
        selected_candidate_id="case-2",
        receipt_ids=("receipt-1",),
        result="consumed",
        reason="receipt_succeeded",
        occurred_at=now,
    )
    outcome = OperationOutcome(
        domain="case_progress",
        operation="create",
        object_ref=OutcomeObjectRef("case_progress", "progress-1", "案件进展"),
        business_status="succeeded",
        message_status="not_applicable",
        changed_fields=("summary",),
        user_visible_snapshot={"content": "敏感案件正文"},
        blocking_reason="",
        receipt_refs=(
            OutcomeReceiptRef(
                "receipt-1",
                "database",
                "executed",
                True,
                external_message_id="SENSITIVE_PROVIDER_MESSAGE_ID",
            ),
        ),
        state_transition=OutcomeStateTransition("missing", "active"),
        actual_write=True,
        source_turn_id="message-answer",
        tenant_id="tenant-1",
        user_id=audit.user_id,
    )

    await persist_selection_settlement_audit(
        session=session,  # type: ignore[arg-type]
        audit=audit,
        outcome=outcome,
        business_context=SimpleNamespace(
            actor_user_id=audit.user_id,
            occurred_at=now,
        ),
    )

    assert session.flushed is True
    assert len(session.events) == 1
    event = session.events[0]
    assert event.backend_action == "agent2_selection_pending_settlement"
    assert event.llm_decision_json["receipt_refs"][0]["receipt_id"] == "receipt-1"
    assert event.message_text == ""
    assert "敏感案件正文" not in str(event.llm_decision_json)
    assert "敏感案件正文" not in str(event.after_snapshot_json)
    assert "SENSITIVE_PROVIDER_MESSAGE_ID" not in str(event.llm_decision_json)
    assert "SENSITIVE_PROVIDER_MESSAGE_ID" not in str(event.after_snapshot_json)
