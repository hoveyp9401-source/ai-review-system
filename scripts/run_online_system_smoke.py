from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, func, select

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import DailyReport, ReportInteractionEvent, Team, User, UserHabit, WebhookEvent


TEAM_CODE = "__system_online_smoke_team__"
TEAM_NAME = "System Online Smoke"
USER_PREFIX = "__system_smoke__"
DEFAULT_REPORT_DATE = datetime.now(ZoneInfo("Asia/Shanghai")).date()


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
        team = Team(code=TEAM_CODE, name=TEAM_NAME, department_name="System Smoke", active=True)
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
                employee_no=f"system-smoke-{_slug(case_name)}",
                name=f"系统Smoke-{case_name[:32]}",
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


async def seed_report(user: User, report_date: date, *, today_work=None, problems=None, tomorrow_plan=None, status="collecting") -> DailyReport:
    async with AsyncSessionLocal() as session:
        managed_user = await session.merge(user)
        existing = (
            await session.execute(
                select(DailyReport).where(DailyReport.user_id == managed_user.id, DailyReport.report_date == report_date)
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
            raw_input="system online smoke seed",
            input_fragments=[],
            section_status={},
            completeness_score=_decimal("1.0"),
            status=status,
            confirmation_type="none",
            confirmed_by_user=False,
            quality_warning=None,
            last_modified_by_user=False,
            source="system_online_smoke_seed",
            llm_model="system-online-smoke-seed",
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
                select(DailyReport).where(DailyReport.user_id == managed_user.id, DailyReport.report_date == report_date)
            )
        ).scalar_one_or_none()


async def count_reports(user: User, report_date: date = DEFAULT_REPORT_DATE) -> int:
    async with AsyncSessionLocal() as session:
        managed_user = await session.merge(user)
        return int(
            (
                await session.execute(
                    select(func.count(DailyReport.id)).where(
                        DailyReport.user_id == managed_user.id,
                        DailyReport.report_date == report_date,
                    )
                )
            ).scalar_one()
        )


async def post_manual(client: httpx.AsyncClient, user: User, raw_input: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dingtalk_user_id": user.dingtalk_user_id,
        "raw_input": raw_input,
        "source": "system_online_smoke",
        "report_date": DEFAULT_REPORT_DATE.isoformat(),
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    response = await client.post("/reports/manual", json=payload)
    response.raise_for_status()
    return response.json()


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def smoke_health_and_admin(client: httpx.AsyncClient) -> None:
    health = await client.get("/health")
    assert_true(health.status_code == 200 and health.json() == {"status": "ok"}, "health endpoint is not ok")

    settings = get_settings()
    admin_token = str(getattr(settings, "admin_token", "") or "").strip()
    admin_enabled = bool(getattr(settings, "admin_enabled", False))
    if admin_enabled and admin_token:
        no_token = await client.get("/admin/learning.json")
        wrong_token = await client.get("/admin/learning.json", headers={"X-Admin-Token": "wrong-token"})
        ok_token = await client.get("/admin/learning.json", headers={"X-Admin-Token": admin_token})
        assert_true(no_token.status_code == 403, f"admin learning without token returned {no_token.status_code}")
        assert_true(wrong_token.status_code == 403, f"admin learning wrong token returned {wrong_token.status_code}")
        assert_true(ok_token.status_code == 200, f"admin learning correct token returned {ok_token.status_code}")


async def smoke_manual_idempotency(client: httpx.AsyncClient) -> None:
    user = await ensure_user("idempotency")
    raw = "今日工作：完成线上幂等测试\n问题/风险：暂无\n明日计划：继续做幂等回归"
    first = await post_manual(client, user, raw, idempotency_key="system-smoke-idempotency-001")
    second = await post_manual(client, user, raw, idempotency_key="system-smoke-idempotency-001")
    assert_true(first.get("report_id") == second.get("report_id"), "manual idempotency did not return same report_id")
    assert_true(await count_reports(user) == 1, "manual idempotency created duplicate reports")


async def smoke_same_user_concurrency(client: httpx.AsyncClient) -> None:
    user = await ensure_user("same-user-concurrency")
    messages = [
        f"今日工作：同用户并发测试第{i}条合同审核\n问题/风险：暂无\n明日计划：继续跟进同用户并发第{i}条"
        for i in range(1, 6)
    ]
    responses = await asyncio.gather(
        *[
            post_manual(client, user, message, idempotency_key=f"system-smoke-same-user-{index}")
            for index, message in enumerate(messages, start=1)
        ]
    )
    assert_true(all(item.get("report_id") for item in responses), "same-user concurrency returned empty report_id")
    assert_true(await count_reports(user) == 1, "same-user concurrency created duplicate daily_reports")


async def smoke_multi_user_isolation(client: httpx.AsyncClient) -> None:
    users = [await ensure_user(f"multi-user-{index:02d}") for index in range(1, 21)]

    async def submit(index: int, user: User) -> tuple[int, dict[str, Any]]:
        marker = f"隔离标记-{index:02d}"
        raw = f"今日工作：完成线上多用户隔离测试 {marker}\n问题/风险：暂无\n明日计划：继续跟进 {marker}"
        return index, await post_manual(client, user, raw, idempotency_key=f"system-smoke-multi-{index:02d}")

    responses = await asyncio.gather(*[submit(index, user) for index, user in enumerate(users, start=1)])
    assert_true(len(responses) == 20 and all(payload.get("report_id") for _index, payload in responses), "multi-user submit failed")
    for index, user in enumerate(users, start=1):
        report = await get_report(user)
        marker = f"隔离标记-{index:02d}"
        assert_true(report is not None, f"multi-user report missing for {marker}")
        assert_true(marker in (report.raw_input or ""), f"multi-user raw_input marker missing for {marker}")
        for other_index in range(1, 21):
            other_marker = f"隔离标记-{other_index:02d}"
            if other_marker != marker:
                assert_true(other_marker not in (report.raw_input or ""), f"cross-user leak: {other_marker} appeared in {marker}")


async def smoke_whole_copy_and_numbered_preview(client: httpx.AsyncClient) -> None:
    user = await ensure_user("whole-copy")
    await seed_report(
        user,
        date(2026, 6, 24),
        today_work=["日报系统的新一轮优化", "整理被告数据", "完成中建合同线上确认"],
        problems=["没碰到什么问题"],
        tomorrow_plan=["明天等额度刷新后开始做日报系统的案件进展与出差协同俩个outbox"],
    )
    response = await post_manual(client, user, "把6月24日日报整篇复制到今天")
    report = await get_report(user)
    assert_true(report is not None, "whole copy did not create today's report")
    assert_true(report.today_work == ["日报系统的新一轮优化", "整理被告数据", "完成中建合同线上确认"], "whole copy today_work mismatch")
    assert_true(report.problems == ["没碰到什么问题"], "whole copy problems mismatch")
    assert_true(report.tomorrow_plan == ["明天等额度刷新后开始做日报系统的案件进展与出差协同俩个outbox"], "whole copy tomorrow_plan mismatch")
    assert_true("1." in (response.get("message") or ""), "whole copy preview did not keep numbering")


async def smoke_previous_plan_range(client: httpx.AsyncClient) -> None:
    user = await ensure_user("previous-plan-range")
    plan_items = [
        "日常用印流程审批",
        "现场用印审核",
        "电子章用印",
        "未归档合同催收",
        "用印事宜咨询答复",
        "合同归档整理移交资料室",
    ]
    await seed_report(
        user,
        DEFAULT_REPORT_DATE - timedelta(days=1),
        today_work=["seed"],
        problems=["seed"],
        tomorrow_plan=plan_items,
    )
    await post_manual(
        client,
        user,
        "昨天除了第6项没有做之外，其他正常完成。今日工作计划同昨日计划的1~5项。昨日未碰到无法解决的问题。",
    )
    report = await get_report(user)
    joined_today = "\n".join(report.today_work if report else [])
    joined_tomorrow = "\n".join(report.tomorrow_plan if report else [])
    for item in plan_items[:5]:
        assert_true(item in joined_today, f"previous plan item not expanded: {item}")
    assert_true(plan_items[5] not in joined_today, "unfinished sixth item was copied into today_work")
    assert_true("完成昨日计划" not in joined_today, "placeholder previous-plan text remained in today_work")
    assert_true("第6项未完成" not in joined_today + "\n" + joined_tomorrow, "unfinished status text leaked into report")


async def smoke_current_short_display(client: httpx.AsyncClient) -> None:
    user = await ensure_user("current-short-display")
    await seed_report(
        user,
        DEFAULT_REPORT_DATE,
        today_work=["处理当前草稿查询问题"],
        problems=["暂无"],
        tomorrow_plan=["继续验证当前草稿"],
    )
    before = await get_report(user)
    response = await post_manual(client, user, "发我下")
    after = await get_report(user)
    message = response.get("message") or ""
    assert_true(response.get("reply_kind") == "agent_query_current", f"short display wrong reply_kind: {response.get('reply_kind')}")
    assert_true("处理当前草稿查询问题" in message, "short display did not return current draft content")
    assert_true(before is not None and after is not None, "short display report missing")
    assert_true(before.today_work == after.today_work and before.problems == after.problems and before.tomorrow_plan == after.tomorrow_plan, "short display modified content")


SMOKES = [
    ("health_and_admin", smoke_health_and_admin),
    ("manual_idempotency", smoke_manual_idempotency),
    ("same_user_concurrency", smoke_same_user_concurrency),
    ("multi_user_isolation", smoke_multi_user_isolation),
    ("whole_copy_and_numbered_preview", smoke_whole_copy_and_numbered_preview),
    ("previous_plan_range", smoke_previous_plan_range),
    ("current_short_display", smoke_current_short_display),
]


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run online system smoke checks against the live local production API and DB.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="outputs/online_system_smoke_latest.json")
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()

    if args.cleanup_only:
        await cleanup_all_smoke_data()
        print("ONLINE_SYSTEM_SMOKE_CLEANUP_DONE", flush=True)
        return 0

    await cleanup_all_smoke_data()
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=120.0, trust_env=False) as client:
        for name, smoke in SMOKES:
            case_started = time.perf_counter()
            try:
                await smoke(client)
                result = {"name": name, "status": "PASS", "seconds": round(time.perf_counter() - case_started, 1), "errors": []}
            except Exception as exc:
                result = {
                    "name": name,
                    "status": "FAIL",
                    "seconds": round(time.perf_counter() - case_started, 1),
                    "errors": [f"{type(exc).__name__}: {exc}"],
                }
            results.append(result)
            print(f"ONLINE_SYSTEM_SMOKE_PROGRESS {result['status']} {name}", flush=True)

    await cleanup_all_smoke_data()

    failed = [item for item in results if item["status"] != "PASS"]
    summary = {
        "pass": len(results) - len(failed),
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

    print("ONLINE_SYSTEM_SMOKE_START", flush=True)
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"OUTPUT {output}", flush=True)
    print("FAILURES_START", flush=True)
    for item in failed:
        print(json.dumps(item, ensure_ascii=False), flush=True)
    print("FAILURES_END", flush=True)
    print("ONLINE_SYSTEM_SMOKE_END", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
