from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent2.personal_weekly_brief import PersonalWeeklyBriefSnapshot
from app.agent2.personal_weekly_brief_scope import PersonalWeeklyBriefTarget
from app.agent2.personal_weekly_brief_service import PersonalWeeklyBriefSnapshotService
from app.agent2.personal_weekly_brief_store import PersonalWeeklyBriefRecord


NOW = datetime(2026, 8, 22, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET = PersonalWeeklyBriefTarget(
    tenant_id="tenant-a",
    internal_user_id="11111111-1111-4111-8111-111111111111",
    dingtalk_user_id="ding-user",
    display_name="脱敏用户",
    conversation_id="conversation-a",
)


def _snapshot() -> PersonalWeeklyBriefSnapshot:
    return PersonalWeeklyBriefSnapshot(
        tenant_id="tenant-a",
        owner_user_id=TARGET.internal_user_id,
        week_start=date(2026, 8, 17),
        week_end=date(2026, 8, 21),
        snapshot_at=NOW,
        daily_report_dates=(),
        weekly_plan_found=False,
        sources=(),
    )


class _Sources:
    def __init__(self) -> None:
        self.calls = 0

    async def load_snapshot(self, **_kwargs):
        self.calls += 1
        return _snapshot(), {"entries": []}


class _Store:
    def __init__(self, existing=None) -> None:
        self.existing = existing
        self.staged: list[PersonalWeeklyBriefRecord] = []

    async def load_by_owner_week(self, **_kwargs):
        return self.existing

    async def stage_snapshot(self, row):
        self.staged.append(row)
        self.existing = row
        return row


@pytest.mark.asyncio
async def test_repeated_saturday_schedule_reuses_first_snapshot_and_does_not_regenerate_source() -> None:
    source = _Sources()
    store = _Store()
    service = PersonalWeeklyBriefSnapshotService(store=store, source_loader=source)

    first = await service.stage_target(
        target=TARGET,
        week_start=date(2026, 8, 17),
        snapshot_at=NOW,
    )
    second = await service.stage_target(
        target=TARGET,
        week_start=date(2026, 8, 17),
        snapshot_at=NOW,
    )

    assert first == second
    assert source.calls == 1
    assert len(store.staged) == 1
    assert first.status == "snapshot_ready"
    assert first.source_fingerprint == _snapshot().fingerprint
    assert first.content_json == {}
