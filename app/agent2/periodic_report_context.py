"""Trusted current-period report context for the Agent2 Tool-Call Core."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent2.report_domain import PERIODIC_REPORT_FIELDS


class TrustedPeriodicReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    item_id: str = Field(min_length=1, max_length=256)
    field: Literal["accomplishments", "risks", "next_plan", "metrics"]
    content: str = Field(min_length=1, max_length=4000)


class TrustedPeriodicReportContext(BaseModel):
    """One authenticated owner's exact current report, including virtual v0."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    owner_user_id: UUID
    report_id: UUID
    report_type: Literal["weekly", "monthly"]
    period_key: str = Field(min_length=1, max_length=16)
    version: int = Field(ge=0)
    status: Literal["collecting", "completed", "cancelled"]
    items: tuple[TrustedPeriodicReportItem, ...] = ()
    provenance: Literal["trusted_context"] = "trusted_context"

    @model_validator(mode="after")
    def item_ids_are_unique(self) -> TrustedPeriodicReportContext:
        ids = tuple(item.item_id for item in self.items)
        if len(ids) != len(set(ids)):
            raise ValueError("trusted periodic report item IDs must be unique")
        if any(item.field not in PERIODIC_REPORT_FIELDS for item in self.items):
            raise ValueError("trusted periodic report field is invalid")
        return self

    def item(self, item_id: str) -> TrustedPeriodicReportItem | None:
        return next((item for item in self.items if item.item_id == item_id), None)

    def safe_snapshot(self) -> dict[str, object]:
        sections: dict[str, list[dict[str, str]]] = {
            field_name: [] for field_name in PERIODIC_REPORT_FIELDS
        }
        for item in self.items:
            sections[item.field].append(
                {"item_id": item.item_id, "content": item.content}
            )
        return {
            "report_id": str(self.report_id),
            "report_type": self.report_type,
            "period_key": self.period_key,
            "version": self.version,
            "status": self.status,
            "sections": sections,
        }
