from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re


FAILURE_RE = re.compile(r"^FAILED\s+(.+)$", re.MULTILINE)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Classify full-repository failures by Agent2 Runtime impact."
    )
    parser.add_argument("--pytest-output", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    text = Path(args.pytest_output).read_text(encoding="utf-8", errors="replace")
    failures = FAILURE_RE.findall(text)
    rows = [_classify(node_id) for node_id in failures]
    counts = Counter(row["classification"] for row in rows)
    summary_match = re.search(
        r"(\d+) failed,\s+(\d+) passed"
        r"(?:,\s+\d+ skipped)?"
        r"(?:,\s+\d+ warnings?)?"
        r"\s+in\s+([0-9.]+)s",
        text,
    )
    payload = {
        "schema_version": "agent2.runtime_regression_classification.v1",
        "source": str(Path(args.pytest_output)),
        "pytest_summary": (
            {
                "failed": int(summary_match.group(1)),
                "passed": int(summary_match.group(2)),
                "seconds": float(summary_match.group(3)),
            }
            if summary_match
            else None
        ),
        "classification_counts": {
            "runtime_unrelated": counts["runtime_unrelated"],
            "potential_runtime_impact": counts["potential_runtime_impact"],
            "direct_runtime_impact": counts["direct_runtime_impact"],
            "test_environment": counts["test_environment"],
            "deprecated_legacy_contract": counts["deprecated_legacy_contract"],
            "real_regression": counts["real_regression"],
            "unknown": counts["unknown"],
        },
        "classification_basis": {
            "runtime_unrelated": "Legacy app.agent_core tests; Runtime architecture tests forbid that dependency.",
            "deprecated_legacy_contract": "DailyReportService/app.agent state-protocol tests outside the v3 Runtime dependency graph.",
            "direct_runtime_impact": "Any failure in Agent2 Runtime/typed-command acceptance suites.",
            "limitation": "Impact classification is architectural, not a claim that deprecated production behavior is healthy.",
        },
        "failures": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload["classification_counts"], sort_keys=True))
    return 0


def _classify(node_id: str) -> dict[str, str]:
    path = node_id.split("::", 1)[0].replace("\\", "/")
    if path.startswith("tests/test_agent_core_"):
        classification = "runtime_unrelated"
        reason = "app.agent_core is forbidden from the Agent2 Runtime dependency graph"
    elif path == "tests/test_report_agent_state_protocol.py":
        classification = "deprecated_legacy_contract"
        reason = "legacy DailyReportService/app.agent state protocol is not a v3 Runtime dependency"
    elif any(
        marker in path
        for marker in (
            "test_agent2_runtime",
            "test_agent2_typed",
            "test_agent2_isolated_db",
            "test_agent2_offline_shadow",
        )
    ):
        classification = "direct_runtime_impact"
        reason = "failure belongs to a Runtime acceptance or typed execution suite"
    else:
        classification = "unknown"
        reason = "no reviewed impact rule matched"
    return {"node_id": node_id, "classification": classification, "reason": reason}


if __name__ == "__main__":
    raise SystemExit(main())
