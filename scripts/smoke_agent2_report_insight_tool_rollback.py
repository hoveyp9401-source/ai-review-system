from __future__ import annotations

import asyncio
from datetime import date
import json
import logging
import os
import re

from sqlalchemy import select

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.canary_service import (
    process_tool_call_canary_ingress,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import DailyReport, User
from app.utils.time import now_in_timezone


PANG_DINGTALK_ID = "40842"
REPORT_DATE = date(2026, 8, 6)
CASES = (
    (
        "chat_preference",
        "叫我庞总",
        ("remember_personal_memory",),
    ),
    ("chat_feedback", "有点无语", ()),
    ("chat_question", "今天周几", ()),
    (
        "person_count",
        "庞浩目前有多少份日报了？",
        ("query_report_insights",),
    ),
    (
        "person_recent",
        "总结下庞浩最近的工作",
        ("query_report_insights",),
    ),
    (
        "team_current_week",
        "总结下综合管理部本周都做了什么？",
        ("query_report_insights",),
    ),
    (
        "team_previous_week",
        "总结下综合管理部上周都做了什么？",
        ("query_report_insights",),
    ),
    (
        "department_attention",
        "最近综合管理部有什么重点需要关注的事情吗？",
        ("query_report_insights",),
    ),
    (
        "person_unclosed",
        "看下刘聪有什么没闭环的工作。",
        ("query_report_insights",),
    ),
    (
        "organization_unclosed",
        "看下综合管理部没闭环的工作。",
        ("query_report_insights",),
    ),
    (
        "organization_unclosed_recent_30_days",
        "看下综合管理部最近30天没闭环的工作。",
        ("query_report_insights",),
    ),
    (
        "weng_previous_week_unclosed",
        "看下翁亚兰上周的未闭环",
        ("query_report_insights",),
    ),
    (
        "two_periods",
        "总结下综合管理部本周和上周都做了什么？",
        ("query_report_insights", "query_report_insights"),
    ),
)


def _snapshot(report: DailyReport) -> dict[str, object]:
    return {
        "today_work": list(report.today_work or []),
        "problems": list(report.problems or []),
        "tomorrow_plan": list(report.tomorrow_plan or []),
        "status": str(report.status),
        "version": int(getattr(report, "version", 0) or 0),
    }


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    llm_client = LLMClient(settings)
    now = now_in_timezone(settings.timezone)
    results: list[dict[str, object]] = []
    compact_output = str(os.getenv("SMOKE_COMPACT", "") or "") == "1"
    before: dict[str, object]
    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(
                User.dingtalk_user_id == PANG_DINGTALK_ID,
                User.active.is_(True),
            )
        )
        assert user is not None
        report = await session.scalar(
            select(DailyReport).where(
                DailyReport.user_id == user.id,
                DailyReport.report_date == REPORT_DATE,
            )
        )
        assert report is not None
        before = _snapshot(report)
        control = await session.scalar(
            select(ToolCallCanaryControl).where(
                ToolCallCanaryControl.user_id == str(user.id)
            )
        )
        assert control is not None
        control.enabled = True
        control.messages_enabled = True
        control.registry_digest = runtime_registry_contract_digest(settings)
        control.prompt_sha256 = canary_prompt_sha256()
        control.model_name = CANARY_MODEL_NAME
        await session.flush()

        selected_case = str(os.getenv("SMOKE_CASE", "") or "").strip()
        selected_cases = tuple(
            item for item in CASES if not selected_case or item[0] == selected_case
        )
        assert selected_cases, selected_case
        for index, (case_name, message, expected_tools) in enumerate(
            selected_cases,
            start=1,
        ):
            source_message_id = f"rollback-insight-tool-{index}"
            outcome = await process_tool_call_canary_ingress(
                session,
                user=user,
                dingtalk_user_id=user.dingtalk_user_id,
                user_text=message,
                source_channel="rollback_insight_tool_smoke",
                conversation_id="pang-report-insight-tool-smoke-20260806",
                source_message_id=source_message_id,
                settings=settings,
                llm_client=llm_client,
                now=now,
            )
            await session.flush()
            await session.refresh(report)
            receipts = list(
                (
                    await session.scalars(
                        select(ToolCallCanaryReceipt)
                        .where(
                            ToolCallCanaryReceipt.user_id == str(user.id),
                            ToolCallCanaryReceipt.source_message_id
                            == source_message_id,
                        )
                        .order_by(ToolCallCanaryReceipt.created_at)
                    )
                ).all()
            )
            tool_names = tuple(item.tool_name for item in receipts)
            assert outcome.owner == "tool_call_core", (case_name, outcome)
            assert outcome.handled is True, (case_name, outcome)
            assert outcome.actual_write is False, (case_name, outcome)
            assert _snapshot(report) == before, (case_name, _snapshot(report))
            assert tool_names == expected_tools, (
                case_name,
                tool_names,
                outcome.message,
            )
            assert "庞总" in outcome.message, (case_name, outcome.message)
            for receipt in receipts:
                assert receipt.changed is False
                assert receipt.safe_user_facts.get("actual_write") is False
            if case_name == "two_periods":
                period_types = {
                    str(
                        receipt.safe_user_facts.get("report_insight", {})
                        .get("facts", {})
                        .get("period_type", "")
                    )
                    for receipt in receipts
                }
                assert period_types == {"current_week", "previous_week"}, (
                    period_types,
                    outcome.message,
                )
            if case_name == "weng_previous_week_unclosed":
                insight_facts = (
                    receipts[0]
                    .safe_user_facts.get("report_insight", {})
                    .get("facts", {})
                )
                assert insight_facts.get("unclosed_count") == 3, (
                    insight_facts,
                    outcome.message,
                )
                assert not re.search(r"(?m)^\s*\d+[.、]\s*\d+[.、]", outcome.message), (
                    outcome.message
                )
                assert "11 项" not in outcome.message
            if case_name == "organization_unclosed":
                insight_facts = (
                    receipts[0]
                    .safe_user_facts.get("report_insight", {})
                    .get("facts", {})
                )
                if insight_facts.get("needs_time_scope") is True:
                    assert insight_facts.get("period_type") == "unspecified"
                    assert insight_facts.get("unclosed_items") == []
                    assert int(
                        insight_facts.get("unclosed_items_withheld_count") or 0
                    ) == int(insight_facts.get("unclosed_count") or 0)
                    compact_reply = re.sub(r"\s+", "", outcome.message)
                    assert all(
                        option in compact_reply
                        for option in ("最近7天", "最近30天", "全部历史")
                    ), outcome.message
                    assert not any(
                        label in outcome.message
                        for label in (
                            "已闭环",
                            "跟进中",
                            "进行中",
                            "待核实",
                            "无法判断",
                        )
                    ), outcome.message
            if case_name == "person_unclosed":
                insight_facts = (
                    receipts[0]
                    .safe_user_facts.get("report_insight", {})
                    .get("facts", {})
                )
                assert insight_facts.get("period_type") == "unspecified"
                if insight_facts.get("needs_time_scope") is True:
                    assert insight_facts.get("unclosed_items") == []
                    compact_reply = re.sub(r"\s+", "", outcome.message)
                    assert all(
                        option in compact_reply
                        for option in ("最近7天", "最近30天", "全部历史")
                    ), outcome.message
                    assert not any(
                        label in outcome.message
                        for label in (
                            "已闭环",
                            "跟进中",
                            "进行中",
                            "待核实",
                            "无法判断",
                        )
                    ), outcome.message
            if case_name == "organization_unclosed_recent_30_days":
                insight_facts = (
                    receipts[0]
                    .safe_user_facts.get("report_insight", {})
                    .get("facts", {})
                )
                assert insight_facts.get("period_type") == "recent_30_days"
                assert insight_facts.get("period_start") == "2026-07-11"
                assert insight_facts.get("period_end") == "2026-08-09"
                assert insight_facts.get("needs_time_scope") is False
                if insight_facts.get("unclosed_preview_truncated") is True:
                    compact_reply = re.sub(r"\s+", "", outcome.message)
                    total = int(insight_facts.get("unclosed_count") or 0)
                    preview = int(
                        insight_facts.get("unclosed_preview_count") or 0
                    )
                    remaining = int(
                        insight_facts.get("unclosed_remaining_count") or 0
                    )
                    assert f"共{total}项" in compact_reply, outcome.message
                    assert f"前{preview}项" in compact_reply, outcome.message
                    assert str(remaining) in compact_reply and any(
                        label in compact_reply
                        for label in ("另有", "剩余", "其余", "还有", "未展开")
                    ), outcome.message
            if case_name == "department_attention":
                insight_facts = (
                    receipts[0]
                    .safe_user_facts.get("report_insight", {})
                    .get("facts", {})
                )
                problem_texts = [
                    str(item.get("text") or "")
                    for item in insight_facts.get("problem_items", [])
                ]
                plan_texts = [
                    str(item.get("text") or "")
                    for item in insight_facts.get("plan_items", [])
                ]
                stale_performance_issue = (
                    "绩效回顾面谈表四部、三部、综合部尚未收集完成"
                )
                assert stale_performance_issue not in problem_texts
                assert all("收齐后发人力中心闭环" not in item for item in plan_texts)
                assert int(insight_facts.get("resolved_problem_count") or 0) >= 1
                assert not re.search(
                    r"翁亚兰[^\n]{0,40}绩效|绩效[^\n]{0,40}翁亚兰",
                    outcome.message,
                ), outcome.message
            results.append(
                {
                    "case": case_name,
                    "message": message,
                    "owner": outcome.owner,
                    "tool_names": list(tool_names),
                    "actual_write": outcome.actual_write,
                    "reply": (
                        outcome.message[:240] if compact_output else outcome.message
                    ),
                    "daily_unchanged": True,
                }
            )
        await session.rollback()

    async with AsyncSessionLocal() as session:
        user = await session.scalar(
            select(User).where(User.dingtalk_user_id == PANG_DINGTALK_ID)
        )
        assert user is not None
        report = await session.scalar(
            select(DailyReport).where(
                DailyReport.user_id == user.id,
                DailyReport.report_date == REPORT_DATE,
            )
        )
        assert report is not None
        assert _snapshot(report) == before

    await engine.dispose()
    print(
        json.dumps(
            {
                "status": "pass",
                "case_count": len(results),
                "cases": results,
                "rollback_verified": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
