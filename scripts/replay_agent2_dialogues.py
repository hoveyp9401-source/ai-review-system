from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent2.dialogue_replay import (
    load_dialogue_cases,
    replay_dialogue_cases,
    write_dialogue_reports,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Agent2 multi-turn dialogue replay.")
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help="Dialogue JSONL file or directory. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--output-dir",
        default="evals/agent2/dialogue_reports",
        help="Directory for dialogue replay results and summaries.",
    )
    parser.add_argument(
        "--gate-mode",
        default="protective_gate",
        choices=["observe_only", "protective_gate", "strict_gate"],
        help="Gate mode used for dialogue replay.",
    )
    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="Exit non-zero when any turn expectation mismatches.",
    )
    args = parser.parse_args()

    cases = load_dialogue_cases(args.input)
    if not cases:
        print("No dialogue cases found.", file=sys.stderr)
        return 2

    results = replay_dialogue_cases(cases, mode=args.gate_mode)
    summary = write_dialogue_reports(results, args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if args.fail_on_mismatch and summary["mismatch_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
