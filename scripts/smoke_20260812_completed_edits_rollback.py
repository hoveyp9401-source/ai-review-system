from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import func, select

from app.agent2.tool_calling import canary_service
from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.typed_daily_executor import TYPED_AUDIT_KEY
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import Agent2DailyCommandReceipt, DailyReport
from scripts.smoke_20260811_overnight_daily_rollback import (
    PANG_USER_ID,
    _receipts,
    _turn,
    _user_and_control,
)


RUN_ID = f"completed-edit-v22-{uuid4()}"


CASES = (
    (
        "append",
        "昨天日报再补一条今日工作：完成付款节点复核。",
        "add_daily_items",
        "append_item",
    ),
    (
        "edit",
        "把昨天日报今日工作的第一条改成：完成合同终稿复核。",
        "edit_daily_items",
        "edit_item",
    ),
    (
        "delete",
        "删掉昨天日报今日工作的第二条。",
        "delete_daily_items",
        "delete_item",
    ),
    (
        "move",
        "把昨天日报问题风险的第一条移到明日计划。",
        "move_daily_items",
        "move_item",
    ),
)


async def _run_case(*, llm_client: LLMClient, case) -> dict[str, object]:
    case_name, user_text, expected_tool, expected_command = case
    report_date = date(2026, 2, 10)
    source_message_id = f"{RUN_ID}-{case_name}"
    conversation_id = f"{RUN_ID}-conversation-{case_name}"
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            submitted_at = datetime(2026, 2, 10, 0, 0, tzinfo=UTC)
            report = DailyReport(
                user_id=user.id,
                team_id=user.team_id,
                report_date=report_date,
                today_work=["完成合同初稿复核", "整理付款材料"],
                problems=["等待补充发票"],
                tomorrow_plan=["跟进回款"],
                section_status={
                    "_agent2_report_version": 4,
                    "_draft_item_ids": {
                        "today_work": ["tw-1", "tw-2"],
                        "problems": ["pr-1"],
                        "tomorrow_plan": ["tp-1"],
                    },
                    TYPED_AUDIT_KEY: [],
                },
                completeness_score=1,
                status="completed",
                confirmation_type="auto_submitted_timeout",
                confirmed_by_user=False,
                submitted_at=submitted_at,
                source="completed_edit_rollback_smoke",
            )
            session.add(report)
            await session.flush()
            original_submission = (
                report.status,
                report.confirmation_type,
                report.confirmed_by_user,
                report.submitted_at,
            )
            model_audits: list[dict[str, object]] = []
            outcome = await _turn(
                session,
                user=user,
                settings=settings,
                llm_client=llm_client,
                text=user_text,
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                now=datetime(2026, 2, 11, 2, 0, tzinfo=UTC),
                model_audit_sink=model_audits,
            )
            await session.flush()
            await session.refresh(report)
            if (
                report.status,
                report.confirmation_type,
                report.confirmed_by_user,
                report.submitted_at,
            ) != original_submission:
                raise AssertionError("completed submission metadata changed")
            tool_receipts = await _receipts(session, source_message_id)
            actual_tools = [row.tool_name for row in tool_receipts]
            expected_tools = (
                ["query_report_by_date", expected_tool]
                if case_name in {"append", "edit", "delete", "move"}
                else [expected_tool]
            )
            if actual_tools != expected_tools:
                raise AssertionError(
                    {"tools": actual_tools, "expected_tools": expected_tools}
                )
            write_receipt = tool_receipts[-1]
            if not write_receipt.changed or write_receipt.status != "success":
                raise AssertionError("tool receipt did not record one successful change")
            typed_ids = tuple(write_receipt.typed_receipt_ids or ())
            if not typed_ids:
                raise AssertionError("tool receipt is not linked to typed audit")
            typed_rows = list(
                (
                    await session.scalars(
                        select(Agent2DailyCommandReceipt)
                        .where(
                            Agent2DailyCommandReceipt.receipt_id.in_(typed_ids)
                        )
                        .order_by(Agent2DailyCommandReceipt.created_at)
                    )
                ).all()
            )
            changed_rows = [row for row in typed_rows if row.actual_write]
            if not changed_rows or expected_command not in {
                row.command_type for row in changed_rows
            }:
                raise AssertionError(
                    {"typed_commands": [row.command_type for row in typed_rows]}
                )
            for row in changed_rows:
                if (
                    row.before_json.get("status") != "completed"
                    or row.after_json.get("status") != "completed"
                    or row.before_json == row.after_json
                    or row.created_at is None
                ):
                    raise AssertionError("typed before/after audit is incomplete")
            return {
                "name": case_name,
                "status": "pass",
                "tools": actual_tools,
                "typed_commands": [row.command_type for row in typed_rows],
                "business_result": outcome.user_visible_result,
                "submission_metadata_preserved": True,
                "audit_before_after_present": True,
            }
        finally:
            await session.rollback()


async def _residue() -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        result = {
            "reports": int(
                await session.scalar(
                    select(func.count(DailyReport.id)).where(
                        DailyReport.source == "completed_edit_rollback_smoke"
                    )
                )
                or 0
            ),
            "tool_receipts": int(
                await session.scalar(
                    select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                        ToolCallCanaryReceipt.source_message_id.like(f"{RUN_ID}%")
                    )
                )
                or 0
            ),
        }
        await session.rollback()
        return result


async def main() -> None:
    initial = await _residue()
    if any(initial.values()):
        raise AssertionError({"preexisting_residue": initial})
    llm_client = LLMClient(get_settings())
    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    original_audit = canary_service._record_model_audit_safely
    try:
        canary_service._record_model_audit_safely = lambda payload: None
        for case in CASES:
            try:
                results.append(await _run_case(llm_client=llm_client, case=case))
            except Exception as exc:
                failures.append(
                    {"name": case[0], "error": f"{type(exc).__name__}: {exc}"}
                )
    finally:
        canary_service._record_model_audit_safely = original_audit
        await llm_client.close()
    residue = await _residue()
    output = {
        "status": "pass" if not failures and not any(residue.values()) else "failed",
        "passed_count": len(results),
        "failed_count": len(failures),
        "dingtalk_send_calls": 0,
        "rollback_residue": residue,
        "results": results,
        "failures": failures,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    await engine.dispose()
    if output["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
