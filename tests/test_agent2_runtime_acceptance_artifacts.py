from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from scripts.build_agent2_runtime_final_acceptance import (
    _evidence_identity,
    _final_decision,
)
from scripts.build_agent2_runtime_phase1_acceptance import TAPE_REQUIRED_FIELDS


ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "evals" / "agent2" / "runtime" / "phase1_acceptance"


def test_final_acceptance_never_goes_when_evidence_identity_is_misaligned():
    all_other_gates_pass = {
        "safety": True,
        "semantic": True,
        "parity": True,
        "operational": True,
        "determinism": True,
    }

    gate_values, all_passed, verdict = _final_decision(
        all_other_gates_pass,
        evidence_identity_aligned=False,
    )

    assert gate_values["evidence_identity"] is False
    assert all_passed is False
    assert verdict == "NO_GO"


def test_final_acceptance_identity_binds_fuzz_runtime_and_exact_closure_artifacts():
    closure_a = {
        "runtime_version_hash": "runtime-final",
        "input_pack_digest": "ledger-input",
        "actual_artifact_hash": "artifact-a",
    }
    closure_b = {
        "runtime_version_hash": "runtime-final",
        "input_pack_digest": "ledger-input",
        "actual_artifact_hash": "artifact-b",
    }
    determinism = {
        "identity": {
            "runtime_version_hash": "runtime-final",
            "input_pack_digest": "ledger-input",
        },
        "first_artifact_hash": "artifact-a",
        "second_artifact_hash": "artifact-b",
    }
    fuzz = {"runtime_version_hash": "runtime-final"}

    aligned, _ = _evidence_identity(closure_a, closure_b, determinism, fuzz)
    assert aligned is True

    stale_fuzz = deepcopy(fuzz)
    stale_fuzz["runtime_version_hash"] = "runtime-old"
    aligned, _ = _evidence_identity(closure_a, closure_b, determinism, stale_fuzz)
    assert aligned is False

    unrelated_closure = deepcopy(closure_b)
    unrelated_closure["actual_artifact_hash"] = "another-nondeterministic-run"
    aligned, _ = _evidence_identity(
        closure_a,
        unrelated_closure,
        determinism,
        fuzz,
    )
    assert aligned is False


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_phase1_case_ledger_keeps_oracle_assisted_anomalies_unverified():
    rows = _jsonl(ACCEPTANCE / "case_ledger.jsonl")
    historical = [row for row in rows if row["present_in_before_replay"]]
    historical_counts = Counter(row["anomaly_kind"] for row in historical)

    assert len(rows) == 73
    assert len(historical) == 71
    assert historical_counts == {
        "unexpected_write_intent": 42,
        "expected_write_not_executed": 29,
    }
    assert sum(row["newly_surfaced_after_fix"] for row in rows) == 2
    assert all(row["user_input"] for row in rows)
    assert all("unknown" not in row["root_cause_category"] for row in rows)
    assert all(row["evidence_validity"] == "oracle_assisted_diagnostic_only" for row in rows)
    assert all(row["closure_claim_allowed"] is False for row in rows)
    assert not any(
        row["fix_after_result"]["resolution_status"] == "closed" for row in rows
    )
    assert sum(
        row["fix_after_result"]["remaining_same_anomaly"]
        for row in rows
        if row["anomaly_kind"] == "unexpected_write_intent"
    ) == 0
    assert sum(
        row["fix_after_result"]["remaining_same_anomaly"]
        for row in rows
        if row["anomaly_kind"] == "expected_write_not_executed"
    ) == 7


def test_phase1_semantic_tape_is_complete_but_cannot_be_scored_before_human_review():
    tape = _jsonl(ROOT / "evals" / "agent2" / "runtime" / "phase1_semantic_tape_review_queue.jsonl")
    manifest = _json(ACCEPTANCE / "semantic_tape_manifest.json")

    assert len(tape) == 14
    assert all(TAPE_REQUIRED_FIELDS.issubset(row) for row in tape)
    assert all(row["review_status"] == "pending_human_review" for row in tape)
    assert all(row["annotation_source"].startswith("codex_machine_proposed") for row in tape)
    assert manifest["independent_from_runtime_core_outputs"] is True
    assert manifest["human_approved_count"] == 0
    assert manifest["gold_set_ready"] is False
    assert manifest["metrics_available"] is False
    assert "semantic_accuracy" in manifest["unavailable_metrics"]
    assert "high_risk_false_positives" in manifest["unavailable_metrics"]


def test_phase1_manifest_and_summary_keep_missing_corpus_and_no_go_explicit():
    corpus = _json(ACCEPTANCE / "corpus_manifest.json")
    summary = _json(ACCEPTANCE / "acceptance_summary.json")
    missing_target = [
        entry
        for entry in corpus["entries"]
        if entry["corpus_type"] == "expected_server_runtime_replay_bundle"
    ]

    assert corpus["target"] == {
        "complete": False,
        "conversation_count": 842,
        "turn_count": 5562,
    }
    assert len(missing_target) == 1
    assert missing_target[0]["source_path"] is None
    assert missing_target[0]["missing_reason"]
    assert missing_target[0]["recovery_command"]
    assert summary["before_replay"]["unexpected_write_intent_count"] == 42
    assert summary["before_replay"]["missing_expected_write_count"] == 29
    assert summary["after_replay"]["unexpected_write_intent_count"] == 0
    assert summary["after_replay"]["missing_expected_write_count"] == 7
    assert summary["after_replay"]["acceptance_eligible"] is False
    assert summary["after_replay"]["safety_ready"] is False
    assert summary["ledger"]["closure_claim_allowed"] is False
    assert summary["gates"]["safety"]["passed"] is False
    assert summary["gates"]["semantic"]["historical_anomaly_closure_verified"] is False
    assert summary["verdict"] == "NO_GO"
    assert summary["production_shadow_allowed"] is False
    assert summary["live_allowed"] is False


def test_phase1_search_and_smoke_evidence_are_reproducible_without_claiming_db_writes():
    corpus = _json(ACCEPTANCE / "corpus_manifest.json")
    smoke = _json(ACCEPTANCE / "smoke_evidence.json")
    search = corpus["search_evidence"]

    assert search["exact_842_dialogue_5562_turn_bundle_found"] is False
    assert {item["scope"] for item in search["searches"]} >= {
        "reachable git history",
        "workspace ZIP archives, including ignored output archives",
        "documented absolute path E:\\桌面\\测试反馈",
    }
    assert all(item["command"] and item["result"] for item in search["searches"])
    assert smoke["reproduction"]["commands"]
    assert smoke["reproduction"]["write_commands_executed"] is False
    assert smoke["safety_policy"]["production_database_write_performed"] is False
