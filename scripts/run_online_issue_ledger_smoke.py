from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select

from app.db import AsyncSessionLocal
from app.models import DailyReport, ReportInteractionEvent, Team, User, UserHabit, WebhookEvent
from scripts.run_issue_ledger_simulation import STATUS_MAP, check_expect, issue_cases


TEAM_CODE = "__issue_ledger_online_smoke_team__"
TEAM_NAME = "Issue Ledger Online Smoke"
USER_PREFIX = "__issue_smoke__"
BASE_REPORT_DATE = date(2026, 6, 26)
DEFAULT_REPORT_DATE = datetime.now(ZoneInfo("Asia/Shanghai")).date()
DATE_SHIFT = DEFAULT_REPORT_DATE - BASE_REPORT_DATE


def _shift_date(value: date) -> date:
    return value + DATE_SHIFT


def _shift_case_dates(value: Any) -> Any:
    if isinstance(value, list):
        return [_shift_case_dates(item) for item in value]
    if isinstance(value, dict):
        return {
            _shift_case_dates(key) if isinstance(key, str) else key: _shift_case_dates(item)
            for key, item in value.items()
        }
    if not isinstance(value, str) or not value:
        return value

    def replace_iso(match: re.Match[str]) -> str:
        try:
            shifted = _shift_date(date.fromisoformat(match.group(0)))
        except ValueError:
            return match.group(0)
        return shifted.isoformat()

    def replace_cn(match: re.Match[str]) -> str:
        month = int(match.group(1))
        day = int(match.group(2))
        suffix = match.group(3) or ""
        try:
            shifted = _shift_date(date(2026, month, day))
        except ValueError:
            return match.group(0)
        return f"{shifted.month}月{shifted.day}日{suffix}"

    value = re.sub(r"2026-06-\d{2}", replace_iso, value)
    value = re.sub(r"(\d{1,2})月(\d{1,2})日?([那这]份)?", replace_cn, value)
    return value


def _case_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value)[:80]


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _status(value: str | None) -> str:
    if not value:
        return "collecting"
    return str(STATUS_MAP.get(value, value))


def _compact_snapshot(report: DailyReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "status": report.status,
        "today_work": list(report.today_work or []),
        "problems": list(report.problems or []),
        "tomorrow_plan": list(report.tomorrow_plan or []),
        "confirmed_by_user": bool(report.confirmed_by_user),
    }


def _response_to_result(response: dict[str, Any], *, saved: bool) -> SimpleNamespace:
    merged = response.get("merged_report") or {}
    return SimpleNamespace(
        report_saved=saved,
        reply_kind=response.get("reply_kind") or "",
        status=response.get("status") or "",
        confirmed_by_user=bool(response.get("confirmed_by_user")),
        report_date=date.fromisoformat(response.get("report_date")),
        today_work=list(merged.get("today_work") or []),
        problems=list(merged.get("problems") or []),
        tomorrow_plan=list(merged.get("tomorrow_plan") or []),
        message=response.get("message") or "",
    )


class DbStoreView:
    def __init__(self, reports: dict[date, DailyReport]):
        self.reports = reports


async def ensure_user(session, case_name: str) -> User:
    team = (await session.execute(select(Team).where(Team.code == TEAM_CODE))).scalar_one_or_none()
    if team is None:
        team = Team(code=TEAM_CODE, name=TEAM_NAME, department_name="Issue Ledger Smoke", active=True)
        session.add(team)
        await session.flush()

    dingtalk_user_id = f"{USER_PREFIX}{_case_slug(case_name)}"
    user = (
        await session.execute(select(User).where(User.dingtalk_user_id == dingtalk_user_id))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            dingtalk_user_id=dingtalk_user_id,
            employee_no=f"issue-smoke-{_case_slug(case_name)}",
            name=f"问题台账Smoke-{case_name[:32]}",
            team_id=team.id,
            role="member",
            timezone="Asia/Shanghai",
            active=True,
        )
        session.add(user)
        await session.flush()
    return user


async def cleanup_user(session, user: User) -> None:
    await session.execute(delete(DailyReport).where(DailyReport.user_id == user.id))
    await session.execute(delete(ReportInteractionEvent).where(ReportInteractionEvent.user_id == user.id))
    await session.execute(delete(UserHabit).where(UserHabit.user_id == user.id))
    await session.execute(delete(WebhookEvent).where(WebhookEvent.dingtalk_user_id == user.dingtalk_user_id))
    await session.commit()


async def cleanup_all_smoke_data() -> None:
    async with AsyncSessionLocal() as session:
        users = (
            await session.execute(select(User).where(User.dingtalk_user_id.like(f"{USER_PREFIX}%")))
        ).scalars().all()
        for user in users:
            await cleanup_user(session, user)
            await session.delete(user)
        await session.commit()


async def seed_report(session, user: User, spec: dict[str, Any], report_date: date) -> DailyReport:
    report = DailyReport(
        user_id=user.id,
        team_id=user.team_id,
        report_date=report_date,
        today_work=list(spec.get("today_work") or []),
        problems=list(spec.get("problems") or []),
        tomorrow_plan=list(spec.get("tomorrow_plan") or []),
        emotion=str(spec.get("emotion") or ""),
        raw_input="issue ledger online smoke seed",
        input_fragments=[],
        section_status=dict(spec.get("section_status") or {}),
        completeness_score=_decimal(spec.get("completeness_score", 0)),
        status=_status(spec.get("status")),
        confirmation_type=str(spec.get("confirmation_type") or "none"),
        confirmed_by_user=bool(spec.get("confirmed_by_user", False)),
        quality_warning=spec.get("quality_warning"),
        last_modified_by_user=bool(spec.get("last_modified_by_user", False)),
        source="issue_ledger_online_smoke_seed",
        llm_model="issue-ledger-online-smoke-seed",
        llm_payload={},
    )
    session.add(report)
    await session.flush()
    return report


async def get_report(session, user: User, report_date: date) -> DailyReport | None:
    return (
        await session.execute(
            select(DailyReport).where(DailyReport.user_id == user.id, DailyReport.report_date == report_date)
        )
    ).scalar_one_or_none()


async def get_reports(session, user: User) -> dict[date, DailyReport]:
    reports = (
        await session.execute(select(DailyReport).where(DailyReport.user_id == user.id))
    ).scalars().all()
    return {report.report_date: report for report in reports}


async def prepare_case(case: dict[str, Any], user: User) -> None:
    async with AsyncSessionLocal() as session:
        managed_user = await session.merge(user)
        await cleanup_user(session, managed_user)
        if case.get("initial"):
            await seed_report(session, managed_user, case["initial"], DEFAULT_REPORT_DATE)
        for item in case.get("reports") or []:
            report_date = date.fromisoformat(item["report_date"])
            await seed_report(session, managed_user, item, report_date)
        await session.commit()


async def final_store(user: User) -> DbStoreView:
    async with AsyncSessionLocal() as session:
        managed_user = await session.merge(user)
        reports = await get_reports(session, managed_user)
        return DbStoreView(reports)


async def run_api_case(client: httpx.AsyncClient, case: dict[str, Any], *, source: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        user = await ensure_user(session, case["name"])
        await session.commit()
        await session.refresh(user)

    await prepare_case(case, user)

    result_response: dict[str, Any] | None = None
    saved = False
    turns: list[dict[str, Any]] = []
    try:
        for turn_index, message in enumerate(case.get("messages") or [], start=1):
            async with AsyncSessionLocal() as session:
                managed_user = await session.merge(user)
                before = _compact_snapshot(await get_report(session, managed_user, DEFAULT_REPORT_DATE))
            response = await client.post(
                "/reports/manual",
                json={
                    "dingtalk_user_id": user.dingtalk_user_id,
                    "raw_input": message,
                    "source": source,
                    "report_date": DEFAULT_REPORT_DATE.isoformat(),
                },
            )
            response.raise_for_status()
            result_response = response.json()
            async with AsyncSessionLocal() as session:
                managed_user = await session.merge(user)
                after = _compact_snapshot(await get_report(session, managed_user, DEFAULT_REPORT_DATE))
            saved = before != after
            turns.append(
                {
                    "index": turn_index,
                    "message": message,
                    "saved": saved,
                    "reply_kind": result_response.get("reply_kind"),
                    "status": result_response.get("status"),
                    "reply": (result_response.get("message") or "")[:260],
                }
            )

        if result_response is None:
            raise AssertionError("case has no messages")
        result = _response_to_result(result_response, saved=saved)
        store = await final_store(user)
        case_for_check = copy.deepcopy(case)
        saved_expect = (case_for_check.get("expect") or {}).pop("saved", None)
        errors = check_expect(case_for_check, result, store)
        if saved_expect is False and saved:
            errors.append("content changed even though this case should not modify report content")
        status = "PASS" if not errors else "FAIL"
        return {
            "name": case["name"],
            "issue_id": case["issue_id"],
            "mode": "online_api",
            "status": status,
            "errors": errors,
            "messages": case.get("messages") or [],
            "turns": turns,
            "reply_kind": result.reply_kind,
            "report_status": result.status,
            "saved": result.report_saved,
            "report_date": result.report_date.isoformat(),
            "today_work": result.today_work,
            "problems": result.problems,
            "tomorrow_plan": result.tomorrow_plan,
            "message": result.message,
        }
    except Exception as exc:
        return {
            "name": case["name"],
            "issue_id": case["issue_id"],
            "mode": "online_api",
            "status": "ERROR",
            "errors": [f"{type(exc).__name__}: {exc}"],
            "messages": case.get("messages") or [],
            "turns": turns,
        }
    finally:
        async with AsyncSessionLocal() as session:
            managed_user = await session.merge(user)
            await cleanup_user(session, managed_user)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run issue-ledger cases against the live local production API and DB.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--filter", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", default="outputs/issue_ledger_online_smoke_latest.json")
    parser.add_argument("--source", default="issue_ledger_online_smoke")
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()

    if args.cleanup_only:
        await cleanup_all_smoke_data()
        print("ONLINE_SMOKE_CLEANUP_DONE", flush=True)
        return 0

    cases = [_shift_case_dates(copy.deepcopy(case)) for case in issue_cases() if case.get("mode") != "documented"]
    if args.filter:
        cases = [case for case in cases if args.filter in case["name"] or args.filter in case["issue_id"]]
    if args.limit:
        cases = cases[: args.limit]

    await cleanup_all_smoke_data()
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=90.0, trust_env=False) as client:
        for index, case in enumerate(cases, start=1):
            result = await run_api_case(client, case, source=args.source)
            results.append(result)
            print(f"ONLINE_SMOKE_PROGRESS {index}/{len(cases)} {result['status']} {result['name']}", flush=True)

    await cleanup_all_smoke_data()

    failed = [item for item in results if item["status"] in {"FAIL", "ERROR"}]
    passed = [item for item in results if item["status"] == "PASS"]
    summary = {
        "pass": len(passed),
        "fail": len(failed),
        "total": len(results),
        "seconds": round(time.perf_counter() - started, 1),
        "base_url": args.base_url,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    payload = {"summary": summary, "results": results}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("ONLINE_ISSUE_LEDGER_SMOKE_START", flush=True)
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"OUTPUT {output}", flush=True)
    print("FAILURES_START", flush=True)
    for item in failed:
        print(json.dumps(item, ensure_ascii=False), flush=True)
    print("FAILURES_END", flush=True)
    print("ONLINE_ISSUE_LEDGER_SMOKE_END", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
