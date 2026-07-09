from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent2.fact_contracts import FACT_CONTRACT_VERSION


@dataclass(frozen=True)
class FactInvariantViolation:
    rule: str
    severity: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "details": dict(self.details),
        }


def evaluate_fact_invariants(facts: dict[str, Any] | list[dict[str, Any]]) -> list[FactInvariantViolation]:
    rows = facts if isinstance(facts, list) else [facts]
    violations: list[FactInvariantViolation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        contract = row.get("fact_contract")
        if _looks_like_structured_case_fact(row) and not isinstance(contract, dict):
            violations.append(
                FactInvariantViolation(
                    rule="structured_case_fact_requires_fact_contract",
                    severity="error",
                    message="Structured case-table facts must carry a fact contract before rendering.",
                    details={"fact_keys": sorted(str(key) for key in row.keys())},
                )
            )
            continue
        if not isinstance(contract, dict):
            continue
        if contract.get("contract_version") != FACT_CONTRACT_VERSION:
            violations.append(
                FactInvariantViolation(
                    rule="fact_contract_version_missing_or_unknown",
                    severity="error",
                    message="Fact contract must declare the active fact contract version.",
                    details={"contract_version": contract.get("contract_version")},
                )
            )
        metric = contract.get("metric") if isinstance(contract.get("metric"), dict) else {}
        if bool(metric.get("sensitive")) and not isinstance(contract.get("permission"), dict):
            violations.append(
                FactInvariantViolation(
                    rule="sensitive_fact_requires_permission_contract",
                    severity="error",
                    message="Sensitive facts must include a permission contract, even before enforcement is enabled.",
                    details={"metric": metric.get("name")},
                )
            )
        permission = contract.get("permission") if isinstance(contract.get("permission"), dict) else {}
        if bool(metric.get("sensitive")) and permission.get("checked") is False:
            violations.append(
                FactInvariantViolation(
                    rule="sensitive_fact_permission_not_enforced",
                    severity="warning",
                    message="Sensitive fact permission is declared but not enforced yet.",
                    details={"metric": metric.get("name"), "policy": permission.get("policy")},
                )
            )
    return violations


def _looks_like_structured_case_fact(row: dict[str, Any]) -> bool:
    if row.get("source_type") == "case_table_rag":
        return True
    return (
        "case_count" in row
        or "groups" in row
        or row.get("metric_mode") == "defendant_monthly"
        or bool(row.get("case_name"))
    )
