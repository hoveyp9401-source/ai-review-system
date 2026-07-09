from __future__ import annotations

import csv
import json
from pathlib import Path

from app.agent2.harness.judges import summarize_results
from app.agent2.harness.schemas import HarnessResult, HarnessRunSummary


def write_reports(results: list[HarnessResult], output_dir: str | Path) -> HarnessRunSummary:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    summary = summarize_results(results)
    _write_jsonl(results, path / "latest_results.jsonl")
    _write_high_risk_csv(results, path / "high_risk_cases.csv")
    _write_markdown(summary, results, path / "latest_summary.md")
    return summary


def _write_jsonl(results: list[HarnessResult], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def _write_high_risk_csv(results: list[HarnessResult], path: Path) -> None:
    high_risk = [
        result
        for result in results
        if not result.passed and result.severity in {"critical", "high"}
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "case_id",
                "source",
                "severity",
                "expected_failure",
                "tags",
                "primary_workflow",
                "matched_workflows",
                "effect_types",
                "gate_allow_legacy_daily",
                "failures",
            ],
        )
        writer.writeheader()
        for result in high_risk:
            actual = result.actual
            writer.writerow(
                {
                    "case_id": result.case_id,
                    "source": result.source,
                    "severity": result.severity,
                    "expected_failure": result.expected_failure,
                    "tags": ",".join(result.tags),
                    "primary_workflow": actual.primary_workflow if actual else "",
                    "matched_workflows": ",".join(actual.matched_workflows) if actual else "",
                    "effect_types": ",".join(actual.effect_types) if actual else "",
                    "gate_allow_legacy_daily": actual.gate_allow_legacy_daily if actual else "",
                    "failures": " | ".join(result.failures),
                }
            )


def _write_markdown(summary: HarnessRunSummary, results: list[HarnessResult], path: Path) -> None:
    unexpected = [result for result in results if result.is_unexpected_failure]
    expected_failed = [result for result in results if not result.passed and result.expected_failure]
    lines = [
        "# Agent2 Evaluation Harness Summary",
        "",
        "## Overview",
        "",
        f"- Total cases: {summary.total_cases}",
        f"- Passed: {summary.passed_cases}",
        f"- Failed: {summary.failed_cases}",
        f"- Unexpected failures: {summary.unexpected_failure_cases}",
        f"- Expected failures: {summary.expected_failure_cases}",
        "",
        "## Risk Counters",
        "",
        f"- Multi-intent failures: {summary.multi_intent_failure_count}",
        f"- Dangerous-action failures: {summary.dangerous_action_failure_count}",
        f"- Naked-confirmation failures: {summary.naked_confirmation_failure_count}",
        f"- Non-daily false allows: {summary.non_daily_false_allow_count}",
        f"- Daily false blocks: {summary.daily_false_block_count}",
        "",
        "## Legacy Adapter Status",
        "",
    ]
    if summary.legacy_adapter_status_counts:
        for status, count in sorted(summary.legacy_adapter_status_counts.items()):
            lines.append(f"- {status}: {count}")
    else:
        lines.append("- none")

    lines.extend([
        "",
        "## Failure By Severity",
        "",
    ])
    if summary.failure_by_severity:
        for severity, count in sorted(summary.failure_by_severity.items()):
            lines.append(f"- {severity}: {count}")
    else:
        lines.append("- none")

    lines.extend(["", "## Unexpected Failures", ""])
    lines.extend(_result_lines(unexpected[:30]))
    if len(unexpected) > 30:
        lines.append(f"- ... {len(unexpected) - 30} more")

    lines.extend(["", "## Expected Failures / Known Gaps", ""])
    lines.extend(_result_lines(expected_failed[:30]))
    if len(expected_failed) > 30:
        lines.append(f"- ... {len(expected_failed) - 30} more")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _result_lines(results: list[HarnessResult]) -> list[str]:
    if not results:
        return ["- none"]
    lines: list[str] = []
    for result in results:
        actual = result.actual
        primary = actual.primary_workflow if actual else ""
        gate = actual.gate_reply_type if actual else ""
        failures = "; ".join(result.failures)
        lines.append(
            f"- `{result.case_id}` [{result.severity}] primary={primary}, gate={gate}: {failures}"
        )
    return lines
