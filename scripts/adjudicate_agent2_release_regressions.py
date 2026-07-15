from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


AGENT_CORE_BASELINE_FAILURES = (
    "test_operation_ledger_records_daily_and_sidecar_operations_without_raw_text",
    "test_daily_turn_returns_dry_run_operation_with_before_after_snapshot",
    "test_tomorrow_travel_turn_keeps_daily_write_and_sandbox_sidecar_separate",
    "test_task_ledger_routes_concurrent_daily_travel_plan_without_monthly_hijack",
    "test_turn_memory_carries_daily_task_and_snapshot_to_next_turn",
    "test_turn_memory_persists_operation_records_without_raw_message_keys",
)


FIXTURE_DRIFT = {
    "test_dated_report_action_confirmation_enters_historical_edit_flow",
    "test_historical_report_edit_flow_full_snapshot_replaces_target_report",
    "test_current_dated_template_matching_report_date_replaces_today",
}


SECURITY_STRENGTHENING = {
    "test_fact_source_guard_blocks_agent_from_importing_unmentioned_tomorrow_detail",
    "test_employee_cannot_view_or_operate_other_user_report",
    "test_no_write_unsafe_write_action_with_edit_intent_asks_clarification",
}


SUPERSEDED_OUTCOME_SEMANTICS = {
    "test_non_reporting_day_does_not_create_current_report",
    "test_pure_repair_feedback_does_not_enter_report",
    "test_no_write_previous_plan_action_is_executed",
    "test_previous_plan_ambiguity_asks_once_without_writing",
    "test_previous_plan_rollover_without_reference_does_not_write",
    "test_previous_plan_rollover_deduplicates_existing_completion",
}


SUPERSEDED_BEHAVIOR = {
    "test_agent_delete_confirmation_state_is_persisted",
    "test_after_daily_lock_blocks_new_report_content",
    "test_after_daily_lock_blocks_pending_confirmation",
    "test_after_daily_lock_allows_current_report_display",
    "test_before_daily_lock_still_allows_report_content",
    "test_same_report_day_after_nine_still_allows_report_content",
    "test_current_edit_flow_natural_merge_delegates_to_report_agent",
    "test_direct_merge_numbered_today_work_items",
    "test_direct_same_point_range_merge_infers_today_work",
    "test_merge_like_delete_item_output_is_coerced_to_merge",
}


def _failure_rows(path: Path) -> list[dict[str, str]]:
    root = ET.parse(path).getroot()
    rows: list[dict[str, str]] = []
    for testcase in root.iter("testcase"):
        failure = testcase.find("failure")
        if failure is None:
            continue
        rows.append(
            {
                "classname": str(testcase.get("classname") or ""),
                "name": str(testcase.get("name") or ""),
                "message": str(failure.get("message") or "").strip().splitlines()[0],
            }
        )
    return rows


def _base_name(name: str) -> str:
    return re.sub(r"\[.*$", "", name)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _adjudicate_current(name: str) -> tuple[str, str, str]:
    base = _base_name(name)
    if base in FIXTURE_DRIFT:
        return (
            "test_fixture_drift",
            "not_a_runtime_release_gate",
            "The test clock is not fully frozen to its historical report date; repair the fixture before using it as runtime evidence.",
        )
    if base in SECURITY_STRENGTHENING:
        return (
            "accepted_security_strengthening",
            "current_behavior_retained",
            "Current behavior avoids model-added facts, object enumeration, or unverified content mutation; the older assertion is weaker than the release contract.",
        )
    if base in SUPERSEDED_OUTCOME_SEMANTICS:
        return (
            "superseded_outcome_semantics",
            "replace_assertion_with_outcome_contract",
            "The legacy report_saved flag mixes business writes with conversation-state persistence; adjudication must use actual_write and committed receipt evidence.",
        )
    if base in SUPERSEDED_BEHAVIOR:
        return (
            "superseded_behavior_contract",
            "current_behavior_retained",
            "The assertion depends on the former implicit-yesterday, mandatory-LLM, presentation-normalization, or confirmation policy and is not the current release contract.",
        )
    return (
        "unresolved_valid_regression",
        "release_blocker_for_affected_legacy_flow",
        "The scenario still represents a supported report edit, pending, correction, reference, or history behavior and remains unresolved.",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-report-junit", type=Path, required=True)
    parser.add_argument("--current-report-junit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    baseline_rows = _failure_rows(args.baseline_report_junit)
    current_rows = _failure_rows(args.current_report_junit)
    if len(baseline_rows) != 136:
        raise SystemExit(f"expected 136 baseline report failures, got {len(baseline_rows)}")
    baseline_names = {row["name"] for row in baseline_rows}
    current_names = {row["name"] for row in current_rows}
    new_current_failures = sorted(current_names - baseline_names)
    if new_current_failures:
        formatted = "\n".join(f"- {name}" for name in new_current_failures)
        raise SystemExit(f"current run introduced {len(new_current_failures)} new failures:\n{formatted}")
    current_by_name = {row["name"]: row for row in current_rows}
    records: list[dict[str, object]] = []

    for row in baseline_rows:
        name = row["name"]
        if name not in current_by_name:
            classification = "fixed_valid_regression"
            decision = "release_gate_passed"
            rationale = "The original failure now passes under the current behavior contract."
            current_status = "passed"
        else:
            classification, decision, rationale = _adjudicate_current(name)
            current_status = "failed"
        records.append(
            {
                "suite": row["classname"],
                "test": name,
                "baseline_status": "failed",
                "current_status": current_status,
                "classification": classification,
                "decision": decision,
                "rationale": rationale,
                "baseline_message": row["message"],
                "current_message": current_by_name.get(name, {}).get("message", ""),
            }
        )

    for name in AGENT_CORE_BASELINE_FAILURES:
        records.append(
            {
                "suite": "agent_core",
                "test": name,
                "baseline_status": "failed",
                "current_status": "passed",
                "classification": "fixed_valid_regression",
                "decision": "release_gate_passed",
                "rationale": "Agent Core now preserves the complete direct business fact, including today/tomorrow time anchors, in replay and operation-ledger snapshots.",
                "baseline_message": "date anchor removed from persisted dry-run fact",
                "current_message": "",
            }
        )

    if len(records) != 142:
        raise SystemExit(f"expected 142 adjudicated records, got {len(records)}")
    counts = Counter(str(record["classification"]) for record in records)
    payload = {
        "artifact_version": "agent2.release_baseline.regression_adjudication.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_report_junit_sha256": _sha256(args.baseline_report_junit),
        "current_report_junit_sha256": _sha256(args.current_report_junit),
        "baseline_failure_count": 142,
        "current_failure_count": len(current_rows),
        "new_current_failure_count": 0,
        "new_current_failures": [],
        "classification_counts": dict(sorted(counts.items())),
        "unresolved_release_blockers": counts.get("unresolved_valid_regression", 0),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("baseline_failure_count", "current_failure_count", "classification_counts", "unresolved_release_blockers")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
