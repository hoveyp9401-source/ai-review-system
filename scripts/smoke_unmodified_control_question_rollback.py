from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

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


USER_ID = "222b1eeb-4faa-40cf-a193-e1892c9377b0"
RUN_ID = f"unmodified-control-question-{uuid4()}"
QUESTIONS = (
    "现在还能补昨天的日报么",
    "我现在可以补填昨天的日报吗？",
    "昨天的日报现在还能补录吗？",
)
GENERIC_BLOCK_REPLIES = {
    "这条消息暂时没有处理成功，本次没有写入任何内容。",
    "当前账号信息无法确认，本次没有执行任何操作。",
}


def _report_snapshot(report: DailyReport | None) -> dict | None:
    if report is None:
        return None
    return {
        "id": str(report.id),
        "today_work": list(report.today_work or ()),
        "problems": list(report.problems or ()),
        "tomorrow_plan": list(report.tomorrow_plan or ()),
        "section_status": dict(report.section_status or {}),
        "status": report.status,
        "confirmation_type": report.confirmation_type,
        "confirmed_by_user": report.confirmed_by_user,
        "submitted_at": report.submitted_at,
        "updated_at": report.updated_at,
    }


async def _run_case(
    client: LLMClient,
    *,
    question: str,
    index: int,
) -> dict[str, object]:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    yesterday = now.date() - timedelta(days=1)
    source_message_id = f"{RUN_ID}-{index}"
    async with AsyncSessionLocal() as session:
        try:
            user = await session.get(User, USER_ID)
            if user is None:
                raise AssertionError("bound smoke user is missing")
            control = await session.scalar(
                select(ToolCallCanaryControl).where(
                    ToolCallCanaryControl.user_id == USER_ID
                )
            )
            if control is None:
                raise AssertionError("bound smoke control is missing")
            expected_registry = runtime_registry_contract_digest(settings)
            expected_prompt = canary_prompt_sha256()
            if (
                not control.enabled
                or not control.messages_enabled
                or control.runtime != "canary_execute"
                or control.registry_digest != expected_registry
                or control.prompt_sha256 != expected_prompt
                or control.model_name != CANARY_MODEL_NAME
            ):
                raise AssertionError("unmodified control does not match runtime")
            report = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == yesterday,
                )
            )
            before = _report_snapshot(report)
            outcome = await process_tool_call_canary_ingress(
                session,
                user=user,
                dingtalk_user_id=user.dingtalk_user_id,
                user_text=question,
                source_channel="unmodified_control_question_rollback",
                conversation_id=f"{RUN_ID}-conversation-{index}",
                source_message_id=source_message_id,
                settings=settings,
                llm_client=client,
                now=now,
            )
            await session.flush()
            report_after = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == yesterday,
                )
            )
            after = _report_snapshot(report_after)
            if before != after:
                raise AssertionError("permission question changed the report")
            if outcome.owner != "tool_call_core":
                raise AssertionError(
                    {"owner": outcome.owner, "reason": outcome.reason}
                )
            if outcome.user_visible_result not in {
                "reply_only",
                "success",
                "clarification",
            }:
                raise AssertionError(
                    {"business_result": outcome.user_visible_result}
                )
            if outcome.model_call_count < 1:
                raise AssertionError("question did not enter the model")
            if not outcome.message.strip() or outcome.message in GENERIC_BLOCK_REPLIES:
                raise AssertionError("question returned a generic block reply")
            return {
                "case": index,
                "status": "pass",
                "business_result": outcome.user_visible_result,
                "model_call_count": outcome.model_call_count,
                "reply": outcome.message,
                "report_changed": False,
                "control_version": control.version,
            }
        finally:
            await session.rollback()


async def _residue() -> int:
    async with AsyncSessionLocal() as session:
        count = int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                    ToolCallCanaryReceipt.source_message_id.like(f"{RUN_ID}%")
                )
            )
            or 0
        )
        await session.rollback()
        return count


async def main() -> None:
    if await _residue():
        raise AssertionError("preexisting smoke residue")
    client = LLMClient(get_settings())
    try:
        results = [
            await _run_case(client, question=question, index=index)
            for index, question in enumerate(QUESTIONS, start=1)
        ]
    finally:
        await client.close()
    residue = await _residue()
    output = {
        "status": "pass" if not residue else "failed",
        "cases": len(results),
        "passed": len(results),
        "dingtalk_send_calls": 0,
        "receipt_residue": residue,
        "controls_mutated_by_smoke": 0,
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    await engine.dispose()
    if output["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
