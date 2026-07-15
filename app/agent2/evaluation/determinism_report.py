from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from app.agent2.runtime.blind import BlindActualArtifact


def build_determinism_report(
    first: BlindActualArtifact,
    second: BlindActualArtifact,
) -> dict[str, Any]:
    """Compare two completed Actual Artifacts without weakening exact equality."""

    first = BlindActualArtifact.from_mapping(first.as_mapping())
    second = BlindActualArtifact.from_mapping(second.as_mapping())
    blockers = _comparison_blockers(first, second)
    first_turns = _turn_index(first)
    second_turns = _turn_index(second)
    keys = sorted(set(first_turns) | set(second_turns))
    exact = 0
    missing_first = 0
    missing_second = 0
    field_counts: Counter[str] = Counter()
    path_counts: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    for key in keys:
        left = first_turns.get(key)
        right = second_turns.get(key)
        if left is None:
            missing_first += 1
            continue
        if right is None:
            missing_second += 1
            continue
        if left == right:
            exact += 1
            continue
        differing_fields = sorted(
            field for field in set(left) | set(right) if left.get(field) != right.get(field)
        )
        field_counts.update(differing_fields)
        paths = _difference_paths(left, right)
        path_counts.update(paths)
        if len(examples) < 25:
            examples.append(
                {
                    "case_id": key[0],
                    "turn_id": key[1],
                    "top_level_fields": differing_fields,
                    "difference_paths": paths[:50],
                }
            )
    different = len(keys) - exact - missing_first - missing_second
    first_safety = _safety_summary(first)
    second_safety = _safety_summary(second)
    safe_both = _safety_passed(first_safety) and _safety_passed(second_safety)
    comparable = not blockers
    deterministic = (
        comparable
        and first.artifact_hash == second.artifact_hash
        and different == 0
        and missing_first == 0
        and missing_second == 0
    )
    return {
        "schema_version": "agent2.runtime_determinism_report.v1",
        "comparable": comparable,
        "comparison_blockers": blockers,
        "determinism_passed": deterministic,
        "artifact_hash_equal": first.artifact_hash == second.artifact_hash,
        "identity": {
            "input_pack_id": first.input_pack_id,
            "input_pack_digest": first.input_pack_digest,
            "runtime_version_hash": first.runtime_version_hash,
            "run_id": first.run_id,
        },
        "first_artifact_hash": first.artifact_hash,
        "second_artifact_hash": second.artifact_hash,
        "turns": {
            "total": len(keys),
            "exact": exact,
            "different": different,
            "missing_from_first": missing_first,
            "missing_from_second": missing_second,
        },
        "top_level_field_difference_turn_counts": dict(sorted(field_counts.items())),
        "difference_path_counts": dict(sorted(path_counts.items())),
        "difference_examples": examples,
        "first_safety": first_safety,
        "second_safety": second_safety,
        "safety_envelope_stable": safe_both,
    }


def _comparison_blockers(
    first: BlindActualArtifact,
    second: BlindActualArtifact,
) -> list[str]:
    pairs = (
        ("input_pack_id_mismatch", first.input_pack_id, second.input_pack_id),
        ("input_pack_digest_mismatch", first.input_pack_digest, second.input_pack_digest),
        ("runtime_version_hash_mismatch", first.runtime_version_hash, second.runtime_version_hash),
    )
    blockers = [name for name, left, right in pairs if left != right]
    if not blockers and first.run_id != second.run_id:
        blockers.append("run_id_mismatch")
    return blockers


def _turn_index(artifact: BlindActualArtifact) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {
        (str(case.get("case_id") or ""), str(turn.get("turn_id") or "")): turn
        for case in artifact.cases
        for turn in case.get("turns") or []
    }


def _difference_paths(left: Any, right: Any, path: str = "") -> list[str]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        paths: list[str] = []
        for key in sorted(set(left) | set(right)):
            nested_path = f"{path}.{key}" if path else str(key)
            if key not in left or key not in right:
                paths.append(nested_path)
            else:
                paths.extend(_difference_paths(left[key], right[key], nested_path))
        return paths
    if _is_sequence(left) and _is_sequence(right):
        paths = []
        for index in range(max(len(left), len(right))):
            nested_path = f"{path}[{index}]"
            if index >= len(left) or index >= len(right):
                paths.append(nested_path)
            else:
                paths.extend(_difference_paths(left[index], right[index], nested_path))
        return paths
    return [] if left == right else [path or "$root"]


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _safety_summary(artifact: BlindActualArtifact) -> dict[str, int]:
    turns = [turn for case in artifact.cases for turn in case.get("turns") or []]
    summary = Counter(
        {
            "top_level_actual_write_count": 0,
            "domain_actual_write_count": 0,
            "receipt_actual_write_count": 0,
            "nested_audit_actual_write_count": 0,
            "legacy_fallback_count": 0,
            "command_receipt_mismatch_count": 0,
            "receipt_audit_flag_mismatch_count": 0,
        }
    )
    for turn in turns:
        summary["top_level_actual_write_count"] += bool(turn.get("actual_write"))
        summary["legacy_fallback_count"] += bool(turn.get("legacy_fallback_used"))
        typed_ids = [
            str(command.get("command_id") or "")
            for command in turn.get("typed_commands") or []
            if isinstance(command, Mapping)
        ]
        receipt_ids: list[str] = []
        for result in turn.get("domain_results") or []:
            if not isinstance(result, Mapping):
                continue
            summary["domain_actual_write_count"] += bool(result.get("actual_write"))
            for receipt in result.get("command_results") or []:
                if not isinstance(receipt, Mapping):
                    continue
                receipt_ids.append(
                    str((receipt.get("typed_command") or {}).get("command_id") or "")
                )
                summary["receipt_actual_write_count"] += bool(receipt.get("actual_write"))
                audit = receipt.get("audit")
                if isinstance(audit, Mapping):
                    summary["nested_audit_actual_write_count"] += bool(
                        audit.get("actual_write")
                    )
                    if any(
                        bool(audit.get(name)) != bool(receipt.get(name))
                        for name in ("actual_write", "would_write", "simulated")
                    ):
                        summary["receipt_audit_flag_mismatch_count"] += 1
        if typed_ids != receipt_ids:
            summary["command_receipt_mismatch_count"] += 1
    return dict(sorted(summary.items()))


def _safety_passed(summary: Mapping[str, int]) -> bool:
    return all(value == 0 for value in summary.values())
