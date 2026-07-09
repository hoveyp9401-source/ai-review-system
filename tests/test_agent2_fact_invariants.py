from __future__ import annotations

from app.agent2.fact_contracts import FACT_CONTRACT_VERSION
from app.agent2.fact_invariants import evaluate_fact_invariants


def test_fact_invariants_require_contract_for_structured_case_fact():
    violations = evaluate_fact_invariants({"case_count": 2, "table_type": "defendant_case_table"})

    assert [violation.rule for violation in violations] == [
        "structured_case_fact_requires_fact_contract"
    ]


def test_fact_invariants_warn_when_sensitive_permission_is_declared_but_not_enforced():
    violations = evaluate_fact_invariants(
        {
            "case_count": 2,
            "fact_contract": {
                "contract_version": FACT_CONTRACT_VERSION,
                "metric": {"name": "case_count", "sensitive": True},
                "permission": {
                    "checked": False,
                    "policy": "reserved_for_permission_gate",
                },
            },
        }
    )

    assert [violation.rule for violation in violations] == [
        "sensitive_fact_permission_not_enforced"
    ]
    assert violations[0].severity == "warning"
