from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.memory import PersonalMemoryModule
from app.agent2.memory.postgres import PostgresPersonalMemoryReadStore
from app.agent2.tool_calling.assembly import TrustedContextAssembler, TrustedContextRequest
from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    CANARY_MODEL_PROVIDER,
    CANARY_RECENT_MESSAGE_LIMIT,
    CANARY_RECENT_OPERATION_LIMIT,
)
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE
from app.agent2.tool_calling.production_store import (
    ProductionContextStore,
    ToolCallCanaryReceipt,
)
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import Agent2DailyCommandReceipt, DailyReport, WebhookEvent
from scripts.smoke_20260811_overnight_daily_rollback import (
    PANG_USER_ID,
    _user_and_control,
)
import scripts.smoke_20260811_overnight_daily_rollback as overnight_smoke


SOURCE_PREFIX = f"agent2-continuous-real-rollback-{uuid4()}"
HISTORY_USER = "优化日报提交后无法修改的限制"
HISTORY_ASSISTANT = "要把‘优化日报提交后无法修改的限制’加入明日计划吗？"
CURRENT_MESSAGES = ("要", "就加到今天的明日计划")


def _event(
    *,
    user,
    conversation_id: str,
    source_id: str,
    user_text: str,
    assistant_text: str,
    received_at: datetime,
) -> WebhookEvent:
    return WebhookEvent(
        idempotency_key=source_id,
        external_message_id=source_id,
        dingtalk_user_id=user.dingtalk_user_id,
        payload={"conversationId": conversation_id, "text": {"content": user_text}},
        response_payload={"msgtype": "text", "text": {"content": assistant_text}},
        status="processed",
        received_at=received_at,
        processed_at=received_at,
    )


async def _context_payload(session, *, user, settings, conversation_id, source_id, now):
    store = ProductionContextStore(
        session,
        user=user,
        tenant_id="default",
        settings=settings,
    )
    context = await TrustedContextAssembler(
        read_port=store,
        policy_port=store,
        recent_message_limit=CANARY_RECENT_MESSAGE_LIMIT,
        recent_operation_limit=CANARY_RECENT_OPERATION_LIMIT,
        namespace=CANARY_STATE_NAMESPACE,
        personal_memory_module=PersonalMemoryModule(
            read_port=PostgresPersonalMemoryReadStore(session)
        ),
    ).assemble(
        TrustedContextRequest(
            tenant_id="default",
            user_id=user.id,
            conversation_id=conversation_id,
            source_message_id=source_id,
            timezone=user.timezone or settings.timezone,
            server_now=now,
            display_name=user.name,
            runtime_provider_name=CANARY_MODEL_PROVIDER,
            runtime_model_name=CANARY_MODEL_NAME,
        )
    )
    payload = context.model_payload()
    return {
        "recent_messages": payload["recent_messages"],
        "recent_operations": payload["recent_operations"],
        "report_reference": payload.get("report_reference"),
        "daily_reporting_context": payload["daily_reporting_context"],
    }


def _model_turns(audits: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        turn
        for audit in audits
        for turn in (audit.get("turns") or ())
        if isinstance(turn, dict)
    ]


def _verify_model_contract(turns: list[dict[str, object]]) -> dict[str, object]:
    if not turns:
        raise AssertionError("real-model smoke did not capture a model turn")
    served_models: list[str] = []
    for turn in turns:
        metadata = turn.get("response_metadata")
        if not isinstance(metadata, dict):
            raise AssertionError("model turn has no response metadata")
        served_model = str(metadata.get("served_model") or "").strip()
        if served_model != CANARY_MODEL_NAME:
            raise AssertionError(
                {"configured_model": CANARY_MODEL_NAME, "served_model": served_model}
            )
        if not str(turn.get("reasoning_content_sha256") or "").strip():
            raise AssertionError("model turn has no captured reasoning")
        served_models.append(served_model)
    return {
        "served_models": sorted(set(served_models)),
        "all_model_turns_have_reasoning": True,
    }


async def _batched_turn(
    session,
    *,
    user,
    settings,
    llm_client,
    conversation_id: str,
    source_message_id: str,
    now: datetime,
    model_audits: list[dict[str, object]],
):
    """Exercise the same ordered-fragment ingress used by the stream batch."""

    original_ingress = overnight_smoke.process_tool_call_canary_ingress

    async def batched_ingress(*args, **kwargs):
        kwargs["user_messages"] = CURRENT_MESSAGES
        return await original_ingress(*args, **kwargs)

    overnight_smoke.process_tool_call_canary_ingress = batched_ingress
    try:
        return await overnight_smoke._turn(
            session,
            user=user,
            settings=settings,
            llm_client=llm_client,
            text=CURRENT_MESSAGES[0],
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            now=now,
            accepted_business_results=frozenset({"reply_only", "success"}),
            model_audit_sink=model_audits,
        )
    finally:
        overnight_smoke.process_tool_call_canary_ingress = original_ingress


async def _run_case(
    llm: LLMClient,
    *,
    name: str,
    local_now: datetime,
    expected_date: date | None,
) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            conversation_id = f"{SOURCE_PREFIX}-{name}"
            history_id = f"{conversation_id}-history"
            session.add(
                _event(
                    user=user,
                    conversation_id=conversation_id,
                    source_id=history_id,
                    user_text=HISTORY_USER,
                    assistant_text=HISTORY_ASSISTANT,
                    received_at=local_now - timedelta(minutes=2),
                )
            )
            await session.flush()
            batch_id = f"{conversation_id}-current-batch"
            trusted_context = await _context_payload(
                session,
                user=user,
                settings=settings,
                conversation_id=conversation_id,
                source_id=batch_id,
                now=local_now,
            )
            model_audits: list[dict[str, object]] = []
            outcome = await _batched_turn(
                session,
                user=user,
                settings=settings,
                llm_client=llm,
                conversation_id=conversation_id,
                source_message_id=batch_id,
                now=local_now,
                model_audits=model_audits,
            )
            high = list(
                (
                    await session.scalars(
                        select(ToolCallCanaryReceipt).where(
                            ToolCallCanaryReceipt.source_message_id == batch_id
                        )
                    )
                ).all()
            )
            typed_ids = {
                UUID(value)
                for row in high
                for value in (row.typed_receipt_ids or ())
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
            report_dates = sorted({row.report_date for row in typed})
            if expected_date is not None and report_dates != [expected_date]:
                raise AssertionError(
                    {"expected_date": expected_date.isoformat(), "typed_dates": [item.isoformat() for item in report_dates]}
                )
            if expected_date is None:
                raise AssertionError("continuous smoke requires one expected date")
            reports = list(
                (
                    await session.scalars(
                        select(DailyReport).where(
                            DailyReport.user_id == user.id,
                            DailyReport.report_date == expected_date,
                        )
                    )
                ).all()
            )
            if len(reports) != 1:
                raise AssertionError(
                    {"expected_report_count": 1, "actual": len(reports)}
                )
            report = reports[0]
            if HISTORY_USER not in report.tomorrow_plan:
                raise AssertionError(
                    {"missing_history_item": HISTORY_USER, "tomorrow_plan": report.tomorrow_plan}
                )
            forbidden_fragments = ("要", "就加到今天的明日计划")
            report_text = "\n".join(
                report.today_work + report.problems + report.tomorrow_plan
            )
            if any(fragment == item for fragment in forbidden_fragments for item in (
                report.today_work + report.problems + report.tomorrow_plan
            )):
                raise AssertionError({"confirmation_fragment_was_written": report_text})
            turns = _model_turns(model_audits)
            if any(
                call.get("name") == "review_daily_report_dates"
                for turn in turns
                for call in ((turn.get("message") or {}).get("calls") or ())
                if isinstance(call, dict)
            ):
                raise AssertionError("second date review reappeared")
            model_contract = _verify_model_contract(turns)
            return {
                "name": name,
                "local_time": local_now.isoformat(),
                "history": [
                    {"role": "user", "content": HISTORY_USER},
                    {"role": "assistant", "content": HISTORY_ASSISTANT},
                ],
                "current_messages": list(CURRENT_MESSAGES),
                "trusted_context": trusted_context,
                "business_result": outcome.user_visible_result,
                "reply": outcome.message,
                "model_turns": turns,
                "receipts": [
                    {
                        "source_message_id": row.source_message_id,
                        "tool_name": row.tool_name,
                        "status": row.status,
                        "changed": row.changed,
                        "target_id": row.target_id,
                    }
                    for row in high
                ],
                "typed_dates": [row.report_date.isoformat() for row in typed],
                **model_contract,
                "dingtalk_send_calls": 0,
            }
        finally:
            await session.rollback()


async def _residue() -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        result = {
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
    settings = get_settings()
    llm = LLMClient(settings)
    tz = ZoneInfo("Asia/Shanghai")
    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    try:
        for index in range(2, 4):
            try:
                results.append(
                    await _run_case(
                        llm,
                        name=f"original_{index}",
                        local_now=datetime(2026, 8, 12, 8, 21, tzinfo=tz),
                        expected_date=date(2026, 8, 12),
                    )
                )
            except Exception as exc:
                failures.append({"name": f"original_{index}", "error": f"{type(exc).__name__}: {exc}"})
        for index in range(2, 4):
            try:
                results.append(
                    await _run_case(
                        llm,
                        name=f"one_am_observe_{index}",
                        local_now=datetime(2026, 8, 13, 1, 0, tzinfo=tz),
                        expected_date=date(2026, 8, 12),
                    )
                )
            except Exception as exc:
                failures.append({"name": f"one_am_observe_{index}", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        await llm.close()
    residue = await _residue()
    output = {
        "status": "pass" if not failures and not any(residue.values()) else "failed",
        "results": results,
        "failures": failures,
        "dingtalk_send_calls": 0,
        "rollback_residue": residue,
    }
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    await engine.dispose()
    if output["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
