from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    label: str
    unit: str
    definition: str
    source_type: str = "case_table_rag"
    sensitive: bool = True
    allowed_aggregations: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "label": self.label,
            "unit": self.unit,
            "definition": self.definition,
            "source_type": self.source_type,
            "sensitive": self.sensitive,
            "allowed_aggregations": list(self.allowed_aggregations),
        }


CASE_TABLE_METRIC_DEFINITIONS: dict[str, MetricDefinition] = {
    "case_count": MetricDefinition(
        metric_id="case_count",
        label="\u6848\u4ef6\u6570\u91cf",
        unit="\u4ef6",
        definition="\u6765\u81ea\u6848\u4ef6\u5e95\u8868\u7d22\u5f15\u7684\u7b5b\u9009\u7edf\u8ba1\u3002",
        allowed_aggregations=("count",),
    ),
    "case_count_by_department": MetricDefinition(
        metric_id="case_count_by_department",
        label="\u6309\u56e2\u961f\u7edf\u8ba1\u6848\u4ef6\u6570\u91cf",
        unit="\u4ef6",
        definition="\u6765\u81ea\u6848\u4ef6\u5e95\u8868\u7d22\u5f15\uff0c\u6309\u6cd5\u52a1\u90e8\u95e8\u5206\u7ec4\u7edf\u8ba1\u6848\u4ef6\u6570\u91cf\u3002",
        allowed_aggregations=("count", "group_count"),
    ),
    "defendant_case_inventory_count": MetricDefinition(
        metric_id="defendant_case_inventory_count",
        label="\u88ab\u544a\u6848\u4ef6\u5b58\u91cf",
        unit="\u4ef6",
        definition=(
            "\u5b58\u91cf=\u767b\u8bb0\u65e5\u2264\u622a\u6b62\u65e5\uff0c"
            "\u4e14\uff08\u672a\u7ed3\u6848\u6216\u7ed3\u6848\u65e5>\u622a\u6b62\u65e5\uff09\u3002"
        ),
        allowed_aggregations=("count", "group_count"),
    ),
    "defendant_case_new_count": MetricDefinition(
        metric_id="defendant_case_new_count",
        label="\u88ab\u544a\u6848\u4ef6\u65b0\u589e",
        unit="\u4ef6",
        definition="\u65b0\u589e=\u767b\u8bb0\u65e5\u5728\u7edf\u8ba1\u671f\u5185\u3002",
        allowed_aggregations=("count", "group_count"),
    ),
    "defendant_case_inventory_and_new_count": MetricDefinition(
        metric_id="defendant_case_inventory_and_new_count",
        label="\u88ab\u544a\u6848\u4ef6\u5b58\u91cf/\u65b0\u589e",
        unit="\u4ef6",
        definition=(
            "\u5b58\u91cf=\u767b\u8bb0\u65e5\u2264\u622a\u6b62\u65e5\uff0c"
            "\u4e14\uff08\u672a\u7ed3\u6848\u6216\u7ed3\u6848\u65e5>\u622a\u6b62\u65e5\uff09\uff1b"
            "\u65b0\u589e=\u767b\u8bb0\u65e5\u5728\u7edf\u8ba1\u671f\u5185\u3002"
        ),
        allowed_aggregations=("count", "group_count"),
    ),
    "case_table_row": MetricDefinition(
        metric_id="case_table_row",
        label="\u6848\u4ef6\u5e95\u8868\u660e\u7ec6",
        unit="",
        definition="\u6765\u81ea\u6848\u4ef6\u5e95\u8868\u7684\u5355\u6761\u660e\u7ec6\u8bb0\u5f55\u3002",
        allowed_aggregations=(),
    ),
}


def get_metric_definition(metric_id: str) -> MetricDefinition:
    return CASE_TABLE_METRIC_DEFINITIONS.get(
        metric_id,
        MetricDefinition(
            metric_id=metric_id or "unknown",
            label=metric_id or "unknown",
            unit="",
            definition="",
            sensitive=True,
            allowed_aggregations=(),
        ),
    )
