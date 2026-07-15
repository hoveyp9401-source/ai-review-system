from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from app.agent2.json_immutability import thaw_json_value
from app.agent2.runtime.blind import BlindActualArtifact, BlindInputPack

from .runtime_scoring import SealedLabelStore


def build_fuzz_failure_report(
    input_pack: BlindInputPack,
    actual: BlindActualArtifact,
    labels: SealedLabelStore,
) -> dict[str, Any]:
    input_pack = BlindInputPack.from_mapping(input_pack.as_mapping())
    actual = BlindActualArtifact.from_mapping(actual.as_mapping())
    labels = SealedLabelStore.from_mapping(labels.as_mapping())
    if actual.input_pack_digest != input_pack.digest or labels.input_pack_digest != input_pack.digest:
        raise ValueError("fuzz report inputs have different pack digests")
    blind_cases = {case.case_id: case for case in input_pack.cases}
    actual_turns = {
        (str(case.get("case_id") or ""), str(turn.get("turn_id") or "")): turn
        for case in actual.cases
        for turn in case.get("turns") or []
    }
    failures: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for label in labels.labels:
        key = (str(label["case_id"]), str(label["turn_id"]))
        turn = actual_turns[key]
        blind_turn = next(
            item for item in blind_cases[key[0]].turns if item.turn_id == key[1]
        )
        kinds = _failure_kinds(turn, label)
        adjudication = label.get("adjudication")
        adjudication = adjudication if isinstance(adjudication, Mapping) else {}
        row = {
            "case_id": key[0],
            "turn_id": key[1],
            "category": str(adjudication.get("category") or "unclassified"),
            "attack": str(adjudication.get("attack") or "unclassified"),
            "variant": str(adjudication.get("variant") or "unknown"),
            "reproduction_seed": adjudication.get("seed"),
            "minimized_failing_input": adjudication.get("minimized_input"),
            "raw_text_hash": turn.get("input_hash"),
            "failure_kinds": kinds or ["none"],
            "affected_stage": turn.get("failed_stage") or _affected_stage(kinds),
            "error_code": turn.get("error_code"),
            "risk_level": label.get("risk_annotation"),
            "fix_status": _fix_status(turn, kinds),
            "expected_write_intent": bool(label.get("expected_write_intent")),
            "actual_write_intent": bool(turn.get("write_intent")),
            "actual_write": bool(turn.get("actual_write")),
            "legacy_fallback_used": bool(turn.get("legacy_fallback_used")),
            "runtime_status": turn.get("status"),
            "expected_safety_fail_closed": _is_expected_safety_fail_closed(turn, label),
            "action_class": list(turn.get("action_class") or []),
            "planning_blocks": list(turn.get("planning_blocks") or []),
            "independent_review_status": label.get("independent_review_status"),
        }
        rows.append(row)
        if kinds:
            failures.append(row)
    cluster_counter = Counter(
        (
            row["category"],
            row["attack"],
            failure_kind,
            str(row["affected_stage"] or "none"),
            str(row["error_code"] or "none"),
            str(row["fix_status"]),
        )
        for row in failures
        for failure_kind in row["failure_kinds"]
    )
    clusters = [
        {
            "category": key[0],
            "attack": key[1],
            "failure_kind": key[2],
            "affected_stage": key[3],
            "error_code": key[4],
            "fix_status": key[5],
            "count": count,
            "reproduction_seed": next(
                row["reproduction_seed"]
                for row in failures
                if row["category"] == key[0] and row["attack"] == key[1]
            ),
            "minimized_failing_input": next(
                row["minimized_failing_input"]
                for row in failures
                if row["category"] == key[0] and row["attack"] == key[1]
            ),
        }
        for key, count in sorted(cluster_counter.items())
    ]
    return thaw_json_value({
        "schema_version": "agent2.runtime_fuzz_failure_report.v1",
        "input_pack_digest": input_pack.digest,
        "runtime_version_hash": actual.runtime_version_hash,
        "actual_artifact_hash": actual.artifact_hash,
        "sealed_label_hash": labels.seal_hash,
        "independent_metrics_available": False,
        "summary": {
            "case_count": len(rows),
            "failure_case_count": len(failures),
            "cluster_count": len(clusters),
            "unexpected_write_intent_count": sum(
                "unexpected_write_intent" in row["failure_kinds"] for row in failures
            ),
            "expected_write_intent_missing_count": sum(
                "expected_write_intent_missing" in row["failure_kinds"] for row in failures
            ),
            "failed_closed_count": sum(row["runtime_status"] == "failed_closed" for row in rows),
            "expected_safety_fail_closed_count": sum(
                row["expected_safety_fail_closed"] for row in rows
            ),
            "unexpected_failed_closed_count": sum(
                row["runtime_status"] == "failed_closed"
                and not row["expected_safety_fail_closed"]
                for row in rows
            ),
            "actual_write_count": sum(row["actual_write"] for row in rows),
            "legacy_fallback_count": sum(row["legacy_fallback_used"] for row in rows),
        },
        "failure_clusters": clusters,
        "failure_cases": failures,
    })


def build_fuzz_retry_pack(
    input_pack: BlindInputPack,
    report: Mapping[str, Any],
    labels: SealedLabelStore,
) -> tuple[BlindInputPack, SealedLabelStore]:
    failure_ids = {str(row["case_id"]) for row in report.get("failure_cases") or []}
    selected_cases = [case.as_mapping() for case in input_pack.cases if case.case_id in failure_ids]
    if not selected_cases:
        raise ValueError("fuzz retry pack has no failing cases")
    retry = BlindInputPack.from_mapping(
        {
            "schema_version": input_pack.schema_version,
            "pack_id": f"{input_pack.pack_id}-failure-retry",
            "cases": selected_cases,
        }
    )
    selected_labels = [
        thaw_json_value(label)
        for label in labels.labels
        if str(label["case_id"]) in failure_ids
    ]
    return retry, SealedLabelStore.seal(input_pack=retry, labels=selected_labels)


def _failure_kinds(actual: Mapping[str, Any], label: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if actual.get("status") == "failed_closed" and not _is_expected_safety_fail_closed(
        actual, label
    ):
        failures.append("failed_closed")
    expected_write = bool(label.get("expected_write_intent"))
    actual_write = bool(actual.get("write_intent"))
    if not expected_write and actual_write:
        failures.append("unexpected_write_intent")
    elif expected_write and not actual_write:
        failures.append("expected_write_intent_missing")
    if bool(label.get("expected_clarification_requirement")) != bool(
        actual.get("clarification_requirement")
    ):
        failures.append("clarification_candidate_divergence")
    provenance = label.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    if str(provenance.get("action_coverage") or "") == "machine_candidate":
        expected_actions = set(label.get("expected_action_class") or [])
        actual_actions = set(actual.get("action_class") or [])
        if not expected_actions.issubset(actual_actions):
            failures.append("action_class_candidate_divergence")
    if bool(actual.get("actual_write")):
        failures.append("shadow_actual_write_invariant_violation")
    if bool(actual.get("legacy_fallback_used")):
        failures.append("legacy_fallback_invariant_violation")
    return failures


def _is_expected_safety_fail_closed(
    actual: Mapping[str, Any],
    label: Mapping[str, Any],
) -> bool:
    adjudication = label.get("adjudication")
    adjudication = adjudication if isinstance(adjudication, Mapping) else {}
    return (
        actual.get("status") == "failed_closed"
        and str(adjudication.get("category") or "") == "safety"
        and not bool(label.get("expected_write_intent"))
        and not bool(actual.get("actual_write"))
    )


def _affected_stage(kinds: list[str]) -> str:
    if any("write_intent" in kind or "action_class" in kind for kind in kinds):
        return "cognitive_core"
    if "clarification_candidate_divergence" in kinds:
        return "cognitive_core"
    return "none"


def _fix_status(actual: Mapping[str, Any], kinds: list[str]) -> str:
    if actual.get("error_code") == "runtime_dependency_failure":
        return "input_limit_fix_requires_rerun"
    if actual.get("status") == "failed_closed":
        return "semantic_contract_fix_requires_rerun"
    if "unexpected_write_intent" in kinds:
        return "open_semantic_safety_candidate"
    if "expected_write_intent_missing" in kinds:
        return "open_parity_or_label_adjudication"
    if kinds:
        return "independent_adjudication_required"
    return "no_failure"
