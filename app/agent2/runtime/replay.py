from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.agent2.cognitive_core_v3 import CognitiveTurn, SemanticInterpretation
from app.agent2.conversation_state import ConversationState
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot
from app.workflows.intake import ActiveWorkflowTask

from .composition import compose_phase1_runtime
from .contracts import InMemoryRuntimeAuditSink, RuntimeActor, RuntimeTurnRequest
from .domains import InMemoryDailyDomainExecutor


REPORT_FIELDS = ("today_work", "problems", "tomorrow_plan")
_TEXT_KEYS = ("text", "raw_text", "message_text", "content", "msg")
EXAMPLE_REPLAY_TENANT_ID = "example-replay-tenant"
EXAMPLE_REPLAY_SENDER_ID = "example-replay-user"
EXAMPLE_REPLAY_SENDER_NAME = "Example Replay User"
EXAMPLE_REPLAY_DINGTALK_USER_ID = "example-replay-dingtalk-user"


@dataclass(frozen=True)
class RuntimeReplayTurn:
    turn_id: str
    text: str
    expected: dict[str, Any]
    active_tasks: tuple[ActiveWorkflowTask, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeReplayCase:
    dialogue_id: str
    turns: tuple[RuntimeReplayTurn, ...]
    source: str = "dialogue"
    sender_id: str = EXAMPLE_REPLAY_SENDER_ID
    sender_name: str = EXAMPLE_REPLAY_SENDER_NAME
    dingtalk_user_id: str = EXAMPLE_REPLAY_DINGTALK_USER_ID
    conversation_id: str = ""
    received_at: datetime | None = None
    active_tasks: tuple[ActiveWorkflowTask, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class BaselineCompiledSemanticInterpreter:
    """Recorded semantic adapter for orchestration replay.

    It consumes an already-produced baseline observation. It never calls the
    legacy chain from inside the Harness and is not a production interpreter.
    """

    def __init__(self, baseline_turns: list[dict[str, Any]]) -> None:
        self._turns = list(baseline_turns)

    async def interpret(self, turn: CognitiveTurn, state: ConversationState) -> SemanticInterpretation:
        if not self._turns:
            raise ValueError("recorded replay interpreter exhausted")
        baseline = self._turns.pop(0)
        if str(baseline.get("text") or "") != turn.text:
            raise ValueError("recorded replay turn does not match Runtime turn")
        return SemanticInterpretation.from_payload(_semantic_payload_from_baseline(baseline, turn))


async def replay_runtime_case(case: RuntimeReplayCase, *, baseline: dict[str, Any]) -> dict[str, Any]:
    if str(baseline.get("dialogue_id") or "") != case.dialogue_id:
        raise ValueError("baseline dialogue does not match Runtime replay case")
    baseline_turns = list(baseline.get("turns") or [])
    actor_id = uuid5(NAMESPACE_URL, f"agent2-runtime-replay-actor:{case.sender_id or case.dingtalk_user_id}")
    initial_snapshot = _initial_daily_snapshot(case, actor_id)
    daily_executor = InMemoryDailyDomainExecutor(initial_snapshot)
    state_store = InMemoryConversationStateStore()
    audit_sink = InMemoryRuntimeAuditSink()
    harness = compose_phase1_runtime(
        mode="replay",
        interpreter=BaselineCompiledSemanticInterpreter(baseline_turns),
        state_store=state_store,
        daily=daily_executor,
        daily_policy={"current_report_date": _replay_time(case).date().isoformat()},
        active_tasks=tuple(_active_task_mapping(item) for item in case.active_tasks),
        audit_sink=audit_sink,
    )

    candidate_turns: list[dict[str, Any]] = []
    for turn, baseline_turn in zip(case.turns, baseline_turns, strict=True):
        request = RuntimeTurnRequest(
            tenant_id=EXAMPLE_REPLAY_TENANT_ID,
            actor=RuntimeActor(
                actor_id=actor_id,
                display_name=case.sender_name,
                dingtalk_user_id=case.dingtalk_user_id,
            ),
            conversation_id=case.conversation_id or case.dialogue_id,
            message_id=f"{case.dialogue_id}:{turn.turn_id}",
            text=turn.text,
            occurred_at=_replay_time(case),
            channel="replay",
            request_metadata={"source": case.source, "external_message_id": turn.turn_id},
        )
        outcome = await harness.handle(request)
        candidate = _candidate_observation(outcome, daily_executor.snapshot)
        diffs = _turn_diffs(baseline_turn, candidate)
        candidate_turns.append(
            {
                "dialogue_id": case.dialogue_id,
                "turn_id": turn.turn_id,
                "message_id": request.message_id,
                "text_hash": hashlib.sha256(turn.text.encode("utf-8")).hexdigest(),
                "baseline": _baseline_observation(baseline_turn),
                "candidate": candidate,
                "diffs": diffs,
            }
        )

    return {
        "dialogue_id": case.dialogue_id,
        "source": case.source,
        "turn_count": len(candidate_turns),
        "mismatch_count": sum(1 for turn in candidate_turns if turn["diffs"]),
        "baseline_mismatch_count": int(baseline.get("mismatch_count") or 0),
        "turns": candidate_turns,
    }


async def replay_runtime_cases(
    cases: Iterable[RuntimeReplayCase],
    *,
    baselines: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    baseline_by_id: dict[str, dict[str, Any]] = {}
    for baseline in baselines:
        dialogue_id = str(baseline.get("dialogue_id") or "")
        if not dialogue_id or dialogue_id in baseline_by_id:
            raise ValueError(f"duplicate or missing baseline dialogue_id: {dialogue_id!r}")
        baseline_by_id[dialogue_id] = baseline
    results: list[dict[str, Any]] = []
    for case in cases:
        baseline = baseline_by_id.get(case.dialogue_id)
        if baseline is None:
            raise ValueError(f"missing baseline for dialogue {case.dialogue_id!r}")
        results.append(await replay_runtime_case(case, baseline=baseline))
    return results


def summarize_runtime_replay(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    dialogues = list(results)
    turns = [turn for dialogue in dialogues for turn in dialogue.get("turns") or []]
    candidate_actual_write_count = sum(
        1 for turn in turns if bool((turn.get("candidate") or {}).get("actual_write"))
    )
    legacy_fallback_count = sum(
        1 for turn in turns if bool((turn.get("candidate") or {}).get("legacy_fallback_used"))
    )
    typed_executor_bypass_count = sum(
        1 for turn in turns if _typed_executor_bypass(turn.get("candidate") or {})
    )
    unexpected_write_intent_count = sum(1 for turn in turns if _unexpected_write_intent(turn))
    missing_expected_write_count = sum(1 for turn in turns if _missing_expected_write(turn))
    failed_closed_count = sum(
        1 for turn in turns if str((turn.get("candidate") or {}).get("status")) == "failed_closed"
    )
    unscored_write_expectation_count = sum(
        1 for turn in turns if _expected_write(turn)[0] is None
    )
    mismatch_count = sum(1 for turn in turns if turn.get("diffs"))
    mismatch_stage_counts = Counter(
        str(diff.get("stage") or "unknown")
        for turn in turns
        for diff in turn.get("diffs") or []
    )
    mismatch_field_counts = Counter(
        str(diff.get("field") or "unknown")
        for turn in turns
        for diff in turn.get("diffs") or []
    )
    baseline_mismatch_count = sum(int(item.get("baseline_mismatch_count") or 0) for item in dialogues)
    summary = {
        "evaluation_scope": "baseline_derived_planner_executor_replay",
        "cognitive_semantic_independence": False,
        "total_dialogues": len(dialogues),
        "total_turns": len(turns),
        "mismatch_count": mismatch_count,
        "baseline_mismatch_count": baseline_mismatch_count,
        "candidate_actual_write_count": candidate_actual_write_count,
        "replay_actual_write_violation_count": candidate_actual_write_count,
        "unexpected_write_intent_count": unexpected_write_intent_count,
        "unexpected_write_count": sum(
            1
            for turn in turns
            if bool((turn.get("candidate") or {}).get("actual_write")) or _unexpected_write_intent(turn)
        ),
        "missing_expected_write_count": missing_expected_write_count,
        "unscored_write_expectation_count": unscored_write_expectation_count,
        "failed_closed_count": failed_closed_count,
        "legacy_fallback_count": legacy_fallback_count,
        "typed_executor_bypass_count": typed_executor_bypass_count,
        "mismatch_stage_counts": dict(sorted(mismatch_stage_counts.items())),
        "mismatch_field_counts": dict(sorted(mismatch_field_counts.items())),
    }
    diagnostic_execution_invariants_passed = (
        candidate_actual_write_count == 0
        and unexpected_write_intent_count == 0
        and legacy_fallback_count == 0
        and typed_executor_bypass_count == 0
    )
    summary["diagnostic_execution_invariants_passed"] = diagnostic_execution_invariants_passed
    summary["acceptance_eligible"] = False
    summary["acceptance_ineligibility_reason"] = (
        "The recorded baseline compiles the semantic decision and is also the comparison oracle; "
        "this replay can diagnose planner/executor orchestration but cannot prove semantic Safety or Parity."
    )
    summary["safety_ready"] = False
    summary["parity_ready"] = False
    return summary


def load_runtime_dialogue_cases(paths: Iterable[str | Path]) -> tuple[list[RuntimeReplayCase], dict[str, Any]]:
    files = _discover_runtime_dialogue_files(paths)
    cases: list[RuntimeReplayCase] = []
    seen_dialogues: dict[str, str] = {}
    manifest_files: list[dict[str, Any]] = []
    for path in files:
        resolved = path.resolve()
        payload = resolved.read_bytes()
        try:
            logical_path = resolved.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            logical_path = resolved.name
        manifest_files.append(
            {
                "path": logical_path,
                "resolved_path": str(resolved),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
        )
        for case in _load_runtime_dialogue_jsonl(resolved):
            previous = seen_dialogues.get(case.dialogue_id)
            if previous is not None:
                raise ValueError(
                    f"duplicate replay dialogue_id {case.dialogue_id!r} in {previous} and {resolved}"
                )
            turn_ids: set[str] = set()
            for turn in case.turns:
                if turn.turn_id in turn_ids:
                    raise ValueError(
                        f"duplicate replay turn_id {turn.turn_id!r} in dialogue {case.dialogue_id!r}"
                    )
                turn_ids.add(turn.turn_id)
            seen_dialogues[case.dialogue_id] = str(resolved)
            cases.append(case)
    return cases, finalize_runtime_input_manifest(
        {"files": manifest_files},
        cases,
        selection_limit=0,
    )


def finalize_runtime_input_manifest(
    manifest: dict[str, Any],
    cases: Iterable[RuntimeReplayCase],
    *,
    selection_limit: int,
) -> dict[str, Any]:
    selected = list(cases)
    files = [dict(item) for item in manifest.get("files") or []]
    selection = {
        "selection_limit": int(selection_limit),
        "dialogue_count": len(selected),
        "turn_count": sum(len(case.turns) for case in selected),
        "dialogue_ids": [case.dialogue_id for case in selected],
    }
    digest_payload = {
        "files": [
            {key: item.get(key) for key in ("path", "sha256", "bytes")}
            for item in files
        ],
        "selection": selection,
        "selection_source": {
            key: manifest.get(key)
            for key in ("selection_manifest_sha256", "schema_version")
            if manifest.get(key) is not None
        },
    }
    return {
        **manifest,
        "files": files,
        "selection": selection,
        "digest": hashlib.sha256(
            json.dumps(digest_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def write_runtime_replay_reports(
    results: list[dict[str, Any]],
    output_dir: str | Path,
    *,
    input_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    summary = summarize_runtime_replay(results)
    if input_manifest is not None:
        summary["input_manifest_digest"] = input_manifest.get("digest")
        (target / "input_manifest.json").write_text(
            json.dumps(input_manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    (target / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    with (target / "results.jsonl").open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    mismatch_rows = [
        turn
        for dialogue in results
        for turn in dialogue.get("turns") or []
        if turn.get("diffs")
    ]
    with (target / "mismatches.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("dialogue_id", "turn_id", "message_id", "diffs"),
        )
        writer.writeheader()
        for row in mismatch_rows:
            writer.writerow(
                {
                    "dialogue_id": row.get("dialogue_id"),
                    "turn_id": row.get("turn_id"),
                    "message_id": row.get("message_id"),
                    "diffs": json.dumps(row.get("diffs") or [], ensure_ascii=False, sort_keys=True),
                }
            )
    return summary


def _semantic_payload_from_baseline(baseline: dict[str, Any], turn: CognitiveTurn) -> dict[str, Any]:
    expected_write, _ = _expected_write(baseline)
    if expected_write is False:
        return _non_daily_semantic_payload_from_expectation(baseline, turn)
    before = _report_projection(baseline.get("report_before") or {})
    after = _report_projection(baseline.get("report_after") or {})
    intents: list[str] = []
    entities: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    unsupported_mutation = False

    if bool(baseline.get("direct_write")):
        baseline_operations = {
            str(command.get("operation") or "")
            for command in baseline.get("daily_commands") or []
        }
        if "copy_previous" in baseline_operations:
            entity_id = "replay-copy-previous-daily-report-target"
            return {
                "intents": ["daily_copy_previous"],
                "entities": [
                    {
                        "entity_id": entity_id,
                        "entity_type": "daily_report",
                        "value": turn.text,
                        "confidence": 1.0,
                        "attributes": {},
                    }
                ],
                "confidence": 1.0,
                "required_actions": [
                    {
                        "action_id": "replay-copy-previous-daily-report",
                        "action_type": "copy_previous_daily_report",
                        "intent": "daily_copy_previous",
                        "entity_ids": [entity_id],
                    }
                ],
                "clarification_need": None,
                "context_update": {"preserve_current_goal": True},
            }
        if before["status"] != "completed" and after["status"] == "completed":
            intents.append("daily_submit")
            actions.append(
                {
                    "action_id": "replay-submit-daily",
                    "action_type": "submit_daily_report",
                    "intent": "daily_submit",
                    "entity_ids": [],
                }
            )
        for field_name in REPORT_FIELDS:
            field_actions = _field_delta_actions(
                field_name=field_name,
                before=list(before[field_name]),
                after=list(after[field_name]),
                resources=turn.resources,
            )
            if field_actions is None:
                if before[field_name] != after[field_name]:
                    unsupported_mutation = True
                continue
            for entity, action in field_actions:
                entities.append(entity)
                actions.append(action)
                intents.append(action["intent"])

    clarification = None
    if bool(baseline.get("blocked_by_gate")) or unsupported_mutation:
        clarification = {
            "reason": "replay_baseline_operation_not_supported",
            "missing_fields": ["typed_target"],
            "question": "该历史操作缺少 Phase 1 可验证的稳定目标，本次 Replay 未执行。",
        }
    if not intents:
        intents.append("daily_modify" if clarification is not None else "chat")
    return {
        "intents": list(dict.fromkeys(intents)),
        "entities": entities,
        "confidence": 1.0,
        "required_actions": actions,
        "clarification_need": clarification,
        "context_update": {"preserve_current_goal": True},
    }


def _non_daily_semantic_payload_from_expectation(
    baseline: dict[str, Any],
    turn: CognitiveTurn,
) -> dict[str, Any]:
    expected = dict(baseline.get("expected") or {})
    workflow = str(expected.get("primary_workflow") or "chat").strip() or "chat"
    entities: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    if workflow == "case_progress":
        entity_id = "replay-case-progress-ref"
        entities.append(
            {
                "entity_id": entity_id,
                "entity_type": "case_ref",
                "value": turn.text,
                "confidence": 1.0,
                "attributes": {"stage": str(expected.get("category") or "progress")},
            }
        )
        actions.append(
            {
                "action_id": "replay-record-case-progress",
                "action_type": "record_case_progress",
                "intent": "case_progress",
                "entity_ids": [entity_id],
            }
        )
    elif workflow == "travel_coordination":
        entity_id = "replay-travel-event"
        entities.append(
            {
                "entity_id": entity_id,
                "entity_type": "travel_event",
                "value": turn.text,
                "confidence": 1.0,
                "attributes": {"purpose": turn.text},
            }
        )
        actions.append(
            {
                "action_id": "replay-record-travel-event",
                "action_type": "record_travel_event",
                "intent": "travel_coordination",
                "entity_ids": [entity_id],
            }
        )
    elif workflow == "internal_qa":
        entity_id = "replay-case-query"
        entities.append(
            {
                "entity_id": entity_id,
                "entity_type": "case_query",
                "value": turn.text,
                "confidence": 1.0,
                "attributes": {"question": turn.text},
            }
        )
        actions.append(
            {
                "action_id": "replay-answer-case-query",
                "action_type": "answer_case_query",
                "intent": "internal_qa",
                "entity_ids": [entity_id],
            }
        )
    return {
        "intents": [workflow],
        "entities": entities,
        "confidence": 1.0,
        "required_actions": actions,
        "clarification_need": None,
        "context_update": {"preserve_current_goal": True},
    }


def _field_delta_actions(
    *,
    field_name: str,
    before: list[str],
    after: list[str],
    resources: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]] | None:
    if before == after:
        return []
    if len(after) >= len(before) and after[: len(before)] == before:
        result = []
        for index, value in enumerate(after[len(before) :], start=1):
            entity_id = f"replay-daily-event-{field_name}-{len(before) + index}"
            result.append(
                (
                    {
                        "entity_id": entity_id,
                        "entity_type": "daily_event",
                        "value": value,
                        "confidence": 1.0,
                        "attributes": {"field": field_name},
                    },
                    {
                        "action_id": f"replay-capture-{field_name}-{len(before) + index}",
                        "action_type": "capture_daily_event",
                        "intent": "daily_append",
                        "entity_ids": [entity_id],
                    },
                )
            )
        return result
    differing = [index for index, (left, right) in enumerate(zip(before, after)) if left != right]
    if len(before) == len(after) and len(differing) == 1:
        index = differing[0]
        target_id = _resource_item_id(resources, field_name, index)
        if not target_id:
            return None
        entity_id = f"replay-daily-target-{field_name}-{index + 1}"
        return [
            (
                {
                    "entity_id": entity_id,
                    "entity_type": "daily_item_target",
                    "value": before[index],
                    "confidence": 1.0,
                    "attributes": {"target_item_ids": [target_id], "replacement": after[index]},
                },
                {
                    "action_id": f"replay-edit-{field_name}-{index + 1}",
                    "action_type": "edit_daily_item",
                    "intent": "daily_modify",
                    "entity_ids": [entity_id],
                },
            )
        ]
    if len(after) == len(before) - 1:
        removed_index = _single_removed_index(before, after)
        if removed_index is not None:
            target_id = _resource_item_id(resources, field_name, removed_index)
            if not target_id:
                return None
            entity_id = f"replay-daily-target-{field_name}-{removed_index + 1}"
            return [
                (
                    {
                        "entity_id": entity_id,
                        "entity_type": "daily_item_target",
                        "value": before[removed_index],
                        "confidence": 1.0,
                        "attributes": {"target_item_ids": [target_id]},
                    },
                    {
                        "action_id": f"replay-delete-{field_name}-{removed_index + 1}",
                        "action_type": "delete_daily_item",
                        "intent": "daily_modify",
                        "entity_ids": [entity_id],
                    },
                )
            ]
        merged_index = _single_merged_pair_index(before, after)
        if merged_index is None:
            return None
        target_ids = [
            _resource_item_id(resources, field_name, merged_index),
            _resource_item_id(resources, field_name, merged_index + 1),
        ]
        if not all(target_ids):
            return None
        entity_id = f"replay-daily-merge-target-{field_name}-{merged_index + 1}"
        return [
            (
                {
                    "entity_id": entity_id,
                    "entity_type": "daily_item_target",
                    "value": "；".join(before[merged_index : merged_index + 2]),
                    "confidence": 1.0,
                    "attributes": {
                        "target_item_ids": target_ids,
                        "replacement": after[merged_index],
                    },
                },
                {
                    "action_id": f"replay-merge-{field_name}-{merged_index + 1}",
                    "action_type": "merge_daily_items",
                    "intent": "daily_modify",
                    "entity_ids": [entity_id],
                },
            )
        ]
    return None


def _single_removed_index(before: list[str], after: list[str]) -> int | None:
    for index in range(len(before)):
        if before[:index] + before[index + 1 :] == after:
            return index
    return None


def _single_merged_pair_index(before: list[str], after: list[str]) -> int | None:
    if len(after) != len(before) - 1:
        return None
    for index in range(len(before) - 1):
        if before[:index] != after[:index]:
            continue
        if before[index + 2 :] != after[index + 1 :]:
            continue
        if after[index] in {before[index], before[index + 1]}:
            continue
        return index
    return None


def _resource_item_id(resources: dict[str, Any], field_name: str, index: int | None) -> str:
    if index is None:
        return ""
    items = list((resources.get("daily_draft") or {}).get("items") or [])
    matching = [item for item in items if item.get("field") == field_name]
    if not 0 <= index < len(matching):
        return ""
    return str(matching[index].get("item_id") or "")


def _candidate_observation(outcome: Any, snapshot: DailyReportMutationSnapshot) -> dict[str, Any]:
    decision = outcome.decision
    plan = outcome.command_plan
    return {
        "run_id": outcome.run_id,
        "status": outcome.status,
        "reply_type": outcome.reply.reply_type,
        "failed_stage": getattr(outcome, "failed_stage", ""),
        "error_code": getattr(outcome, "error_code", ""),
        "decision": decision.as_dict() if decision is not None else None,
        "intents": list(decision.intents) if decision is not None else [],
        "actions": [action.action_type for action in decision.required_actions] if decision is not None else [],
        "daily_commands": [command.as_dict() for command in plan.daily_commands] if plan is not None else [],
        "business_commands": [command.as_dict() for command in plan.business_commands] if plan is not None else [],
        "planning_blocks": [
            {
                "action_id": block.action_id,
                "reason_code": block.reason_code,
                "detail": block.detail,
            }
            for block in plan.blocked_actions
        ] if plan is not None else [],
        "planner_output": plan.as_dict() if plan is not None else None,
        "domain_results": [
            {
                "domain_id": result.domain_id,
                "status": result.status,
                "command_count": result.command_count,
                "actual_write": result.actual_write,
                "would_write": result.would_write,
                "command_results": [dict(item) for item in result.command_results],
            }
            for result in outcome.domain_results
        ],
        "actual_write": outcome.actual_write,
        "would_write": outcome.would_write,
        "legacy_fallback_used": outcome.legacy_fallback_used,
        "report_after": _snapshot_projection(snapshot),
        "state_version": outcome.state_version,
        "conversation_state": outcome.state.as_payload() if outcome.state is not None else None,
        "trace": [
            {"sequence": event.sequence, "stage": event.stage, "detail": dict(event.detail)}
            for event in outcome.trace
        ],
    }


def _baseline_observation(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_status": turn.get("execution_status"),
        "direct_write": bool(turn.get("direct_write")),
        "fallback_to_legacy": bool(turn.get("fallback_to_legacy")),
        "blocked_by_gate": bool(turn.get("blocked_by_gate")),
        "daily_commands": list(turn.get("daily_commands") or []),
        "report_after": _report_projection(turn.get("report_after") or {}),
        "expected": dict(turn.get("expected") or {}),
        "baseline_mismatches": list(turn.get("mismatches") or []),
    }


def _turn_diffs(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    expected = dict(baseline.get("expected") or {})
    expected_write, expected_write_source = _expected_write(baseline)
    baseline_write = bool(baseline.get("direct_write")) if expected_write is None else expected_write
    candidate_write = bool(candidate.get("would_write"))
    if baseline_write != candidate_write:
        diffs.append(
            {
                "stage": "execution",
                "field": "write_intent",
                "baseline": baseline_write,
                "candidate": candidate_write,
                "expectation_source": expected_write_source,
            }
        )
    if expected_write is False and candidate_write:
        diffs.append(
            {
                "stage": "safety",
                "field": "unexpected_write_intent",
                "baseline": False,
                "candidate": True,
                "expectation_source": expected_write_source,
            }
        )
    expected_workflow = str(expected.get("primary_workflow") or "").strip()
    candidate_workflows = _candidate_workflows(candidate)
    if expected_workflow and expected_workflow not in candidate_workflows:
        diffs.append(
            {
                "stage": "semantic",
                "field": "primary_workflow",
                "baseline": expected_workflow,
                "candidate": sorted(candidate_workflows),
            }
        )
    expected_commands = {_normalize_expected_command(value) for value in expected.get("expected_commands") or []}
    expected_commands.discard("")
    candidate_commands = _candidate_command_categories(candidate)
    if expected_commands and not expected_commands.issubset(candidate_commands):
        diffs.append(
            {
                "stage": "plan",
                "field": "command_categories",
                "baseline": sorted(expected_commands),
                "candidate": sorted(candidate_commands),
            }
        )
    target_field = str(expected.get("target_field") or "").strip()
    candidate_fields = _candidate_target_fields(candidate)
    if target_field and target_field not in candidate_fields:
        diffs.append(
            {
                "stage": "plan",
                "field": "target_field",
                "baseline": target_field,
                "candidate": sorted(candidate_fields),
            }
        )
    baseline_report = _report_projection(baseline.get("report_after") or {})
    candidate_report = _report_projection(candidate.get("report_after") or {})
    if baseline_report != candidate_report:
        diffs.append(
            {
                "stage": "execution",
                "field": "report_after",
                "baseline": baseline_report,
                "candidate": candidate_report,
            }
        )
    expected_reply = str(expected.get("assistant_reply_type") or expected.get("expected_reply_type") or "").strip()
    if expected_reply and not _candidate_satisfies_reply(expected_reply, candidate, candidate_workflows):
        diffs.append(
            {
                "stage": "reply",
                "field": "reply_type",
                "baseline": expected_reply,
                "candidate": candidate.get("reply_type"),
            }
        )
    forbidden_hits = _forbidden_report_hits(expected, candidate_report)
    if forbidden_hits:
        diffs.append(
            {
                "stage": "safety",
                "field": "forbidden_report_content",
                "baseline": "absent",
                "candidate": forbidden_hits,
            }
        )
    forbidden_commands = {
        _normalize_expected_command(value) for value in expected.get("forbidden_commands") or []
    }
    forbidden_commands.discard("")
    forbidden_command_hits = sorted(forbidden_commands & candidate_commands)
    if forbidden_command_hits:
        diffs.append(
            {
                "stage": "safety",
                "field": "forbidden_commands",
                "baseline": "absent",
                "candidate": forbidden_command_hits,
            }
        )
    if _typed_executor_bypass(candidate):
        diffs.append(
            {
                "stage": "safety",
                "field": "typed_command_receipt_partition",
                "baseline": "complete",
                "candidate": "incomplete",
            }
        )
    if candidate.get("status") == "failed_closed":
        diffs.append(
            {
                "stage": "runtime",
                "field": "failed_closed",
                "baseline": baseline.get("execution_status"),
                "candidate": {
                    "failed_stage": candidate.get("failed_stage"),
                    "error_code": candidate.get("error_code"),
                },
            }
        )
    return diffs


def _typed_executor_bypass(candidate: dict[str, Any]) -> bool:
    planned = [
        command
        for key in ("daily_commands", "business_commands")
        for command in candidate.get(key) or []
    ]
    planned_ids = [str(command.get("command_id") or "") for command in planned]
    results = list(candidate.get("domain_results") or [])
    receipts = [receipt for result in results for receipt in result.get("command_results") or []]
    receipt_ids = [str((receipt.get("typed_command") or {}).get("command_id") or "") for receipt in receipts]
    if bool(candidate.get("would_write")) and not planned_ids:
        return True
    if (
        not all(planned_ids)
        or not all(receipt_ids)
        or len(planned_ids) != len(set(planned_ids))
        or len(receipt_ids) != len(set(receipt_ids))
        or set(planned_ids) != set(receipt_ids)
    ):
        return bool(planned_ids or receipt_ids)
    if sum(int(result.get("command_count") or 0) for result in results) != len(planned):
        return True
    daily_ids = {str(command.get("command_id") or "") for command in candidate.get("daily_commands") or []}
    for receipt in receipts:
        command_id = str((receipt.get("typed_command") or {}).get("command_id") or "")
        if command_id not in daily_ids:
            continue
        validation_status = str(receipt.get("validation_status") or "")
        if validation_status not in {"authorized", "blocked", "not_executed"}:
            return True
        if validation_status == "authorized" and not receipt.get("simulated"):
            return True
    return False


def _expected_write(turn_or_baseline: dict[str, Any]) -> tuple[bool | None, str]:
    baseline = turn_or_baseline.get("baseline") or turn_or_baseline
    expected = dict(baseline.get("expected") or {})
    for key in ("should_write_db", "agent2_direct_write"):
        if key in expected:
            return bool(expected[key]), f"expected.{key}"
    if expected.get("should_enter_daily") is False:
        return False, "expected.should_enter_daily"
    if "direct_write" in baseline:
        return bool(baseline.get("direct_write")), "baseline.direct_write"
    return None, "unscored"


def _unexpected_write_intent(turn: dict[str, Any]) -> bool:
    expected, _ = _expected_write(turn)
    return expected is False and bool((turn.get("candidate") or {}).get("would_write"))


def _missing_expected_write(turn: dict[str, Any]) -> bool:
    expected, _ = _expected_write(turn)
    return expected is True and not bool((turn.get("candidate") or {}).get("would_write"))


def _candidate_workflows(candidate: dict[str, Any]) -> set[str]:
    workflows: set[str] = set()
    for raw_intent in candidate.get("intents") or []:
        intent = str(raw_intent)
        if intent.startswith("daily_"):
            workflows.add("daily_report")
        elif intent in {"case_query", "case_progress", "case_update"}:
            workflows.add("case_progress")
        elif intent in {"travel", "travel_coordination", "travel_event"}:
            workflows.add("travel_coordination")
        else:
            workflows.add(intent)
    return workflows


def _normalize_expected_command(value: Any) -> str:
    command = str(value or "").strip()
    return {
        "fill": "append",
        "edit": "edit",
        "query_current": "query",
        "confirm": "submit",
    }.get(command, command)


def _candidate_command_categories(candidate: dict[str, Any]) -> set[str]:
    categories: set[str] = set()
    for command in candidate.get("daily_commands") or []:
        command_type = str(command.get("command_type") or "")
        categories.add(
            {
                "append_item": "append",
                "edit_item": "edit",
                "delete_item": "edit",
                "merge_items": "edit",
                "submit_report": "submit",
            }.get(command_type, command_type)
        )
    for command in candidate.get("business_commands") or []:
        categories.add(str(command.get("command_type") or ""))
    categories.discard("")
    return categories


def _candidate_target_fields(candidate: dict[str, Any]) -> set[str]:
    fields = {
        str((command.get("patch") or {}).get("field") or "")
        for command in candidate.get("daily_commands") or []
    }
    fields.discard("")
    return fields


def _candidate_satisfies_reply(
    expected_reply: str,
    candidate: dict[str, Any],
    candidate_workflows: set[str],
) -> bool:
    normalized = {
        "daily": "daily_report",
        "case_query": "case_progress",
    }.get(expected_reply, expected_reply)
    return normalized in candidate_workflows or normalized == str(candidate.get("reply_type") or "")


def _forbidden_report_hits(expected: dict[str, Any], report: dict[str, Any]) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    field_keys = {
        "forbidden_today_work_contains": "today_work",
        "forbidden_problems_contains": "problems",
        "forbidden_tomorrow_plan_contains": "tomorrow_plan",
    }
    for expectation_key, report_field in field_keys.items():
        values = [str(value) for value in report.get(report_field) or []]
        for forbidden in expected.get(expectation_key) or []:
            needle = str(forbidden)
            if any(needle and needle in value for value in values):
                hits.append({"field": report_field, "text": needle})
    return hits


def _initial_daily_snapshot(case: RuntimeReplayCase, actor_id: UUID) -> DailyReportMutationSnapshot:
    data = dict((case.metadata or {}).get("initial_report") or {})
    item_ids = {
        field_name: tuple(
            str(uuid5(NAMESPACE_URL, f"agent2-runtime-replay-item:{case.dialogue_id}:{field_name}:{index}:{value}"))
            for index, value in enumerate(_string_list(data.get(field_name)))
        )
        for field_name in REPORT_FIELDS
    }
    return DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, f"agent2-runtime-replay-report:{case.dialogue_id}"),
        owner_user_id=actor_id,
        version=0,
        status=str(data.get("status") or "collecting"),
        today_work=tuple(_string_list(data.get("today_work"))),
        problems=tuple(_string_list(data.get("problems"))),
        tomorrow_plan=tuple(_string_list(data.get("tomorrow_plan"))),
        item_ids=item_ids,
    )


def _report_projection(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "today_work": _string_list(value.get("today_work")),
        "problems": _string_list(value.get("problems")),
        "tomorrow_plan": _string_list(value.get("tomorrow_plan")),
        "status": str(value.get("status") or "collecting"),
    }


def _snapshot_projection(snapshot: DailyReportMutationSnapshot) -> dict[str, Any]:
    return {
        "today_work": list(snapshot.today_work),
        "problems": list(snapshot.problems),
        "tomorrow_plan": list(snapshot.tomorrow_plan),
        "status": snapshot.status,
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _replay_time(case: RuntimeReplayCase) -> datetime:
    value = case.received_at or datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _active_task_mapping(task: Any) -> dict[str, Any]:
    return {
        "workflow": task.workflow,
        "task_id": task.task_id,
        "status": task.status,
        "reply_candidate": task.reply_candidate,
        "awaiting_confirmation": task.awaiting_confirmation,
        "metadata": dict(task.metadata),
    }


def _discover_runtime_dialogue_files(paths: Iterable[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.jsonl")))
        elif path.is_file():
            files.append(path)
        else:
            raise ValueError(f"Runtime replay input does not exist: {path}")
    return files


def _load_runtime_dialogue_jsonl(path: Path) -> list[RuntimeReplayCase]:
    cases: list[RuntimeReplayCase] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid Runtime replay JSON in {path}:{line_no}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Invalid Runtime replay case in {path}:{line_no}: expected object")
            cases.append(_runtime_replay_case_from_mapping(payload, path=path, line_no=line_no))
    return cases


def _runtime_replay_case_from_mapping(
    value: dict[str, Any],
    *,
    path: Path,
    line_no: int,
) -> RuntimeReplayCase:
    context = _mapping(value.get("context"))
    dialogue_id = str(value.get("dialogue_id") or value.get("case_id") or value.get("id") or "").strip()
    if not dialogue_id:
        raise ValueError(f"Runtime replay dialogue_id is required in {path}:{line_no}")
    raw_turns = value.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ValueError(f"Runtime replay dialogue {dialogue_id!r} requires at least one turn")
    turns: list[RuntimeReplayTurn] = []
    for index, raw_turn in enumerate(raw_turns, start=1):
        if not isinstance(raw_turn, dict):
            raise ValueError(f"Runtime replay {dialogue_id!r} turn {index} must be an object")
        turn_context = _mapping(raw_turn.get("context"))
        turn_id = str(
            raw_turn.get("turn_id") or raw_turn.get("message_id") or raw_turn.get("id") or ""
        ).strip()
        text = _first_text(raw_turn, _TEXT_KEYS).strip()
        expected = raw_turn.get("expected")
        if not turn_id or not text:
            raise ValueError(
                f"Runtime replay {dialogue_id!r} turn {index} requires non-empty turn_id and text"
            )
        if not isinstance(expected, dict) or not expected:
            raise ValueError(
                f"Runtime replay {dialogue_id!r} turn {turn_id!r} requires scored expected assertions"
            )
        raw_tasks = raw_turn.get("active_tasks")
        if raw_tasks is None:
            raw_tasks = turn_context.get("active_tasks")
        turns.append(
            RuntimeReplayTurn(
                turn_id=turn_id,
                text=text,
                expected=dict(expected),
                active_tasks=tuple(_active_task_from_mapping(task) for task in list(raw_tasks or [])),
                metadata=dict(raw_turn.get("metadata") or {}),
            )
        )
    raw_tasks = value.get("active_tasks")
    if raw_tasks is None:
        raw_tasks = context.get("active_tasks")
    return RuntimeReplayCase(
        dialogue_id=dialogue_id,
        turns=tuple(turns),
        source=str(value.get("source") or context.get("source") or "dialogue"),
        sender_id=str(
            value.get("sender_id")
            or context.get("sender_id")
            or context.get("user_id")
            or EXAMPLE_REPLAY_SENDER_ID
        ),
        sender_name=str(
            value.get("sender_name")
            or context.get("sender_name")
            or EXAMPLE_REPLAY_SENDER_NAME
        ),
        dingtalk_user_id=str(
            value.get("dingtalk_user_id")
            or context.get("dingtalk_user_id")
            or context.get("userid")
            or value.get("sender_id")
            or EXAMPLE_REPLAY_DINGTALK_USER_ID
        ),
        conversation_id=str(value.get("conversation_id") or context.get("conversation_id") or dialogue_id),
        received_at=_parse_datetime(value.get("received_at") or context.get("received_at")),
        active_tasks=tuple(_active_task_from_mapping(task) for task in list(raw_tasks or [])),
        metadata=dict(value.get("metadata") or {}),
    )


def _active_task_from_mapping(value: Any) -> ActiveWorkflowTask:
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


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
