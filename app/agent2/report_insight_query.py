from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


REPORT_INSIGHT_TOPIC = "daily_report_insight"
REPORT_INSIGHT_QUERY_KINDS = frozenset(
    {
        "report_count",
        "recent_work",
        "period_work",
        "recent_attention",
        "unclosed_work",
    }
)
REPORT_INSIGHT_SCOPE_TYPES = frozenset({"person", "organization"})
REPORT_INSIGHT_PERIOD_TYPES = frozenset(
    {"all_history", "recent_7_days", "current_week", "previous_week"}
)
REPORT_INSIGHT_STATUS_FILTERS = frozenset({"all_saved", "completed"})


@dataclass(frozen=True)
class StructuredReportInsightQuery:
    """Model-understood read request; it contains no database identity or authority."""

    query: str
    query_kind: str
    scope_type: str
    scope_name: str
    period_type: str
    status_filter: str = "all_saved"

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("report insight query text is required")
        if self.query_kind not in REPORT_INSIGHT_QUERY_KINDS:
            raise ValueError("unsupported report insight query kind")
        if self.scope_type not in REPORT_INSIGHT_SCOPE_TYPES:
            raise ValueError("unsupported report insight scope type")
        if not self.scope_name.strip():
            raise ValueError("report insight scope name is required")
        if self.period_type not in REPORT_INSIGHT_PERIOD_TYPES:
            raise ValueError("unsupported report insight period type")
        if self.status_filter not in REPORT_INSIGHT_STATUS_FILTERS:
            raise ValueError("unsupported report insight status filter")
        if self.query_kind == "report_count":
            if self.scope_type != "person" or self.period_type != "all_history":
                raise ValueError("report count requires one person over all history")
        elif self.status_filter != "all_saved":
            raise ValueError("status filter is only supported for report count")
        if self.query_kind == "recent_work":
            if self.scope_type != "person":
                raise ValueError("recent work requires a person scope")
            if self.period_type == "all_history":
                raise ValueError("recent work requires a bounded period")
        if self.query_kind in {"period_work", "recent_attention"} and self.scope_type != "organization":
            raise ValueError("organization insight requires an organization scope")
        if self.query_kind == "period_work" and self.period_type not in {
            "current_week",
            "previous_week",
        }:
            raise ValueError("period work requires current_week or previous_week")
        if self.query_kind == "recent_attention" and self.period_type != "recent_7_days":
            raise ValueError("recent attention requires recent_7_days")

    @classmethod
    def from_entity_payload(cls, entity: Mapping[str, Any]) -> "StructuredReportInsightQuery":
        if str(entity.get("entity_type") or "") != "knowledge_query":
            raise ValueError("report insight requires a knowledge_query entity")
        attributes = entity.get("attributes")
        if not isinstance(attributes, Mapping):
            raise ValueError("report insight query attributes are required")
        if str(attributes.get("topic") or "") != REPORT_INSIGHT_TOPIC:
            raise ValueError("knowledge query is not a report insight")
        return cls(
            query=str(attributes.get("query") or entity.get("value") or "").strip(),
            query_kind=str(attributes.get("query_kind") or "").strip(),
            scope_type=str(attributes.get("scope_type") or "").strip(),
            scope_name=str(attributes.get("scope_name") or "").strip(),
            period_type=str(attributes.get("period_type") or "").strip(),
            status_filter=str(attributes.get("status_filter") or "all_saved").strip(),
        )


def report_insight_query_from_command(command: Any) -> StructuredReportInsightQuery | None:
    if str(getattr(command, "command_type", "") or "") != "search_enterprise_knowledge":
        return None
    payload = getattr(command, "payload", None)
    entities = payload.get("entities") if isinstance(payload, Mapping) else None
    if not isinstance(entities, list) or len(entities) != 1 or not isinstance(entities[0], Mapping):
        return None
    attributes = entities[0].get("attributes")
    if not isinstance(attributes, Mapping) or str(attributes.get("topic") or "") != REPORT_INSIGHT_TOPIC:
        return None
    return StructuredReportInsightQuery.from_entity_payload(entities[0])

