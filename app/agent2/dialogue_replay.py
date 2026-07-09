from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import csv
import json
from pathlib import Path
from typing import Any, Iterable

from app.agent2.daily_shadow import evaluate_daily_shadow
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT


_TEXT_KEYS = ("text", "raw_text", "message_text", "content", "msg")


@dataclass(frozen=True)
class DialogueTurn:
    turn_id: str
    text: str
    expected: dict[str, Any] = field(default_factory=dict)
    active_tasks: tuple[ActiveWorkflowTask, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, fallback_id: str) -> "DialogueTurn":
        context = _mapping(value.get("context"))
        active_tasks = value.get("active_tasks")
        if active_tasks is None:
            active_tasks = context.get("active_tasks")
        return cls(
            turn_id=str(value.get("turn_id") or value.get("message_id") or value.get("id") or fallback_id),
            text=_first_text(value, _TEXT_KEYS),
            expected=dict(value.get("expected") or {}),
            active_tasks=tuple(_active_task_from_mapping(task) for task in list(active_tasks or [])),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class DialogueCase:
    dialogue_id: str
    turns: tuple[DialogueTurn, ...]
    source: str = "dialogue"
    sender_id: str = "dialogue-user"
    sender_name: str = "Dialogue User"
    dingtalk_user_id: str = "dialogue-dingtalk-user"
    conversation_id: str = ""
    received_at: datetime | None = None
    active_tasks: tuple[ActiveWorkflowTask, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, fallback_id: str) -> "DialogueCase":
        context = _mapping(value.get("context"))
        turns = tuple(
            DialogueTurn.from_mapping(turn, fallback_id=f"turn-{index}")
            for index, turn in enumerate(list(value.get("turns") or []), start=1)
        )
        active_tasks = value.get("active_tasks")
        if active_tasks is None:
            active_tasks = context.get("active_tasks")
        source = str(value.get("source") or context.get("source") or "dialogue")
        sender_id = str(value.get("sender_id") or context.get("sender_id") or context.get("user_id") or "dialogue-user")
        dingtalk_user_id = str(
            value.get("dingtalk_user_id")
            or context.get("dingtalk_user_id")
            or context.get("userid")
            or sender_id
        )
        return cls(
            dialogue_id=str(value.get("dialogue_id") or value.get("case_id") or value.get("id") or fallback_id),
            turns=turns,
            source=source,
            sender_id=sender_id,
            sender_name=str(value.get("sender_name") or context.get("sender_name") or "Dialogue User"),
            dingtalk_user_id=dingtalk_user_id,
            conversation_id=str(value.get("conversation_id") or context.get("conversation_id") or fallback_id),
            received_at=_parse_datetime(str(value.get("received_at") or context.get("received_at") or "")),
            active_tasks=tuple(_active_task_from_mapping(task) for task in list(active_tasks or [])),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass
class DialogueState:
    active_tasks: list[ActiveWorkflowTask] = field(default_factory=list)

    def tasks_for_turn(self, turn: DialogueTurn) -> tuple[ActiveWorkflowTask, ...]:
        return tuple(_merge_tasks([*self.active_tasks, *turn.active_tasks]))

    def update_from_turn(self, result: dict[str, Any]) -> None:
        self.active_tasks = [task for task in self.active_tasks if task.workflow != WORKFLOW_DAILY_REPORT]
        carried = _daily_task_after_result(result)
        if carried is not None:
            self.active_tasks.append(carried)
            return
        if _had_daily_context(result) and not _daily_context_should_clear(result):
            previous = result.get("active_tasks_before") or []
            for task in previous:
                if task.get("workflow") == WORKFLOW_DAILY_REPORT:
                    self.active_tasks.append(_active_task_from_mapping(task))
                    break


def discover_dialogue_files(paths: Iterable[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.jsonl")))
        elif path.is_file():
            files.append(path)
    return files


def load_dialogue_cases(paths: Iterable[str | Path]) -> list[DialogueCase]:
    cases: list[DialogueCase] = []
    for path in discover_dialogue_files(paths):
        cases.extend(load_dialogue_jsonl(path))
    return cases


def load_dialogue_jsonl(path: str | Path) -> list[DialogueCase]:
    dialogue_path = Path(path)
    cases: list[DialogueCase] = []
    with dialogue_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            try:
                payload = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {dialogue_path}:{line_no}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Invalid dialogue case in {dialogue_path}:{line_no}: expected object")
            cases.append(DialogueCase.from_mapping(payload, fallback_id=f"{dialogue_path.stem}-{line_no}"))
    return cases


def replay_dialogue_case(case: DialogueCase, *, mode: str = "protective_gate") -> dict[str, Any]:
    state = DialogueState(active_tasks=list(case.active_tasks))
    turn_results: list[dict[str, Any]] = []
    for index, turn in enumerate(case.turns, start=1):
        active_tasks = state.tasks_for_turn(turn)
        envelope = IncomingMessageEnvelope(
            sender_id=case.sender_id,
            sender_name=case.sender_name,
            dingtalk_user_id=case.dingtalk_user_id,
            source=case.source,
            raw_text=turn.text,
            message_id=f"{case.dialogue_id}:{turn.turn_id}",
            conversation_id=case.conversation_id or case.dialogue_id,
            received_at=case.received_at,
            active_tasks=active_tasks,
            recent_state={},
        )
        evaluation = evaluate_daily_shadow(envelope, mode=mode)
        route_observation = evaluation.route_observation(envelope)
        gate_observation = evaluation.gate_observation(envelope)
        result = {
            "dialogue_id": case.dialogue_id,
            "turn_id": turn.turn_id,
            "turn_index": index,
            "source": case.source,
            "metadata": {**case.metadata, **turn.metadata},
            "raw_text_hash": route_observation["raw_text_hash"],
            "raw_text_chars": route_observation["raw_text_chars"],
            "active_tasks_before": [_task_observation(task) for task in active_tasks],
            "route": route_observation,
            "primary_workflow": evaluation.plan.primary_workflow,
            "matched_workflows": list(evaluation.plan.matched_workflows),
            "gate": gate_observation["gate"],
            "coordination_plan": gate_observation["coordination_plan"],
            "coordination_sandbox": gate_observation["coordination_sandbox"],
            "daily_commands": gate_observation["daily_commands"],
            "legacy_adapter": gate_observation["legacy_adapter"],
            "assistant_reply": gate_observation.get("assistant_reply"),
            "summary": gate_observation["summary"],
            "expected": dict(turn.expected),
        }
        result["risk_flags"] = _turn_risk_flags(result)
        result["mismatches"] = _expected_mismatches(result, turn.expected)
        turn_results.append(result)
        state.update_from_turn(result)
    return {
        "dialogue_id": case.dialogue_id,
        "source": case.source,
        "turn_count": len(turn_results),
        "passed": all(not result["mismatches"] for result in turn_results),
        "mismatch_count": sum(1 for result in turn_results if result["mismatches"]),
        "risk_count": sum(1 for result in turn_results if result["risk_flags"]),
        "turns": turn_results,
    }


def replay_dialogue_cases(cases: Iterable[DialogueCase], *, mode: str = "protective_gate") -> list[dict[str, Any]]:
    return [replay_dialogue_case(case, mode=mode) for case in cases]


def summarize_dialogue_results(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    dialogues = list(results)
    turns = [turn for dialogue in dialogues for turn in dialogue.get("turns") or []]
    workflow_counts = Counter(str(turn.get("primary_workflow") or "") for turn in turns)
    coordination_action_counts: Counter[str] = Counter()
    sandbox_candidate_counts: Counter[str] = Counter()
    command_operation_counts: Counter[str] = Counter()
    adapter_status_counts: Counter[str] = Counter()
    for turn in turns:
        coordination_action_counts.update(
            str(action_type or "")
            for action_type in (turn.get("coordination_plan") or {}).get("action_types") or []
        )
        sandbox_candidate_counts.update((turn.get("coordination_sandbox") or {}).get("candidate_type_counts") or {})
        command_operation_counts.update(str(command.get("operation") or "") for command in turn.get("daily_commands") or [])
        adapter_status_counts.update(str(adapter.get("status") or "") for adapter in turn.get("legacy_adapter") or [])
    return {
        "total_dialogues": len(dialogues),
        "total_turns": len(turns),
        "passed_dialogues": sum(1 for dialogue in dialogues if dialogue.get("passed")),
        "failed_dialogues": sum(1 for dialogue in dialogues if not dialogue.get("passed")),
        "mismatch_count": sum(int(dialogue.get("mismatch_count") or 0) for dialogue in dialogues),
        "risk_turn_count": sum(1 for turn in turns if turn.get("risk_flags")),
        "cross_turn_daily_write_count": sum(
            1 for turn in turns if "cross_turn_daily_write" in set(turn.get("risk_flags") or [])
        ),
        "cross_turn_daily_pending_count": sum(
            1 for turn in turns if "cross_turn_daily_pending" in set(turn.get("risk_flags") or [])
        ),
        "workflow_counts": dict(sorted(workflow_counts.items())),
        "coordination_action_counts": dict(sorted(coordination_action_counts.items())),
        "sandbox_candidate_counts": dict(sorted(sandbox_candidate_counts.items())),
        "sandbox_official_write_count": sum(
            int((turn.get("coordination_sandbox") or {}).get("official_write_count") or 0)
            for turn in turns
        ),
        "sandbox_notification_count": sum(
            int((turn.get("coordination_sandbox") or {}).get("notification_count") or 0)
            for turn in turns
        ),
        "command_operation_counts": dict(sorted(command_operation_counts.items())),
        "legacy_adapter_status_counts": dict(sorted(adapter_status_counts.items())),
    }


def write_dialogue_reports(results: list[dict[str, Any]], output_dir: str | Path) -> dict[str, Any]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_dialogue_results(results)
    _write_jsonl(out_dir / "dialogue_results.jsonl", results)
    (out_dir / "dialogue_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "dialogue_summary.md").write_text(_summary_markdown(summary), encoding="utf-8")
    _write_risk_csv(out_dir / "dialogue_risks.csv", results)
    return summary


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
                "primary_workflow",
                "risk_flags",
                "mismatches",
                "command_operations",
                "coordination_actions",
                "sandbox_candidates",
                "adapter_statuses",
            ],
        )
        writer.writeheader()
        for turn in rows:
            writer.writerow(
                {
                    "dialogue_id": turn.get("dialogue_id"),
                    "turn_id": turn.get("turn_id"),
                    "turn_index": turn.get("turn_index"),
                    "primary_workflow": turn.get("primary_workflow"),
                    "risk_flags": ";".join(turn.get("risk_flags") or []),
                    "mismatches": ";".join(turn.get("mismatches") or []),
                    "command_operations": ";".join(
                        str(command.get("operation") or "") for command in turn.get("daily_commands") or []
                    ),
                    "coordination_actions": ";".join(
                        str(action_type or "")
                        for action_type in (turn.get("coordination_plan") or {}).get("action_types") or []
                    ),
                    "sandbox_candidates": ";".join(
                        str(candidate_type or "")
                        for candidate_type in (turn.get("coordination_sandbox") or {}).get("candidate_type_counts") or {}
                    ),
                    "adapter_statuses": ";".join(
                        str(adapter.get("status") or "") for adapter in turn.get("legacy_adapter") or []
                    ),
                }
            )


def _summary_markdown(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Agent2 Dialogue Replay Summary",
            "",
            "## Overview",
            "",
            f"- Total dialogues: {summary['total_dialogues']}",
            f"- Total turns: {summary['total_turns']}",
            f"- Passed dialogues: {summary['passed_dialogues']}",
            f"- Failed dialogues: {summary['failed_dialogues']}",
            f"- Mismatches: {summary['mismatch_count']}",
            f"- Risk turns: {summary['risk_turn_count']}",
            f"- Cross-turn daily write: {summary['cross_turn_daily_write_count']}",
            f"- Cross-turn daily pending: {summary['cross_turn_daily_pending_count']}",
            "",
            "## Workflow Counts",
            "",
            *_bullet_map(summary["workflow_counts"]),
            "",
            "## Daily Command Operations",
            "",
            *_bullet_map(summary["command_operation_counts"]),
            "",
            "## Coordination Actions",
            "",
            *_bullet_map(summary["coordination_action_counts"]),
            "",
            "## Sandbox Candidates",
            "",
            f"- Official writes: {summary['sandbox_official_write_count']}",
            f"- Notifications: {summary['sandbox_notification_count']}",
            *_bullet_map(summary["sandbox_candidate_counts"]),
            "",
            "## Legacy Adapter Status",
            "",
            *_bullet_map(summary["legacy_adapter_status_counts"]),
            "",
        ]
    )


def _turn_risk_flags(result: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    had_daily_context = _had_daily_context(result)
    if had_daily_context:
        flags.append("had_daily_context")
    if any(adapter.get("requires_confirmation") for adapter in result.get("legacy_adapter") or []):
        flags.append("confirmation_required")
    if any(adapter.get("write_impact") for adapter in result.get("legacy_adapter") or []):
        flags.append("dry_run_write_impact")
    if had_daily_context and result.get("primary_workflow") != WORKFLOW_DAILY_REPORT and result.get("daily_commands"):
        if any(adapter.get("write_impact") for adapter in result.get("legacy_adapter") or []):
            flags.append("cross_turn_daily_write")
        else:
            flags.append("cross_turn_daily_pending")
    sandbox = result.get("coordination_sandbox") or {}
    if int(sandbox.get("official_write_count") or 0):
        flags.append("sandbox_official_write")
    if int(sandbox.get("notification_count") or 0):
        flags.append("sandbox_notification")
    return _dedupe(flags)


def _expected_mismatches(result: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    primary_workflow = expected.get("primary_workflow")
    if primary_workflow is not None and result.get("primary_workflow") != primary_workflow:
        mismatches.append(f"primary_workflow expected {primary_workflow!r} got {result.get('primary_workflow')!r}")
    should_enter_daily = _optional_bool(expected.get("should_enter_daily"))
    if should_enter_daily is not None:
        actual = bool((result.get("gate") or {}).get("allow_legacy_daily"))
        if actual is not should_enter_daily:
            mismatches.append(f"should_enter_daily expected {should_enter_daily} got {actual}")
    need_confirmation = _optional_bool(expected.get("need_confirmation"))
    if need_confirmation is not None:
        actual = bool((result.get("gate") or {}).get("need_confirmation"))
        if actual is not need_confirmation:
            mismatches.append(f"need_confirmation expected {need_confirmation} got {actual}")
    need_clarification = _optional_bool(expected.get("need_clarification"))
    if need_clarification is not None:
        actual = bool((result.get("gate") or {}).get("need_clarification"))
        if actual is not need_clarification:
            mismatches.append(f"need_clarification expected {need_clarification} got {actual}")
    legacy_write_impact = _optional_bool(expected.get("legacy_write_impact"))
    if legacy_write_impact is not None:
        actual = any(adapter.get("write_impact") for adapter in result.get("legacy_adapter") or [])
        if actual is not legacy_write_impact:
            mismatches.append(f"legacy_write_impact expected {legacy_write_impact} got {actual}")
    assistant_reply_type = expected.get("assistant_reply_type")
    if assistant_reply_type is not None:
        actual_reply_type = str((result.get("assistant_reply") or {}).get("reply_type") or "")
        if actual_reply_type != str(assistant_reply_type):
            mismatches.append(f"assistant_reply_type expected {assistant_reply_type!r} got {actual_reply_type!r}")
    expected_commands = list(expected.get("expected_commands") or [])
    actual_commands = [str(command.get("operation") or "") for command in result.get("daily_commands") or []]
    for command in expected_commands:
        if command not in actual_commands:
            mismatches.append(f"commands missing required operation {command!r}")
    for command in list(expected.get("forbidden_commands") or []):
        if command in actual_commands:
            mismatches.append(f"commands contains forbidden operation {command!r}")
    target_field = expected.get("target_field")
    if target_field is not None:
        actual_fields = [str(command.get("target_field") or "") for command in result.get("daily_commands") or []]
        if str(target_field) not in actual_fields:
            mismatches.append(f"target_field expected {target_field!r} got {actual_fields!r}")
    target_date = expected.get("target_date")
    if target_date is not None:
        actual_dates = [str(command.get("target_date") or "") for command in result.get("daily_commands") or []]
        if str(target_date) not in actual_dates:
            mismatches.append(f"target_date expected {target_date!r} got {actual_dates!r}")
    coordination_actions = list((result.get("coordination_plan") or {}).get("action_types") or [])
    for action_type in list(expected.get("expected_coordination_actions") or []):
        if action_type not in coordination_actions:
            mismatches.append(f"coordination_actions missing required action {action_type!r}")
    for action_type in list(expected.get("forbidden_coordination_actions") or []):
        if action_type in coordination_actions:
            mismatches.append(f"coordination_actions contains forbidden action {action_type!r}")
    sandbox_candidates = list(((result.get("coordination_sandbox") or {}).get("candidate_type_counts") or {}).keys())
    for candidate_type in list(expected.get("expected_sandbox_candidates") or []):
        if candidate_type not in sandbox_candidates:
            mismatches.append(f"sandbox_candidates missing required candidate {candidate_type!r}")
    for candidate_type in list(expected.get("forbidden_sandbox_candidates") or []):
        if candidate_type in sandbox_candidates:
            mismatches.append(f"sandbox_candidates contains forbidden candidate {candidate_type!r}")
    gate_reply_type = expected.get("gate_reply_type")
    if gate_reply_type is not None:
        actual_reply_type = str((result.get("gate") or {}).get("reply_type") or "")
        if str(gate_reply_type) != actual_reply_type:
            mismatches.append(f"gate_reply_type expected {gate_reply_type!r} got {actual_reply_type!r}")
    adapter_status = expected.get("adapter_status")
    if adapter_status is not None:
        statuses = [adapter.get("status") for adapter in result.get("legacy_adapter") or []]
        if str(adapter_status) not in statuses:
            mismatches.append(f"adapter_status expected {adapter_status!r} got {statuses!r}")
    return mismatches


def _daily_task_after_result(result: dict[str, Any]) -> ActiveWorkflowTask | None:
    commands = result.get("daily_commands") or []
    adapters = result.get("legacy_adapter") or []
    if not commands:
        return None
    if any(command.get("operation") in {"clear", "revoke"} for command in commands):
        return None
    if any(command.get("operation") == "confirm" and adapter.get("status") == "ready" for command in commands for adapter in adapters):
        return None
    if any(adapter.get("requires_confirmation") for adapter in adapters):
        return ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id=_task_id_from_result(result),
            status="pending_confirmation",
            reply_candidate=True,
            awaiting_confirmation=True,
            reason="dialogue replay daily command awaiting confirmation",
        )
    if any(adapter.get("write_impact") for adapter in adapters):
        return ActiveWorkflowTask(
            workflow=WORKFLOW_DAILY_REPORT,
            task_id=_task_id_from_result(result),
            status="collecting",
            reply_candidate=True,
            awaiting_confirmation=False,
            reason="dialogue replay daily write keeps context active",
        )
    return None


def _task_id_from_result(result: dict[str, Any]) -> str:
    for command in result.get("daily_commands") or []:
        task_id = str(command.get("task_id") or "")
        if task_id:
            return task_id
    return f"{result.get('dialogue_id')}:{result.get('turn_index')}"


def _daily_context_should_clear(result: dict[str, Any]) -> bool:
    return any(command.get("operation") in {"confirm", "clear", "revoke"} for command in result.get("daily_commands") or [])


def _had_daily_context(result: dict[str, Any]) -> bool:
    return any(task.get("workflow") == WORKFLOW_DAILY_REPORT for task in result.get("active_tasks_before") or [])


def _active_task_from_mapping(value: dict[str, Any]) -> ActiveWorkflowTask:
    task = _mapping(value)
    return ActiveWorkflowTask(
        workflow=str(task.get("workflow") or ""),
        task_id=str(task.get("task_id") or ""),
        status=str(task.get("status") or ""),
        reply_candidate=bool(task.get("reply_candidate") or False),
        awaiting_confirmation=bool(task.get("awaiting_confirmation") or False),
        reason=str(task.get("reason") or ""),
        metadata=dict(task.get("metadata") or {}),
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


def _bullet_map(values: dict[str, int]) -> list[str]:
    if not values:
        return ["- none"]
    return [f"- {key or 'empty'}: {value}" for key, value in values.items()]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(value: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if value.get(key) is not None:
            return str(value[key])
    return ""


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    return None


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
