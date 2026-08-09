from __future__ import annotations

from dataclasses import dataclass, field
import csv
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from app.agent2.daily_execution import (
    DailyCommandApplication,
    _attach_agent2_edit_memory,
    apply_commands_to_snapshot,
)
from app.agent2.contract_invariants import evaluate_cognitive_invariants
from app.agent2.cognitive_decision import with_execution_trace
from app.agent2.daily_shadow import evaluate_daily_shadow
from app.agent2.daily_state import DRAFT_ITEM_IDS_KEY
from app.agent2.dialogue_replay import DialogueCase, DialogueTurn
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT


REPORT_FIELDS = ("today_work", "problems", "tomorrow_plan")


@dataclass
class ReplayReportState:
    today_work: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    tomorrow_plan: list[str] = field(default_factory=list)
    status: str = "collecting"
    section_status: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "ReplayReportState":
        data = value if isinstance(value, dict) else {}
        return cls(
            today_work=_string_list(data.get("today_work")),
            problems=_string_list(data.get("problems")),
            tomorrow_plan=_string_list(data.get("tomorrow_plan")),
            status=str(data.get("status") or "collecting"),
            section_status=dict(data.get("section_status") or {}) if isinstance(data.get("section_status"), dict) else {},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "today_work": list(self.today_work),
            "problems": list(self.problems),
            "tomorrow_plan": list(self.tomorrow_plan),
            "status": self.status,
        }

    def has_active_daily_context(self) -> bool:
        return self.status != "completed" and any(getattr(self, field) for field in REPORT_FIELDS)

    def apply(self, application: DailyCommandApplication) -> None:
        self.today_work = list(application.today_work)
        self.problems = list(application.problems)
        self.tomorrow_plan = list(application.tomorrow_plan)
        self.status = application.status
        self.section_status = dict(self.section_status or {})
        self.section_status[DRAFT_ITEM_IDS_KEY] = dict(application.item_ids)
        _attach_agent2_edit_memory(self.section_status, application)


def replay_daily_execution_case(case: DialogueCase, *, mode: str = "protective_gate") -> dict[str, Any]:
    state = ReplayReportState.from_mapping((case.metadata or {}).get("initial_report"))
    previous_report = _previous_report(case)
    active_tasks = list(case.active_tasks)
    turns: list[dict[str, Any]] = []

    for index, turn in enumerate(case.turns, start=1):
        turn_tasks = _tasks_for_turn(active_tasks, turn, state, case.dialogue_id, index)
        envelope = IncomingMessageEnvelope(
            sender_id=case.sender_id,
            sender_name=case.sender_name,
            dingtalk_user_id=case.dingtalk_user_id,
            source=case.source,
            raw_text=turn.text,
            message_id=f"{case.dialogue_id}:{turn.turn_id}",
            conversation_id=case.conversation_id or case.dialogue_id,
            received_at=case.received_at,
            active_tasks=tuple(turn_tasks),
            recent_state={},
        )
        before = state.as_dict()
        evaluation = evaluate_daily_shadow(envelope, mode=mode)
        execution_status = "no_agent2_commands"
        application: DailyCommandApplication | None = None
        direct_write = False
        fallback_to_legacy = False
        blocked_by_gate = False
        block_reason = ""

        if evaluation.gate_decision.block_legacy_daily:
            execution_status = "blocked_by_gate"
            blocked_by_gate = True
            block_reason = evaluation.gate_decision.reason
        elif evaluation.commands:
            application = apply_commands_to_snapshot(
                today_work=list(state.today_work),
                problems=list(state.problems),
                tomorrow_plan=list(state.tomorrow_plan),
                status=state.status,
                commands=list(evaluation.commands),
                previous_report=previous_report,
                section_status=state.section_status,
            )
            if application.read_only:
                execution_status = "read_only"
            elif application.changed:
                execution_status = "agent2_direct_write"
                direct_write = True
                state.apply(application)
            else:
                execution_status = "no_change"

        after = state.as_dict()
        cognitive_decision = with_execution_trace(
            evaluation.cognitive_decision,
            actual_write=direct_write,
            execution_status=execution_status,
            block_reason=block_reason,
            fallback_used=fallback_to_legacy,
            execution_result={
                "command_actions": list(application.actions if application else []),
                "report_changed": bool(application.changed) if application else False,
                "read_only": bool(application.read_only) if application else False,
            },
        )
        contract_violations = evaluate_cognitive_invariants(cognitive_decision)
        turn_result = {
            "dialogue_id": case.dialogue_id,
            "turn_id": turn.turn_id,
            "turn_index": index,
            "text": turn.text,
            "active_tasks_before": [_task_observation(task) for task in turn_tasks],
            "report_before": before,
            "report_after": after,
            "primary_workflow": evaluation.plan.primary_workflow,
            "matched_workflows": list(evaluation.plan.matched_workflows),
            "gate": evaluation.gate_decision.as_observation(),
            "daily_commands": [command.as_dict() for command in evaluation.commands],
            "cognitive_decision": cognitive_decision.as_dict(),
            "assistant_reply": evaluation.assistant_reply.as_observation() if evaluation.assistant_reply else None,
            "command_actions": list(application.actions if application else []),
            "execution_status": execution_status,
            "direct_write": direct_write,
            "fallback_to_legacy": fallback_to_legacy,
            "blocked_by_gate": blocked_by_gate,
            "block_reason": block_reason,
            "contract_invariant_violations": [violation.as_dict() for violation in contract_violations],
            "raw_text_written": _raw_text_written(turn.text, after),
            "expected": dict(turn.expected),
        }
        turn_result["mismatches"] = _execution_mismatches(turn_result, turn.expected)
        turn_result["risk_flags"] = _risk_flags(turn_result)
        turns.append(turn_result)
        active_tasks = _active_tasks_after_turn(case, active_tasks, state, turn_result)

    return {
        "dialogue_id": case.dialogue_id,
        "source": case.source,
        "turn_count": len(turns),
        "passed": all(not turn["mismatches"] for turn in turns),
        "mismatch_count": sum(1 for turn in turns if turn["mismatches"]),
        "risk_count": sum(1 for turn in turns if turn["risk_flags"]),
        "final_report": state.as_dict(),
        "turns": turns,
    }


def replay_daily_execution_cases(cases: Iterable[DialogueCase], *, mode: str = "protective_gate") -> list[dict[str, Any]]:
    return [replay_daily_execution_case(case, mode=mode) for case in cases]


def summarize_daily_execution_results(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    dialogues = list(results)
    turns = [turn for dialogue in dialogues for turn in dialogue.get("turns") or []]
    return {
        "total_dialogues": len(dialogues),
        "total_turns": len(turns),
        "passed_dialogues": sum(1 for dialogue in dialogues if dialogue.get("passed")),
        "failed_dialogues": sum(1 for dialogue in dialogues if not dialogue.get("passed")),
        "mismatch_count": sum(int(dialogue.get("mismatch_count") or 0) for dialogue in dialogues),
        "risk_turn_count": sum(1 for turn in turns if turn.get("risk_flags")),
        "contract_invariant_violation_count": sum(
            len(turn.get("contract_invariant_violations") or []) for turn in turns
        ),
        "direct_write_count": sum(1 for turn in turns if turn.get("direct_write")),
        "fallback_to_legacy_count": sum(1 for turn in turns if turn.get("fallback_to_legacy")),
        "blocked_by_gate_count": sum(1 for turn in turns if turn.get("blocked_by_gate")),
        "raw_text_written_count": sum(1 for turn in turns if turn.get("raw_text_written")),
        "unexpected_direct_write_count": sum(
            1 for turn in turns if turn.get("direct_write") and turn.get("expected", {}).get("agent2_direct_write") is False
        ),
        "gray_ready": all(
            [
                sum(int(dialogue.get("mismatch_count") or 0) for dialogue in dialogues) == 0,
                not any(
                    turn.get("raw_text_written")
                    and (
                        turn.get("expected", {}).get("raw_text_written") is False
                        or turn.get("expected", {}).get("agent2_direct_write") is False
                    )
                    for turn in turns
                ),
                not any(turn.get("direct_write") and turn.get("expected", {}).get("agent2_direct_write") is False for turn in turns),
                not any(turn.get("contract_invariant_violations") for turn in turns),
            ]
        ),
    }


def write_daily_execution_reports(results: list[dict[str, Any]], output_dir: str | Path) -> dict[str, Any]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_daily_execution_results(results)
    _write_jsonl(out_dir / "daily_execution_results.jsonl", results)
    (out_dir / "daily_execution_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "daily_execution_summary.md").write_text(_summary_markdown(summary), encoding="utf-8")
    _write_risk_csv(out_dir / "daily_execution_risks.csv", results)
    return summary


def _tasks_for_turn(
    active_tasks: list[ActiveWorkflowTask],
    turn: DialogueTurn,
    state: ReplayReportState,
    dialogue_id: str,
    turn_index: int,
) -> list[ActiveWorkflowTask]:
    tasks = [*active_tasks, *turn.active_tasks]
    if state.has_active_daily_context() and not any(task.workflow == WORKFLOW_DAILY_REPORT for task in tasks):
        tasks.append(
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id=f"{dialogue_id}:daily:{turn_index}",
                status=state.status,
                reply_candidate=True,
                awaiting_confirmation=state.status == "pending_confirmation",
                reason="daily execution replay active report state",
            )
        )
    return _merge_tasks(tasks)


def _active_tasks_after_turn(
    case: DialogueCase,
    previous_tasks: list[ActiveWorkflowTask],
    state: ReplayReportState,
    turn_result: dict[str, Any],
) -> list[ActiveWorkflowTask]:
    non_daily = [task for task in previous_tasks if task.workflow != WORKFLOW_DAILY_REPORT]
    if not state.has_active_daily_context():
        return non_daily
    return [
        *non_daily,
        ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id=f"{case.dialogue_id}:daily",
            status=state.status,
            reply_candidate=True,
            awaiting_confirmation=state.status == "pending_confirmation",
            reason=f"daily execution replay carried after {turn_result.get('turn_id')}",
        ),
    ]


def _execution_mismatches(result: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    _expect_equal(mismatches, result, expected, "execution_status")
    if not _allowed_no_change_edit_attempt(result, expected):
        _expect_equal(mismatches, result, expected, "direct_write", expected_key="agent2_direct_write")
    _expect_equal(mismatches, result, expected, "fallback_to_legacy")
    _expect_equal(mismatches, result, expected, "blocked_by_gate")
    _expect_assistant_reply_type(mismatches, result, expected)
    if expected.get("raw_text_written") is not None and bool(result.get("raw_text_written")) is not bool(expected.get("raw_text_written")):
        mismatches.append(f"raw_text_written expected {expected.get('raw_text_written')} got {result.get('raw_text_written')}")
    for field in REPORT_FIELDS:
        key = f"report_{field}"
        if key in expected:
            actual = list((result.get("report_after") or {}).get(field) or [])
            wanted = _string_list(expected.get(key))
            if actual != wanted and _canonical_report_items(actual, field=field) != _canonical_report_items(wanted, field=field):
                mismatches.append(f"{key} expected {wanted!r} got {actual!r}")
    if "report_status" in expected:
        actual_status = str((result.get("report_after") or {}).get("status") or "")
        if actual_status != str(expected["report_status"]):
            mismatches.append(f"report_status expected {expected['report_status']!r} got {actual_status!r}")
    for field in REPORT_FIELDS:
        forbidden_key = f"forbidden_{field}_contains"
        forbidden_values = _string_list(expected.get(forbidden_key))
        if forbidden_values:
            actual_joined = "\n".join((result.get("report_after") or {}).get(field) or [])
            for value in forbidden_values:
                if value in actual_joined:
                    mismatches.append(f"{forbidden_key} contains {value!r}")
    return mismatches


def _allowed_no_change_edit_attempt(result: dict[str, Any], expected: dict[str, Any]) -> bool:
    if expected.get("agent2_direct_write") is not True:
        return False
    if result.get("direct_write") or result.get("execution_status") != "no_change":
        return False
    commands = result.get("daily_commands") or []
    if not commands:
        return False
    operations = {str(command.get("operation") or "") for command in commands if command.get("should_write")}
    return bool(operations) and operations.issubset({"edit", "clear", "revoke"})


def _expect_equal(
    mismatches: list[str],
    result: dict[str, Any],
    expected: dict[str, Any],
    actual_key: str,
    *,
    expected_key: str | None = None,
) -> None:
    key = expected_key or actual_key
    if key not in expected:
        return
    if result.get(actual_key) != expected[key]:
        mismatches.append(f"{key} expected {expected[key]!r} got {result.get(actual_key)!r}")


def _expect_assistant_reply_type(mismatches: list[str], result: dict[str, Any], expected: dict[str, Any]) -> None:
    if "assistant_reply_type" not in expected:
        return
    actual = str((result.get("assistant_reply") or {}).get("reply_type") or "")
    wanted = str(expected.get("assistant_reply_type") or "")
    if actual != wanted:
        mismatches.append(f"assistant_reply_type expected {wanted!r} got {actual!r}")


def _risk_flags(result: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if result.get("raw_text_written") and (
        result.get("expected", {}).get("raw_text_written") is False
        or result.get("expected", {}).get("agent2_direct_write") is False
    ):
        flags.append("raw_text_written")
    if result.get("direct_write") and result.get("expected", {}).get("agent2_direct_write") is False:
        flags.append("unexpected_direct_write")
    if result.get("execution_status") == "fallback_to_legacy":
        flags.append("fallback_to_legacy")
    if result.get("contract_invariant_violations"):
        flags.append("contract_invariant_violation")
    return flags


def _raw_text_written(raw_text: str, report: dict[str, Any]) -> bool:
    text = str(raw_text or "").strip()
    if not text:
        return False
    return any(text == str(item or "").strip() for field in REPORT_FIELDS for item in report.get(field) or [])


def _previous_report(case: DialogueCase) -> Any:
    previous = (case.metadata or {}).get("previous_report")
    if not isinstance(previous, dict):
        return None
    return SimpleNamespace(
        id=previous.get("id") or "previous-report",
        today_work=_string_list(previous.get("today_work")),
        problems=_string_list(previous.get("problems")),
        tomorrow_plan=_string_list(previous.get("tomorrow_plan")),
    )


def _task_observation(task: ActiveWorkflowTask) -> dict[str, Any]:
    return {
        "workflow": task.workflow,
        "task_id": task.task_id,
        "status": task.status,
        "reply_candidate": task.reply_candidate,
        "awaiting_confirmation": task.awaiting_confirmation,
    }


def _merge_tasks(tasks: list[ActiveWorkflowTask]) -> list[ActiveWorkflowTask]:
    by_workflow: dict[str, ActiveWorkflowTask] = {}
    for task in tasks:
        if task.workflow:
            by_workflow[task.workflow] = task
    return list(by_workflow.values())


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value).strip()
    return [text] if text else []


def _canonical_report_items(items: list[str], *, field: str) -> list[str]:
    return [_canonical_report_item(item, field=field) for item in items]


def _canonical_report_item(item: str, *, field: str) -> str:
    value = str(item or "").strip()
    if field == "today_work":
        value = value.removeprefix("今天").removeprefix("今日").strip()
    elif field == "tomorrow_plan":
        for prefix in ("明天计划", "明日计划", "明天准备", "明日准备", "明天打算", "明日打算", "明天", "明日", "明儿"):
            if value.startswith(prefix):
                value = value[len(prefix) :].strip()
                break
    value = value.replace("了", "")
    value = " ".join(value.split())
    return value.strip(" ，,。；;")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_risk_csv(path: Path, results: list[dict[str, Any]]) -> None:
    rows = [turn for dialogue in results for turn in dialogue.get("turns") or [] if turn.get("risk_flags") or turn.get("mismatches")]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "dialogue_id",
                "turn_id",
                "turn_index",
                "text",
                "execution_status",
                "primary_workflow",
                "direct_write",
                "fallback_to_legacy",
                "risk_flags",
                "mismatches",
            ],
        )
        writer.writeheader()
        for turn in rows:
            writer.writerow(
                {
                    "dialogue_id": turn.get("dialogue_id"),
                    "turn_id": turn.get("turn_id"),
                    "turn_index": turn.get("turn_index"),
                    "text": turn.get("text"),
                    "execution_status": turn.get("execution_status"),
                    "primary_workflow": turn.get("primary_workflow"),
                    "direct_write": turn.get("direct_write"),
                    "fallback_to_legacy": turn.get("fallback_to_legacy"),
                    "risk_flags": ";".join(turn.get("risk_flags") or []),
                    "mismatches": ";".join(turn.get("mismatches") or []),
                }
            )


def _summary_markdown(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Agent2 Daily Execution Replay Summary",
            "",
            f"- Total dialogues: {summary['total_dialogues']}",
            f"- Total turns: {summary['total_turns']}",
            f"- Passed dialogues: {summary['passed_dialogues']}",
            f"- Failed dialogues: {summary['failed_dialogues']}",
            f"- Mismatches: {summary['mismatch_count']}",
            f"- Risk turns: {summary['risk_turn_count']}",
            f"- Contract invariant violations: {summary['contract_invariant_violation_count']}",
            f"- Direct writes: {summary['direct_write_count']}",
            f"- Fallback to legacy: {summary['fallback_to_legacy_count']}",
            f"- Blocked by gate: {summary['blocked_by_gate_count']}",
            f"- Raw text written: {summary['raw_text_written_count']}",
            f"- Unexpected direct writes: {summary['unexpected_direct_write_count']}",
            f"- Gray ready: {summary['gray_ready']}",
            "",
        ]
    )
