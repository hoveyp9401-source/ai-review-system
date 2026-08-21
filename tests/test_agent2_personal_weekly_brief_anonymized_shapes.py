from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo

from app.agent2.personal_weekly_brief_sources import (
    build_personal_weekly_brief_snapshot,
)


FIXTURE = Path(__file__).parent / "fixtures" / "personal_weekly_brief_anonymized_cases.json"
USER_ID = "11111111-1111-4111-8111-111111111111"


def test_recent_daily_shapes_are_anonymized_and_preserved_as_traceable_sources() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    reports = []
    for item in cases:
        reports.append(
            SimpleNamespace(
                id=uuid5(NAMESPACE_URL, item["case_id"]),
                user_id=UUID(USER_ID),
                report_date=date.fromisoformat(item["report_date"]),
                created_at=datetime.fromisoformat(item["created_at"]),
                updated_at=datetime.fromisoformat(item["created_at"]),
                status="collecting",
                today_work=list(item["today_work"]),
                problems=list(item["problems"]),
                tomorrow_plan=list(item["tomorrow_plan"]),
            )
        )

    # The second August 19 snapshot represents a later saved supplement; the
    # database's unique owner/date row exposes the latest complete saved state.
    latest_by_date = {report.report_date: report for report in reports}
    snapshot = build_personal_weekly_brief_snapshot(
        tenant_id="tenant-a",
        owner_user_id=USER_ID,
        week_start=date(2026, 8, 17),
        snapshot_at=datetime(2026, 8, 22, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        daily_reports=tuple(latest_by_date.values()),
        weekly_plan=None,
    )

    originals = [source.original_text for source in snapshot.sources]
    assert "完成甲项目合同复核。" in originals
    assert any("付款审批将无法按原计划发起" in value for value in originals)
    assert "与法院沟通后得知仍需补充一份送达证明。" in originals
    assert any("120万元" in value and "没有承诺付款" in value for value in originals)
    assert any("只有补充材料齐全后" in value for value in originals)
    assert snapshot.daily_report_dates == tuple(
        date(2026, 8, day) for day in range(17, 21)
    )
    assert all(source.source_record_id for source in snapshot.sources)


def test_fixture_contains_no_real_names_or_identifiers() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")

    assert all(marker not in raw for marker in ("赵卫中", "朱佳佳", "丁益明", "薛旭"))
    assert "dingtalk" not in raw.lower()
    assert "employee_no" not in raw
