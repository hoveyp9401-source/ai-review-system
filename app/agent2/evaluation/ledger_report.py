from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from app.agent2.json_immutability import thaw_json_value
from app.agent2.runtime.blind import BlindActualArtifact, BlindInputPack

from .runtime_scoring import SealedLabelStore


def build_ledger_closure_report(
    input_pack: BlindInputPack,
    actual: BlindActualArtifact,
    labels: SealedLabelStore,
) -> dict[str, Any]:
    """Join Blind actuals and sealed candidates without upgrading them to Gold."""

    input_pack = BlindInputPack.from_mapping(input_pack.as_mapping())
    actual = BlindActualArtifact.from_mapping(actual.as_mapping())
    labels = SealedLabelStore.from_mapping(labels.as_mapping())
    if actual.input_pack_id != input_pack.pack_id or labels.input_pack_id != input_pack.pack_id:
        raise ValueError("ledger report inputs have different pack IDs")
    if actual.input_pack_digest != input_pack.digest or labels.input_pack_digest != input_pack.digest:
        raise ValueError("ledger report inputs have different pack digests")

    blind_turns = {
        (case.case_id, turn.turn_id): turn
        for case in input_pack.cases
        for turn in case.turns
    }
    actual_turns = {
        (str(case.get("case_id") or ""), str(turn.get("turn_id") or "")): turn
        for case in actual.cases
        for turn in case.get("turns") or []
    }
    rows: list[dict[str, Any]] = []
    for label in labels.labels:
        key = (str(label["case_id"]), str(label["turn_id"]))
        blind_turn = blind_turns.get(key)
        actual_turn = actual_turns.get(key)
        if blind_turn is None or actual_turn is None:
            raise ValueError(f"ledger label has no Blind/Actual turn: {key[0]}/{key[1]}")
        rows.append(
            _case_row(
                blind_turn=blind_turn,
                actual_turn=actual_turn,
                label=label,
                actual=actual,
                labels=labels,
            )
        )

    original_rows = [row for row in rows if row["historical_population"] == "original"]
    surfaced_rows = [row for row in rows if row["historical_population"] == "post_fix_surfaced"]
    return thaw_json_value({
        "schema_version": "agent2.runtime_ledger_closure.v1",
        "input_pack_id": input_pack.pack_id,
        "input_pack_digest": input_pack.digest,
        "runtime_version_hash": actual.runtime_version_hash,
        "actual_artifact_hash": actual.artifact_hash,
        "sealed_label_hash": labels.seal_hash,
        "evidence_class": "blind_runtime_actual_plus_machine_candidate_labels",
        "independent_review_status": (
            "complete"
            if rows and all(row["independent_review_status"] == "human_approved" for row in rows)
            else "pending"
        ),
        "summary": {
            "ledger_rows": len(rows),
            "original_rows": len(original_rows),
            "post_fix_surfaced_rows": len(surfaced_rows),
            "original_42_29": {
                "unexpected_write_intent": _write_group(
                    original_rows,
                    anomaly_kind="unexpected_write_intent",
                ),
                "expected_write_not_executed": _write_group(
                    original_rows,
                    anomaly_kind="expected_write_not_executed",
                ),
            },
            "post_fix_surfaced": {
                "expected_write_not_executed": _write_group(
                    surfaced_rows,
                    anomaly_kind="expected_write_not_executed",
                )
            },
            "actual_write_count": sum(bool(row["runtime_actual"]["actual_write"]) for row in rows),
            "legacy_fallback_count": sum(
                bool(row["runtime_actual"]["legacy_fallback_used"]) for row in rows
            ),
            "failed_closed_count": sum(
                row["runtime_actual"]["status"] == "failed_closed" for row in rows
            ),
            "local_status_counts": dict(Counter(row["local_status"] for row in rows)),
            "independent_closure_count": sum(bool(row["closure_eligibility"]) for row in rows),
        },
        "cases": rows,
    })


def _case_row(
    *,
    blind_turn: Any,
    actual_turn: Mapping[str, Any],
    label: Mapping[str, Any],
    actual: BlindActualArtifact,
    labels: SealedLabelStore,
) -> dict[str, Any]:
    adjudication = label.get("adjudication")
    adjudication = adjudication if isinstance(adjudication, Mapping) else {}
    provenance = label.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    historical_kind = str(adjudication.get("historical_anomaly_kind") or "unclassified")
    population = (
        "original"
        if bool(adjudication.get("present_in_before_replay", True))
        else "post_fix_surfaced"
    )
    mismatch_types = _mismatch_types(actual_turn, label, provenance)
    review_status = str(label.get("independent_review_status") or "pending")
    local_pass = not mismatch_types
    candidate_disposition = _non_executable_candidate_disposition(
        actual_turn,
        mismatch_types,
    )
    closure_eligible = review_status == "human_approved" and local_pass
    if candidate_disposition is not None:
        local_status = "machine_candidate_non_executable_pending_independent_review"
    elif local_pass and not closure_eligible:
        local_status = "machine_candidate_pass_pending_independent_review"
    elif local_pass:
        local_status = "independently_reviewed_pass"
    else:
        local_status = "open_runtime_or_semantic_mismatch"
    root_cause, fix_location = _root_cause(actual_turn, mismatch_types)
    runtime_actual = {
        "goal": actual_turn.get("goal"),
        "entities": list(actual_turn.get("entities") or []),
        "segments": list(actual_turn.get("segments") or []),
        "domain_ownership": dict(actual_turn.get("domain_ownership") or {}),
        "action_class": list(actual_turn.get("action_class") or []),
        "write_intent": bool(actual_turn.get("write_intent")),
        "actual_write": bool(actual_turn.get("actual_write")),
        "would_write": bool(actual_turn.get("would_write")),
        "clarification_requirement": bool(actual_turn.get("clarification_requirement")),
        "executable_status": actual_turn.get("executable_status"),
        "typed_commands": list(actual_turn.get("typed_commands") or []),
        "planning_blocks": list(actual_turn.get("planning_blocks") or []),
        "domain_results": list(actual_turn.get("domain_results") or []),
        "status": actual_turn.get("status"),
        "failed_stage": actual_turn.get("failed_stage"),
        "error_code": actual_turn.get("error_code"),
        "legacy_fallback_used": bool(actual_turn.get("legacy_fallback_used")),
    }
    return {
        "case_id": str(label["case_id"]),
        "turn_id": str(label["turn_id"]),
        "raw_text": blind_turn.raw_text,
        "blind_input_hash": str(actual_turn.get("input_hash") or ""),
        "historical_population": population,
        "historical_anomaly_kind": historical_kind,
        "risk_level": str(adjudication.get("historical_risk_level") or "unreviewed"),
        "sealed_expected": thaw_json_value({
            key: value
            for key, value in dict(label).items()
            if key not in {"case_id", "turn_id", "adjudication"}
        }),
        "runtime_actual": runtime_actual,
        "mismatch_type": mismatch_types or ["none"],
        "failed_stage": actual_turn.get("failed_stage"),
        "root_cause": root_cause,
        "fix_location": fix_location,
        "before_after": {
            "before": {
                "anomaly_kind": historical_kind,
                "root_cause_category": adjudication.get("historical_root_cause_category"),
                "old_closure_evidence_eligible": False,
            },
            "after": {
                "write_intent": runtime_actual["write_intent"],
                "actual_write": runtime_actual["actual_write"],
                "status": runtime_actual["status"],
                "action_class": runtime_actual["action_class"],
            },
        },
        "closure_evidence": {
            "input_pack_digest": actual.input_pack_digest,
            "runtime_version_hash": actual.runtime_version_hash,
            "actual_artifact_hash": actual.artifact_hash,
            "sealed_label_hash": labels.seal_hash,
            "actual_completed": actual.completed,
            "oracle_assisted_legacy_evidence_used": False,
        },
        "independent_review_status": review_status,
        "local_status": local_status,
        "candidate_disposition": candidate_disposition,
        "closure_eligibility": closure_eligible,
    }


def _mismatch_types(
    actual: Mapping[str, Any],
    label: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> list[str]:
    mismatches: list[str] = []
    if actual.get("status") == "failed_closed":
        mismatches.append("runtime_failed_closed")
    expected_write = bool(label.get("expected_write_intent"))
    actual_write_intent = bool(actual.get("write_intent"))
    if not expected_write and actual_write_intent:
        mismatches.append("unexpected_write_intent")
    elif expected_write and not actual_write_intent:
        mismatches.append("expected_write_intent_missing")
    if "expected_clarification_requirement" in label and bool(
        label.get("expected_clarification_requirement")
    ) != bool(actual.get("clarification_requirement")):
        mismatches.append("clarification_requirement_mismatch")

    coverage = str(provenance.get("action_coverage") or "exact")
    if coverage not in {"inconsistent", "coarse_legacy_command", "none"}:
        expected_actions = set(label.get("expected_action_class") or [])
        actual_actions = set(actual.get("action_class") or [])
        action_matches = (
            expected_actions.issubset(actual_actions)
            if coverage == "machine_candidate"
            else expected_actions == actual_actions
        )
        if not action_matches:
            mismatches.append("action_class_candidate_divergence")
    return mismatches


def _root_cause(
    actual: Mapping[str, Any],
    mismatches: list[str],
) -> tuple[str, list[str]]:
    if actual.get("status") == "failed_closed":
        return (
            f"runtime_failed_closed:{actual.get('failed_stage')}:{actual.get('error_code')}",
            ["app/agent2/runtime/harness.py", "app/agent2/semantic_interpreter_v3.py"],
        )
    block_reasons = [
        str(block.get("reason_code") or "")
        for block in actual.get("planning_blocks") or []
        if isinstance(block, Mapping)
    ]
    if "unsupported_phase1_copy_previous" in block_reasons:
        return (
            "copy intent recognized but no legal previous-report snapshot/typed copy contract is available",
            ["app/agent2/command_planner_v3.py", "app/agent2/runtime/context.py"],
        )
    if "target_not_found" in block_reasons:
        return (
            "requested mutation target is absent from the current legal snapshot",
            ["app/agent2/semantic_interpreter_v3.py", "app/agent2/command_planner_v3.py"],
        )
    receipt_reasons = [
        str(receipt.get("reason") or "")
        for result in actual.get("domain_results") or []
        if isinstance(result, Mapping)
        for receipt in result.get("command_results") or []
        if isinstance(receipt, Mapping)
    ]
    if "invalid_report_state" in receipt_reasons:
        return (
            "sealed write candidate targets a report that is already completed in the legal replay snapshot",
            ["app/agent2/evaluation/blind_pack_builder.py", "independent reviewer adjudication"],
        )
    if "unexpected_write_intent" in mismatches:
        return (
            "semantic interpretation projected a daily write without a sealed daily-write candidate",
            ["app/llm/prompts/cognitive_core_v3.md", "app/agent2/cognitive_contract_v3.py"],
        )
    if "expected_write_intent_missing" in mismatches:
        return (
            "expected write candidate did not reach an executable typed command",
            ["app/agent2/semantic_interpreter_v3.py", "app/agent2/command_planner_v3.py"],
        )
    if mismatches:
        return (
            "machine candidate and Runtime semantic output require independent adjudication",
            ["app/agent2/evaluation/ledger_report.py"],
        )
    return "no_machine_detected_runtime_mismatch", []


def _non_executable_candidate_disposition(
    actual: Mapping[str, Any],
    mismatches: list[str],
) -> str | None:
    """Classify machine write candidates that are illegal for the supplied resources.

    These candidates are not upgraded to Gold or independently closed. They are
    separated from local Runtime misses because executing them would violate the
    Phase-1 contract or the current legal snapshot.
    """

    if "expected_write_intent_missing" not in mismatches:
        return None
    block_reasons = {
        str(block.get("reason_code") or "")
        for block in actual.get("planning_blocks") or []
        if isinstance(block, Mapping)
    }
    if "unsupported_phase1_copy_previous" in block_reasons:
        return "phase1_contract_unsupported"
    if "target_not_found" in block_reasons:
        return "legal_resource_missing"
    receipt_reasons = {
        str(receipt.get("reason") or "")
        for result in actual.get("domain_results") or []
        if isinstance(result, Mapping)
        for receipt in result.get("command_results") or []
        if isinstance(receipt, Mapping)
    }
    if "invalid_report_state" in receipt_reasons:
        return "legal_state_disallows_mutation"
    return None


def _write_group(rows: list[dict[str, Any]], *, anomaly_kind: str) -> dict[str, int]:
    selected = [row for row in rows if row["historical_anomaly_kind"] == anomaly_kind]
    if anomaly_kind == "unexpected_write_intent":
        open_count = sum(bool(row["runtime_actual"]["write_intent"]) for row in selected)
        return {
            "total": len(selected),
            "machine_candidate_resolved": len(selected) - open_count,
            "open": open_count,
        }
    non_executable = sum(
        row.get("candidate_disposition") is not None for row in selected
    )
    resolved = sum(
        bool(row["runtime_actual"]["write_intent"]) for row in selected
    )
    open_count = len(selected) - resolved - non_executable
    return {
        "total": len(selected),
        "machine_candidate_resolved": resolved,
        "machine_candidate_non_executable": non_executable,
        "open": open_count,
    }
