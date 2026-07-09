from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent2.metric_definitions import get_metric_definition


FACT_CONTRACT_VERSION = "fact_contract.v1"


@dataclass(frozen=True)
class FactContract:
    fact_type: str
    source_type: str
    source_id: str
    title: str
    scope: dict[str, Any] = field(default_factory=dict)
    metric: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    definition: str = ""
    value: Any = None
    unit: str = ""
    confidence: float = 0.0
    freshness: str = ""
    answerable: bool = True
    permission: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    contract_version: str = FACT_CONTRACT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "fact_type": self.fact_type,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "title": self.title,
            "scope": dict(self.scope),
            "metric": dict(self.metric),
            "source": dict(self.source),
            "definition": self.definition,
            "value": self.value,
            "unit": self.unit,
            "confidence": self.confidence,
            "freshness": self.freshness,
            "answerable": self.answerable,
            "permission": dict(self.permission),
            "warnings": list(self.warnings),
        }


def build_case_table_fact_contract(
    *,
    source_id: str,
    title: str,
    facts: dict[str, Any],
    confidence: float,
    freshness: str,
) -> dict[str, Any]:
    """Describe a case-table fact before it is rendered as an answer."""

    fact_type = _case_fact_type(facts)
    metric_kind = str(facts.get("metric_kind") or "").strip()
    metric_name = _metric_name(facts)
    metric_definition = get_metric_definition(metric_name)
    value = _value(facts)
    warnings = _warnings(facts)
    return FactContract(
        fact_type=fact_type,
        source_type="case_table_rag",
        source_id=source_id,
        title=title,
        scope={
            "department": str(facts.get("department") or ""),
            "assignee_name": str(facts.get("assignee_name") or ""),
            "table_type": str(facts.get("table_type") or ""),
            "table_label": str(facts.get("table_label") or ""),
            "status_filter": str(facts.get("status_filter") or ""),
            "group_by": str(facts.get("group_by") or ""),
        },
        metric={
            "name": metric_name,
            "kind": metric_kind,
            "definition_id": metric_definition.metric_id,
            "label": metric_definition.label,
            "sensitive": metric_definition.sensitive,
            "allowed_aggregations": list(metric_definition.allowed_aggregations),
            "period_type": str(facts.get("period_type") or ""),
            "period_start": str(facts.get("period_start") or ""),
            "period_end": str(facts.get("period_end") or facts.get("as_of_date") or ""),
            "period_label": str(facts.get("period_label") or ""),
        },
        source={
            "source_file": str(facts.get("source_file") or ""),
            "sheet_name": str(facts.get("sheet_name") or ""),
            "row_number": facts.get("row_number"),
            "index": "case_table_index",
            "requested_by_user_id": str(facts.get("requested_by_user_id") or ""),
            "requested_by_dingtalk_user_id": str(facts.get("requested_by_dingtalk_user_id") or ""),
        },
        definition=_definition(facts, metric_definition.definition),
        value=value,
        unit=metric_definition.unit if _has_numeric_or_group_value(value) else "",
        confidence=float(confidence or 0.0),
        freshness=str(freshness or ""),
        answerable="case_count" in facts or "groups" in facts or bool(facts.get("case_name")),
        permission={
            "checked": False,
            "policy": "reserved_for_permission_gate",
            "scope": "not_enforced_yet",
        },
        warnings=warnings,
    ).as_dict()


def _case_fact_type(facts: dict[str, Any]) -> str:
    if facts.get("metric_mode") == "defendant_monthly":
        if facts.get("group_by") == "department":
            return "case_metric_group"
        return "case_metric"
    if facts.get("group_by") == "department":
        return "case_count_group"
    if "case_count" in facts:
        return "case_count"
    return "case_table_row"


def _metric_name(facts: dict[str, Any]) -> str:
    metric_kind = str(facts.get("metric_kind") or "")
    if facts.get("metric_mode") == "defendant_monthly":
        if metric_kind == "new":
            return "defendant_case_new_count"
        if metric_kind == "inventory_and_new":
            return "defendant_case_inventory_and_new_count"
        return "defendant_case_inventory_count"
    if facts.get("group_by") == "department":
        return "case_count_by_department"
    if "case_count" in facts:
        return "case_count"
    return "case_table_row"


def _definition(facts: dict[str, Any], fallback: str) -> str:
    if facts.get("metric_mode") == "defendant_monthly":
        period_type = str(facts.get("period_type") or "month")
        period_unit = "\u81ea\u7136\u5b63\u5ea6" if period_type == "quarter" else "\u81ea\u7136\u6708"
        return fallback.replace("\u7edf\u8ba1\u671f", f"\u7edf\u8ba1{period_unit}")
    if facts.get("status_filter") == "unclosed" or bool(facts.get("unclosed_only")):
        return "\u672a\u7ed3\u6848=\u6848\u4ef6\u5e95\u8868\u4e2d\u672a\u6807\u8bb0\u7ed3\u6848\uff0c\u6216\u7ed3\u6848\u72b6\u6001\u5b57\u6bb5\u663e\u793a\u672a\u7ed3\u6848\u3002"
    return fallback or "\u6765\u81ea\u6848\u4ef6\u5e95\u8868\u7d22\u5f15\u7684\u7b5b\u9009\u7edf\u8ba1\u3002"


def _value(facts: dict[str, Any]) -> Any:
    if isinstance(facts.get("groups"), list):
        groups = [dict(item) for item in facts.get("groups") or [] if isinstance(item, dict)]
        total_count = _int_or_none(facts.get("case_count"))
        if total_count is None:
            total_count = sum(int(item.get("case_count") or 0) for item in groups)
        return {
            "total_count": total_count,
            "group_count": len(groups),
            "groups": groups,
        }
    if "case_count" in facts:
        try:
            return int(facts.get("case_count") or 0)
        except (TypeError, ValueError):
            return facts.get("case_count")
    return facts.get("case_name") or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _has_numeric_or_group_value(value: Any) -> bool:
    return isinstance(value, int) or (
        isinstance(value, dict)
        and (
            "total_count" in value
            or "group_count" in value
            or "groups" in value
        )
    )


def _warnings(facts: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not facts.get("source_file") and not facts.get("metric_mode"):
        warnings.append("source_file_missing")
    if not facts.get("as_of_date") and facts.get("metric_mode") == "defendant_monthly":
        warnings.append("as_of_date_missing")
    return warnings
