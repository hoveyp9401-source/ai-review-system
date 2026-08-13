from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.agent2.periodic_report_context_loader import (
    ProductionPeriodicReportContextLoader,
)
from app.agent2.report_sql_executor import periodic_report_id


TENANT_ID = "tenant-a"
USER_ID = UUID("10000000-0000-4000-8000-000000000001")


class _Session:
    def __init__(self, row=None) -> None:
        self.row = row

    async def scalar(self, _statement):
        return self.row


@pytest.mark.asyncio
async def test_missing_current_weekly_report_is_exposed_as_server_bound_version_zero():
    context = await ProductionPeriodicReportContextLoader(_Session()).load_current_weekly(
        tenant_id=TENANT_ID,
        owner_user_id=USER_ID,
        local_date=date(2026, 8, 14),
    )

    assert context.report_id == periodic_report_id(
        TENANT_ID, str(USER_ID), "weekly", "2026-W33"
    )
    assert context.period_key == "2026-W33"
    assert context.version == 0
    assert context.status == "collecting"
    assert context.items == ()


@pytest.mark.asyncio
async def test_existing_current_weekly_report_preserves_stable_item_bindings():
    report_id = periodic_report_id(TENANT_ID, str(USER_ID), "weekly", "2026-W33")
    row = SimpleNamespace(
        tenant_id=TENANT_ID,
        owner_user_id=str(USER_ID),
        report_id=report_id,
        report_type="weekly",
        period_key="2026-W33",
        version=2,
        status="collecting",
        sections_json={"accomplishments": ["完成合同复核"]},
        item_ids_json={"accomplishments": ["weekly-item-1"]},
    )

    context = await ProductionPeriodicReportContextLoader(
        _Session(row)
    ).load_current_weekly(
        tenant_id=TENANT_ID,
        owner_user_id=USER_ID,
        local_date=date(2026, 8, 14),
    )

    assert context.report_id == report_id
    assert context.version == 2
    assert context.item("weekly-item-1").content == "完成合同复核"
