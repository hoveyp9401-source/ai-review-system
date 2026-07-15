from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid5

from app.agent2.runtime.blind import BlindInputPack
from app.agent2.runtime.replay import RuntimeReplayCase

from .runtime_scoring import SealedLabelStore


_COMMAND_ACTIONS = {
    "fill": "capture_daily_event",
    "append": "capture_daily_event",
    "edit": "edit_daily_item",
    "delete": "delete_daily_item",
    "merge": "merge_daily_items",
    "confirm": "submit_daily_report",
    "submit": "submit_daily_report",
    "copy_previous": "copy_previous_daily_report",
}
_COMMAND_TYPES = {
    "fill": "append_item",
    "append": "append_item",
    "edit": "edit_item",
    "delete": "delete_item",
    "merge": "merge_items",
    "confirm": "submit_report",
    "submit": "submit_report",
}


def build_blind_pack(
    cases: Iterable[RuntimeReplayCase],
    *,
    pack_id: str,
    focus_annotations: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> tuple[BlindInputPack, SealedLabelStore]:
    """Split scored dialogue cases into a blind pack and sealed label store."""

    case_rows: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []
    for case in cases:
        opaque_case_id = _opaque_id("case", case.dialogue_id)
        actor_id = uuid5(NAMESPACE_URL, f"agent2-blind-actor:{case.dialogue_id}")
        conversation_id = _opaque_id(
            "conversation",
            case.conversation_id or case.dialogue_id,
        )
        active_tasks = _active_tasks(case)
        turn_rows: list[dict[str, Any]] = []
        for index, turn in enumerate(case.turns, start=1):
            opaque_turn_id = _opaque_id("turn", f"{case.dialogue_id}:{turn.turn_id}")
            occurred_at = case.received_at or datetime(
                2026,
                7,
                10,
                9,
                index % 60,
                tzinfo=timezone.utc,
            )
            turn_rows.append(
                {
                    "turn_id": opaque_turn_id,
                    "raw_text": turn.text,
                    "occurred_at": occurred_at.isoformat(),
                    "channel": "blind_replay",
                    "request_metadata": {
                        "source": "blind_runtime_pack",
                        "external_message_id": opaque_turn_id,
                    },
                }
            )
            label_rows.append(
                _sealed_label(
                    case=case,
                    turn_id=opaque_turn_id,
                    case_id=opaque_case_id,
                    expected=turn.expected,
                    focus_annotation=(focus_annotations or {}).get(
                        (case.dialogue_id, turn.turn_id)
                    ),
                )
            )
        case_rows.append(
            {
                "case_id": opaque_case_id,
                "actor_id": str(actor_id),
                "conversation_id": conversation_id,
                "initial_state": None,
                "initial_daily_snapshot": _initial_daily_snapshot(case, actor_id),
                "runtime_config": {
                    "daily_policy": {
                        "current_report_date": (
                            case.received_at.date().isoformat()
                            if case.received_at is not None
                            else "2026-07-10"
                        )
                    },
                    "active_tasks": active_tasks,
                },
                "turns": turn_rows,
            }
        )
    blind_pack = BlindInputPack.from_mapping(
        {
            "schema_version": "agent2.runtime_blind_input.v1",
            "pack_id": pack_id,
            "cases": case_rows,
        }
    )
    return blind_pack, SealedLabelStore.seal(input_pack=blind_pack, labels=label_rows)


def _initial_daily_snapshot(case: RuntimeReplayCase, actor_id: Any) -> dict[str, Any]:
    initial = case.metadata.get("initial_report")
    initial = dict(initial) if isinstance(initial, dict) else {}
    report_id = uuid5(NAMESPACE_URL, f"agent2-blind-report:{case.dialogue_id}")
    values = {
        field_name: [str(value) for value in initial.get(field_name) or []]
        for field_name in ("today_work", "problems", "tomorrow_plan")
    }
    item_ids_raw = initial.get("item_ids")
    item_ids_raw = item_ids_raw if isinstance(item_ids_raw, dict) else {}
    item_ids = {
        field_name: [
            str(item_ids_raw.get(field_name, [])[index])
            if index < len(item_ids_raw.get(field_name, []))
            else _opaque_id("item", f"{case.dialogue_id}:{field_name}:{index}:{value}")
            for index, value in enumerate(field_values)
        ]
        for field_name, field_values in values.items()
    }
    return {
        "report_id": str(report_id),
        "version": int(initial.get("version", 0)),
        "status": str(initial.get("status") or "collecting"),
        **values,
        "item_ids": item_ids,
    }


def _active_tasks(case: RuntimeReplayCase) -> list[dict[str, Any]]:
    tasks = [*case.active_tasks]
    for turn in case.turns:
        tasks.extend(turn.active_tasks)
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for task in tasks:
        key = (task.workflow, task.task_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "workflow": task.workflow,
                "task_id": task.task_id,
                "status": task.status,
                "reply_candidate": task.reply_candidate,
                "awaiting_confirmation": task.awaiting_confirmation,
                "reason": task.reason,
                "metadata": dict(task.metadata),
            }
        )
    return result


def _sealed_label(
    *,
    case: RuntimeReplayCase,
    case_id: str,
    turn_id: str,
    expected: dict[str, Any],
    focus_annotation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    expected_commands = [str(value).strip() for value in expected.get("expected_commands") or []]
    action_classes = [
        _COMMAND_ACTIONS[value]
        for value in expected_commands
        if value in _COMMAND_ACTIONS
    ]
    workflow = str(expected.get("primary_workflow") or "").strip()
    if not action_classes and workflow == "case_progress":
        action_classes.append("record_case_progress")
    elif not action_classes and workflow == "travel_coordination":
        action_classes.append("record_travel_event")
    elif not action_classes and workflow == "internal_qa":
        action_classes.append("answer_case_query")
    expected_write = _expected_write(expected)
    action_coverage = _action_coverage(
        expected_commands=expected_commands,
        expected_write=expected_write,
    )
    return {
        "case_id": case_id,
        "turn_id": turn_id,
        "expected_current_goal": workflow or None,
        "expected_action_class": action_classes,
        "expected_write_intent": expected_write,
        "expected_command_type": [
            _COMMAND_TYPES[value]
            for value in expected_commands
            if value in _COMMAND_TYPES
        ],
        "expected_clarification_requirement": bool(expected.get("requires_clarification", False)),
        "risk_annotation": str(expected.get("risk_annotation") or "corpus_regression"),
        "provenance": {
            "source": case.source,
            "annotation_source": "existing_corpus_machine_candidate",
            "action_coverage": action_coverage,
        },
        "confidence": None,
        "independent_review_status": "pending",
        "adjudication": dict(focus_annotation) if focus_annotation is not None else None,
    }


def _expected_write(expected: dict[str, Any]) -> bool:
    for key in (
        "agent2_direct_write",
        "should_write",
        "expected_write_intent",
        "legacy_write_impact",
    ):
        if key in expected:
            return bool(expected[key])
    if expected.get("should_enter_daily") is False:
        return False
    return False


def _action_coverage(*, expected_commands: list[str], expected_write: bool) -> str:
    mutating_commands = set(_COMMAND_ACTIONS)
    if not expected_write and any(command in mutating_commands for command in expected_commands):
        return "inconsistent"
    if "edit" in expected_commands:
        # Legacy ``edit`` covered replacement, deletion, and merge.  It cannot
        # be promoted to one exact v3 semantic action without independent review.
        return "coarse_legacy_command"
    return "machine_candidate"


def _opaque_id(kind: str, material: str) -> str:
    digest = hashlib.sha256(f"{kind}:{material}".encode("utf-8")).hexdigest()[:20]
    return f"opaque-{kind}-{digest}"
