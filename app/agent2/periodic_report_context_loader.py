"""Production read adapter for current weekly/monthly Report context."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.agent2.business.models import PeriodicReport
from app.agent2.periodic_report_context import (
    TrustedPeriodicReportContext,
    TrustedPeriodicReportItem,
)
from app.agent2.report_domain import PERIODIC_REPORT_FIELDS, period_bounds
from app.agent2.report_sql_executor import periodic_report_id


class ProductionPeriodicReportContextLoader:
    def __init__(self, session) -> None:
        self._session = session

    async def load_current_weekly(
        self,
        *,
        tenant_id: str,
        owner_user_id,
        local_date: date,
    ) -> TrustedPeriodicReportContext:
        period_key, _, _ = period_bounds("weekly", local_date)
        row = await self._session.scalar(
            select(PeriodicReport).where(
                PeriodicReport.tenant_id == tenant_id,
                PeriodicReport.owner_user_id == str(owner_user_id),
                PeriodicReport.report_type == "weekly",
                PeriodicReport.period_key == period_key,
            )
        )
        if row is None:
            return TrustedPeriodicReportContext(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                report_id=periodic_report_id(
                    tenant_id,
                    str(owner_user_id),
                    "weekly",
                    period_key,
                ),
                report_type="weekly",
                period_key=period_key,
                version=0,
                status="collecting",
            )
        if (
            str(row.tenant_id) != tenant_id
            or str(row.owner_user_id) != str(owner_user_id)
            or str(row.report_type) != "weekly"
            or str(row.period_key) != period_key
        ):
            raise ValueError("periodic report query returned an untrusted row")
        items: list[TrustedPeriodicReportItem] = []
        sections = row.sections_json or {}
        item_ids = row.item_ids_json or {}
        for field_name in PERIODIC_REPORT_FIELDS:
            values = tuple(sections.get(field_name) or ())
            ids = tuple(item_ids.get(field_name) or ())
            if len(values) != len(ids):
                raise ValueError("periodic report item IDs do not match content")
            items.extend(
                TrustedPeriodicReportItem(
                    item_id=str(item_id),
                    field=field_name,
                    content=str(content),
                )
                for item_id, content in zip(ids, values, strict=True)
            )
        return TrustedPeriodicReportContext(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            report_id=row.report_id,
            report_type="weekly",
            period_key=period_key,
            version=row.version,
            status=row.status,
            items=tuple(items),
        )
