from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.tool_calling.canary_service import (
    build_canary_persisted_response_payload,
)
from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import Agent2DailyCommandReceipt, DailyReport, WebhookEvent
from scripts.smoke_20260811_overnight_daily_rollback import (
    PANG_USER_ID,
    _turn,
    _user_and_control,
)


SOURCE_PREFIX = f"agent2-followup-rollback-{uuid4()}"
LOCAL_DATE = date(2026, 3, 2)
REPORT_DATE = LOCAL_DATE - timedelta(days=1)
FIRST_TEXT = "补一下昨日日报：今日完成合同复核；明日整理附件。"
POSITIVE_TEXT = "风险这块没遇到什么，前面那版就这么定了。"
NEGATIVE_TEXT = "风险没遇到，不过先别定稿，我还要补。"


def _model_calls(
    model_audits: list[dict[str, object]],
    tool_name: str,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for audit in model_audits:
        turns = audit.get("turns")
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            message = turn.get("message")
            if not isinstance(message, dict):
                continue
            turn_calls = message.get("calls")
            if not isinstance(turn_calls, list):
                continue
            for call in turn_calls:
                if not isinstance(call, dict) or call.get("name") != tool_name:
                    continue
                arguments = call.get("arguments")
                calls.append(arguments if isinstance(arguments, dict) else {})
    return calls


def _webhook_event(
    *,
    dingtalk_user_id: str,
    conversation_id: str,
    source_message_id: str,
    text: str,
    now: datetime,
) -> WebhookEvent:
    return WebhookEvent(
        idempotency_key=source_message_id,
        external_message_id=source_message_id,
        dingtalk_user_id=dingtalk_user_id,
        payload={
            "conversationId": conversation_id,
            "text": {"content": text},
        },
        response_payload={},
        status="processing",
        received_at=now,
    )


async def _logged_turn(
    session,
    *,
    user,
    settings,
    llm_client: LLMClient,
    conversation_id: str,
    source_message_id: str,
    text: str,
    now: datetime,
    model_audits: list[dict[str, object]],
):
    event = _webhook_event(
        dingtalk_user_id=user.dingtalk_user_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        text=text,
        now=now,
    )
    session.add(event)
    await session.flush()
    outcome = await _turn(
        session,
        user=user,
        settings=settings,
        llm_client=llm_client,
        text=text,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        now=now,
        model_audit_sink=model_audits,
    )
    if outcome.messages_enabled:
        raise AssertionError("rollback smoke unexpectedly enabled DingTalk transport")

    # Keep the real model reply in the uncommitted webhook row so that the next
    # turn sees the same user/assistant history as a delivered production turn.
    # This writes only to the outer transaction; no transport method is called.
    persisted_response = build_canary_persisted_response_payload(outcome)
    persisted_response.update(
        {
            "msgtype": "text",
            "text": {"content": outcome.message},
        }
    )
    event.response_payload = persisted_response
    event.status = "processed"
    event.processed_at = now
    if outcome.report_id:
        event.report_id = UUID(outcome.report_id)
    await session.flush()
    return outcome


async def _high_and_typed_receipts(
    session,
    *,
    source_message_id: str,
) -> tuple[list[ToolCallCanaryReceipt], list[Agent2DailyCommandReceipt]]:
    high = list(
        (
            await session.scalars(
                select(ToolCallCanaryReceipt)
                .where(
                    ToolCallCanaryReceipt.source_message_id
                    == source_message_id
                )
                .order_by(ToolCallCanaryReceipt.created_at)
            )
        ).all()
    )
    typed_ids = {
        UUID(str(receipt_id))
        for receipt in high
        for receipt_id in (receipt.typed_receipt_ids or [])
    }
    typed = (
        sorted(
            (
                await session.scalars(
                    select(Agent2DailyCommandReceipt)
                    .where(
                        Agent2DailyCommandReceipt.receipt_id.in_(typed_ids)
                    )
                )
            ).all(),
            key=lambda row: int(row.idempotency_key.rsplit(":daily:", 1)[-1]),
        )
        if typed_ids
        else []
    )
    return high, typed


def _required_add_receipt(
    high: list[ToolCallCanaryReceipt],
) -> ToolCallCanaryReceipt:
    matching = [row for row in high if row.tool_name == "add_daily_items"]
    if len(matching) != 1:
        raise AssertionError(
            {"expected_one_add_receipt": [row.tool_name for row in high]}
        )
    receipt = matching[0]
    if receipt.status not in {"success", "no_op"}:
        raise AssertionError({"add_receipt_status": receipt.status})
    if receipt.target_type != "daily_report" or not receipt.target_id:
        raise AssertionError("add receipt did not bind a daily report")
    if not receipt.typed_receipt_ids:
        raise AssertionError("add receipt did not retain typed execution receipts")
    return receipt


def _assert_first_report(report: DailyReport | None) -> None:
    if report is None:
        raise AssertionError("first turn did not create yesterday's report")
    if report.report_date != REPORT_DATE:
        raise AssertionError({"first_report_date": report.report_date.isoformat()})
    if report.status != "collecting":
        raise AssertionError({"first_report_status": report.status})
    if "合同复核" not in "\n".join(report.today_work):
        raise AssertionError({"first_today_work": report.today_work})
    if "整理附件" not in "\n".join(report.tomorrow_plan):
        raise AssertionError({"first_tomorrow_plan": report.tomorrow_plan})
    if report.problems:
        raise AssertionError({"first_problems": report.problems})
    if bool(
        dict(report.section_status or {}).get("problems_acknowledged_empty")
    ):
        raise AssertionError("first turn guessed that problems were empty")


def _assert_typed_scope(
    typed: list[Agent2DailyCommandReceipt],
    *,
    expected_report_id: UUID,
) -> None:
    if not typed:
        raise AssertionError("typed daily receipt layer is missing")
    if any(row.report_id != expected_report_id for row in typed):
        raise AssertionError("typed receipts point at a different report")
    if any(row.report_date != REPORT_DATE for row in typed):
        raise AssertionError("typed receipts point at a different report date")
    if any(row.status != "executed" or not row.actual_write for row in typed):
        raise AssertionError(
            {
                "typed_receipts": [
                    {
                        "command": row.command_type,
                        "status": row.status,
                        "actual_write": row.actual_write,
                    }
                    for row in typed
                ]
            }
        )


async def _assert_no_today_report(session, user_id: UUID) -> None:
    today_report = await session.scalar(
        select(DailyReport).where(
            DailyReport.user_id == user_id,
            DailyReport.report_date == LOCAL_DATE,
        )
    )
    if today_report is not None:
        raise AssertionError("follow-up unexpectedly wrote today's report")


async def _run_case(
    *,
    llm_client: LLMClient,
    case_name: str,
    second_text: str,
    should_submit: bool,
) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            timezone = ZoneInfo(user.timezone or settings.timezone)
            first_now = datetime.combine(
                LOCAL_DATE,
                datetime.min.time().replace(hour=8, minute=59),
                tzinfo=timezone,
            )
            second_now = datetime.combine(
                LOCAL_DATE,
                datetime.min.time().replace(hour=9, minute=1),
                tzinfo=timezone,
            )
            existing = list(
                (
                    await session.scalars(
                        select(DailyReport).where(
                            DailyReport.user_id == user.id,
                            DailyReport.report_date.in_((REPORT_DATE, LOCAL_DATE)),
                        )
                    )
                ).all()
            )
            if existing:
                raise AssertionError(
                    {
                        "preexisting_reports": [
                            row.report_date.isoformat() for row in existing
                        ]
                    }
                )

            conversation_id = f"{SOURCE_PREFIX}-{case_name}"
            first_source = f"{conversation_id}-first"
            second_source = f"{conversation_id}-second"
            first_audits: list[dict[str, object]] = []
            second_audits: list[dict[str, object]] = []

            first = await _logged_turn(
                session,
                user=user,
                settings=settings,
                llm_client=llm_client,
                conversation_id=conversation_id,
                source_message_id=first_source,
                text=FIRST_TEXT,
                now=first_now,
                model_audits=first_audits,
            )
            report = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == REPORT_DATE,
                )
            )
            _assert_first_report(report)
            await _assert_no_today_report(session, user.id)
            first_high, first_typed = await _high_and_typed_receipts(
                session,
                source_message_id=first_source,
            )
            first_add = _required_add_receipt(first_high)
            if not first_add.changed:
                raise AssertionError("first add receipt did not record a change")
            _assert_typed_scope(first_typed, expected_report_id=report.id)

            second = await _logged_turn(
                session,
                user=user,
                settings=settings,
                llm_client=llm_client,
                conversation_id=conversation_id,
                source_message_id=second_source,
                text=second_text,
                now=second_now,
                model_audits=second_audits,
            )
            await session.refresh(report)
            await _assert_no_today_report(session, user.id)
            second_high, second_typed = await _high_and_typed_receipts(
                session,
                source_message_id=second_source,
            )
            _required_add_receipt(second_high)
            _assert_typed_scope(second_typed, expected_report_id=report.id)

            add_calls = _model_calls(second_audits, "add_daily_items")
            if len(add_calls) != 1:
                raise AssertionError({"second_add_model_calls": add_calls})
            add_arguments = add_calls[0]
            if add_arguments.get("date_selection") != "trusted_report":
                raise AssertionError(
                    {"second_date_selection": add_arguments.get("date_selection")}
                )
            if add_arguments.get("report_id") != str(report.id):
                raise AssertionError(
                    {
                        "second_model_report_id": add_arguments.get("report_id"),
                        "actual_report_id": str(report.id),
                    }
                )
            if "problems" not in set(
                add_arguments.get("acknowledged_empty_fields") or []
            ):
                raise AssertionError(
                    {
                        "second_acknowledged_empty_fields": add_arguments.get(
                            "acknowledged_empty_fields"
                        )
                    }
                )
            if bool(add_arguments.get("submit_after_write")) != should_submit:
                raise AssertionError(
                    {"second_submit_after_write": add_arguments.get("submit_after_write")}
                )

            command_types = [row.command_type for row in second_typed]
            if "acknowledge_empty_section" not in command_types:
                raise AssertionError({"second_typed_commands": command_types})
            if should_submit:
                if command_types != ["acknowledge_empty_section", "submit_report"]:
                    raise AssertionError(
                        {"positive_atomic_commands": command_types}
                    )
                if report.status != "completed":
                    raise AssertionError({"positive_report_status": report.status})
                if not report.confirmed_by_user:
                    raise AssertionError("positive report was not user-confirmed")
            else:
                if "submit_report" in command_types:
                    raise AssertionError("negative follow-up submitted the report")
                if report.status == "completed":
                    raise AssertionError("negative follow-up completed the report")

            if not bool(
                dict(report.section_status or {}).get(
                    "problems_acknowledged_empty"
                )
            ):
                raise AssertionError("problems were not acknowledged as empty")
            webhook_rows = list(
                (
                    await session.scalars(
                        select(WebhookEvent)
                        .where(
                            WebhookEvent.external_message_id.in_(
                                (first_source, second_source)
                            )
                        )
                        .order_by(WebhookEvent.received_at)
                    )
                ).all()
            )
            if len(webhook_rows) != 2 or any(
                row.status != "processed" for row in webhook_rows
            ):
                raise AssertionError("two-turn webhook history was not persisted")
            if not all(
                str(row.response_payload.get("text", {}).get("content") or "")
                for row in webhook_rows
            ):
                raise AssertionError("webhook history is missing a model reply")

            return {
                "name": case_name,
                "status": "pass",
                "report_date": report.report_date.isoformat(),
                "report_status": report.status,
                "trusted_report_binding": True,
                "high_level_receipts": len(first_high) + len(second_high),
                "typed_receipts": len(first_typed) + len(second_typed),
                "webhook_turns": len(webhook_rows),
                "model_calls": first.model_call_count + second.model_call_count,
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
                        DailyReport.report_date.in_((REPORT_DATE, LOCAL_DATE)),
                    )
                )
                or 0
            ),
            "tool_receipts": int(
                await session.scalar(
                    select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                        ToolCallCanaryReceipt.source_message_id.like(
                            f"{SOURCE_PREFIX}%"
                        )
                    )
                )
                or 0
            ),
            "typed_receipts": int(
                await session.scalar(
                    select(func.count(Agent2DailyCommandReceipt.receipt_id)).where(
                        Agent2DailyCommandReceipt.audit_json[
                            "_execution_scope"
                        ]["source_turn_id"].astext.like(
                            f"{SOURCE_PREFIX}%"
                        )
                    )
                )
                or 0
            ),
            "webhooks": int(
                await session.scalar(
                    select(func.count(WebhookEvent.id)).where(
                        WebhookEvent.external_message_id.like(
                            f"{SOURCE_PREFIX}%"
                        )
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
        raise AssertionError({"preexisting_test_residue": initial})

    from app.config import get_settings

    llm_client = LLMClient(get_settings())
    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    try:
        for case_name, second_text, should_submit in (
            ("positive_confirm", POSITIVE_TEXT, True),
            ("negative_keep_collecting", NEGATIVE_TEXT, False),
        ):
            try:
                results.append(
                    await _run_case(
                        llm_client=llm_client,
                        case_name=case_name,
                        second_text=second_text,
                        should_submit=should_submit,
                    )
                )
            except Exception as exc:
                failures.append(
                    {
                        "name": case_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    finally:
        await llm_client.close()

    final = await _residue()
    output = {
        "status": "pass" if not failures and not any(final.values()) else "failed",
        "real_model_case_count": len(results) + len(failures),
        "passed_count": len(results),
        "failed_count": len(failures),
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
