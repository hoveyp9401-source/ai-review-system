from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.typed_daily_executor import TYPED_AUDIT_KEY
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import DailyReport
from scripts.smoke_20260811_overnight_daily_rollback import (
    PANG_USER_ID,
    _turn,
    _user_and_control,
)


RUN_ID = f"unified-daily-rollback-{uuid4()}"
BASE_DATE = date(2026, 4, 6)


def _snapshot(report: DailyReport) -> dict[str, object]:
    return {
        "today_work": list(report.today_work or ()),
        "problems": list(report.problems or ()),
        "tomorrow_plan": list(report.tomorrow_plan or ()),
        "status": report.status,
        "confirmation_type": report.confirmation_type,
        "confirmed_by_user": report.confirmed_by_user,
    }


def _seed_report(*, user, report_date: date) -> DailyReport:
    return DailyReport(
        user_id=user.id,
        team_id=user.team_id,
        report_date=report_date,
        today_work=["完成合同初稿复核", "整理付款材料"],
        problems=[],
        tomorrow_plan=["跟进回款安排"],
        section_status={
            "_agent2_report_version": 4,
            "_draft_item_ids": {
                "today_work": ["tw-1", "tw-2"],
                "problems": [],
                "tomorrow_plan": ["tp-1"],
            },
            "problems_acknowledged_empty": True,
            TYPED_AUDIT_KEY: [],
        },
        completeness_score=Decimal("1"),
        status="collecting",
        confirmation_type="none",
        confirmed_by_user=False,
        source="unified_daily_rollback_smoke",
    )


async def _receipts(session, source_message_id: str) -> list[ToolCallCanaryReceipt]:
    return list(
        (
            await session.scalars(
                select(ToolCallCanaryReceipt)
                .where(ToolCallCanaryReceipt.source_message_id == source_message_id)
                .order_by(ToolCallCanaryReceipt.created_at)
            )
        ).all()
    )


async def _assert_unused_date(session, *, user_id, report_date: date) -> None:
    existing = await session.scalar(
        select(DailyReport.id).where(
            DailyReport.user_id == user_id,
            DailyReport.report_date == report_date,
        )
    )
    if existing is not None:
        raise AssertionError({"preexisting_report_date": report_date.isoformat()})


async def _run_add_case(
    llm_client: LLMClient,
    *,
    name: str,
    report_date: date,
    text: str,
    required: dict[str, tuple[str, ...]],
    expected_status: str,
    expected_empty_fields: tuple[str, ...] = (),
    expected_field_counts: dict[str, int] | None = None,
) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            await _assert_unused_date(
                session, user_id=user.id, report_date=report_date
            )
            source_message_id = f"{RUN_ID}-{name}"
            outcome = await _turn(
                session,
                user=user,
                settings=settings,
                llm_client=llm_client,
                text=text,
                conversation_id=f"{RUN_ID}-conversation-{name}",
                source_message_id=source_message_id,
                now=datetime.combine(
                    report_date,
                    datetime.min.time().replace(hour=10),
                    tzinfo=ZoneInfo(user.timezone or settings.timezone),
                ),
                accepted_business_results=frozenset({"success", "reply_only"}),
            )
            await session.flush()
            report = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == report_date,
                )
            )
            if report is None:
                raise AssertionError("daily report was not created")
            for field, fragments in required.items():
                body = "\n".join(getattr(report, field) or ())
                missing = [fragment for fragment in fragments if fragment not in body]
                if missing:
                    raise AssertionError({"field": field, "missing": missing})
            if report.status != expected_status:
                raise AssertionError(
                    {"expected_status": expected_status, "actual_status": report.status}
                )
            for field in expected_empty_fields:
                if getattr(report, field):
                    raise AssertionError(
                        {"expected_empty_field": field, "values": getattr(report, field)}
                    )
                if not bool(
                    (report.section_status or {}).get(
                        f"{field}_acknowledged_empty"
                    )
                ):
                    raise AssertionError(
                        {"missing_empty_acknowledgement": field}
                    )
            for field, expected_count in (expected_field_counts or {}).items():
                actual_count = len(getattr(report, field) or ())
                if actual_count != expected_count:
                    raise AssertionError(
                        {
                            "field": field,
                            "expected_count": expected_count,
                            "actual_count": actual_count,
                            "values": list(getattr(report, field) or ()),
                        }
                    )
            receipts = await _receipts(session, source_message_id)
            if [row.tool_name for row in receipts] != ["add_daily_items"]:
                raise AssertionError(
                    {"unexpected_tools": [row.tool_name for row in receipts]}
                )
            return {
                "name": name,
                "status": "pass",
                "tools": [row.tool_name for row in receipts],
                "business_result": outcome.user_visible_result,
                "report_status": report.status,
                "item_count": sum(
                    len(getattr(report, field) or ())
                    for field in ("today_work", "problems", "tomorrow_plan")
                ),
                "field_counts": {
                    field: len(getattr(report, field) or ())
                    for field in ("today_work", "problems", "tomorrow_plan")
                },
                "acknowledged_empty_fields": sorted(
                    field
                    for field in ("today_work", "problems", "tomorrow_plan")
                    if bool(
                        (report.section_status or {}).get(
                            f"{field}_acknowledged_empty"
                        )
                    )
                ),
            }
        finally:
            await session.rollback()


async def _run_existing_case(
    llm_client: LLMClient,
    *,
    name: str,
    report_date: date,
    text: str,
    expected_tool: str,
    assert_after,
    accepted_business_results: frozenset[str] = frozenset(
        {"success", "reply_only"}
    ),
) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            await _assert_unused_date(
                session, user_id=user.id, report_date=report_date
            )
            report = _seed_report(user=user, report_date=report_date)
            session.add(report)
            await session.flush()
            before = _snapshot(report)
            source_message_id = f"{RUN_ID}-{name}"
            outcome = await _turn(
                session,
                user=user,
                settings=settings,
                llm_client=llm_client,
                text=text,
                conversation_id=f"{RUN_ID}-conversation-{name}",
                source_message_id=source_message_id,
                now=datetime.combine(
                    report_date,
                    datetime.min.time().replace(hour=10),
                    tzinfo=ZoneInfo(user.timezone or settings.timezone),
                ),
                accepted_business_results=accepted_business_results,
            )
            await session.flush()
            await session.refresh(report)
            after = _snapshot(report)
            assert_after(before, after)
            receipts = await _receipts(session, source_message_id)
            tools = [row.tool_name for row in receipts]
            if tools != [expected_tool]:
                raise AssertionError(
                    {"expected_tool": expected_tool, "actual_tools": tools}
                )
            return {
                "name": name,
                "status": "pass",
                "tools": tools,
                "business_result": outcome.user_visible_result,
                "changed": before != after,
            }
        finally:
            await session.rollback()


def _assert_submit(before: dict[str, object], after: dict[str, object]) -> None:
    if after["status"] != "completed" or not after["confirmed_by_user"]:
        raise AssertionError({"submit_after": after})
    for field in ("today_work", "problems", "tomorrow_plan"):
        if after[field] != before[field]:
            raise AssertionError({"submit_changed_content": field})


def _assert_query(before: dict[str, object], after: dict[str, object]) -> None:
    if after != before:
        raise AssertionError("query changed the report")


def _assert_edit(before: dict[str, object], after: dict[str, object]) -> None:
    if "完成合同终稿复核" not in "\n".join(after["today_work"]):
        raise AssertionError({"edit_after": after})
    if after["status"] not in {"collecting", "pending_confirmation"}:
        raise AssertionError({"edit_report_status": after["status"]})


def _assert_delete(before: dict[str, object], after: dict[str, object]) -> None:
    if "整理付款材料" in after["today_work"] or len(after["today_work"]) != 1:
        raise AssertionError({"delete_after": after})
    if after["status"] not in {"collecting", "pending_confirmation"}:
        raise AssertionError({"delete_report_status": after["status"]})


def _assert_clear_requested(
    before: dict[str, object], after: dict[str, object]
) -> None:
    if after != before:
        raise AssertionError("clear request changed content before confirmation")


async def _residue() -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        report_count = int(
            await session.scalar(
                select(func.count(DailyReport.id)).where(
                    DailyReport.source == "unified_daily_rollback_smoke"
                )
            )
            or 0
        )
        receipt_count = int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                    ToolCallCanaryReceipt.source_message_id.like(f"{RUN_ID}%")
                )
            )
            or 0
        )
        await session.rollback()
        return {"reports": report_count, "receipts": receipt_count}


async def main() -> None:
    initial = await _residue()
    if any(initial.values()):
        raise AssertionError({"preexisting_residue": initial})
    llm_client = LLMClient(get_settings())
    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    cases = [
        (
            "short_add",
            lambda: _run_add_case(
                llm_client,
                name="short_add",
                report_date=BASE_DATE,
                text="今天完成合同审核。",
                required={"today_work": ("合同审核",)},
                expected_status="collecting",
            ),
        ),
        (
            "long_add",
            lambda: _run_add_case(
                llm_client,
                name="long_add",
                report_date=BASE_DATE + timedelta(days=1),
                text=(
                    "帮我填写今天的日报。今日工作：1.复核采购合同；2.整理付款资料；"
                    "3.核对用印清单；4.参加项目会议；5.回复业务咨询；6.修订合作协议；"
                    "7.跟进诉讼材料；8.更新合同台账；9.确认发票信息；10.复核授权文件；"
                    "11.整理会议纪要；12.沟通履约安排。"
                ),
                required={
                    "today_work": (
                        "采购合同",
                        "付款资料",
                        "用印清单",
                        "项目会议",
                        "业务咨询",
                        "合作协议",
                        "诉讼材料",
                        "合同台账",
                        "发票信息",
                        "授权文件",
                        "会议纪要",
                        "履约安排",
                    )
                },
                expected_status="collecting",
            ),
        ),
        (
            "same_turn_submit_without_risk",
            lambda: _run_add_case(
                llm_client,
                name="same_turn_submit_without_risk",
                report_date=BASE_DATE + timedelta(days=2),
                text="今天完成合同复核，明天继续整理附件。请提交日报。",
                required={
                    "today_work": ("合同复核",),
                    "tomorrow_plan": ("整理附件",),
                },
                expected_status="completed",
                expected_empty_fields=("problems",),
            ),
        ),
        (
            "same_turn_colloquial_no_problem_submit",
            lambda: _run_add_case(
                llm_client,
                name="same_turn_colloquial_no_problem_submit",
                report_date=BASE_DATE + timedelta(days=11),
                text=(
                    "今天完成合同复核，没啥问题，"
                    "明天继续跟进项目，请提交日报。"
                ),
                required={
                    "today_work": ("合同复核",),
                    "tomorrow_plan": ("跟进项目",),
                },
                expected_status="completed",
            ),
        ),
        (
            "late_aug17_short_report",
            lambda: _run_add_case(
                llm_client,
                name="late_aug17_short_report",
                report_date=BASE_DATE + timedelta(days=8),
                text=(
                    "今日工作：\n"
                    "1. 蓝海风案信托材料报审待确认\n"
                    "2. 继续诉讼评估谢忠柏、王伟两个班组超付案件，核对原件等材料\n"
                    "3. 中天系列债权联系管理人\n\n"
                    "明日计划：\n"
                    "1. 不夜城案和分公司沟通下一步计划\n\n"
                    "问题风险已按要求记为无。"
                ),
                required={
                    "today_work": ("蓝海风", "谢忠柏", "中天系列"),
                    "tomorrow_plan": ("不夜城",),
                },
                expected_status="pending_confirmation",
            ),
        ),
        (
            "morning_long_redictation",
            lambda: _run_add_case(
                llm_client,
                name="morning_long_redictation",
                report_date=BASE_DATE + timedelta(days=9),
                text=(
                    "今天主要做以下几件事情，第一个是评估一下岳阳广济医院项目的诉讼项目，跟进一下流转单。"
                    "第二个是郑州高投的项目，因需要进行行政的报批报报批备案，以及需要总包进行配合盖章资料。"
                    "总包要求我们集团公司出一个承诺书以及盖章的说明，实质是要求集团公司进行背书和担保。"
                    "需要给对方进行写一个函，明确说明一下，拒绝对方。"
                    "第三个是张家港永卓永煤项目的硬盘材料涨价调查的这个补充协议，跟进一下对方的盖章情况。"
                    "第四个是继续配合梳理一下这个南京园博园项目，一诉评估以及这个后续资料的收收集。"
                    "第五个事情是还有两个项目的标前的风控评审，进行一诉，进行那个风控提报。"
                    "明天的主要工作是完成新疆那拉提酒店项目的民宿评估以及提报，"
                    "然后跟一下悦榕庄酒店项目对方二审上诉的进展。"
                    "第三个是梳理月底前工期没有闭环的工期项目，根据工期手续的闭环以及上下游履约资料的梳理"
                ),
                required={
                    "today_work": (
                        "岳阳广济医院",
                        "郑州高投",
                        "张家港永卓永煤",
                        "南京园博园",
                        "标前",
                        "风控评审",
                    ),
                    "tomorrow_plan": (
                        "新疆那拉提",
                        "悦榕庄酒店",
                        "月底前工期",
                    ),
                },
                expected_status="collecting",
            ),
        ),
        (
            "morning_explicit_dated_complete",
            lambda: _run_add_case(
                llm_client,
                name="morning_explicit_dated_complete",
                report_date=BASE_DATE + timedelta(days=10),
                text=(
                    "2026年4月16日 日报（已完成）\n\n"
                    "今日工作\n1. 今日请假\n\n"
                    "问题风险\n（无）\n\n"
                    "明日计划\n1. 日常用印审核\n2. 未归档协议跟盯\n"
                    "3. 未归档施工合同跟盯\n4. 项目章审计"
                ),
                required={
                    "today_work": ("今日请假",),
                    "tomorrow_plan": (
                        "日常用印审核",
                        "未归档协议跟盯",
                        "未归档施工合同跟盯",
                        "项目章审计",
                    ),
                },
                expected_status="pending_confirmation",
            ),
        ),
        (
            "pang_initial_semantic_split",
            lambda: _run_add_case(
                llm_client,
                name="pang_initial_semantic_split",
                report_date=BASE_DATE + timedelta(days=12),
                text=(
                    "今天：1修复了日报agent的bug。"
                    "2.将合同评审技能变成了网页端的网页agent调用速度快了10倍，"
                    "明天计划继续找可以做成网页端的agent技能"
                    "然后被告案件进行通报与未结案案件的签约 没啥别的问题"
                ),
                required={
                    "today_work": ("日报agent", "合同评审技能"),
                    "tomorrow_plan": ("网页端的agent技能", "被告案件"),
                },
                expected_status="pending_confirmation",
                expected_empty_fields=("problems",),
                expected_field_counts={
                    "today_work": 2,
                    "problems": 0,
                    "tomorrow_plan": 2,
                },
            ),
        ),
        (
            "submit_only",
            lambda: _run_existing_case(
                llm_client,
                name="submit_only",
                report_date=BASE_DATE + timedelta(days=3),
                text="没有要增加、修改或删除的内容，帮我提交日报。",
                expected_tool="confirm_report",
                assert_after=_assert_submit,
            ),
        ),
        (
            "query_today",
            lambda: _run_existing_case(
                llm_client,
                name="query_today",
                report_date=BASE_DATE + timedelta(days=4),
                text="给我看一下今天的日报。",
                expected_tool="query_today_report",
                assert_after=_assert_query,
            ),
        ),
        (
            "edit_today",
            lambda: _run_existing_case(
                llm_client,
                name="edit_today",
                report_date=BASE_DATE + timedelta(days=5),
                text="把今天日报今日工作的第一条改成：完成合同终稿复核。",
                expected_tool="edit_daily_items",
                assert_after=_assert_edit,
            ),
        ),
        (
            "delete_today",
            lambda: _run_existing_case(
                llm_client,
                name="delete_today",
                report_date=BASE_DATE + timedelta(days=6),
                text="删除今天日报今日工作的第二条。",
                expected_tool="delete_daily_items",
                assert_after=_assert_delete,
            ),
        ),
        (
            "clear_today",
            lambda: _run_existing_case(
                llm_client,
                name="clear_today",
                report_date=BASE_DATE + timedelta(days=7),
                text="清空今天的日报草稿。",
                expected_tool="request_clear_report",
                assert_after=_assert_clear_requested,
            ),
        ),
    ]
    selected = {
        value.strip()
        for value in os.getenv("SMOKE_CASE_NAMES", "").split(",")
        if value.strip()
    }
    available = {name for name, _run in cases}
    if selected - available:
        raise RuntimeError(
            f"unknown smoke cases: {sorted(selected - available)}"
        )
    try:
        for name, run in cases:
            if selected and name not in selected:
                continue
            try:
                results.append(await run())
            except Exception as exc:
                failures.append(
                    {"name": name, "error": f"{type(exc).__name__}: {exc}"}
                )
    finally:
        await llm_client.close()
    final = await _residue()
    output = {
        "status": "pass" if not failures and not any(final.values()) else "failed",
        "passed": len(results),
        "failed": len(failures),
        "dingtalk_send_calls": 0,
        "rollback_residue": final,
        "results": results,
        "failures": failures,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    await engine.dispose()
    if output["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
