from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agent2.harness.case_loader import load_cases
from app.agent2.harness.reporter import write_reports
from app.agent2.harness.runner import run_cases


SEVERITY_ORDER = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "none": 0,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Agent2 offline evaluation harness.")
    parser.add_argument(
        "--cases",
        action="append",
        default=None,
        help="Case file or directory. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--output-dir",
        default="evals/agent2/reports",
        help="Directory for latest_results.jsonl, high_risk_cases.csv, and latest_summary.md.",
    )
    parser.add_argument(
        "--gate-mode",
        default="protective_gate",
        choices=["observe_only", "protective_gate", "strict_gate"],
        help="Gate mode used for evaluation.",
    )
    parser.add_argument(
        "--fail-on-severity",
        default="none",
        choices=["none", "critical", "high", "medium", "low"],
        help="Exit non-zero when an unexpected failure at or above this severity appears.",
    )
    args = parser.parse_args()

    case_paths = args.cases or ["evals/agent2/golden"]
    cases = load_cases(case_paths)
    if not cases:
        print("No harness cases found.", file=sys.stderr)
        return 2

    results = run_cases(cases, gate_mode=args.gate_mode)
    summary = write_reports(results, args.output_dir)
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    threshold = SEVERITY_ORDER[args.fail_on_severity]
    if threshold:
        for result in results:
            if (
                result.is_unexpected_failure
                and SEVERITY_ORDER.get(result.severity, 0) >= threshold
            ):
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
