from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import date
from pathlib import Path

from sqlalchemy import select


def load_service_access_environment() -> None:
    pid = subprocess.check_output(
        ["systemctl", "show", "ai-review-api.service", "-p", "MainPID", "--value"],
        text=True,
    ).strip()
    if not pid.isdigit():
        raise AssertionError("API service PID is unavailable")
    entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    for entry in entries:
        if b"=" not in entry:
            continue
        key_bytes, value_bytes = entry.split(b"=", 1)
        key = key_bytes.decode("utf-8", errors="strict")
        if key in {
            "AGENT2_FACT_ALL_ACCESS_DINGTALK_USER_IDS",
            "AGENT2_FACT_ALL_ACCESS_NAMES",
        }:
            os.environ[key] = value_bytes.decode("utf-8", errors="strict")


load_service_access_environment()

from app.agent2.fact_permissions import ALL_ACCESS_DINGTALK_USER_IDS
from app.agent2.report_insight_intent import is_report_insight_question
from app.agent2.report_insights import load_live_report_insight_answer
from app.db import AsyncSessionLocal
from app.models import DailyReport, Team, User


QUESTIONS = (
    ("person_count", "庞浩目前有多少份日报了？"),
    ("person_recent", "总结下庞浩最近的工作"),
    ("team_current_week", "总结下综合管理部本周都做了什么"),
    ("team_previous_week", "总结下综合管理部上周都做了什么"),
    ("department_attention", "最近部门有什么重点需要关注的事情吗？"),
    ("person_unclosed", "看下刘聪有什么没闭环的工作"),
    ("team_unclosed", "看下综合部没闭环的工作"),
)


async def report_fingerprint(session) -> tuple[tuple[str, str], ...]:
    rows = (
        await session.execute(
            select(DailyReport.id, DailyReport.updated_at).order_by(DailyReport.id)
        )
    ).all()
    return tuple((str(report_id), updated_at.isoformat()) for report_id, updated_at in rows)


async def main() -> None:
    results: list[dict[str, object]] = []
    async with AsyncSessionLocal() as session:
        organization_catalog = [
            {"team": str(team_name), "department": str(department_name)}
            for team_name, department_name in (
                await session.execute(
                    select(Team.name, Team.department_name)
                    .where(Team.active.is_(True))
                    .order_by(Team.name)
                )
            ).all()
        ]
        requester = (
            await session.execute(
                select(User).where(
                    User.dingtalk_user_id.in_(ALL_ACCESS_DINGTALK_USER_IDS),
                    User.active.is_(True),
                )
            )
        ).scalars().first()
        if requester is None:
            configured_names = tuple(
                value.strip()
                for value in os.getenv("AGENT2_FACT_ALL_ACCESS_NAMES", "").split(",")
                if value.strip()
            )
            raise AssertionError(
                "privileged production requester is missing "
                f"configured_id_count={len(ALL_ACCESS_DINGTALK_USER_IDS)} "
                f"configured_name_count={len(configured_names)}"
            )

        before = await report_fingerprint(session)
        for label, question in QUESTIONS:
            if not is_report_insight_question(question):
                raise AssertionError(f"intent not recognized: {label}")
            answer = await load_live_report_insight_answer(
                session,
                requester=requester,
                text=question,
                current_date=date(2026, 8, 6),
            )
            if answer is None:
                raise AssertionError(f"no answer returned: {label}")
            if answer.evidence.source_type != "daily_report_insight":
                raise AssertionError(f"wrong evidence source: {label}")
            facts = answer.evidence.facts
            results.append(
                {
                    "case": label,
                    "query_kind": facts.get("query_kind", ""),
                    "scope_label": facts.get("scope_label", ""),
                    "report_count": facts.get("report_count"),
                    "unclosed_count": facts.get("unclosed_count"),
                    "reply_length": len(answer.text),
                }
            )
        after = await report_fingerprint(session)
        if after != before:
            raise AssertionError("daily report rows changed during read-only smoke")
        await session.rollback()

    print(
        json.dumps(
            {
                "status": "pass",
                "cases": results,
                "organization_catalog": organization_catalog,
                "daily_report_rows_unchanged": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
