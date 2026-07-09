from __future__ import annotations

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.daily_execution_replay import replay_daily_execution_cases, write_daily_execution_reports
from app.agent2.dialogue_replay import load_dialogue_cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Agent2 daily execution replay on multi-turn dialogues.")
    parser.add_argument(
        "input",
        nargs="*",
        default=["evals/agent2/dialogues"],
        help="Dialogue JSONL file or directory. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--output-dir",
        default="evals/agent2/daily_execution_reports",
        help="Directory for execution replay reports.",
    )
    parser.add_argument(
        "--gate-mode",
        default="protective_gate",
        help="Gate mode used for replay.",
    )
    parser.add_argument(
        "--require-gray-ready",
        action="store_true",
        help="Exit non-zero unless the replay summary satisfies the limited gray-ready criteria.",
    )
    args = parser.parse_args()

    cases = load_dialogue_cases(args.input)
    if not cases:
        print("No dialogue cases found.", file=sys.stderr)
        return 2
    results = replay_daily_execution_cases(cases, mode=args.gate_mode)
    summary = write_daily_execution_reports(results, args.output_dir)
    print(
        "agent2 daily execution replay: "
        f"dialogues={summary['total_dialogues']} turns={summary['total_turns']} "
        f"failed={summary['failed_dialogues']} mismatches={summary['mismatch_count']} "
        f"risk_turns={summary['risk_turn_count']} gray_ready={summary['gray_ready']}"
    )
    if args.require_gray_ready and not summary["gray_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
