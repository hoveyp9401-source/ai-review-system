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

from app.agent2.daily_commands import compile_daily_commands
from app.db import AsyncSessionLocal
from app.models import DailyReport, ReportInteractionEvent, Team, User, WebhookEvent
from app.workflows.gate import build_gate_decision
from app.workflows.intake import IncomingMessageEnvelope, WorkflowRouter


TEAM_CODE = "__agent2_date_smoke_team__"
TEAM_NAME = "Agent2 Date Smoke"
USER_PREFIX = "__agent2_date_smoke__"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value)[:80]


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


async def _ensure_team(session) -> Team:
    team = (await session.execute(select(Team).where(Team.code == TEAM_CODE))).scalar_one_or_none()
    if team is None:
        team = Team(code=TEAM_CODE, name=TEAM_NAME, department_name="Smoke", active=True)
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
                employee_no=f"agent2-date-smoke-{_slug(case_name)}",
                name=f"Agent2日期Smoke-{case_name[:24]}",
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


async def cleanup_user(user: User) -> None:
    async with AsyncSessionLocal() as session:
        managed = await session.merge(user)
        await session.execute(delete(DailyReport).where(DailyReport.user_id == managed.id))
        await session.execute(delete(ReportInteractionEvent).where(ReportInteractionEvent.user_id == managed.id))
        await session.execute(delete(WebhookEvent).where(WebhookEvent.dingtalk_user_id == managed.dingtalk_user_id))
        await session.commit()


async def cleanup_all_smoke_data() -> None:
    async with AsyncSessionLocal() as session:
        users = (
            await session.execute(select(User).where(User.dingtalk_user_id.like(f"{USER_PREFIX}%")))
        ).scalars().all()
        for user in users:
            await session.execute(delete(DailyReport).where(DailyReport.user_id == user.id))
            await session.execute(delete(ReportInteractionEvent).where(ReportInteractionEvent.user_id == user.id))
            await session.execute(delete(WebhookEvent).where(WebhookEvent.dingtalk_user_id == user.dingtalk_user_id))
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
        managed = await session.merge(user)
        existing = (
            await session.execute(
                select(DailyReport).where(
                    DailyReport.user_id == managed.id,
                    DailyReport.report_date == report_date,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        report = DailyReport(
            user_id=managed.id,
            team_id=managed.team_id,
            report_date=report_date,
            today_work=list(today_work or []),
            problems=list(problems or []),
            tomorrow_plan=list(tomorrow_plan or []),
            emotion="",
            raw_input="agent2 date smoke seed",
            input_fragments=[],
            section_status={},
            completeness_score=_decimal("1.0"),
            status=status,
            confirmation_type="none",
            confirmed_by_user=False,
            quality_warning=None,
            last_modified_by_user=False,
            source="agent2_date_smoke_seed",
            llm_model="agent2-date-smoke-seed",
            llm_payload={},
        )
        session.add(report)
        await session.commit()
        await session.refresh(report)
        return report


async def reports_for_user(user: User) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        managed = await session.merge(user)
        rows = (
            await session.execute(
                select(DailyReport)
                .where(DailyReport.user_id == managed.id)
                .order_by(DailyReport.report_date)
            )
        ).scalars().all()
        return [
            {
                "report_date": row.report_date.isoformat(),
                "status": row.status,
                "today_work": list(row.today_work or []),
                "problems": list(row.problems or []),
                "tomorrow_plan": list(row.tomorrow_plan or []),
            }
            for row in rows
        ]


async def post_manual(
    client: httpx.AsyncClient,
    user: User,
    raw_input: str,
    *,
    report_date: date | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dingtalk_user_id": user.dingtalk_user_id,
        "raw_input": raw_input,
        "source": "agent2_online_date_smoke",
        "idempotency_key": idempotency_key,
    }
    if report_date is not None:
        payload["report_date"] = report_date.isoformat()
    response = await client.post("/reports/manual", json=payload)
    response.raise_for_status()
    return response.json()


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def smoke_begin_edit_yesterday(client: httpx.AsyncClient, today: date) -> dict[str, Any]:
    user = await ensure_user("begin-edit-yesterday")
    await cleanup_user(user)
    yesterday = today - timedelta(days=1)
    await seed_report(
        user,
        yesterday,
        today_work=["合同审核"],
        problems=["暂无"],
        tomorrow_plan=["继续推进"],
    )
    before = await reports_for_user(user)
    response = await post_manual(
        client,
        user,
        "我要改昨天日报",
        report_date=today,
        idempotency_key="agent2-date-begin-edit-yesterday",
    )
    after = await reports_for_user(user)
    message = str(response.get("message") or "")
    assert_true(before == after, "after-nine historical edit entry must not write DB")
    assert_true(
        "不能再直接编辑" in message or "不能再直接修改" in message or "只支持查看" in message,
        "after-nine historical edit block message missing",
    )
    return {"response": response, "before": before, "after": after}


async def smoke_concrete_yesterday_edit(client: httpx.AsyncClient, today: date) -> dict[str, Any]:
    user = await ensure_user("concrete-yesterday-edit")
    await cleanup_user(user)
    yesterday = today - timedelta(days=1)
    await seed_report(
        user,
        yesterday,
        today_work=["合同审核", "资料归档"],
        problems=["暂无"],
        tomorrow_plan=["继续推进"],
    )
    before = await reports_for_user(user)
    response = await post_manual(
        client,
        user,
        "昨天日报今日工作第2条删掉",
        report_date=today,
        idempotency_key="agent2-date-concrete-yesterday-edit",
    )
    reports = await reports_for_user(user)
    yesterday_report = next((item for item in reports if item["report_date"] == yesterday.isoformat()), None)
    today_report = next((item for item in reports if item["report_date"] == today.isoformat()), None)
    assert_true(yesterday_report is not None, "yesterday report missing after edit")
    assert_true(reports == before, "after-nine concrete historical delete must not change reports")
    assert_true(yesterday_report["today_work"] == ["合同审核", "资料归档"], "yesterday report was changed")
    assert_true(today_report is None, "concrete yesterday edit created today's report")
    return {"response": response, "before": before, "reports": reports}


def smoke_before_nine_command_contract() -> dict[str, Any]:
    received_at = datetime(2026, 7, 7, 8, 30, tzinfo=LOCAL_TZ)
    envelope = IncomingMessageEnvelope(
        sender_id="agent2-date-contract-user",
        sender_name="Agent2 Date Contract",
        dingtalk_user_id="agent2-date-contract-user",
        source="agent2_online_date_smoke",
        raw_text="明日计划继续优化日报系统",
        received_at=received_at,
    )
    plan = WorkflowRouter().plan(envelope)
    gate = build_gate_decision(plan, mode="protective_gate")
    commands = compile_daily_commands(plan, envelope)
    assert_true(gate.allow_legacy_daily is True, "before-nine daily fill should enter daily executor")
    assert_true(len(commands) == 1, "before-nine command count should be one")
    command = commands[0]
    assert_true(command.operation == "fill", "before-nine command should fill")
    assert_true(command.target_field == "tomorrow_plan", "明日计划 should remain tomorrow_plan field")
    assert_true(command.target_date == "yesterday", "before-nine bare daily operation should target yesterday")
    assert_true(command.should_write is True, "before-nine fill should write")
    return {"command": command.as_dict(), "received_at": received_at.isoformat()}


async def _debug_time(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/debug/time")
    if response.status_code == 404:
        return {"clock_override_enabled": False, "status_code": response.status_code}
    response.raise_for_status()
    return response.json()


async def _set_debug_time(client: httpx.AsyncClient, value: str | None) -> dict[str, Any]:
    response = await client.post("/debug/time/override", json={"now": value})
    response.raise_for_status()
    return response.json()


async def smoke_before_nine_api_if_enabled(client: httpx.AsyncClient, today: date) -> dict[str, Any]:
    debug = await _debug_time(client)
    if not bool(debug.get("clock_override_enabled")):
        return {
            "skipped": True,
            "reason": "CLOCK_OVERRIDE_ENABLED is disabled; contract-level before-nine check still ran",
            "debug": debug,
        }
    user = await ensure_user("before-nine-api")
    await cleanup_user(user)
    override = f"{today.isoformat()}T08:30:00+08:00"
    try:
        await _set_debug_time(client, override)
        response = await post_manual(
            client,
            user,
            "明日计划继续优化日报系统",
            report_date=today,
            idempotency_key="agent2-date-before-nine-api",
        )
        reports = await reports_for_user(user)
    finally:
        await _set_debug_time(client, None)
    yesterday = today - timedelta(days=1)
    yesterday_report = next((item for item in reports if item["report_date"] == yesterday.isoformat()), None)
    today_report = next((item for item in reports if item["report_date"] == today.isoformat()), None)
    assert_true(response.get("report_date") == yesterday.isoformat(), "before-nine API response should target yesterday")
    assert_true(yesterday_report is not None, "before-nine API did not create yesterday report")
    assert_true(yesterday_report["tomorrow_plan"], "before-nine API did not write tomorrow_plan")
    assert_true(today_report is None, "before-nine API created today's report")
    return {"skipped": False, "response": response, "reports": reports}


async def run(base_url: str, output: str) -> int:
    started = time.perf_counter()
    today = datetime.now(LOCAL_TZ).date()
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        cases = [
            ("begin_edit_yesterday", lambda: smoke_begin_edit_yesterday(client, today)),
            ("concrete_yesterday_edit", lambda: smoke_concrete_yesterday_edit(client, today)),
            ("before_nine_command_contract", lambda: asyncio.to_thread(smoke_before_nine_command_contract)),
            ("before_nine_api_if_enabled", lambda: smoke_before_nine_api_if_enabled(client, today)),
        ]
        for index, (name, factory) in enumerate(cases, start=1):
            try:
                detail = await factory()
                status = "SKIP" if detail.get("skipped") else "PASS"
                item = {"name": name, "status": status, "detail": detail}
                print(f"AGENT2_DATE_SMOKE_PROGRESS {index}/{len(cases)} {status} {name}", flush=True)
            except Exception as exc:
                item = {"name": name, "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
                failures.append(item)
                print(f"AGENT2_DATE_SMOKE_PROGRESS {index}/{len(cases)} FAIL {name}", flush=True)
            results.append(item)
    summary = {
        "pass": sum(1 for item in results if item["status"] == "PASS"),
        "skip": sum(1 for item in results if item["status"] == "SKIP"),
        "fail": len(failures),
        "total": len(results),
        "seconds": round(time.perf_counter() - started, 1),
        "base_url": base_url,
        "generated_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
    }
    payload = {"summary": summary, "results": results, "failures": failures}
    if output:
        path = Path(output)
    else:
        path = Path("outputs") / f"agent2_date_smoke_{datetime.now(LOCAL_TZ).strftime('%Y%m%d_%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("AGENT2_DATE_SMOKE_START", flush=True)
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"OUTPUT {path}", flush=True)
    print("FAILURES_START", flush=True)
    for item in failures:
        print(json.dumps(item, ensure_ascii=False), flush=True)
    print("FAILURES_END", flush=True)
    print("AGENT2_DATE_SMOKE_END", flush=True)
    await cleanup_all_smoke_data()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Online smoke for Agent2 date targeting and daily edit entry.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
