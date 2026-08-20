from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from datetime import date
from typing import Any

from sqlalchemy import select, text

from app.db import AsyncSessionLocal, engine
from app.models import DailyReport


FIELDS = ("today_work", "problems", "tomorrow_plan")


def _items(report: DailyReport, field_name: str) -> list[str]:
    return [
        str(item).strip()
        for item in (getattr(report, field_name) or ())
        if str(item).strip()
    ]


def _shape(report: DailyReport) -> dict[str, Any]:
    values = {field: _items(report, field) for field in FIELDS}
    all_items = [item for field in FIELDS for item in values[field]]
    return {
        "report_date": report.report_date.isoformat(),
        "status": report.status,
        "fields": values,
        "field_counts": {
            field: len(values[field]) for field in FIELDS
        },
        "total_items": len(all_items),
        "total_characters": sum(len(item) for item in all_items),
        "longest_item_characters": max(
            (len(item) for item in all_items),
            default=0,
        ),
    }


def _select_samples(reports: list[DailyReport]) -> list[dict[str, Any]]:
    candidates: list[tuple[str, DailyReport]] = []

    def choose(label: str, key, predicate=lambda _report: True) -> None:
        matching = [report for report in reports if predicate(report)]
        if matching:
            candidates.append((label, max(matching, key=key)))

    choose(
        "longest_total",
        lambda report: _shape(report)["total_characters"],
    )
    choose(
        "most_items",
        lambda report: _shape(report)["total_items"],
    )
    choose(
        "most_tomorrow_plans",
        lambda report: len(_items(report, "tomorrow_plan")),
        lambda report: bool(_items(report, "tomorrow_plan")),
    )
    choose(
        "longest_single_item",
        lambda report: _shape(report)["longest_item_characters"],
    )
    choose(
        "work_without_plan",
        lambda report: len(_items(report, "today_work")),
        lambda report: bool(_items(report, "today_work"))
        and not _items(report, "tomorrow_plan"),
    )
    choose(
        "risk_and_plan",
        lambda report: _shape(report)["total_characters"],
        lambda report: bool(_items(report, "problems"))
        and bool(_items(report, "tomorrow_plan")),
    )
    choose(
        "same_matter_today_and_tomorrow",
        lambda report: _shape(report)["total_characters"],
        lambda report: bool(
            set(_items(report, "today_work"))
            & set(_items(report, "tomorrow_plan"))
        ),
    )

    by_user: dict[object, list[DailyReport]] = defaultdict(list)
    for report in reports:
        by_user[report.user_id].append(report)
    recurring: list[tuple[int, DailyReport, str]] = []
    for user_reports in by_user.values():
        occurrences: Counter[str] = Counter(
            item
            for report in user_reports
            for field in ("today_work", "tomorrow_plan")
            for item in _items(report, field)
        )
        repeated = [
            (count, item)
            for item, count in occurrences.items()
            if count >= 2
        ]
        if not repeated:
            continue
        count, item = max(repeated)
        report = max(
            (
                row
                for row in user_reports
                if item in _items(row, "today_work")
                or item in _items(row, "tomorrow_plan")
            ),
            key=lambda row: row.report_date,
        )
        recurring.append((count, report, item))
    recurring_items: dict[object, tuple[int, str]] = {}
    if recurring:
        count, report, item = max(recurring, key=lambda value: value[0])
        candidates.append(("recurring_across_days", report))
        recurring_items[report.id] = (count, item)

    selected: list[dict[str, Any]] = []
    seen: set[object] = set()
    for label, report in candidates:
        if report.id in seen:
            continue
        seen.add(report.id)
        sample = _shape(report)
        sample["sample_id"] = f"source-{len(selected) + 1:02d}"
        sample["selection_reason"] = label
        recurring_fact = recurring_items.get(report.id)
        if recurring_fact is not None:
            sample["recurring_evidence"] = {
                "occurrence_count": recurring_fact[0],
                "item": recurring_fact[1],
            }
        selected.append(sample)
    return selected


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=date.fromisoformat, required=True)
    parser.add_argument("--until", type=date.fromisoformat, required=True)
    args = parser.parse_args()
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            reports = list(
                (
                    await session.scalars(
                        select(DailyReport)
                        .where(
                            DailyReport.report_date >= args.since,
                            DailyReport.report_date <= args.until,
                        )
                        .order_by(
                            DailyReport.report_date,
                            DailyReport.id,
                        )
                    )
                ).all()
            )
    shapes = [_shape(report) for report in reports]
    payload = {
        "schema_version": "weekly-plan.daily-source-audit.v1",
        "range": {
            "since": args.since.isoformat(),
            "until": args.until.isoformat(),
        },
        "report_count": len(reports),
        "anonymous_user_count": len({report.user_id for report in reports}),
        "status_counts": dict(Counter(report.status for report in reports)),
        "field_item_totals": {
            field: sum(shape["field_counts"][field] for shape in shapes)
            for field in FIELDS
        },
        "reports_without_tomorrow_plan": sum(
            shape["field_counts"]["tomorrow_plan"] == 0
            for shape in shapes
        ),
        "reports_with_problems": sum(
            shape["field_counts"]["problems"] > 0
            for shape in shapes
        ),
        "samples": _select_samples(reports),
        "identity_fields_included": False,
        "database_writes": 0,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
