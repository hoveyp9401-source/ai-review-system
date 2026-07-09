from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent2.shadow_replay import (
    load_shadow_replay_records,
    replay_shadow_records,
    write_shadow_replay_reports,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Agent2 shadow replay on historical messages.")
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help="JSONL file or directory with historical messages. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--output-dir",
        default="evals/agent2/shadow_replay",
        help="Directory for shadow replay results and summaries.",
    )
    parser.add_argument(
        "--gate-mode",
        default="protective_gate",
        choices=["observe_only", "protective_gate", "strict_gate"],
        help="Gate mode used for shadow replay.",
    )
    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="Exit non-zero when records with expected outcomes produce mismatches.",
    )
    args = parser.parse_args()

    records = load_shadow_replay_records(args.input)
    if not records:
        print("No shadow replay records found.", file=sys.stderr)
        return 2

    results = replay_shadow_records(records, mode=args.gate_mode)
    summary = write_shadow_replay_reports(results, args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if args.fail_on_mismatch and summary["mismatch_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
