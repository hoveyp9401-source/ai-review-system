from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select

from app.agent2.daily_commands import DailyCommand
from app.agent2.daily_execution import execute_agent2_daily_commands
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import Agent2ConversationState, DailyReport, ReportInteractionEvent, Team, User, WebhookEvent


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
REPORT_DATE = datetime.now(LOCAL_TZ).date()
TEAM_CODE = "__agent2_daily_p0_smoke_team__"
USER_PREFIX = "__agent2_daily_p0_smoke__"


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value)[:64]


async def ensure_user(case_name: str) -> User:
    async with AsyncSessionLocal() as session:
        team = (await session.execute(select(Team).where(Team.code == TEAM_CODE))).scalar_one_or_none()
        if team is None:
            team = Team(code=TEAM_CODE, name="Agent2 Daily P0 Smoke", department_name="Smoke", active=True)
            session.add(team)
            await session.flush()
        dingtalk_user_id = f"{USER_PREFIX}{_slug(case_name)}"
        user = (await session.execute(select(User).where(User.dingtalk_user_id == dingtalk_user_id))).scalar_one_or_none()
        if user is None:
            user = User(
                dingtalk_user_id=dingtalk_user_id,
                employee_no=f"agent2-p0-{_slug(case_name)}",
                name=f"Agent2P0-{case_name}",
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


async def seed_report(
    user: User,
    *,
    today_work: list[str],
    problems: list[str] | None = None,
    tomorrow_plan: list[str] | None = None,
    version: int = 0,
) -> None:
    problems = list(problems or [])
    tomorrow_plan = list(tomorrow_plan or [])
    async with AsyncSessionLocal() as session:
        managed = await session.merge(user)
        await session.execute(delete(DailyReport).where(DailyReport.user_id == managed.id))
        await session.execute(delete(ReportInteractionEvent).where(ReportInteractionEvent.user_id == managed.id))
        await session.execute(delete(WebhookEvent).where(WebhookEvent.dingtalk_user_id == managed.dingtalk_user_id))
        report = DailyReport(
            user_id=managed.id,
            team_id=managed.team_id,
            report_date=REPORT_DATE,
            today_work=list(today_work),
            problems=problems,
            tomorrow_plan=tomorrow_plan,
            emotion="",
            raw_input="agent2 daily p0 smoke seed",
            input_fragments=[],
            section_status={
                "_draft_item_ids": {
                    "today_work": [f"tw-{index}" for index, _ in enumerate(today_work, start=1)],
                    "problems": [f"pb-{index}" for index, _ in enumerate(problems, start=1)],
                    "tomorrow_plan": [f"tp-{index}" for index, _ in enumerate(tomorrow_plan, start=1)],
                },
                "_agent2_report_version": version,
            },
            completeness_score=Decimal("1.0"),
            status="collecting",
            confirmation_type="none",
            confirmed_by_user=False,
            last_modified_by_user=False,
            source="agent2_daily_p0_smoke_seed",
            llm_model="agent2-daily-p0-smoke",
            llm_payload={},
        )
        session.add(report)
        await session.commit()


async def report_snapshot(user: User) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        managed = await session.merge(user)
        report = (
            await session.execute(
                select(DailyReport).where(DailyReport.user_id == managed.id, DailyReport.report_date == REPORT_DATE)
            )
        ).scalar_one()
        return {
            "today_work": list(report.today_work or []),
            "problems": list(report.problems or []),
            "tomorrow_plan": list(report.tomorrow_plan or []),
            "status": report.status,
            "version": int((report.section_status or {}).get("_agent2_report_version", 0)),
            "typed_keys": list((report.section_status or {}).get("_agent2_typed_command_keys", [])),
            "typed_audit": list((report.section_status or {}).get("_agent2_typed_audit", [])),
        }


async def post_manual(client: httpx.AsyncClient, user: User, text: str, key: str) -> dict[str, Any]:
    response = await client.post(
        "/reports/manual",
        json={
            "dingtalk_user_id": user.dingtalk_user_id,
            "raw_input": text,
            "source": "agent2_daily_p0_online_smoke",
            "report_date": REPORT_DATE.isoformat(),
            "idempotency_key": key,
        },
    )
    response.raise_for_status()
    return response.json()


async def case_ambiguous_delete(client: httpx.AsyncClient) -> dict[str, Any]:
    user = await ensure_user("ambiguous-delete")
    await seed_report(user, today_work=["alpha", "beta"])
    before = await report_snapshot(user)
    response = await post_manual(client, user, "那条删掉", "agent2-p0-ambiguous-delete")
    after = await report_snapshot(user)
    assert_true(before == after, "ambiguous delete changed DB")
    assert_true("哪一条" in str(response.get("message") or "") or "第 2 条" in str(response.get("message") or ""), "ambiguous delete did not clarify target")
    return {"response": response, "after": after}


async def case_exact_mutations(client: httpx.AsyncClient) -> dict[str, Any]:
    results: dict[str, Any] = {}
    cases = (
        ("delete", "删除第 2 条", ["alpha", "beta"], ["alpha"]),
        ("edit", "把第 2 条改成完成合同审核", ["alpha", "beta"], ["alpha", "完成合同审核"]),
        ("merge", "合并今天工作第 1、2 条", ["alpha", "beta", "gamma"], ["alpha，beta", "gamma"]),
    )
    for name, text, before_items, expected_items in cases:
        user = await ensure_user(f"exact-{name}")
        await seed_report(user, today_work=before_items)
        response = await post_manual(client, user, text, f"agent2-p0-exact-{name}")
        after = await report_snapshot(user)
        assert_true(after["today_work"] == expected_items, f"exact {name} result mismatch: {after['today_work']}")
        assert_true(after["version"] == 1, f"exact {name} version did not increment once")
        assert_true(len(after["typed_audit"]) == 1 and after["typed_audit"][0]["actual_write"] is True, f"exact {name} audit missing")
        results[name] = {"response": response, "after": after}
    return results


async def case_submit_and_bare_affirmation(client: httpx.AsyncClient) -> dict[str, Any]:
    submit_user = await ensure_user("submit")
    await seed_report(submit_user, today_work=["alpha"], problems=["暂无"], tomorrow_plan=["继续跟进"])
    submit_response = await post_manual(client, submit_user, "提交日报", "agent2-p0-submit")
    submitted = await report_snapshot(submit_user)
    assert_true(submitted["status"] == "completed" and submitted["version"] == 1, "explicit submit did not complete directly")

    affirm_user = await ensure_user("bare-affirmation")
    await seed_report(affirm_user, today_work=["alpha"], problems=["暂无"], tomorrow_plan=["继续跟进"])
    before = await report_snapshot(affirm_user)
    affirm_response = await post_manual(client, affirm_user, "是的", "agent2-p0-bare-affirmation")
    after = await report_snapshot(affirm_user)
    assert_true(before == after, "bare affirmation without pending changed DB")
    return {"submit": submit_response, "submitted": submitted, "affirm": affirm_response, "affirm_after": after}


async def case_api_idempotency(client: httpx.AsyncClient) -> dict[str, Any]:
    user = await ensure_user("api-idempotency")
    await seed_report(user, today_work=[])
    responses = [await post_manual(client, user, "今天完成合同审核", "agent2-p0-api-replay") for _ in range(3)]
    after = await report_snapshot(user)
    assert_true(after["today_work"] == ["完成合同审核"], "API replay duplicated daily item")
    assert_true(after["version"] == 1 and len(after["typed_keys"]) == 1, "API replay wrote more than once")
    return {"responses_equal": responses[0] == responses[1] == responses[2], "after": after}


async def case_actionless_context_instruction(client: httpx.AsyncClient) -> dict[str, Any]:
    user = await ensure_user("actionless-context-instruction")
    await seed_report(user, today_work=["完成合同审核"])
    before = await report_snapshot(user)
    response = await post_manual(
        client,
        user,
        "帮我整合优化",
        "agent2-p0-actionless-context-instruction",
    )
    after = await report_snapshot(user)
    assert_true(before == after, "actionless context instruction changed DB")
    return {"response": response, "after": after}


async def case_version_and_reply_retry() -> dict[str, Any]:
    user = await ensure_user("version-conflict")
    await seed_report(user, today_work=["base"], version=0)
    settings = get_settings()
    command_a = [DailyCommand(operation="fill", target_field="today_work", content=["first"], should_write=True)]
    command_b = [DailyCommand(operation="fill", target_field="today_work", content=["second"], should_write=True)]
    async with AsyncSessionLocal() as session:
        managed = await session.merge(user)
        first = await execute_agent2_daily_commands(
            session,
            user=managed,
            raw_input="first",
            source="agent2_daily_p0_direct_smoke",
            commands=command_a,
            settings=settings,
            report_date=REPORT_DATE,
            message_id="agent2-p0-version-first",
            expected_report_version=0,
        )
        await session.commit()
    async with AsyncSessionLocal() as session:
        managed = await session.merge(user)
        stale = await execute_agent2_daily_commands(
            session,
            user=managed,
            raw_input="second",
            source="agent2_daily_p0_direct_smoke",
            commands=command_b,
            settings=settings,
            report_date=REPORT_DATE,
            message_id="agent2-p0-version-stale",
            expected_report_version=0,
        )
        await session.commit()
    after_conflict = await report_snapshot(user)
    assert_true(first.report_saved is True and stale.report_saved is False, "stale version was not blocked")
    assert_true(after_conflict["today_work"] == ["base", "first"] and after_conflict["version"] == 1, "stale version overwrote DB")
    assert_true(stale.command_results[0]["reason"] == "version_conflict", "version conflict reason missing")

    async with AsyncSessionLocal() as session:
        managed = await session.merge(user)
        retry = await execute_agent2_daily_commands(
            session,
            user=managed,
            raw_input="first",
            source="agent2_daily_p0_direct_smoke",
            commands=command_a,
            settings=settings,
            report_date=REPORT_DATE,
            message_id="agent2-p0-version-first",
            expected_report_version=1,
        )
        await session.commit()
    after_retry = await report_snapshot(user)
    assert_true(retry.report_saved is False and retry.command_results[0]["reason"] == "duplicate_message", "reply retry was not idempotent")
    assert_true(after_retry == after_conflict, "reply retry duplicated DB write")
    return {"first": first.command_results, "stale": stale.command_results, "retry": retry.command_results, "after": after_retry}


async def cleanup() -> None:
    async with AsyncSessionLocal() as session:
        users = (await session.execute(select(User).where(User.dingtalk_user_id.like(f"{USER_PREFIX}%")))).scalars().all()
        user_ids = [user.id for user in users]
        if user_ids:
            await session.execute(delete(DailyReport).where(DailyReport.user_id.in_(user_ids)))
            await session.execute(delete(ReportInteractionEvent).where(ReportInteractionEvent.user_id.in_(user_ids)))
            await session.execute(
                delete(Agent2ConversationState).where(
                    Agent2ConversationState.user_key.in_([str(user_id) for user_id in user_ids])
                )
            )
        await session.execute(
            delete(Agent2ConversationState).where(
                Agent2ConversationState.last_message_id.like("agent2-p0-%")
            )
        )
        await session.execute(delete(WebhookEvent).where(WebhookEvent.dingtalk_user_id.like(f"{USER_PREFIX}%")))
        for user in users:
            await session.delete(user)
        team = (await session.execute(select(Team).where(Team.code == TEAM_CODE))).scalar_one_or_none()
        if team is not None:
            await session.delete(team)
        await session.commit()


async def run(base_url: str, output: Path) -> int:
    await cleanup()
    cases: list[tuple[str, Callable[[], Awaitable[dict[str, Any]]]]] = []
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        cases = [
            ("ambiguous_delete", lambda: case_ambiguous_delete(client)),
            ("exact_mutations", lambda: case_exact_mutations(client)),
            ("submit_and_bare_affirmation", lambda: case_submit_and_bare_affirmation(client)),
            ("api_idempotency", lambda: case_api_idempotency(client)),
            ("actionless_context_instruction", lambda: case_actionless_context_instruction(client)),
            ("version_and_reply_retry", case_version_and_reply_retry),
        ]
        for name, factory in cases:
            started = time.perf_counter()
            try:
                detail = await factory()
            except Exception as exc:
                results.append({"name": name, "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"})
                print(f"AGENT2_DAILY_P0_SMOKE FAIL {name}: {exc}", flush=True)
            else:
                results.append({"name": name, "status": "PASS", "seconds": round(time.perf_counter() - started, 3), "detail": detail})
                print(f"AGENT2_DAILY_P0_SMOKE PASS {name}", flush=True)
    summary = {
        "pass": sum(item["status"] == "PASS" for item in results),
        "fail": sum(item["status"] == "FAIL" for item in results),
        "total": len(results),
        "generated_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "base_url": base_url,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"OUTPUT {output}", flush=True)
    await cleanup()
    return 1 if summary["fail"] else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="outputs/agent2_daily_p0_online_smoke_latest.json")
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, Path(args.output)))


if __name__ == "__main__":
    raise SystemExit(main())
