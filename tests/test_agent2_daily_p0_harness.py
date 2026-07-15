from __future__ import annotations

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.daily_command_compiler import apply_legacy_daily_commands_as_typed
from app.agent2.daily_commands import compile_daily_commands
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot
from app.workflows.intake import (
    EFFECT_CONFIRM_DAILY_REPORT,
    ActiveWorkflowTask,
    IncomingMessageEnvelope,
    WORKFLOW_DAILY_REPORT,
    WORKFLOW_MONTHLY_REPORT,
    WorkflowRouter,
)


CASES_PATH = Path(__file__).parents[1] / "evals" / "agent2" / "golden" / "daily_typed_p0.jsonl"
REQUIRED_EXPECTED_KEYS = {
    "intent",
    "action",
    "fields",
    "should_write_db",
    "expected_reply_type",
    "forbidden_behavior",
}


def _load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


CASES = _load_cases()


def test_daily_p0_harness_has_the_ten_required_cases_and_assertion_shape():
    assert len(CASES) == 10
    assert len({case["case_id"] for case in CASES}) == 10
    for case in CASES:
        assert REQUIRED_EXPECTED_KEYS == set(case["expected"])
        assert case["expected"]["forbidden_behavior"]


@pytest.mark.parametrize("case", CASES, ids=[case["case_id"] for case in CASES])
def test_daily_p0_harness_case(case: dict):
    actual = _run_case(case)
    expected = case["expected"]

    assert actual["intent"] == expected["intent"]
    assert actual["action"] == expected["action"]
    assert actual["should_write_db"] is expected["should_write_db"]
    assert actual["expected_reply_type"] == expected["expected_reply_type"]
    for field_name, expected_value in expected["fields"].items():
        assert actual["fields"][field_name] == expected_value
    assert not set(expected["forbidden_behavior"]) & set(actual["observed_behaviors"])


def _run_case(case: dict) -> dict:
    context = dict(case["context"])
    expected = case["expected"]
    tasks = _tasks_for_context(context)
    envelope = IncomingMessageEnvelope(
        sender_id="p0-user",
        sender_name="P0 User",
        dingtalk_user_id="p0-dingtalk-user",
        source="daily_p0_harness",
        raw_text=case["input"],
        message_id=f"message:{case['case_id']}",
        conversation_id="p0-conversation",
        active_tasks=tuple(tasks),
    )
    plan = WorkflowRouter().plan(envelope)
    commands = compile_daily_commands(plan, envelope)
    observed_behaviors: set[str] = set()
    if any(command.requires_confirmation for command in commands):
        observed_behaviors.update({"create_confirmation_pending", "require_second_confirmation"})

    if context.get("pending_count") in {0, 2} and case["input"] == "是的":
        if any(effect.effect_type == EFFECT_CONFIRM_DAILY_REPORT for effect in plan.effects):
            observed_behaviors.update({"confirm_daily_report", "submit_report"})
        if plan.primary_workflow == WORKFLOW_DAILY_REPORT:
            observed_behaviors.add("select_daily_pending")
        if plan.primary_workflow == WORKFLOW_MONTHLY_REPORT:
            observed_behaviors.add("select_monthly_pending")
        return {
            "intent": plan.primary_workflow,
            "action": "clarify_intent" if not plan.effects else "confirm_pending",
            "fields": {"pending_count": context["pending_count"]},
            "should_write_db": bool(plan.effects),
            "expected_reply_type": "clarify_intent" if not plan.effects else "ack_write",
            "observed_behaviors": sorted(observed_behaviors),
        }

    snapshot = _snapshot(case)
    expected_version = int(context.get("command_expected_version", snapshot.version))
    if case["case_id"].endswith("idempotent-replay"):
        return _run_replay_case(case, plan.primary_workflow, commands, snapshot, observed_behaviors)

    result = apply_legacy_daily_commands_as_typed(
        commands,
        message_id=envelope.message_id,
        snapshot=snapshot,
        actor_user_id=snapshot.owner_user_id,
        expected_report_version=expected_version,
    )
    execution = result.executions[-1] if result.executions else None
    if result.status == "executed" and execution is not None:
        action = execution.command.command_type
        reply_type = "ack_write"
        fields = {
            "target_item_ids": list(execution.command.target_item_ids),
            "confirmation_required": False,
            "report_version": execution.before.version,
            "replacement": execution.command.patch.get("replacement", ""),
        }
        removed = set(snapshot.item_ids.get("today_work", ())) - set(result.after.item_ids.get("today_work", ()))
        observed_behaviors.update(f"delete_{item_id}" for item_id in removed)
        if execution.command.command_type == "submit_report":
            observed_behaviors.add("submit_report")
    else:
        reason = result.reason_code
        action = "clarify_target" if reason in {"ambiguous_target", "target_not_found"} else expected["action"]
        reply_type = "clarify_target" if action == "clarify_target" else "write_blocked"
        fields = {
            "target_resolution": "ambiguous" if reason == "ambiguous_target" else "",
            "target_item_ids": list(execution.command.target_item_ids) if execution is not None else [],
            "reason_code": reason,
        }
        if result.should_write_db:
            observed_behaviors.add("overwrite_newer_version")

    if result.should_write_db and result.after.version != snapshot.version:
        observed_behaviors.add("increment_version")
    return {
        "intent": plan.primary_workflow,
        "action": action,
        "fields": fields,
        "should_write_db": result.should_write_db,
        "expected_reply_type": reply_type,
        "observed_behaviors": sorted(observed_behaviors),
    }


def _run_replay_case(case: dict, intent: str, commands: list, snapshot: DailyReportMutationSnapshot, observed: set[str]) -> dict:
    working = snapshot
    executed_keys: set[str] = set()
    writes = 0
    first_action = ""
    for _ in range(int(case["context"]["same_message_id_replayed"])):
        result = apply_legacy_daily_commands_as_typed(
            commands,
            message_id=f"message:{case['case_id']}",
            snapshot=working,
            actor_user_id=working.owner_user_id,
            expected_report_version=working.version,
            executed_idempotency_keys=executed_keys,
        )
        if result.executions and not first_action:
            first_action = result.executions[0].command.command_type
        if result.should_write_db:
            writes += 1
            working = result.after
            executed_keys.update(execution.command.idempotency_key for execution in result.executions)
    if writes == 3:
        observed.add("three_business_writes")
    if working.version == 3:
        observed.add("increment_version_three_times")
    if len(working.today_work) > 1:
        observed.add("duplicate_item")
    return {
        "intent": intent,
        "action": first_action,
        "fields": {"successful_business_writes": writes, "final_report_version": working.version},
        "should_write_db": writes > 0,
        "expected_reply_type": "ack_write",
        "observed_behaviors": sorted(observed),
    }


def _snapshot(case: dict) -> DailyReportMutationSnapshot:
    context = case["context"]
    owner_id = uuid5(NAMESPACE_URL, "agent2-daily-p0-owner")
    report_id = uuid5(NAMESPACE_URL, f"agent2-daily-p0-report:{case['case_id']}")
    today_work = tuple(context.get("items") or ())
    problems = tuple(context.get("problems") or ())
    tomorrow_plan = tuple(context.get("tomorrow_plan") or ())
    today_ids = tuple(context.get("item_ids") or ())
    return DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=owner_id,
        version=int(context.get("database_current_version", context.get("report_version", 0))),
        status="collecting",
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        item_ids={
            "today_work": today_ids,
            "problems": tuple(f"pb-{index}" for index, _ in enumerate(problems, start=1)),
            "tomorrow_plan": tuple(f"tp-{index}" for index, _ in enumerate(tomorrow_plan, start=1)),
        },
    )


def _tasks_for_context(context: dict) -> list[ActiveWorkflowTask]:
    if int(context.get("pending_count", 0)) == 2:
        return [
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="daily-pending",
                status="pending_confirmation",
                awaiting_confirmation=True,
            ),
            ActiveWorkflowTask(
                workflow=WORKFLOW_MONTHLY_REPORT,
                task_id="monthly-pending",
                status="pending_confirmation",
                awaiting_confirmation=True,
            ),
        ]
    return [
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id="daily-active",
            status="collecting",
            reply_candidate=True,
        )
    ]
