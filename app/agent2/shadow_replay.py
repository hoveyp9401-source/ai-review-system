from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import csv
import json
from pathlib import Path
from typing import Any, Iterable

from app.agent2.daily_shadow import evaluate_daily_shadow
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope


_TEXT_KEYS = ("text", "raw_text", "message_text", "content", "msg")
_RECORD_ID_KEYS = ("record_id", "case_id", "message_id", "external_message_id", "id")


@dataclass(frozen=True)
class ShadowReplayRecord:
    """One historical or golden message for Agent2 shadow replay."""

    record_id: str
    text: str
    sender_id: str = "shadow-replay-user"
    sender_name: str = "Shadow Replay User"
    dingtalk_user_id: str = "shadow-replay-dingtalk-user"
    source: str = "shadow_replay"
    conversation_id: str = ""
    received_at: datetime | None = None
    active_tasks: tuple[ActiveWorkflowTask, ...] = ()
    recent_state: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, fallback_id: str) -> "ShadowReplayRecord":
        context = _mapping(value.get("context"))
        record_id = _first_text(value, _RECORD_ID_KEYS) or _first_text(context, _RECORD_ID_KEYS) or fallback_id
        text = _first_text(value, _TEXT_KEYS) or _first_text(context, _TEXT_KEYS) or ""
        source = _first_text(value, ("source",)) or _first_text(context, ("source",)) or "shadow_replay"
        sender_id = _first_text(value, ("sender_id", "user_id")) or _first_text(context, ("sender_id", "user_id"))
        dingtalk_user_id = (
            _first_text(value, ("dingtalk_user_id", "userid", "user_id"))
            or _first_text(context, ("dingtalk_user_id", "userid", "user_id"))
            or sender_id
            or "shadow-replay-dingtalk-user"
        )
        received_at = _parse_datetime(
            _first_text(value, ("received_at", "message_time", "created_at"))
            or _first_text(context, ("received_at", "message_time", "created_at"))
        )
        active_tasks = value.get("active_tasks")
        if active_tasks is None:
            active_tasks = context.get("active_tasks")
        return cls(
            record_id=str(record_id),
            text=str(text),
            sender_id=str(sender_id or "shadow-replay-user"),
            sender_name=(
                _first_text(value, ("sender_name", "name"))
                or _first_text(context, ("sender_name", "name"))
                or "Shadow Replay User"
            ),
            dingtalk_user_id=str(dingtalk_user_id),
            source=str(source),
            conversation_id=(
                _first_text(value, ("conversation_id", "channel", "conversation"))
                or _first_text(context, ("conversation_id", "channel", "conversation"))
                or ""
            ),
            received_at=received_at,
            active_tasks=tuple(_active_task_from_mapping(task) for task in list(active_tasks or [])),
            recent_state=dict(value.get("recent_state") or context.get("recent_state") or {}),
            expected=_expected_from_mapping(value),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_envelope(self) -> IncomingMessageEnvelope:
        return IncomingMessageEnvelope(
            sender_id=self.sender_id,
            sender_name=self.sender_name,
            dingtalk_user_id=self.dingtalk_user_id,
            source=self.source,
            raw_text=self.text,
            message_id=self.record_id,
            conversation_id=self.conversation_id,
            received_at=self.received_at,
            active_tasks=self.active_tasks,
            recent_state=dict(self.recent_state),
        )


def discover_shadow_replay_files(paths: Iterable[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.jsonl")))
        elif path.is_file():
            files.append(path)
    return files


def load_shadow_replay_records(paths: Iterable[str | Path]) -> list[ShadowReplayRecord]:
    records: list[ShadowReplayRecord] = []
    for path in discover_shadow_replay_files(paths):
        records.extend(load_shadow_replay_jsonl(path))
    return records


def load_shadow_replay_jsonl(path: str | Path) -> list[ShadowReplayRecord]:
    replay_path = Path(path)
    records: list[ShadowReplayRecord] = []
    with replay_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            try:
                payload = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {replay_path}:{line_no}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Invalid shadow replay record in {replay_path}:{line_no}: expected object")
            records.append(ShadowReplayRecord.from_mapping(payload, fallback_id=f"{replay_path.stem}-{line_no}"))
    return records


def replay_shadow_records(
    records: Iterable[ShadowReplayRecord],
    *,
    mode: str = "protective_gate",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for record in records:
        envelope = record.to_envelope()
        evaluation = evaluate_daily_shadow(envelope, mode=mode)
        route_observation = evaluation.route_observation(envelope)
        gate_observation = evaluation.gate_observation(envelope)
        result = {
            "record_id": record.record_id,
            "source": record.source,
            "metadata": dict(record.metadata),
            "raw_text_hash": route_observation["raw_text_hash"],
            "raw_text_chars": route_observation["raw_text_chars"],
            "primary_workflow": evaluation.plan.primary_workflow,
            "matched_workflows": list(evaluation.plan.matched_workflows),
            "route": route_observation,
            "gate": gate_observation["gate"],
            "coordination_plan": gate_observation["coordination_plan"],
            "coordination_sandbox": gate_observation["coordination_sandbox"],
            "daily_commands": gate_observation["daily_commands"],
            "legacy_adapter": gate_observation["legacy_adapter"],
            "summary": gate_observation["summary"],
            "risk_flags": _risk_flags(gate_observation),
            "expected": dict(record.expected),
        }
        result["mismatches"] = _expected_mismatches(result, record.expected)
        results.append(result)
    return results


def summarize_shadow_replay(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    result_list = list(results)
    workflow_counts = Counter(str(result.get("primary_workflow") or "") for result in result_list)
    coordination_action_counts: Counter[str] = Counter()
    sandbox_candidate_counts: Counter[str] = Counter()
    command_operation_counts: Counter[str] = Counter()
    adapter_status_counts: Counter[str] = Counter()
    for result in result_list:
        coordination_action_counts.update(
            str(action_type or "")
            for action_type in (result.get("coordination_plan") or {}).get("action_types") or []
        )
        sandbox_candidate_counts.update((result.get("coordination_sandbox") or {}).get("candidate_type_counts") or {})
        command_operation_counts.update(str(command.get("operation") or "") for command in result.get("daily_commands") or [])
        adapter_status_counts.update(str(adapter.get("status") or "") for adapter in result.get("legacy_adapter") or [])
    return {
        "total_records": len(result_list),
        "mismatch_count": sum(1 for result in result_list if result.get("mismatches")),
        "records_with_risk_flags": sum(1 for result in result_list if result.get("risk_flags")),
        "workflow_counts": dict(sorted(workflow_counts.items())),
        "coordination_action_counts": dict(sorted(coordination_action_counts.items())),
        "sandbox_candidate_counts": dict(sorted(sandbox_candidate_counts.items())),
        "sandbox_official_write_count": sum(
            int((result.get("coordination_sandbox") or {}).get("official_write_count") or 0)
            for result in result_list
        ),
        "sandbox_notification_count": sum(
            int((result.get("coordination_sandbox") or {}).get("notification_count") or 0)
            for result in result_list
        ),
        "command_operation_counts": dict(sorted(command_operation_counts.items())),
        "legacy_adapter_status_counts": dict(sorted(adapter_status_counts.items())),
        "dry_run_write_impact_count": sum(
            1
            for result in result_list
            if any(adapter.get("write_impact") for adapter in result.get("legacy_adapter") or [])
        ),
        "read_only_count": sum(
            1
            for result in result_list
            if any(adapter.get("read_only") for adapter in result.get("legacy_adapter") or [])
        ),
        "confirmation_count": sum(
            1
            for result in result_list
            if any(adapter.get("requires_confirmation") for adapter in result.get("legacy_adapter") or [])
        ),
        "no_daily_command_count": sum(1 for result in result_list if not result.get("daily_commands")),
    }


def write_shadow_replay_reports(results: list[dict[str, Any]], output_dir: str | Path) -> dict[str, Any]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_shadow_replay(results)
    _write_jsonl(out_dir / "shadow_replay_results.jsonl", results)
    (out_dir / "shadow_replay_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "shadow_replay_summary.md").write_text(_summary_markdown(summary), encoding="utf-8")
    _write_risk_csv(out_dir / "shadow_replay_risks.csv", results)
    return summary


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_risk_csv(path: Path, results: list[dict[str, Any]]) -> None:
    rows = [result for result in results if result.get("risk_flags") or result.get("mismatches")]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "record_id",
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
        for result in rows:
            writer.writerow(
                {
                    "record_id": result.get("record_id"),
                    "primary_workflow": result.get("primary_workflow"),
                    "risk_flags": ";".join(result.get("risk_flags") or []),
                    "mismatches": ";".join(result.get("mismatches") or []),
                    "command_operations": ";".join(
                        str(command.get("operation") or "") for command in result.get("daily_commands") or []
                    ),
                    "coordination_actions": ";".join(
                        str(action_type or "")
                        for action_type in (result.get("coordination_plan") or {}).get("action_types") or []
                    ),
                    "sandbox_candidates": ";".join(
                        str(candidate_type or "")
                        for candidate_type in (result.get("coordination_sandbox") or {}).get("candidate_type_counts") or {}
                    ),
                    "adapter_statuses": ";".join(
                        str(adapter.get("status") or "") for adapter in result.get("legacy_adapter") or []
                    ),
                }
            )


def _summary_markdown(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Agent2 Shadow Replay Summary",
            "",
            "## Overview",
            "",
            f"- Total records: {summary['total_records']}",
            f"- Mismatches: {summary['mismatch_count']}",
            f"- Records with risk flags: {summary['records_with_risk_flags']}",
            f"- Dry-run write impact: {summary['dry_run_write_impact_count']}",
            f"- Needs confirmation: {summary['confirmation_count']}",
            f"- Read-only: {summary['read_only_count']}",
            f"- No daily command: {summary['no_daily_command_count']}",
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


def _bullet_map(values: dict[str, int]) -> list[str]:
    if not values:
        return ["- none"]
    return [f"- {key or 'empty'}: {value}" for key, value in values.items()]


def _risk_flags(gate_observation: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    gate = gate_observation.get("gate") or {}
    plan = gate_observation.get("plan") or {}
    sandbox = gate_observation.get("coordination_sandbox") or {}
    commands = gate_observation.get("daily_commands") or []
    adapters = gate_observation.get("legacy_adapter") or []
    if any(adapter.get("write_impact") for adapter in adapters):
        flags.append("dry_run_write_impact")
    if any(adapter.get("requires_confirmation") for adapter in adapters):
        flags.append("confirmation_required")
    if gate.get("block_legacy_daily") and plan.get("primary_workflow") == "daily_report":
        flags.append("daily_blocked_by_gate")
    if plan.get("primary_workflow") != "daily_report" and any(adapter.get("write_impact") for adapter in adapters):
        flags.append("non_daily_adapter_write_plan")
    if any(command.get("requires_confirmation") for command in commands):
        flags.append("command_requires_confirmation")
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
    legacy_write_impact = _optional_bool(expected.get("legacy_write_impact"))
    if legacy_write_impact is not None:
        actual = any(adapter.get("write_impact") for adapter in result.get("legacy_adapter") or [])
        if actual is not legacy_write_impact:
            mismatches.append(f"legacy_write_impact expected {legacy_write_impact} got {actual}")
    adapter_status = expected.get("adapter_status")
    if adapter_status is not None:
        statuses = [adapter.get("status") for adapter in result.get("legacy_adapter") or []]
        if str(adapter_status) not in statuses:
            mismatches.append(f"adapter_status expected {adapter_status!r} got {statuses!r}")
    return mismatches


def _expected_from_mapping(value: dict[str, Any]) -> dict[str, Any]:
    expected = dict(value.get("expected") or {})
    aliases = {
        "expected_primary_workflow": "primary_workflow",
        "expected_should_enter_daily": "should_enter_daily",
        "expected_legacy_write_impact": "legacy_write_impact",
        "expected_adapter_status": "adapter_status",
    }
    for source_key, target_key in aliases.items():
        if source_key in value and target_key not in expected:
            expected[target_key] = value[source_key]
    return expected


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
