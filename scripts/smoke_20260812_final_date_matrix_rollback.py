from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import Agent2DailyCommandReceipt, DailyReport, WebhookEvent
from scripts.smoke_20260811_overnight_daily_rollback import (
    PANG_USER_ID,
    _turn,
    _user_and_control,
)
from scripts.smoke_20260812_agent2_followup_rollback import _logged_turn


SOURCE_PREFIX = f"agent2-final-date-rollback-{uuid4()}"
LOCAL_DATE = date(2026, 8, 13)


@dataclass(frozen=True)
class Case:
    name: str
    hour: int
    minute: int
    messages: tuple[str, ...]
    expected_date: date
    expected_fragment: str


CASES = (
    Case(
        "one_am_yesterday",
        1,
        0,
        ("昨天完成了合同付款节点复核，问题暂无，明天继续跟进。",),
        date(2026, 8, 12),
        "付款节点",
    ),
    Case(
        "morning_today_habit",
        1,
        5,
        ("今天完成了合同归档，问题暂无，明天继续整理附件。",),
        date(2026, 8, 12),
        "合同归档",
    ),
    Case(
        "explicit_new_day",
        1,
        10,
        ("这份日报明确记到8月13日：完成合同归档，问题暂无，明天复核清单。",),
        date(2026, 8, 13),
        "合同归档",
    ),
    Case(
        "semantic_new_work",
        8,
        30,
        ("早上刚完成了付款节点复核，这项记到今天这份日报。",),
        date(2026, 8, 13),
        "付款节点",
    ),
)
SELECTED_CASES = frozenset(
    value.strip()
    for value in os.getenv("FINAL_DATE_CASES", "").split(",")
    if value.strip()
)


def _all_tool_calls(audits: list[dict[str, object]]) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for audit in audits:
        turns = audit.get("turns")
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            message = turn.get("message")
            if not isinstance(message, dict):
                continue
            for call in message.get("calls") or ():
                if isinstance(call, dict):
                    calls.append(call)
    return calls


def _reasoning_forwarded(audits: list[dict[str, object]]) -> bool:
    seen = False
    for audit in audits:
        turns = audit.get("turns")
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            metadata = turn.get("response_metadata")
            if not isinstance(metadata, dict):
                continue
            usage = metadata.get("usage")
            if not isinstance(usage, dict):
                continue
            details = usage.get("completion_tokens_details")
            if isinstance(details, dict) and int(details.get("reasoning_tokens") or 0) > 0:
                seen = True
    return seen


def _model_contract_evidence(
    audits: list[dict[str, object]],
) -> dict[str, object]:
    turns = [
        turn
        for audit in audits
        for turn in (
            audit.get("turns")
            if isinstance(audit.get("turns"), list)
            else []
        )
        if isinstance(turn, dict)
    ]
    if not turns:
        raise AssertionError("real-model smoke did not capture any model turn")
    served_models = [
        str(
            (turn.get("response_metadata") or {}).get("served_model")
            if isinstance(turn.get("response_metadata"), dict)
            else ""
        ).strip()
        for turn in turns
    ]
    if any(model != CANARY_MODEL_NAME for model in served_models):
        raise AssertionError(
            {
                "configured_model": CANARY_MODEL_NAME,
                "served_models": served_models,
            }
        )
    missing_reasoning_iterations = [
        turn.get("iteration")
        for turn in turns
        if not str(turn.get("reasoning_content_sha256") or "").strip()
    ]
    if missing_reasoning_iterations:
        raise AssertionError(
            {"model_turns_missing_reasoning": missing_reasoning_iterations}
        )
    return {
        "model_turn_count": len(turns),
        "served_models": sorted(set(served_models)),
        "all_model_turns_have_reasoning": True,
    }


async def _run_case(llm: LLMClient, case: Case, iteration: int) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            timezone = ZoneInfo(user.timezone or settings.timezone)
            now = datetime.combine(
                LOCAL_DATE,
                datetime.min.time().replace(hour=case.hour, minute=case.minute),
                tzinfo=timezone,
            )
            conversation_id = f"{SOURCE_PREFIX}-{case.name}-{iteration}"
            audits: list[dict[str, object]] = []
            for message_index, text in enumerate(case.messages, start=1):
                source_id = f"{conversation_id}-{message_index}"
                if len(case.messages) > 1 and message_index == 1:
                    event = WebhookEvent(
                        idempotency_key=source_id,
                        external_message_id=source_id,
                        dingtalk_user_id=user.dingtalk_user_id,
                        payload={
                            "conversationId": conversation_id,
                            "text": {"content": text},
                        },
                        response_payload={},
                        status="processing",
                        received_at=now + timedelta(seconds=message_index),
                    )
                    session.add(event)
                    await session.flush()
                    outcome = await _turn(
                        session,
                        user=user,
                        settings=settings,
                        llm_client=llm,
                        text=text,
                        conversation_id=conversation_id,
                        source_message_id=source_id,
                        now=now + timedelta(seconds=message_index),
                        accepted_business_results=frozenset({"reply_only"}),
                        model_audit_sink=audits,
                    )
                    event.response_payload = {
                        "msgtype": "text",
                        "text": {"content": outcome.message},
                    }
                    event.status = "processed"
                    await session.flush()
                else:
                    outcome = await _logged_turn(
                        session,
                        user=user,
                        settings=settings,
                        llm_client=llm,
                        conversation_id=conversation_id,
                        source_message_id=source_id,
                        text=text,
                        now=now + timedelta(seconds=message_index),
                        model_audits=audits,
                    )
                if outcome.messages_enabled:
                    raise AssertionError("transport enabled")
            reports = list(
                (
                    await session.scalars(
                        select(DailyReport).where(
                            DailyReport.user_id == user.id,
                            DailyReport.report_date.in_(
                                (LOCAL_DATE, LOCAL_DATE - timedelta(days=1), LOCAL_DATE - timedelta(days=2))
                            ),
                        )
                    )
                ).all()
            )
            expected = next((row for row in reports if row.report_date == case.expected_date), None)
            if expected is None:
                source_ids = tuple(
                    f"{conversation_id}-{i}"
                    for i in range(1, len(case.messages) + 1)
                )
                observed_high = list(
                    (
                        await session.scalars(
                            select(ToolCallCanaryReceipt).where(
                                ToolCallCanaryReceipt.source_message_id.in_(source_ids)
                            )
                        )
                    ).all()
                )
                raise AssertionError(
                    {
                        "expected": case.expected_date.isoformat(),
                        "actual": [row.report_date.isoformat() for row in reports],
                        "tools": [row.tool_name for row in observed_high],
                        "targets": [row.target_id for row in observed_high],
                        "tool_calls": _all_tool_calls(audits),
                        "reasoning_tokens_present": _reasoning_forwarded(audits),
                    }
                )
            all_text = "\n".join(expected.today_work + expected.problems + expected.tomorrow_plan)
            if case.expected_fragment not in all_text:
                raise AssertionError({"missing_fragment": case.expected_fragment, "snapshot": all_text})
            source_ids = tuple(f"{conversation_id}-{i}" for i in range(1, len(case.messages) + 1))
            high = list(
                (
                    await session.scalars(
                        select(ToolCallCanaryReceipt).where(
                            ToolCallCanaryReceipt.source_message_id.in_(source_ids)
                        )
                    )
                ).all()
            )
            typed_ids = {
                UUID(receipt_id)
                for row in high
                for receipt_id in (row.typed_receipt_ids or ())
            }
            typed = list(
                (
                    await session.scalars(
                        select(Agent2DailyCommandReceipt).where(
                            Agent2DailyCommandReceipt.receipt_id.in_(typed_ids)
                        )
                    )
                ).all()
            ) if typed_ids else []
            if not high or not any(row.changed for row in high):
                raise AssertionError("no executed write receipt")
            if any(row.report_date != case.expected_date for row in typed):
                raise AssertionError({"typed_dates": [row.report_date.isoformat() for row in typed]})
            target_ids = {str(row.target_id) for row in high if row.target_type == "daily_report"}
            if target_ids != {str(expected.id)}:
                raise AssertionError(
                    {"expected_report_id": str(expected.id), "receipt_target_ids": sorted(target_ids)}
                )
            calls = _all_tool_calls(audits)
            if any(call.get("name") == "review_daily_report_dates" for call in calls):
                raise AssertionError("removed second date reviewer reappeared")
            model_contract = _model_contract_evidence(audits)
            return {
                "name": case.name,
                "iteration": iteration,
                "status": "pass",
                "expected_date": case.expected_date.isoformat(),
                "written_date": expected.report_date.isoformat(),
                "tools": [row.tool_name for row in high],
                "tool_calls": calls,
                "typed_dates": [row.report_date.isoformat() for row in typed],
                "model_entered": bool(calls),
                "reasoning_tokens_present": _reasoning_forwarded(audits),
                **model_contract,
                "second_date_review_count": 0,
                "dingtalk_send_calls": 0,
            }
        finally:
            await session.rollback()


async def _residue() -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        result = {
            "reports": int(
                await session.scalar(
                    select(func.count(DailyReport.id)).where(
                        DailyReport.user_id == PANG_USER_ID,
                        DailyReport.report_date.in_(
                            (LOCAL_DATE, LOCAL_DATE - timedelta(days=1), LOCAL_DATE - timedelta(days=2))
                        ),
                        DailyReport.source == "overnight_v22_rollback_smoke",
                    )
                )
                or 0
            ),
            "tool_receipts": int(
                await session.scalar(
                    select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                        ToolCallCanaryReceipt.source_message_id.like(f"{SOURCE_PREFIX}%")
                    )
                )
                or 0
            ),
            "typed_receipts": int(
                await session.scalar(
                    select(func.count(Agent2DailyCommandReceipt.receipt_id)).where(
                        Agent2DailyCommandReceipt.message_id.like(f"{SOURCE_PREFIX}%")
                    )
                )
                or 0
            ),
            "webhooks": int(
                await session.scalar(
                    select(func.count(WebhookEvent.id)).where(
                        WebhookEvent.external_message_id.like(f"{SOURCE_PREFIX}%")
                    )
                )
                or 0
            ),
        }
        await session.rollback()
        return result


async def main() -> None:
    llm = LLMClient(get_settings())
    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    try:
        for case in CASES:
            if SELECTED_CASES and case.name not in SELECTED_CASES:
                continue
            for iteration in range(1, 4):
                try:
                    results.append(await _run_case(llm, case, iteration))
                except Exception as exc:
                    failures.append(
                        {"name": case.name, "iteration": str(iteration), "error": f"{type(exc).__name__}: {exc}"}
                    )
    finally:
        await llm.close()
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
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    await engine.dispose()
    if output["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
