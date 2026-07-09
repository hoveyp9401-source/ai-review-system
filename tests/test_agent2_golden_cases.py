from pathlib import Path

from app.agent2.harness.case_loader import load_cases
from app.agent2.harness.judges import summarize_results
from app.agent2.harness.runner import run_cases


def test_agent2_golden_cases_are_runnable():
    cases = load_cases([Path("evals/agent2/golden")])
    results = run_cases(cases)
    summary = summarize_results(results)

    assert summary.total_cases >= 15
    assert summary.passed_cases >= 1
    assert summary.failed_cases >= summary.expected_failure_cases
    assert all(result.actual is not None for result in results)
