from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.runtime.replay import (  # noqa: E402
    _expected_write,
    _missing_expected_write,
    _unexpected_write_intent,
    load_runtime_dialogue_cases,
)


TAPE_REQUIRED_FIELDS = {
    "tape_id",
    "input_text",
    "relevant_prior_turns",
    "expected_current_goal",
    "expected_entities",
    "expected_segment_boundaries",
    "expected_domain_ownership",
    "expected_action_class",
    "expected_executable",
    "expected_clarification_requirement",
    "expected_write_intent",
    "expected_command_type",
    "expected_no_write_reason",
    "risk_annotation",
    "annotation_source",
    "review_status",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Agent2 Runtime Phase 1 acceptance artifacts.")
    parser.add_argument(
        "--before-results",
        default="outputs/agent2_runtime_phase1_replay/results.jsonl",
    )
    parser.add_argument(
        "--after-results",
        default="outputs/agent2_runtime_phase1_acceptance_replay/results.jsonl",
    )
    parser.add_argument("--input-manifest", default="evals/agent2/runtime/phase1_inputs.json")
    parser.add_argument(
        "--semantic-tape",
        default="evals/agent2/runtime/phase1_semantic_tape_review_queue.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/agent2_runtime_phase1_acceptance",
    )
    parser.add_argument(
        "--smoke-evidence",
        default="evals/agent2/runtime/phase1_acceptance/smoke_evidence.json",
    )
    parser.add_argument(
        "--test-evidence",
        default="evals/agent2/runtime/phase1_acceptance/test_evidence.json",
    )
    parser.add_argument(
        "--corpus-search-evidence",
        default="evals/agent2/runtime/phase1_acceptance/corpus_search_evidence.json",
    )
    args = parser.parse_args()

    before_results = _read_jsonl(Path(args.before_results))
    after_results = _read_jsonl(Path(args.after_results))
    selection = json.loads(Path(args.input_manifest).read_text(encoding="utf-8"))
    cases, _ = load_runtime_dialogue_cases(selection["inputs"])
    texts = {
        (case.dialogue_id, turn.turn_id): turn.text
        for case in cases
        for turn in case.turns
    }
    ledger = build_case_ledger(before_results, after_results, texts=texts)
    tape_rows = _read_jsonl(Path(args.semantic_tape))
    tape_manifest = build_tape_manifest(Path(args.semantic_tape), tape_rows)
    corpus_search_path = Path(args.corpus_search_evidence)
    corpus_search_evidence = (
        json.loads(corpus_search_path.read_text(encoding="utf-8"))
        if corpus_search_path.exists()
        else None
    )
    corpus_manifest = build_corpus_manifest(
        roots=(
            Path("evals"),
            Path("outputs"),
            Path("data"),
            Path(".tmp_inputs"),
            Path("scripts"),
            Path("docs"),
            Path("fixtures"),
            Path("corpus"),
            Path("replay"),
        ),
        selection=selection,
        semantic_tape_manifest=tape_manifest,
        search_evidence=corpus_search_evidence,
    )
    smoke_path = Path(args.smoke_evidence)
    smoke_evidence = (
        json.loads(smoke_path.read_text(encoding="utf-8")) if smoke_path.exists() else None
    )
    test_path = Path(args.test_evidence)
    test_evidence = (
        json.loads(test_path.read_text(encoding="utf-8")) if test_path.exists() else None
    )
    before_summary = _summary_for_results(before_results)
    after_summary = _summary_for_results(after_results)
    acceptance_summary = build_acceptance_summary(
        ledger=ledger,
        before_summary=before_summary,
        after_summary=after_summary,
        tape_manifest=tape_manifest,
        corpus_manifest=corpus_manifest,
        smoke_evidence=smoke_evidence,
        test_evidence=test_evidence,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "case_ledger.jsonl", ledger)
    _write_json(output_dir / "corpus_manifest.json", corpus_manifest)
    _write_json(output_dir / "semantic_tape_manifest.json", tape_manifest)
    _write_json(output_dir / "acceptance_summary.json", acceptance_summary)
    print(
        "agent2 phase1 acceptance artifacts: "
        f"ledger={len(ledger)} before_unexpected={before_summary['unexpected_write_intent_count']} "
        f"after_unexpected={after_summary['unexpected_write_intent_count']} "
        f"before_missing={before_summary['missing_expected_write_count']} "
        f"after_missing={after_summary['missing_expected_write_count']} "
        f"verdict={acceptance_summary['verdict']}"
    )
    return 0


def build_case_ledger(
    before_results: list[dict[str, Any]],
    after_results: list[dict[str, Any]],
    *,
    texts: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    after_turns = _turn_index(after_results)
    prior_states = _prior_state_index(after_results)
    ledger: list[dict[str, Any]] = []
    for dialogue in before_results:
        dialogue_id = str(dialogue.get("dialogue_id") or "")
        for turn in dialogue.get("turns") or []:
            turn_id = str(turn.get("turn_id") or "")
            key = (dialogue_id, turn_id)
            after_turn = after_turns.get(key)
            if after_turn is None:
                raise ValueError(f"after replay is missing {dialogue_id}/{turn_id}")
            before_kind = _anomaly_kind(turn)
            after_kind = _anomaly_kind(after_turn)
            kind = before_kind or after_kind
            if kind is None:
                continue
            before_candidate = dict(turn.get("candidate") or {})
            after_candidate = dict(after_turn.get("candidate") or {})
            expected_write, expected_source = _expected_write(turn)
            root = _root_cause(kind, turn)
            after_still_open = after_kind == kind
            planning_blocks = list(after_candidate.get("planning_blocks") or [])
            ledger.append(
                {
                    "schema_version": "agent2.runtime_acceptance_case.v2",
                    "evidence_validity": "oracle_assisted_diagnostic_only",
                    "closure_claim_allowed": False,
                    "anomaly_kind": kind,
                    "present_in_before_replay": before_kind == kind,
                    "newly_surfaced_after_fix": before_kind is None and after_kind == kind,
                    "conversation_case_id": dialogue_id,
                    "turn_id": turn_id,
                    "user_input": texts.get(key, ""),
                    "prior_conversation_state": prior_states.get(key),
                    "current_goal": ((after_candidate.get("conversation_state") or {}).get("current_goal")),
                    "current_entities": ((after_candidate.get("decision") or {}).get("entities") or []),
                    "core_decision": after_candidate.get("decision"),
                    "planner_output": after_candidate.get("planner_output"),
                    "typed_commands_or_planning_blocks": {
                        "daily_commands": list(after_candidate.get("daily_commands") or []),
                        "business_commands": list(after_candidate.get("business_commands") or []),
                        "planning_blocks": planning_blocks,
                    },
                    "baseline_expectation": dict((turn.get("baseline") or {}).get("expected") or {}),
                    "runtime_expectation": {
                        "expected_write_intent": expected_write,
                        "expectation_source": expected_source,
                        "expected_fail_closed_if_unsupported": True,
                    },
                    "original_trigger_stage": {
                        "semantic_actions": list(before_candidate.get("actions") or []),
                        "planned_daily_commands": list(before_candidate.get("daily_commands") or []),
                        "would_write": bool(before_candidate.get("would_write")),
                        "diff_stages": sorted(
                            {
                                str(diff.get("stage") or "unknown")
                                for diff in turn.get("diffs") or []
                            }
                        ),
                    },
                    "root_cause_category": root["category"],
                    "root_cause": root["detail"],
                    "risk_level": root["risk"],
                    "fix_location": root["fix_location"],
                    "fix_after_result": {
                        "resolution_status": (
                            "oracle_assisted_open_contract_gap"
                            if after_still_open and planning_blocks
                            else "oracle_assisted_unverified"
                            if not after_still_open
                            else "oracle_assisted_open"
                        ),
                        "status": after_candidate.get("status"),
                        "would_write": bool(after_candidate.get("would_write")),
                        "actual_write": bool(after_candidate.get("actual_write")),
                        "planning_blocks": planning_blocks,
                        "daily_command_types": [
                            str(command.get("command_type") or "")
                            for command in after_candidate.get("daily_commands") or []
                        ],
                        "business_command_types": [
                            str(command.get("command_type") or "")
                            for command in after_candidate.get("business_commands") or []
                        ],
                        "remaining_same_anomaly": after_still_open,
                    },
                }
            )
    return ledger


def build_tape_manifest(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        missing = TAPE_REQUIRED_FIELDS - set(row)
        if missing:
            raise ValueError(f"semantic tape row {index} missing fields: {sorted(missing)}")
        tape_id = str(row["tape_id"])
        if not tape_id or tape_id in ids:
            raise ValueError(f"semantic tape contains duplicate/empty id: {tape_id!r}")
        ids.add(tape_id)
    review_counts = Counter(str(row.get("review_status") or "unknown") for row in rows)
    human_approved = review_counts.get("human_approved", 0)
    return {
        "schema_version": "agent2.semantic_tape_manifest.v1",
        "source_path": path.as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "record_count": len(rows),
        "review_status_counts": dict(sorted(review_counts.items())),
        "independent_from_runtime_core_outputs": True,
        "annotation_source": "machine_proposed_from_raw_text_and_phase1_contract",
        "human_approved_count": human_approved,
        "gold_set_ready": human_approved == len(rows) and bool(rows),
        "metrics_available": human_approved == len(rows) and bool(rows),
        "unavailable_metrics": (
            []
            if human_approved == len(rows) and rows
            else [
                "semantic_accuracy",
                "write_intent_precision",
                "write_intent_recall",
                "executable_action_precision",
                "clarification_precision",
                "domain_ownership_accuracy",
                "segmentation_accuracy",
                "high_risk_false_positives",
            ]
        ),
        "missing_reason": (
            None
            if human_approved == len(rows) and rows
            else "All candidate annotations require independent human review before scoring."
        ),
    }


def build_corpus_manifest(
    *,
    roots: Iterable[Path],
    selection: dict[str, Any],
    semantic_tape_manifest: dict[str, Any],
    search_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            entry = _dialogue_file_manifest(path)
            if entry is not None:
                entries.append(entry)
    entries.append(
        {
            "source_path": None,
            "corpus_type": "expected_server_runtime_replay_bundle",
            "conversation_count": 842,
            "turn_count": 5562,
            "hash": None,
            "generation_source": "production_server database history export",
            "expected_schema": "dialogue JSONL with raw text, prior state/resources, and scored expectations",
            "whether_independent": False,
            "whether_baseline_derived": False,
            "missing_reason": (
                "No exact 842-dialogue/5562-turn raw bundle or selection manifest was found by the "
                "recorded JSONL scan and the separately recorded corpus-search evidence."
            ),
            "recovery_command": (
                "PYTHONPATH=. venv/bin/python scripts/run_workflow_gate_replay.py --source all "
                "--mode protective_gate --days 60 --limit 10000 --include-text "
                "--output /tmp/agent2_phase1_server_history.json"
            ),
            "required_external_files": [
                "server-side raw replay export containing the exact 842/5562 selection",
                "selection manifest with source window/query and SHA-256 hashes",
            ],
        }
    )
    selection_paths = {str(value).replace("\\", "/") for value in selection.get("inputs") or []}
    selected_entries = [entry for entry in entries if entry.get("source_path") in selection_paths]
    return {
        "schema_version": "agent2.corpus_manifest.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": {"conversation_count": 842, "turn_count": 5562, "complete": False},
        "phase1_selection": {
            "source_path": "evals/agent2/runtime/phase1_inputs.json",
            "expected_conversation_count": int(selection.get("expected_dialogues") or 0),
            "expected_turn_count": int(selection.get("expected_turns") or 0),
            "manifest_entry_count": len(selected_entries),
            "complete_against_target": False,
        },
        "semantic_tape": semantic_tape_manifest,
        "search_evidence": search_evidence or {
            "status": "missing",
            "limitation": "No reproducible search evidence file was supplied.",
        },
        "entries": entries,
    }


def build_acceptance_summary(
    *,
    ledger: list[dict[str, Any]],
    before_summary: dict[str, Any],
    after_summary: dict[str, Any],
    tape_manifest: dict[str, Any],
    corpus_manifest: dict[str, Any],
    smoke_evidence: dict[str, Any] | None,
    test_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    root_counts = Counter(row["root_cause_category"] for row in ledger)
    resolution_counts = Counter(row["fix_after_result"]["resolution_status"] for row in ledger)
    historical_count = sum(bool(row["present_in_before_replay"]) for row in ledger)
    newly_surfaced_count = sum(bool(row["newly_surfaced_after_fix"]) for row in ledger)
    blockers = [
        "The 42/29 anomaly replay is oracle-assisted: baseline expectations compile the candidate semantic decision, so no closure count is acceptance-valid.",
        "The diagnostic replay still exposes 7 copy_previous misses because the Phase 1 typed daily contract has no copy command.",
        "Independent human review of the semantic tape is not complete; semantic metrics are unavailable.",
        "The exact 842-dialogue/5562-turn server corpus and manifest are missing locally.",
        "DB integration smoke is blocked because only the writable production database was discoverable.",
        "Online read-only smoke found that the Phase 1 Runtime Harness is not deployed or wired.",
    ]
    return {
        "schema_version": "agent2.runtime_phase1_acceptance.v2",
        "verdict": "NO_GO",
        "production_shadow_allowed": False,
        "live_allowed": False,
        "before_replay": before_summary,
        "after_replay": after_summary,
        "ledger": {
            "case_count": len(ledger),
            "historical_anomaly_count": historical_count,
            "newly_surfaced_after_fix_count": newly_surfaced_count,
            "root_cause_counts": dict(sorted(root_counts.items())),
            "resolution_counts": dict(sorted(resolution_counts.items())),
            "evidence_validity": "oracle_assisted_diagnostic_only",
            "closure_claim_allowed": False,
        },
        "semantic_tape": tape_manifest,
        "corpus_complete": bool(corpus_manifest["target"]["complete"]),
        "tests": {
            "agent2_and_first_layer": (
                (test_evidence or {}).get("agent2_and_first_layer", {}).get("result", "not_run")
            ),
            "full_repository": (
                (test_evidence or {}).get("full_repository", {}).get("result", "not_run")
            ),
            "evidence_path": (
                "evals/agent2/runtime/phase1_acceptance/test_evidence.json"
                if test_evidence is not None
                else None
            ),
        },
        "gates": {
            "safety": {
                "passed": False,
                "replay_acceptance_eligible": False,
                "replay_ineligibility_reason": after_summary["acceptance_ineligibility_reason"],
                "oracle_assisted_diagnostic_counts": {
                    "unexpected_high_risk_write_intent": after_summary["unexpected_write_intent_count"],
                    "legacy_fallback": after_summary["legacy_fallback_count"],
                    "typed_executor_bypass": after_summary["typed_executor_bypass_count"],
                    "simulator_actual_write_receipt": after_summary["candidate_actual_write_count"],
                },
                "production_db_write_measurement": "not_run_production_db_write_prohibited",
                "isolated_contract_test_status": (
                    (smoke_evidence or {}).get("executor_contract_smoke", {}).get("status", "not_run")
                ),
                "isolated_contract_test_coverage": (
                    (smoke_evidence or {}).get("executor_contract_smoke", {}).get("covered", {})
                ),
                "contract_evidence_path": (
                    "evals/agent2/runtime/phase1_acceptance/smoke_evidence.json"
                    if smoke_evidence is not None
                    else None
                ),
            },
            "semantic": {
                "passed": False,
                "human_reviewed_gold_set": bool(tape_manifest["gold_set_ready"]),
                "historical_anomaly_closure_verified": False,
                "oracle_assisted_diagnostic_unexpected_write_count": after_summary["unexpected_write_intent_count"],
                "oracle_assisted_diagnostic_expected_write_misses": after_summary["missing_expected_write_count"],
                "metrics_available": bool(tape_manifest["metrics_available"]),
            },
            "parity": {
                "passed": False,
                "legacy_daily_execution_baseline_mismatch_count": 0,
                "agent2_and_first_layer_passed": (
                    (test_evidence or {}).get("agent2_and_first_layer", {}).get("status") == "passed"
                ),
                "complete_corpus_executed": False,
                "baseline_derived_mismatch_count": after_summary["mismatch_count"],
            },
            "operational": {
                "passed": False,
                "db_smoke": (
                    (smoke_evidence or {}).get("database_discovery", {}).get("status", "not_run")
                ),
                "online_smoke": (
                    (smoke_evidence or {}).get("online_read_only_smoke", {}).get("status", "not_run")
                ),
                "evidence_path": (
                    "evals/agent2/runtime/phase1_acceptance/smoke_evidence.json"
                    if smoke_evidence is not None
                    else None
                ),
            },
        },
        "blockers": blockers,
    }


def _root_cause(kind: str, turn: dict[str, Any]) -> dict[str, Any]:
    baseline = dict(turn.get("baseline") or {})
    expected = dict(baseline.get("expected") or {})
    workflow = str(expected.get("primary_workflow") or "")
    operations = {
        str(command.get("operation") or "") for command in baseline.get("daily_commands") or []
    }
    if kind == "unexpected_write_intent" and workflow == "travel_coordination":
        return {
            "category": "cross_domain_travel_misinterpreted_as_daily_write",
            "detail": "Replay semantic adapter converted a legacy daily delta despite an explicit travel/no-daily label.",
            "risk": "high",
            "fix_location": ["app/agent2/runtime/replay.py"],
        }
    if kind == "unexpected_write_intent":
        return {
            "category": "case_progress_misinterpreted_as_daily_write",
            "detail": "Replay semantic adapter converted case progress/query content into capture_daily_event.",
            "risk": "high",
            "fix_location": [
                "app/agent2/runtime/replay.py",
                "app/agent2/command_planner_v3.py",
                "app/agent2/runtime/domains.py",
            ],
        }
    if "copy_previous" in operations:
        return {
            "category": "typed_contract_copy_previous_unsupported",
            "detail": "Legacy expected copy_previous, but Phase 1 has no typed copy command; the operation now produces an explicit PlanningBlock.",
            "risk": "high",
            "fix_location": [
                "app/agent2/runtime/replay.py",
                "app/agent2/command_planner_v3.py",
            ],
        }
    return {
        "category": "replay_delta_compiler_missing_merge",
        "detail": "A two-item-to-one-item report delta was not compiled as merge_daily_items even though the typed contract supports merge_items.",
        "risk": "medium",
        "fix_location": ["app/agent2/runtime/replay.py"],
    }


def _dialogue_file_manifest(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
        rows = [
            json.loads(line)
            for line in raw.decode("utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    dialogues = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("turns"), list)
        and (row.get("dialogue_id") or row.get("case_id"))
    ]
    if not dialogues:
        return None
    turn_count = sum(len(row["turns"]) for row in dialogues)
    text_turn_count = sum(
        1
        for row in dialogues
        for turn in row["turns"]
        if isinstance(turn, dict)
        and any(key in turn for key in ("text", "raw_text", "message_text", "content", "msg"))
    )
    result_like = text_turn_count == 0 or path.name in {"daily_execution_results.jsonl", "dialogue_results.jsonl", "results.jsonl"}
    normalized = path.as_posix()
    return {
        "source_path": normalized,
        "corpus_type": "replay_result_dialogue" if result_like else "source_dialogue",
        "conversation_count": len(dialogues),
        "turn_count": turn_count,
        "hash": hashlib.sha256(raw).hexdigest(),
        "generation_source": _generation_source(normalized),
        "expected_schema": (
            "result JSONL with dialogue_id and turn observations"
            if result_like
            else "source dialogue JSONL with dialogue_id, raw turn text, and expected"
        ),
        "whether_independent": False,
        "whether_baseline_derived": result_like,
        "missing_reason": None,
    }


def _generation_source(path: str) -> str:
    if "history_dialogues" in path:
        return "server history export"
    if "legal_daily_500" in path or "legal_daily_realistic" in path:
        return "GLM/generated legal corpus; reference only"
    if "random_generated" in path or "self_generated" in path:
        return "generated regression corpus"
    if "/dialogues/" in path:
        return "curated repository dialogue corpus"
    if "runtime_phase1" in path:
        return "Runtime Phase 1 replay output"
    return "historical local replay output"


def _summary_for_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    turns = [turn for dialogue in results for turn in dialogue.get("turns") or []]
    diagnostic_execution_invariants_passed = (
        not any(_unexpected_write_intent(turn) for turn in turns)
        and not any(bool((turn.get("candidate") or {}).get("actual_write")) for turn in turns)
        and not any(bool((turn.get("candidate") or {}).get("legacy_fallback_used")) for turn in turns)
        and not any(_typed_executor_bypass(turn) for turn in turns)
    )
    return {
        "dialogue_count": len(results),
        "turn_count": len(turns),
        "mismatch_count": sum(1 for turn in turns if turn.get("diffs")),
        "unexpected_write_intent_count": sum(_unexpected_write_intent(turn) for turn in turns),
        "missing_expected_write_count": sum(_missing_expected_write(turn) for turn in turns),
        "candidate_actual_write_count": sum(
            bool((turn.get("candidate") or {}).get("actual_write")) for turn in turns
        ),
        "legacy_fallback_count": sum(
            bool((turn.get("candidate") or {}).get("legacy_fallback_used")) for turn in turns
        ),
        "typed_executor_bypass_count": sum(_typed_executor_bypass(turn) for turn in turns),
        "diagnostic_execution_invariants_passed": diagnostic_execution_invariants_passed,
        "acceptance_eligible": False,
        "acceptance_ineligibility_reason": (
            "The recorded baseline compiles the semantic decision and is also the comparison oracle; "
            "diagnostic counts cannot prove semantic Safety or Parity."
        ),
        "safety_ready": False,
        "evaluation_scope": "baseline_derived_planner_executor_replay",
        "cognitive_semantic_independence": False,
    }


def _typed_executor_bypass(turn: dict[str, Any]) -> bool:
    candidate = dict(turn.get("candidate") or {})
    commands = [
        command
        for key in ("daily_commands", "business_commands")
        for command in candidate.get(key) or []
    ]
    receipts = [
        receipt
        for result in candidate.get("domain_results") or []
        for receipt in result.get("command_results") or []
    ]
    command_ids = [str(command.get("command_id") or "") for command in commands]
    receipt_ids = [str((receipt.get("typed_command") or {}).get("command_id") or "") for receipt in receipts]
    return bool(commands or receipts) and (
        not all(command_ids)
        or not all(receipt_ids)
        or command_ids != receipt_ids
    )


def _anomaly_kind(turn: dict[str, Any]) -> str | None:
    if _unexpected_write_intent(turn):
        return "unexpected_write_intent"
    if _missing_expected_write(turn):
        return "expected_write_not_executed"
    return None


def _turn_index(results: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(dialogue.get("dialogue_id") or ""), str(turn.get("turn_id") or "")): turn
        for dialogue in results
        for turn in dialogue.get("turns") or []
    }


def _prior_state_index(results: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any] | None]:
    index: dict[tuple[str, str], dict[str, Any] | None] = {}
    for dialogue in results:
        dialogue_id = str(dialogue.get("dialogue_id") or "")
        prior: dict[str, Any] | None = None
        for turn in dialogue.get("turns") or []:
            key = (dialogue_id, str(turn.get("turn_id") or ""))
            index[key] = prior
            candidate = dict(turn.get("candidate") or {})
            state = candidate.get("conversation_state")
            if isinstance(state, dict):
                prior = state
    return index


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
