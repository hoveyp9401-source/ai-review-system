from datetime import datetime, timezone
import json

from app.agent_core import DailySnapshot, InMemoryTaskLedger, TaskLedgerEntry, process_agent_turn
from app.agent_core.operation_ledger import (
    InMemoryOperationLedgerStore,
    build_persistable_operation_records,
)
from app.workflows.intake import IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT


def _envelope(text: str) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="tester",
        dingtalk_user_id="dt-1",
        source="unit_test",
        raw_text=text,
        message_id="msg-1",
        conversation_id="conv-1",
    )


def test_operation_ledger_records_daily_and_sidecar_operations_without_raw_text():
    result = process_agent_turn(
        _envelope("\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee"),
        daily_snapshot=DailySnapshot(),
    )

    records = build_persistable_operation_records(
        result,
        created_at=datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc),
    )

    assert [(record.workflow, record.operation, record.write_policy) for record in records] == [
        ("daily_report", "fill", "dry_run"),
        ("travel_coordination", "upsert_travel_plan", "sandbox"),
    ]
    assert records[0].before_snapshot["today_work"] == []
    assert records[0].after_snapshot["tomorrow_plan"] == ["\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee"]
    assert records[0].auth_chain["authorization_status"] == "allowed"
    assert records[1].auth_chain["authorization_status"] == "allowed"
    assert records[1].result["changed"] is False

    payload = json.dumps([record.as_storage_dict() for record in records], ensure_ascii=False)
    assert "\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee" in payload
    assert "raw_text" not in payload
    assert "sender_name" not in payload


def test_operation_ledger_records_denied_authorization_for_downstream_overreach():
    class NoEffectDailyRouter:
        def plan(self, envelope: IncomingMessageEnvelope):
            from app.workflows.intake import RoutingPlan, SafetyDecision

            return RoutingPlan(
                primary_workflow="daily_report",
                matched_workflows=["daily_report"],
                effects=[],
                safety_decision=SafetyDecision(commit_policy="partial_allowed"),
                confidence=0.9,
                reason="test route has no effects",
            )

    result = process_agent_turn(
        _envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"),
        daily_snapshot=DailySnapshot(),
        router=NoEffectDailyRouter(),
    )

    records = build_persistable_operation_records(result)

    assert len(records) == 1
    assert records[0].authorization_status == "denied"
    assert records[0].write_policy == "blocked"
    assert "operation_not_authorized" in records[0].safety_flags
    assert records[0].result["changed"] is False


def test_operation_ledger_store_is_idempotent_and_queryable_by_turn_and_task():
    ledger = InMemoryTaskLedger(
        [
            TaskLedgerEntry(
                task_id="daily-1",
                user_id="user-1",
                workflow=WORKFLOW_DAILY_REPORT,
                status="collecting",
                awaited_reply="daily_tomorrow_plan",
            )
        ]
    )
    result = process_agent_turn(
        _envelope("\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee"),
        daily_snapshot=DailySnapshot(),
        task_ledger=ledger,
    )
    records = build_persistable_operation_records(result)
    store = InMemoryOperationLedgerStore()

    store.upsert_many(records)
    store.upsert_many(records)

    assert len(store.list_by_turn(result.turn_id)) == len(records)
    assert [record.task_id for record in store.list_by_task("daily-1")] == ["daily-1", "daily-1"]
    assert {record.workflow for record in store.list_by_task("daily-1")} == {
        "daily_report",
        "travel_coordination",
    }
    assert store.list_by_turn("missing") == []


def test_operation_ledger_storage_dict_is_json_serializable():
    result = process_agent_turn(
        _envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"),
        daily_snapshot=DailySnapshot(),
    )

    records = build_persistable_operation_records(result)
    payload = [record.as_storage_dict() for record in records]

    json.dumps(payload, ensure_ascii=False)
    assert payload[0]["turn_id"] == result.turn_id
    assert payload[0]["record_schema"] == "agent_core_operation_ledger.v1"


def test_process_agent_turn_can_persist_operation_records_when_store_is_supplied():
    store = InMemoryOperationLedgerStore()

    result = process_agent_turn(
        _envelope("\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838"),
        daily_snapshot=DailySnapshot(),
        operation_ledger_store=store,
    )

    assert result.persisted_operation_records
    assert store.list_by_turn(result.turn_id) == result.persisted_operation_records
    assert result.production_write is False
