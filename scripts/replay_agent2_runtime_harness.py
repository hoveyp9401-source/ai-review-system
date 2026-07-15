from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.daily_execution_replay import replay_daily_execution_cases  # noqa: E402
from app.agent2.runtime.replay import (  # noqa: E402
    finalize_runtime_input_manifest,
    load_runtime_dialogue_cases,
    replay_runtime_cases,
    write_runtime_replay_reports,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare the isolated Agent2 legacy baseline with Runtime Harness replay adapters."
    )
    parser.add_argument(
        "--input",
        nargs="+",
        default=None,
        help="Dialogue JSONL files or directories. Overrides --manifest when supplied.",
    )
    parser.add_argument(
        "--manifest",
        default="evals/agent2/runtime/phase1_inputs.json",
        help="Explicit replay input selection used when --input is omitted.",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/agent2_runtime_phase1_replay",
        help="Output directory for summary, JSONL, mismatch CSV, and input manifest.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional dialogue limit for a quick smoke run.")
    parser.add_argument(
        "--require-safety-gates",
        action="store_true",
        help="Exit non-zero unless unexpected write, legacy fallback, and typed-executor bypass are all zero.",
    )
    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="Exit non-zero when baseline/candidate orchestration differences remain.",
    )
    args = parser.parse_args()

    selection_config = None if args.input else _manifest_payload(args.manifest)
    selected_inputs = args.input or list(selection_config["inputs"])
    cases, manifest = load_runtime_dialogue_cases(selected_inputs)
    if selection_config is not None:
        _assert_manifest_counts(selection_config, cases)
        manifest.update(
            {
                "schema_version": selection_config.get("schema_version"),
                "selection_manifest": str(Path(args.manifest).resolve()),
                "selection_manifest_sha256": hashlib.sha256(
                    Path(args.manifest).read_bytes()
                ).hexdigest(),
                "excluded": list(selection_config.get("excluded") or []),
                "known_evidence_gap": selection_config.get("known_evidence_gap"),
            }
        )
    if args.limit > 0:
        cases = cases[: args.limit]
    manifest = finalize_runtime_input_manifest(
        manifest,
        cases,
        selection_limit=max(0, args.limit),
    )
    if not cases:
        print("No Runtime replay cases found.", file=sys.stderr)
        return 2
    baselines = replay_daily_execution_cases(cases)
    results = asyncio.run(replay_runtime_cases(cases, baselines=baselines))
    summary = write_runtime_replay_reports(results, args.output_dir, input_manifest=manifest)
    print(
        "agent2 runtime phase1 replay: "
        f"dialogues={summary['total_dialogues']} turns={summary['total_turns']} "
        f"mismatches={summary['mismatch_count']} "
        f"unexpected_write={summary['unexpected_write_count']} "
        f"missing_expected_write={summary['missing_expected_write_count']} "
        f"failed_closed={summary['failed_closed_count']} "
        f"legacy_fallback={summary['legacy_fallback_count']} "
        f"typed_executor_bypass={summary['typed_executor_bypass_count']} "
        f"safety_ready={summary['safety_ready']} "
        f"parity_ready={summary['parity_ready']} "
        f"scope={summary['evaluation_scope']}"
    )
    if args.require_safety_gates and not summary["safety_ready"]:
        return 1
    if args.fail_on_mismatch and summary["mismatch_count"]:
        return 1
    return 0


def _manifest_payload(path: str) -> dict:
    manifest_path = Path(path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    inputs = payload.get("inputs")
    if not isinstance(inputs, list) or not inputs or any(not str(item).strip() for item in inputs):
        raise ValueError("Runtime replay input manifest requires a non-empty inputs list")
    return {**payload, "inputs": [str(item) for item in inputs]}


def _assert_manifest_counts(payload: dict, cases: list) -> None:
    actual_dialogues = len(cases)
    actual_turns = sum(len(case.turns) for case in cases)
    expected_dialogues = int(payload.get("expected_dialogues") or 0)
    expected_turns = int(payload.get("expected_turns") or 0)
    if expected_dialogues and actual_dialogues != expected_dialogues:
        raise ValueError(
            f"Runtime replay manifest expected {expected_dialogues} dialogues, found {actual_dialogues}"
        )
    if expected_turns and actual_turns != expected_turns:
        raise ValueError(f"Runtime replay manifest expected {expected_turns} turns, found {actual_turns}")


if __name__ == "__main__":
    raise SystemExit(main())
