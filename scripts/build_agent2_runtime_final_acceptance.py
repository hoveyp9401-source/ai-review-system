from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the final offline Agent2 Runtime Phase-1 acceptance decision."
    )
    parser.add_argument(
        "--closure-a",
        default="outputs/agent2_runtime_blind/ledger73/closure_report_final_a.json",
    )
    parser.add_argument(
        "--closure-b",
        default="outputs/agent2_runtime_blind/ledger73/closure_report_final_b.json",
    )
    parser.add_argument(
        "--determinism",
        default="outputs/agent2_runtime_blind/ledger73/determinism_report_final.json",
    )
    parser.add_argument(
        "--fuzz",
        default="outputs/agent2_runtime_adversarial/failure_clusters_post_review.json",
    )
    parser.add_argument(
        "--reviewer-packet",
        default="outputs/agent2_runtime_blind/independent_reviewer_packet.json",
    )
    parser.add_argument(
        "--corpus",
        default="outputs/agent2_runtime_corpus_manifest_v2.json",
    )
    parser.add_argument(
        "--db-smoke",
        default="outputs/agent2_isolated_db_smoke_evidence.json",
    )
    parser.add_argument(
        "--online-smoke",
        default="evals/agent2/runtime/phase1_acceptance/smoke_evidence.json",
    )
    parser.add_argument(
        "--regression",
        default="outputs/agent2_runtime_regression_classification_post_review.json",
    )
    parser.add_argument("--agent2-passed", type=int, required=True)
    parser.add_argument("--full-passed", type=int, required=True)
    parser.add_argument("--full-failed", type=int, required=True)
    parser.add_argument(
        "--output",
        default="outputs/agent2_runtime_final_acceptance_summary.json",
    )
    args = parser.parse_args()

    closure_a = _read(args.closure_a)
    closure_b = _read(args.closure_b)
    determinism = _read(args.determinism)
    fuzz = _read(args.fuzz)
    reviewer = _read(args.reviewer_packet)
    corpus = _read(args.corpus)
    db_smoke = _read(args.db_smoke)
    online_smoke = _read(args.online_smoke)
    regression = _read(args.regression)

    evidence_identity_aligned, evidence_identity = _evidence_identity(
        closure_a,
        closure_b,
        determinism,
        fuzz,
    )
    closure_summary = closure_a["summary"]
    fuzz_summary = fuzz["summary"]
    corpus_target = corpus["target"]
    online = online_smoke["online_read_only_smoke"]
    real_db = db_smoke["real_database_smoke"]
    safety_engineering_invariants = (
        closure_summary["actual_write_count"] == 0
        and closure_summary["legacy_fallback_count"] == 0
        and fuzz_summary["actual_write_count"] == 0
        and fuzz_summary["legacy_fallback_count"] == 0
        and fuzz_summary["unexpected_failed_closed_count"] == 0
        and determinism["safety_envelope_stable"] is True
    )
    safety_gate = (
        safety_engineering_invariants
        and fuzz_summary["unexpected_write_intent_count"] == 0
        and reviewer["human_approved_count"] == reviewer["record_count"]
    )
    semantic_gate = (
        reviewer["record_count"] > 0
        and reviewer["human_approved_count"] == reviewer["record_count"]
        and closure_a["independent_review_status"] == "complete"
        and closure_b["independent_review_status"] == "complete"
    )
    direct_runtime_failures = regression["classification_counts"]["direct_runtime_impact"]
    parity_gate = bool(corpus_target["complete"]) and direct_runtime_failures == 0
    operational_gate = (
        real_db["status"] == "passed"
        and online["status"] == "passed"
        and online["shadow_conversation_state_non_persistence_confirmed_online"]
        and online["shadow_business_write_zero_confirmed_online"]
        and online["shadow_live_physical_isolation_confirmed_online"]
        and online["runtime_kill_switch_confirmed_online"]
        and online["runtime_log_locator_confirmed_online"]
    )
    determinism_gate = bool(determinism["determinism_passed"])
    semantic_gates = {
        "safety": safety_gate,
        "semantic": semantic_gate,
        "parity": parity_gate,
        "operational": operational_gate,
        "determinism": determinism_gate,
    }
    gate_values, all_gates_passed, verdict = _final_decision(
        semantic_gates,
        evidence_identity_aligned=evidence_identity_aligned,
    )
    blockers = []
    if not evidence_identity_aligned:
        blockers.append(
            "Closure, determinism and fuzz evidence do not share the required Runtime/input/artifact identity links."
        )
    if not determinism_gate:
        blockers.append(
            f"Real-model determinism failed: {determinism['turns']['different']}/"
            f"{determinism['turns']['total']} turns differ for the same input and Runtime hash."
        )
    if reviewer["human_approved_count"] != reviewer["record_count"]:
        blockers.append(
            f"Independent semantic review is incomplete: {reviewer['human_approved_count']}/"
            f"{reviewer['record_count']} records are human-approved."
        )
    if fuzz_summary["unexpected_write_intent_count"]:
        blockers.append(
            f"Adversarial machine candidates contain {fuzz_summary['unexpected_write_intent_count']} "
            "unexpected write-intent cases requiring independent adjudication."
        )
    if not corpus_target["complete"]:
        blockers.append(
            f"The exact target corpus remains incomplete: required "
            f"{corpus_target['conversation_count']} conversations/"
            f"{corpus_target['turn_count']} turns."
        )
    if real_db["status"] != "passed":
        blockers.append("Real isolated PostgreSQL smoke is blocked; simulator evidence is not DB parity.")
    if online["status"] != "passed":
        blockers.append("Read-only online smoke found that the Phase-1 Runtime is not deployed or wired.")

    payload = {
        "schema_version": "agent2.runtime_final_acceptance.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "production_shadow_allowed": verdict == "GO_FOR_PRODUCTION_SHADOW",
        "live_allowed": False,
        "all_gates_passed": all_gates_passed,
        "evidence_identity_aligned": evidence_identity_aligned,
        "runtime_version_hash": determinism["identity"]["runtime_version_hash"],
        "input_pack_digest": determinism["identity"]["input_pack_digest"],
        "gates": {
            "evidence_identity": {
                "passed": evidence_identity_aligned,
                **evidence_identity,
            },
            "safety": {
                "passed": safety_gate,
                "engineering_invariants_passed": safety_engineering_invariants,
                "actual_write_count": closure_summary["actual_write_count"],
                "legacy_fallback_count": closure_summary["legacy_fallback_count"],
                "receipt_audit_mismatch_count": determinism["first_safety"][
                    "receipt_audit_flag_mismatch_count"
                ],
                "adversarial_unexpected_write_intent_count": fuzz_summary[
                    "unexpected_write_intent_count"
                ],
            },
            "semantic": {
                "passed": semantic_gate,
                "reviewer_records": reviewer["record_count"],
                "human_approved": reviewer["human_approved_count"],
                "independent_metrics_available": False,
                "ledger_a": closure_summary["original_42_29"],
                "ledger_b": closure_b["summary"]["original_42_29"],
                "independent_closure_count": closure_summary["independent_closure_count"],
            },
            "parity": {
                "passed": parity_gate,
                "corpus_complete": bool(corpus_target["complete"]),
                "corpus_manifest_entries": corpus["entry_count"],
                "direct_runtime_regression_failures": direct_runtime_failures,
                "agent2_first_layer_passed": args.agent2_passed,
                "full_repository_passed": args.full_passed,
                "full_repository_failed": args.full_failed,
            },
            "operational": {
                "passed": operational_gate,
                "isolated_db": real_db["status"],
                "online_read_only": online["status"],
                "production_database_accessed": db_smoke["production_database_accessed"],
                "online_http_methods": online_smoke["reproduction"]["http_methods_used_online"],
            },
            "determinism": {
                "passed": determinism_gate,
                "turns": determinism["turns"],
                "artifact_hash_equal": determinism["artifact_hash_equal"],
                "safety_envelope_stable": determinism["safety_envelope_stable"],
            },
        },
        "blockers": blockers,
        "evidence": {
            "closure_a": args.closure_a,
            "closure_b": args.closure_b,
            "determinism": args.determinism,
            "adversarial_fuzz": args.fuzz,
            "reviewer_packet": args.reviewer_packet,
            "corpus_manifest": args.corpus,
            "db_smoke": args.db_smoke,
            "online_smoke": args.online_smoke,
            "regression_classification": args.regression,
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "verdict": verdict,
                "gates": gate_values,
                "blocker_count": len(blockers),
            },
            sort_keys=True,
        )
    )
    return 0


def _final_decision(
    gates: dict[str, bool],
    *,
    evidence_identity_aligned: bool,
) -> tuple[dict[str, bool], bool, str]:
    gate_values = {"evidence_identity": evidence_identity_aligned, **gates}
    all_gates_passed = all(gate_values.values())
    verdict = "GO_FOR_PRODUCTION_SHADOW" if all_gates_passed else "NO_GO"
    return gate_values, all_gates_passed, verdict


def _evidence_identity(
    closure_a: dict,
    closure_b: dict,
    determinism: dict,
    fuzz: dict,
) -> tuple[bool, dict[str, int | bool]]:
    runtime_hashes = {
        closure_a["runtime_version_hash"],
        closure_b["runtime_version_hash"],
        determinism["identity"]["runtime_version_hash"],
        fuzz["runtime_version_hash"],
    }
    ledger_input_digests = {
        closure_a["input_pack_digest"],
        closure_b["input_pack_digest"],
        determinism["identity"]["input_pack_digest"],
    }
    closure_artifacts_match_determinism = (
        closure_a["actual_artifact_hash"] == determinism["first_artifact_hash"]
        and closure_b["actual_artifact_hash"] == determinism["second_artifact_hash"]
    )
    details: dict[str, int | bool] = {
        "runtime_hash_count": len(runtime_hashes),
        "ledger_input_digest_count": len(ledger_input_digests),
        "closure_artifacts_match_determinism": closure_artifacts_match_determinism,
        "fuzz_runtime_bound": fuzz["runtime_version_hash"]
        == determinism["identity"]["runtime_version_hash"],
    }
    aligned = (
        details["runtime_hash_count"] == 1
        and details["ledger_input_digest_count"] == 1
        and closure_artifacts_match_determinism
    )
    return aligned, details


def _read(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
