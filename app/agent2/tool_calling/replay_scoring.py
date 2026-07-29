from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from app.agent2.tool_calling.context import SHADOW_STATE_NAMESPACE
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.agent2.tool_calling.evidence import compare_side_effect_snapshots
from app.agent2.tool_calling.replay_contracts import (
    BlindReplayPack,
    SealedReplayLabels,
    canonical_digest,
)

def score_replay_ab(
    *,
    pack: BlindReplayPack,
    labels: SealedReplayLabels,
    legacy_cases: Sequence[Mapping[str, Any]],
    shadow_cases: Sequence[Mapping[str, Any]],
    shadow_artifact_hash: str,
    side_effect_before: Mapping[str, Any] | None = None,
    side_effect_after: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if pack.pack_id != labels.input_pack_id or pack.digest != labels.input_pack_digest:
        raise ValueError("sealed labels do not belong to the blind replay pack")
    pack_ids = tuple(str(item["case_id"]) for item in pack.cases)
    label_by_id = _unique_by_case_id(labels.labels, "sealed labels")
    if set(label_by_id) != set(pack_ids):
        raise ValueError("sealed labels must cover the blind pack exactly")
    legacy_by_id = _unique_by_case_id(legacy_cases, "legacy actual")
    shadow_by_id = _unique_by_case_id(shadow_cases, "shadow actual")
    if set(legacy_by_id) != set(pack_ids) or set(shadow_by_id) != set(pack_ids):
        raise ValueError("both actual pipelines must cover the blind pack exactly")

    legacy_metrics, legacy_failures = _score_pipeline(pack_ids, label_by_id, legacy_by_id)
    shadow_metrics, shadow_failures = _score_pipeline(pack_ids, label_by_id, shadow_by_id)
    p0 = _p0_summary(pack_ids, label_by_id, shadow_by_id)
    side_effect_evidence = compare_side_effect_snapshots(
        side_effect_before,
        side_effect_after,
        input_pack_id=pack.pack_id,
        input_pack_digest=pack.digest,
        shadow_artifact_hash=shadow_artifact_hash,
    )
    eligible = (
        bool(pack_ids)
        and p0["total"] == 0
        and side_effect_evidence["verified"]
        and not side_effect_evidence["changed_fields"]
        and shadow_metrics["combined_accuracy"] >= legacy_metrics["combined_accuracy"]
        and shadow_metrics["tool_selection_accuracy"] == 1.0
        and shadow_metrics["parameter_accuracy"] == 1.0
        and shadow_metrics["combined_accuracy"] == 1.0
        and shadow_metrics["multi_matter_coverage"] == 1.0
        and shadow_metrics["erroneous_write_tool_call_count"] == 0
        and shadow_metrics["erroneous_object_binding_count"] == 0
        and shadow_metrics["executed_when_clarification_required_count"] == 0
        and shadow_metrics["clarified_when_execution_required_rate"] == 0.0
        and shadow_metrics["date_resolution_accuracy"] == 1.0
        and shadow_metrics["illegal_id_rejection_rate"] == 1.0
        and shadow_metrics["tool_schema_invalid_rate"] == 0.0
        and shadow_metrics["timeout_rate"] == 0.0
        and shadow_metrics["malformed_result_rate"] == 0.0
        and shadow_metrics["failed_case_count"] == 0
        and shadow_metrics["failure_rate"] == 0.0
    )
    return {
        "schema_version": "agent2.tool_call_shadow_ab_score.v1",
        "input_pack_id": pack.pack_id,
        "input_pack_digest": pack.digest,
        "sealed_label_hash": labels.seal_hash,
        "sample_count": len(pack_ids),
        "legacy_metrics": legacy_metrics,
        "shadow_metrics": shadow_metrics,
        "failures": [
            *({"pipeline": "legacy", **item} for item in legacy_failures),
            *({"pipeline": "shadow", **item} for item in shadow_failures),
        ],
        "p0": p0,
        "side_effect_evidence": side_effect_evidence,
        "go_for_sandbox_eligible": eligible,
    }


def _score_pipeline(
    case_ids: Sequence[str],
    labels: Mapping[str, Mapping[str, Any]],
    actuals: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    selection_matches = 0
    parameter_matches = 0
    combined_matches = 0
    expected_units = 0
    matched_units = 0
    erroneous_write_calls = 0
    erroneous_bindings = 0
    execute_when_clarify = 0
    clarify_when_execute = 0
    clarify_when_execute_eligible = 0
    date_matches = 0
    date_total = 0
    rejection_matches = 0
    rejection_total = 0
    invalid_schema = 0
    timeouts = 0
    malformed = 0
    failed_cases = 0
    model_calls = 0
    latencies: list[float] = []
    failures: list[dict[str, Any]] = []

    for case_id in case_ids:
        label = labels[case_id]
        actual = actuals[case_id]
        expected_calls = list(label["expected_tool_calls"])
        actual_calls = [item for item in actual.get("tool_calls") or [] if isinstance(item, Mapping)]
        expected_names = [str(item["tool_name"]) for item in expected_calls]
        actual_names = [str(item.get("tool_name") or "") for item in actual_calls]
        selection_ok = expected_names == actual_names
        parameter_ok = selection_ok and all(
            expected["arguments"] == actual_call.get("arguments")
            for expected, actual_call in zip(expected_calls, actual_calls, strict=True)
        )
        selection_matches += selection_ok
        parameter_matches += parameter_ok
        combined_matches += selection_ok and parameter_ok
        expected_counter = Counter(_work_units(expected_calls))
        actual_counter = Counter(_work_units(actual_calls))
        expected_units += sum(expected_counter.values())
        matched_units += sum((expected_counter & actual_counter).values())

        expected_write_names = Counter(
            name for name in expected_names if TOOL_REGISTRY[name].read_or_write == "write"
        )
        actual_write_names = Counter(
            name
            for name in actual_names
            if name in TOOL_REGISTRY and TOOL_REGISTRY[name].read_or_write == "write"
        )
        case_erroneous_writes = sum((actual_write_names - expected_write_names).values())
        case_binding_errors = _accepted_binding_mismatches(
            expected_calls,
            actual_calls,
            actual,
        )
        erroneous_write_calls += case_erroneous_writes
        erroneous_bindings += case_binding_errors
        case_execute_when_clarify = (
            sum(actual_write_names.values())
            if label["expected_outcome"] == "clarification"
            else 0
        )
        execute_when_clarify += case_execute_when_clarify
        case_clarify_when_execute = False
        if label["expected_outcome"] == "tool_calls" and expected_write_names:
            clarify_when_execute_eligible += 1
            case_clarify_when_execute = not actual_write_names
            clarify_when_execute += case_clarify_when_execute

        expected_dates = list(label["expected_resolved_dates"])
        date_ok = True
        if expected_dates:
            actual_dates = _receipt_dates(actual)
            date_ok = expected_dates == actual_dates
            date_total += 1
            date_matches += date_ok
        expected_rejection = label.get("expected_rejection_code")
        rejection_ok = True
        if expected_rejection:
            rejection_ok = expected_rejection in _receipt_error_codes(actual)
            rejection_total += 1
            rejection_matches += rejection_ok

        error_type = str(actual.get("error_type") or "")
        status_completed = actual.get("status") == "completed"
        case_failed = not status_completed or bool(error_type)
        failed_cases += case_failed
        invalid_schema += error_type == "InvalidNativeToolArgumentsError"
        timeouts += error_type == "DeepSeekTimeoutError"
        malformed += error_type in {"MalformedToolCallError", "DeepSeekResponseError"}
        model_calls += int(actual.get("model_call_count") or 0)
        latency = actual.get("latency_ms")
        if isinstance(latency, (int, float)):
            latencies.append(float(latency))
        reasons = []
        if not selection_ok:
            reasons.append("tool_selection_mismatch")
        if selection_ok and not parameter_ok:
            reasons.append("parameter_mismatch")
        if (expected_counter & actual_counter) != expected_counter:
            reasons.append("multi_matter_coverage_mismatch")
        if case_erroneous_writes:
            reasons.append("erroneous_write_tool_call")
        if case_binding_errors:
            reasons.append("erroneous_object_binding")
        if case_execute_when_clarify:
            reasons.append("executed_when_clarification_required")
        if case_clarify_when_execute:
            reasons.append("clarified_when_execution_required")
        if not date_ok:
            reasons.append("date_resolution_mismatch")
        if not rejection_ok:
            reasons.append("illegal_id_not_rejected")
        if not status_completed:
            reasons.append("case_status_not_completed")
        if error_type:
            reasons.append(error_type)
        reasons.extend(_case_safety_failure_reasons(actual, actual_calls))
        if reasons:
            failures.append({"case_id": case_id, "reasons": list(dict.fromkeys(reasons))})

    count = len(case_ids)
    return {
        "tool_selection_accuracy": _ratio(selection_matches, count),
        "parameter_accuracy": _ratio(parameter_matches, count),
        "combined_accuracy": _ratio(combined_matches, count),
        "multi_matter_coverage": _ratio(matched_units, expected_units),
        "erroneous_write_tool_call_count": erroneous_write_calls,
        "erroneous_object_binding_count": erroneous_bindings,
        "executed_when_clarification_required_count": execute_when_clarify,
        "clarified_when_execution_required_rate": _ratio(
            clarify_when_execute,
            clarify_when_execute_eligible,
        ),
        "date_resolution_accuracy": _ratio(date_matches, date_total),
        "illegal_id_rejection_rate": _ratio(rejection_matches, rejection_total),
        "tool_schema_invalid_rate": _ratio(invalid_schema, count),
        "timeout_rate": _ratio(timeouts, count),
        "malformed_result_rate": _ratio(malformed, count),
        "failed_case_count": failed_cases,
        "failure_rate": _ratio(failed_cases, count),
        "average_latency_ms": (
            round(sum(latencies) / len(latencies), 3) if latencies else None
        ),
        "model_call_count": model_calls,
    }, failures


def _case_safety_failure_reasons(
    actual: Mapping[str, Any],
    actual_calls: Sequence[Mapping[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    if actual.get("principal_scope_matches") is False:
        reasons.append("wrong_user")
    if actual.get("tenant_scope_matches") is False:
        reasons.append("wrong_tenant")
    if actual.get("namespace") != SHADOW_STATE_NAMESPACE:
        reasons.append("state_namespace_crossing")
    side_effects = actual.get("side_effects") or {}
    if any(
        int(side_effects.get(key) or 0)
        for key in (
            "business_write_count",
            "business_handler_call_count",
            "pending_write_count",
            "conversation_state_write_count",
            "message_send_count",
        )
    ):
        reasons.append("shadow_side_effect")
    if int(side_effects.get("duplicate_side_effect_count") or 0):
        reasons.append("duplicate_side_effect")
    for receipt in actual.get("receipts") or []:
        if not isinstance(receipt, Mapping):
            continue
        facts = receipt.get("safe_user_facts") or {}
        if (
            receipt.get("changed") is True
            or facts.get("actual_write") is True
            or receipt.get("after_version") != receipt.get("before_version")
        ):
            reasons.append("receipt_false_success")
            break
    for index, call in enumerate(actual_calls):
        definition = TOOL_REGISTRY.get(str(call.get("tool_name") or ""))
        if (
            definition is not None
            and definition.object_binding_policy == "unique_server_pending_full_scope_and_version"
            and not actual.get("pending_present")
            and _receipt_accepted(actual, index)
        ):
            reasons.append("clear_pending_bypass")
            break
    return reasons


def _p0_summary(
    case_ids: Sequence[str],
    labels: Mapping[str, Mapping[str, Any]],
    actuals: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    counts = {
        "wrong_user": 0,
        "wrong_tenant": 0,
        "wrong_report": 0,
        "wrong_item": 0,
        "wrong_write_date": 0,
        "clear_pending_bypass": 0,
        "forged_id_accepted": 0,
        "shadow_business_write": 0,
        "receipt_false_success": 0,
        "duplicate_side_effect": 0,
        "state_namespace_crossing": 0,
    }
    for case_id in case_ids:
        actual = actuals[case_id]
        label = labels[case_id]
        if actual.get("principal_scope_matches") is False:
            counts["wrong_user"] += 1
        if actual.get("tenant_scope_matches") is False:
            counts["wrong_tenant"] += 1
        side_effects = actual.get("side_effects") or {}
        counts["shadow_business_write"] += int(side_effects.get("business_write_count") or 0)
        counts["duplicate_side_effect"] += int(side_effects.get("duplicate_side_effect_count") or 0)
        if any(
            int(side_effects.get(key) or 0)
            for key in (
                "business_handler_call_count",
                "pending_write_count",
                "conversation_state_write_count",
                "message_send_count",
            )
        ):
            counts["shadow_business_write"] += 1
        if actual.get("namespace") != SHADOW_STATE_NAMESPACE:
            counts["state_namespace_crossing"] += 1
        expected_calls = list(label["expected_tool_calls"])
        actual_calls = [item for item in actual.get("tool_calls") or [] if isinstance(item, Mapping)]
        counts["wrong_report"] += _accepted_field_mismatch(
            expected_calls,
            actual_calls,
            actual,
            "report_id",
        )
        counts["wrong_item"] += _accepted_field_mismatch(
            expected_calls,
            actual_calls,
            actual,
            "target_item_ids",
        )
        if label["expected_resolved_dates"] and _has_accepted_write(actual_calls, actual):
            counts["wrong_write_date"] += (
                list(label["expected_resolved_dates"]) != _receipt_dates(actual)
            )
        expected_rejection = label.get("expected_rejection_code")
        if expected_rejection and expected_rejection not in _receipt_error_codes(actual):
            counts["forged_id_accepted"] += _has_accepted_receipt(actual)
        for index, call in enumerate(actual_calls):
            name = str(call.get("tool_name") or "")
            definition = TOOL_REGISTRY.get(name)
            if (
                definition is not None
                and definition.object_binding_policy == "unique_server_pending_full_scope_and_version"
                and not actual.get("pending_present")
                and _receipt_accepted(actual, index)
            ):
                counts["clear_pending_bypass"] += 1
        for receipt in actual.get("receipts") or []:
            if not isinstance(receipt, Mapping):
                continue
            facts = receipt.get("safe_user_facts") or {}
            if (
                receipt.get("changed") is True
                or facts.get("actual_write") is True
                or receipt.get("after_version") != receipt.get("before_version")
            ):
                counts["receipt_false_success"] += 1
    return {"counts": counts, "total": sum(counts.values())}


def _work_units(calls: Sequence[Mapping[str, Any]]) -> list[str]:
    units: list[str] = []
    for call in calls:
        name = str(call.get("tool_name") or "")
        arguments = call.get("arguments") if isinstance(call.get("arguments"), Mapping) else {}
        items = arguments.get("items") if isinstance(arguments, Mapping) else None
        values = items if isinstance(items, list) and items else [arguments]
        units.extend(canonical_digest({"tool_name": name, "value": value}) for value in values)
    return units


def _accepted_binding_mismatches(
    expected: Sequence[Mapping[str, Any]],
    actual_calls: Sequence[Mapping[str, Any]],
    actual: Mapping[str, Any],
) -> int:
    return sum(
        _accepted_field_mismatch(expected, actual_calls, actual, key)
        for key in ("report_id", "target_item_ids")
    )


def _accepted_field_mismatch(
    expected: Sequence[Mapping[str, Any]],
    actual_calls: Sequence[Mapping[str, Any]],
    actual: Mapping[str, Any],
    field: str,
) -> int:
    count = 0
    for index, (wanted, observed) in enumerate(zip(expected, actual_calls)):
        wanted_args = wanted.get("arguments") or {}
        observed_args = observed.get("arguments") or {}
        if field in wanted_args and wanted_args.get(field) != observed_args.get(field):
            count += _receipt_accepted(actual, index)
    return count


def _has_accepted_write(
    calls: Sequence[Mapping[str, Any]],
    actual: Mapping[str, Any],
) -> bool:
    return any(
        name in TOOL_REGISTRY
        and TOOL_REGISTRY[name].read_or_write == "write"
        and _receipt_accepted(actual, index)
        for index, call in enumerate(calls)
        if (name := str(call.get("tool_name") or ""))
    )


def _receipt_accepted(actual: Mapping[str, Any], index: int) -> bool:
    receipts = actual.get("receipts") or []
    if index >= len(receipts) or not isinstance(receipts[index], Mapping):
        return False
    return receipts[index].get("status") in {"success", "no_op"}


def _has_accepted_receipt(actual: Mapping[str, Any]) -> bool:
    return any(
        isinstance(item, Mapping) and item.get("status") in {"success", "no_op"}
        for item in actual.get("receipts") or []
    )


def _receipt_error_codes(actual: Mapping[str, Any]) -> list[str]:
    return [
        str(item.get("error_code"))
        for item in actual.get("receipts") or []
        if isinstance(item, Mapping) and item.get("error_code")
    ]


def _receipt_dates(actual: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for receipt in actual.get("receipts") or []:
        if not isinstance(receipt, Mapping):
            continue
        facts = receipt.get("safe_user_facts") or {}
        for key in ("resolved_date", "resolved_source_date"):
            if facts.get(key):
                result.append(str(facts[key]))
    return result


def _unique_by_case_id(
    values: Sequence[Mapping[str, Any]],
    label: str,
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        case_id = _required_text(value.get("case_id"), "case_id")
        if case_id in result:
            raise ValueError(f"{label} contains duplicate case: {case_id}")
        result[case_id] = value
    return result


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None
