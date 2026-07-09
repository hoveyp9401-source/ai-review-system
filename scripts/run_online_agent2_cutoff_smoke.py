from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select

from app.db import AsyncSessionLocal
from app.models import DailyReport, ReportInteractionEvent, Team, User, UserHabit, WebhookEvent


TEAM_CODE = "__agent2_cutoff_smoke_team__"
TEAM_NAME = "Agent2 Cutoff Online Smoke"
USER_PREFIX = "__agent2_cutoff_smoke__"
DEFAULT_REPORT_DATE = datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value)[:80]


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


async def _ensure_team(session: Any) -> Team:
    team = (await session.execute(select(Team).where(Team.code == TEAM_CODE))).scalar_one_or_none()
    if team is None:
        team = Team(code=TEAM_CODE, name=TEAM_NAME, department_name="Agent2 Smoke", active=True)
        session.add(team)
        await session.flush()
    return team


async def ensure_user(case_name: str) -> User:
    async with AsyncSessionLocal() as session:
        team = await _ensure_team(session)
        dingtalk_user_id = f"{USER_PREFIX}{_slug(case_name)}"
        user = (
            await session.execute(select(User).where(User.dingtalk_user_id == dingtalk_user_id))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                dingtalk_user_id=dingtalk_user_id,
                employee_no=f"agent2-cutoff-smoke-{_slug(case_name)}",
                name=f"Agent2CutoffSmoke-{case_name[:32]}",
                team_id=team.id,
                role="member",
                timezone="Asia/Shanghai",
                active=True,
            )
            session.add(user)
            await session.flush()
        await session.commit()
        await session.refresh(user)
        return user


async def cleanup_all_smoke_data() -> None:
    async with AsyncSessionLocal() as session:
        users = (
            await session.execute(select(User).where(User.dingtalk_user_id.like(f"{USER_PREFIX}%")))
        ).scalars().all()
        user_ids = [user.id for user in users]
        if user_ids:
            await session.execute(delete(DailyReport).where(DailyReport.user_id.in_(user_ids)))
            await session.execute(delete(ReportInteractionEvent).where(ReportInteractionEvent.user_id.in_(user_ids)))
            await session.execute(delete(UserHabit).where(UserHabit.user_id.in_(user_ids)))
        await session.execute(delete(WebhookEvent).where(WebhookEvent.dingtalk_user_id.like(f"{USER_PREFIX}%")))
        for user in users:
            await session.delete(user)
        team = (await session.execute(select(Team).where(Team.code == TEAM_CODE))).scalar_one_or_none()
        if team is not None:
            await session.delete(team)
        await session.commit()


async def seed_report(
    user: User,
    report_date: date,
    *,
    today_work: list[str] | None = None,
    problems: list[str] | None = None,
    tomorrow_plan: list[str] | None = None,
    status: str = "collecting",
) -> DailyReport:
    async with AsyncSessionLocal() as session:
        managed_user = await session.merge(user)
        existing = (
            await session.execute(
                select(DailyReport).where(
                    DailyReport.user_id == managed_user.id,
                    DailyReport.report_date == report_date,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        report = DailyReport(
            user_id=managed_user.id,
            team_id=managed_user.team_id,
            report_date=report_date,
            today_work=list(today_work or []),
            problems=list(problems or []),
            tomorrow_plan=list(tomorrow_plan or []),
            emotion="",
            raw_input="agent2 cutoff online smoke seed",
            input_fragments=[],
            section_status={},
            completeness_score=_decimal("1.0"),
            status=status,
            confirmation_type="none",
            confirmed_by_user=False,
            quality_warning=None,
            last_modified_by_user=False,
            source="agent2_cutoff_online_smoke_seed",
            llm_model="agent2-cutoff-online-smoke-seed",
            llm_payload={},
        )
        session.add(report)
        await session.commit()
        await session.refresh(report)
        return report


async def get_report(user: User, report_date: date = DEFAULT_REPORT_DATE) -> DailyReport | None:
    async with AsyncSessionLocal() as session:
        managed_user = await session.merge(user)
        return (
            await session.execute(
                select(DailyReport).where(
                    DailyReport.user_id == managed_user.id,
                    DailyReport.report_date == report_date,
                )
            )
        ).scalar_one_or_none()


async def post_manual(client: httpx.AsyncClient, user: User, raw_input: str, *, case_name: str) -> dict[str, Any]:
    response = await client.post(
        "/reports/manual",
        json={
            "dingtalk_user_id": user.dingtalk_user_id,
            "raw_input": raw_input,
            "source": "agent2_cutoff_online_smoke",
            "report_date": DEFAULT_REPORT_DATE.isoformat(),
            "idempotency_key": f"agent2-cutoff-{case_name}-{int(time.time() * 1000)}",
        },
    )
    response.raise_for_status()
    return response.json()


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def smoke_today_write_ignores_historical_context(client: httpx.AsyncClient) -> None:
    user = await ensure_user("today-write")
    yesterday = DEFAULT_REPORT_DATE - timedelta(days=1)
    await seed_report(user, yesterday, today_work=["昨天历史上下文"], problems=["暂无"], tomorrow_plan=["继续历史事项"])
    response = await post_manual(client, user, "今天完成线上截止规则验证", case_name="today-write")
    today_report = await get_report(user, DEFAULT_REPORT_DATE)
    yesterday_report = await get_report(user, yesterday)
    assert_true(response.get("report_date") == DEFAULT_REPORT_DATE.isoformat(), "today write response did not target today")
    assert_true(today_report is not None, "today write did not create today's report")
    assert_true("今天完成线上截止规则验证" in list(today_report.today_work or []), "today write missing from today's report")
    assert_true(yesterday_report is not None and list(yesterday_report.today_work or []) == ["昨天历史上下文"], "today write changed yesterday")


async def smoke_bare_clear_targets_today_not_historical_context(client: httpx.AsyncClient) -> None:
    user = await ensure_user("bare-clear")
    yesterday = DEFAULT_REPORT_DATE - timedelta(days=1)
    await seed_report(user, DEFAULT_REPORT_DATE, today_work=["今天应被清空"], problems=["今天问题"], tomorrow_plan=["今天计划"])
    await asyncio.sleep(0.05)
    await seed_report(user, yesterday, today_work=["昨天不应被清空"], problems=["昨天问题"], tomorrow_plan=["昨天计划"])
    response = await post_manual(client, user, "清空日报", case_name="bare-clear")
    today_report = await get_report(user, DEFAULT_REPORT_DATE)
    yesterday_report = await get_report(user, yesterday)
    assert_true(response.get("report_date") == DEFAULT_REPORT_DATE.isoformat(), "bare clear response did not target today")
    assert_true(today_report is not None and list(today_report.today_work or []) == [], "bare clear did not clear today's report")
    assert_true(yesterday_report is not None and list(yesterday_report.today_work or []) == ["昨天不应被清空"], "bare clear changed yesterday")


async def smoke_explicit_yesterday_edit_is_blocked_after_cutoff(client: httpx.AsyncClient) -> None:
    user = await ensure_user("explicit-yesterday-edit")
    yesterday = DEFAULT_REPORT_DATE - timedelta(days=1)
    await seed_report(user, yesterday, today_work=["昨天第一条"], problems=["暂无"], tomorrow_plan=["继续"])
    response = await post_manual(client, user, "昨天第一条删掉", case_name="explicit-yesterday-edit")
    yesterday_report = await get_report(user, yesterday)
    assert_true("9点后" in str(response.get("message") or ""), "explicit historical edit did not explain cutoff")
    assert_true(yesterday_report is not None and list(yesterday_report.today_work or []) == ["昨天第一条"], "explicit historical edit changed yesterday")


async def smoke_daily_meta_status_is_no_write(client: httpx.AsyncClient) -> None:
    user = await ensure_user("daily-meta-status")
    response = await post_manual(client, user, "写日报了", case_name="daily-meta-status")
    today_report = await get_report(user, DEFAULT_REPORT_DATE)
    assert_true(today_report is None or "写日报了" not in list(today_report.today_work or []), "daily meta status was written as daily work")
    assert_true("不写入日报" in str(response.get("message") or ""), "daily meta status did not return no-write reply")


async def smoke_chatter_does_not_match_daily_candidate(client: httpx.AsyncClient) -> None:
    user = await ensure_user("chatter-candidate")
    await seed_report(
        user,
        DEFAULT_REPORT_DATE,
        today_work=["今天完成合同审核"],
        problems=["暂无"],
        tomorrow_plan=["明天出差三亚沟通海花岛案件"],
    )
    response = await post_manual(client, user, "明天吃屎", case_name="chatter-candidate")
    report = await get_report(user, DEFAULT_REPORT_DATE)
    message = str(response.get("message") or "")
    assert_true("不写入日报" in message, "chatter did not return no-write reply")
    assert_true("最接近" not in message, "chatter was matched to a daily candidate")
    assert_true(report is not None and list(report.tomorrow_plan or []) == ["明天出差三亚沟通海花岛案件"], "chatter changed daily draft")


async def run(base_url: str, output: Path) -> int:
    cases = [
        ("today_write_ignores_historical_context", smoke_today_write_ignores_historical_context),
        ("bare_clear_targets_today_not_historical_context", smoke_bare_clear_targets_today_not_historical_context),
        ("explicit_yesterday_edit_is_blocked_after_cutoff", smoke_explicit_yesterday_edit_is_blocked_after_cutoff),
        ("daily_meta_status_is_no_write", smoke_daily_meta_status_is_no_write),
        ("chatter_does_not_match_daily_candidate", smoke_chatter_does_not_match_daily_candidate),
    ]
    results: list[dict[str, Any]] = []
    await cleanup_all_smoke_data()
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        for name, func in cases:
            started = time.perf_counter()
            try:
                await func(client)
            except Exception as exc:
                results.append({"name": name, "status": "FAIL", "error": str(exc), "seconds": round(time.perf_counter() - started, 3)})
                print(f"AGENT2_CUTOFF_SMOKE_PROGRESS FAIL {name}: {exc}")
            else:
                results.append({"name": name, "status": "PASS", "seconds": round(time.perf_counter() - started, 3)})
                print(f"AGENT2_CUTOFF_SMOKE_PROGRESS PASS {name}")
    await cleanup_all_smoke_data()
    summary = {
        "pass": sum(1 for item in results if item["status"] == "PASS"),
        "fail": sum(1 for item in results if item["status"] == "FAIL"),
        "total": len(results),
        "base_url": base_url,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("AGENT2_CUTOFF_SMOKE_START")
    print("SUMMARY", json.dumps(summary, ensure_ascii=False))
    print("OUTPUT", output)
    print("AGENT2_CUTOFF_SMOKE_END")
    return 0 if summary["fail"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="outputs/agent2_cutoff_online_smoke_latest.json")
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, Path(args.output)))


if __name__ == "__main__":
    raise SystemExit(main())
