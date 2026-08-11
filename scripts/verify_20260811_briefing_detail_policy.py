from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date

from sqlalchemy import text

from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.services.management_daily_briefing import ManagementDailyBriefingService


def _missing_section(message: str) -> str:
    marker = "**二、未交人员**"
    if marker not in message:
        return ""
    remainder = message.split(marker, 1)[1]
    return remainder.split("────────────", 1)[0]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-date", type=date.fromisoformat, required=True)
    parser.add_argument("--expected-total", type=int, default=74)
    parser.add_argument("--observe-only", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        payload = await ManagementDailyBriefingService(settings).build(
            session,
            args.report_date,
        )
        await session.rollback()

    department = dict(payload.get("department_message") or {})
    message = str(department.get("text") or "")
    missing_section = _missing_section(message)
    snapshot = dict(department.get("briefing_snapshot") or {})
    members = list(snapshot.get("members") or [])
    zhao_matches = [
        dict(member)
        for member in members
        if str(member.get("member_name") or "") == "赵卫中"
    ]
    zhao = zhao_matches[0] if len(zhao_matches) == 1 else None
    stats = dict(department.get("stats") or {})
    zhao_is_missing = bool(zhao and zhao.get("classification") == "missing")
    zhao_visible_in_missing_detail = "赵卫中" in missing_section
    center_members = [
        dict(member)
        for member in members
        if zhao is not None and member.get("team_ref") == zhao.get("team_ref")
    ]
    center_completed = sum(
        member.get("classification") in {"submitted", "pending_confirmation"}
        for member in center_members
    )
    center_missing = sum(
        member.get("classification") == "missing"
        for member in center_members
    )
    expected_center_summary = (
        f"**中心直属**\n   已交 {center_completed}/2｜未交 {center_missing}"
    )
    expected_center_missing_heading = f"**中心直属（{center_missing}人）**"
    other_missing_names = sorted(
        str(member.get("member_name") or "")
        for member in members
        if member.get("classification") == "missing"
        and str(member.get("member_name") or "") != "赵卫中"
    )
    other_missing_names_absent = [
        name for name in other_missing_names if name not in missing_section
    ]
    total_ok = stats.get("total") == args.expected_total
    center_direct_ok = stats.get("center_direct_members") == 2
    totals_consistent = (
        int(stats.get("completed") or 0)
        + int(stats.get("missing") or 0)
        + int(stats.get("unknown_responsibility") or 0)
        == int(stats.get("total") or 0)
    )
    center_text_ok = (
        len(center_members) == 2
        and expected_center_summary in message
        and expected_center_missing_heading in missing_section
    )
    hidden_ok = zhao_is_missing and not zhao_visible_in_missing_detail
    policy_ok = (
        len(zhao_matches) == 1
        and total_ok
        and center_direct_ok
        and center_text_ok
        and totals_consistent
        and not other_missing_names_absent
        and hidden_ok
    )
    output = {
        "report_date": args.report_date.isoformat(),
        "department_recipients": [
            recipient.get("name")
            for recipient in department.get("recipients", [])
        ],
        "stats": stats,
        "zhao_snapshot_match_count": len(zhao_matches),
        "zhao_snapshot": zhao,
        "zhao_visible_in_missing_detail": zhao_visible_in_missing_detail,
        "center_snapshot_member_count": len(center_members),
        "center_snapshot_completed": center_completed,
        "center_snapshot_missing": center_missing,
        "center_text_ok": center_text_ok,
        "other_missing_names_count": len(other_missing_names),
        "other_missing_names_absent": other_missing_names_absent,
        "totals_consistent": totals_consistent,
        "center_direct_lines": [
            line
            for line in message.splitlines()
            if "中心直属" in line
        ],
        "policy_ok": policy_ok,
        "database_transaction": "read_only_rolled_back",
        "dingtalk_send_calls": 0,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    await engine.dispose()
    if not args.observe_only and not policy_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
