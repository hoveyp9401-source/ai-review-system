from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.tool_calling.canary_config import canary_prompt_sha256
from app.agent2.tool_calling.canary_service import process_tool_call_canary_ingress
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.llm.client import LLMClient
from app.models import DailyReport, ReportInteractionEvent, User, WebhookEvent


TENANT_ID = "sandbox-agent2-phase2-20260711"
LIU_USER_ID = "91a1b0b0-201e-490f-8272-ad2d74e803f7"
PANG_USER_ID = "222b1eeb-4faa-40cf-a193-e1892c9377b0"
REPORT_DATE = date(2026, 8, 3)


def _history_event(
    *,
    dingtalk_user_id: str,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    now: datetime,
) -> WebhookEvent:
    source_id = f"rollback-smoke-history-{uuid4()}"
    return WebhookEvent(
        idempotency_key=source_id,
        external_message_id=source_id,
        dingtalk_user_id=dingtalk_user_id,
        payload={
            "conversationId": conversation_id,
            "text": {"content": user_text},
        },
        response_payload={
            "msgtype": "text",
            "text": {"content": assistant_text},
        },
        status="processed",
        received_at=now - timedelta(minutes=2),
        processed_at=now - timedelta(minutes=2),
    )


async def _receipts(session, source_message_id: str):
    return list(
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


def _receipt_output(receipts) -> list[dict[str, object]]:
    return [
        {
            "tool": row.tool_name,
            "status": row.status,
            "changed": row.changed,
            "facts": row.safe_user_facts,
        }
        for row in receipts
    ]


def _report_dates(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"report_date", "target_report_date"}:
                found.add(str(item))
            found.update(_report_dates(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_report_dates(item))
    return found


async def _run_turn(
    session,
    *,
    user,
    text: str,
    conversation_id: str,
    source_message_id: str,
    settings,
    llm_client,
    now,
):
    return await process_tool_call_canary_ingress(
        session,
        user=user,
        dingtalk_user_id=user.dingtalk_user_id,
        user_text=text,
        source_channel="rollback_smoke",
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        settings=settings,
        llm_client=llm_client,
        now=now,
    )


async def main() -> None:
    settings = get_settings()
    # Reproduce the reported next-morning conversation before the 09:00
    # historical edit lock. The live incident occurred in this window.
    now = datetime(
        2026,
        8,
        4,
        8,
        30,
        tzinfo=ZoneInfo(settings.timezone),
    )
    registry_digest = runtime_registry_contract_digest(settings)
    prompt_digest = canary_prompt_sha256()
    llm_client = LLMClient(settings)
    output: dict[str, object] = {}

    try:
        async with AsyncSessionLocal() as session:
            baseline_receipt_count = await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id))
            )
            baseline_webhook_count = await session.scalar(
                select(func.count(WebhookEvent.id))
            )
            baseline_briefing_count = await session.scalar(
                select(func.count(ReportInteractionEvent.id)).where(
                    ReportInteractionEvent.backend_action
                    == "daily_briefing_sent"
                )
            )

            users = list(
                (
                    await session.scalars(
                        select(User).where(
                            User.id.in_([LIU_USER_ID, PANG_USER_ID])
                        )
                    )
                ).all()
            )
            users_by_id = {str(user.id): user for user in users}
            if set(users_by_id) != {LIU_USER_ID, PANG_USER_ID}:
                raise AssertionError(sorted(users_by_id))
            liu = users_by_id[LIU_USER_ID]
            pang = users_by_id[PANG_USER_ID]

            controls = list(
                (
                    await session.scalars(
                        select(ToolCallCanaryControl).where(
                            ToolCallCanaryControl.user_id.in_(
                                [LIU_USER_ID, PANG_USER_ID]
                            )
                        )
                    )
                ).all()
            )
            if len(controls) != 2 or not all(row.enabled for row in controls):
                raise AssertionError(
                    [(row.user_id, row.enabled) for row in controls]
                )
            output["candidate_hashes"] = {
                "registry": registry_digest,
                "prompt": prompt_digest,
            }
            for control in controls:
                control.registry_digest = registry_digest
                control.prompt_sha256 = prompt_digest
            await session.flush()

            report = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == LIU_USER_ID,
                    DailyReport.report_date == REPORT_DATE,
                )
            )
            if report is None:
                raise AssertionError("Liu report not found")
            original_report = {
                "status": report.status,
                "confirmation_type": report.confirmation_type,
                "confirmed_by_user": report.confirmed_by_user,
                "submitted_at": report.submitted_at,
                "pending_confirmation_at": report.pending_confirmation_at,
                "auto_submit_at": report.auto_submit_at,
                "problems": list(report.problems or []),
            }

            report_text = (
                "2026-08-03 日报\n"
                f"今日工作：{'；'.join(report.today_work)}\n"
                f"问题/风险：{'；'.join(report.problems) or '无'}\n"
                f"明日计划：{'；'.join(report.tomorrow_plan)}"
            )

            def prepare_report_for_confirmation(
                *,
                complete: bool = True,
            ) -> None:
                report.status = "pending_confirmation"
                report.confirmation_type = "none"
                report.confirmed_by_user = False
                report.submitted_at = None
                report.pending_confirmation_at = now - timedelta(minutes=5)
                report.auto_submit_at = now + timedelta(hours=1)
                report.problems = ["无"] if complete else []

            prepare_report_for_confirmation()
            direct_conversation = f"rollback-smoke-liu-direct-{uuid4()}"
            session.add(
                _history_event(
                    dingtalk_user_id=liu.dingtalk_user_id,
                    conversation_id=direct_conversation,
                    user_text="查看8月3日的日报",
                    assistant_text=report_text,
                    now=now,
                )
            )
            await session.flush()
            direct_source = f"rollback-smoke-liu-direct-current-{uuid4()}"
            direct = await _run_turn(
                session,
                user=liu,
                text="可以，提交",
                conversation_id=direct_conversation,
                source_message_id=direct_source,
                settings=settings,
                llm_client=llm_client,
                now=now,
            )
            await session.flush()
            direct_receipts = await _receipts(session, direct_source)
            if (
                not direct.actual_write
                or not any(
                    row.tool_name == "confirm_report" and row.changed
                    for row in direct_receipts
                )
                or report.status != "completed"
                or not report.confirmed_by_user
            ):
                raise AssertionError(
                    (direct, report.status, _receipt_output(direct_receipts))
                )
            output["liu_direct_confirmation"] = {
                "message": direct.message,
                "receipts": _receipt_output(direct_receipts),
                "report_status": report.status,
            }

            prepare_report_for_confirmation()
            date_conversation = f"rollback-smoke-liu-date-{uuid4()}"
            session.add(
                _history_event(
                    dingtalk_user_id=liu.dingtalk_user_id,
                    conversation_id=date_conversation,
                    user_text="可以，提交",
                    assistant_text="你要提交哪一天的日报？",
                    now=now,
                )
            )
            await session.flush()
            date_source = f"rollback-smoke-liu-date-current-{uuid4()}"
            date_answer = await _run_turn(
                session,
                user=liu,
                text="昨天的",
                conversation_id=date_conversation,
                source_message_id=date_source,
                settings=settings,
                llm_client=llm_client,
                now=now,
            )
            await session.flush()
            date_receipts = await _receipts(session, date_source)
            if (
                not date_answer.actual_write
                or not any(
                    row.tool_name == "confirm_report" and row.changed
                    for row in date_receipts
                )
                or report.status != "completed"
                or not report.confirmed_by_user
            ):
                raise AssertionError(
                    (
                        date_answer,
                        report.status,
                        _receipt_output(date_receipts),
                    )
                )
            output["liu_date_answer_confirmation"] = {
                "message": date_answer.message,
                "receipts": _receipt_output(date_receipts),
                "report_status": report.status,
            }

            prepare_report_for_confirmation()
            deictic_conversation = (
                f"rollback-smoke-liu-deictic-{uuid4()}"
            )
            session.add(
                _history_event(
                    dingtalk_user_id=liu.dingtalk_user_id,
                    conversation_id=deictic_conversation,
                    user_text="可以，提交",
                    assistant_text=(
                        "请确认要提交哪一天（刚才展示的是2026-08-03日报）。"
                    ),
                    now=now,
                )
            )
            await session.flush()
            deictic_source = (
                f"rollback-smoke-liu-deictic-current-{uuid4()}"
            )
            deictic = await _run_turn(
                session,
                user=liu,
                text="就刚才那份",
                conversation_id=deictic_conversation,
                source_message_id=deictic_source,
                settings=settings,
                llm_client=llm_client,
                now=now,
            )
            await session.flush()
            deictic_receipts = await _receipts(session, deictic_source)
            if (
                not deictic.actual_write
                or not any(
                    row.tool_name == "confirm_report" and row.changed
                    for row in deictic_receipts
                )
                or report.status != "completed"
                or not report.confirmed_by_user
            ):
                raise AssertionError(
                    (
                        deictic,
                        report.status,
                        _receipt_output(deictic_receipts),
                    )
                )
            output["liu_deictic_date_answer_confirmation"] = {
                "message": deictic.message,
                "receipts": _receipt_output(deictic_receipts),
                "report_status": report.status,
            }

            prepare_report_for_confirmation(complete=False)
            incomplete_conversation = (
                f"rollback-smoke-liu-incomplete-{uuid4()}"
            )
            session.add(
                _history_event(
                    dingtalk_user_id=liu.dingtalk_user_id,
                    conversation_id=incomplete_conversation,
                    user_text="补昨天的日报",
                    assistant_text=(
                        "2026-08-03 日报：今日工作和明日计划已填写，"
                        "问题/风险尚未填写。"
                    ),
                    now=now,
                )
            )
            await session.flush()
            incomplete_source = (
                f"rollback-smoke-liu-incomplete-current-{uuid4()}"
            )
            incomplete = await _run_turn(
                session,
                user=liu,
                text="可以，提交",
                conversation_id=incomplete_conversation,
                source_message_id=incomplete_source,
                settings=settings,
                llm_client=llm_client,
                now=now,
            )
            if (
                incomplete.reason
                != "tool_call_canary_report_incomplete"
                or incomplete.actual_write
                or "问题/风险" not in incomplete.message
                or "已经提交" in incomplete.message
            ):
                raise AssertionError(incomplete)
            output["liu_incomplete_guidance"] = {
                "message": incomplete.message,
                "actual_write": incomplete.actual_write,
            }
            for field, value in original_report.items():
                setattr(report, field, value)
            await session.flush()

            pang_conversation = f"rollback-smoke-pang-focus-{uuid4()}"
            session.add(
                _history_event(
                    dingtalk_user_id=pang.dingtalk_user_id,
                    conversation_id=pang_conversation,
                    user_text="8月3日的",
                    assistant_text=(
                        "查询日期：2026-08-03。刘聪的日报状态：已完成。"
                    ),
                    now=now,
                )
            )
            await session.flush()
            pang_source = f"rollback-smoke-pang-focus-current-{uuid4()}"
            pang_focus = await _run_turn(
                session,
                user=pang,
                text="那你的晨报怎么说他没交",
                conversation_id=pang_conversation,
                source_message_id=pang_source,
                settings=settings,
                llm_client=llm_client,
                now=now,
            )
            await session.flush()
            pang_receipts = await _receipts(session, pang_source)
            receipt_report_dates = set().union(
                *(
                    _report_dates(row.safe_user_facts)
                    for row in pang_receipts
                )
            )
            grounded_by_receipt = bool(pang_receipts) and (
                any(
                    row.tool_name == "query_managed_daily_reports"
                    for row in pang_receipts
                )
                and receipt_report_dates == {"2026-08-03"}
            )
            grounded_by_recent_context = (
                not pang_receipts
                and ("2026-08-03" in pang_focus.message or "8月3" in pang_focus.message)
                and "2026-08-04" not in pang_focus.message
                and "8月4" not in pang_focus.message
            )
            if not (grounded_by_receipt or grounded_by_recent_context):
                raise AssertionError(
                    (pang_focus, _receipt_output(pang_receipts))
                )
            output["pang_date_focus"] = {
                "message": pang_focus.message,
                "receipts": _receipt_output(pang_receipts),
            }

            briefing_conversation = f"rollback-smoke-pang-briefing-{uuid4()}"
            briefing_text = (
                "8月3日管理晨报：综合管理部日报提交情况中，刘聪显示未提交。"
            )
            session.add(
                ReportInteractionEvent(
                    user_id=pang.id,
                    dingtalk_user_id=pang.dingtalk_user_id,
                    report_date=REPORT_DATE,
                    message_text=briefing_text,
                    llm_decision_json={
                        "interaction_type": "daily_briefing",
                        "message_status": "accepted_by_provider",
                        "provider_references": [
                            "rollback-smoke-provider-ref"
                        ],
                    },
                    backend_action="daily_briefing_sent",
                    before_snapshot_json={},
                    after_snapshot_json={},
                    created_at=now - timedelta(minutes=1),
                )
            )
            await session.flush()
            briefing_source = (
                f"rollback-smoke-pang-briefing-current-{uuid4()}"
            )
            provenance = await _run_turn(
                session,
                user=pang,
                text="你刚才发我的这份晨报是从哪里来的？",
                conversation_id=briefing_conversation,
                source_message_id=briefing_source,
                settings=settings,
                llm_client=llm_client,
                now=now,
            )
            denied_markers = (
                "不是我发",
                "我没有发",
                "无法确认是否",
                "没有这条发送记录",
                "看不到这条",
            )
            if (
                not provenance.handled
                or not provenance.message.strip()
                or any(marker in provenance.message for marker in denied_markers)
            ):
                raise AssertionError(provenance)
            output["scheduled_briefing_provenance"] = {
                "message": provenance.message,
                "denied": False,
            }

            for field, value in original_report.items():
                setattr(report, field, value)
            await session.rollback()

        async with AsyncSessionLocal() as verification_session:
            restored = await verification_session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == LIU_USER_ID,
                    DailyReport.report_date == REPORT_DATE,
                )
            )
            restored_counts = {
                "receipts": await verification_session.scalar(
                    select(func.count(ToolCallCanaryReceipt.receipt_id))
                ),
                "webhooks": await verification_session.scalar(
                    select(func.count(WebhookEvent.id))
                ),
                "briefings": await verification_session.scalar(
                    select(func.count(ReportInteractionEvent.id)).where(
                        ReportInteractionEvent.backend_action
                        == "daily_briefing_sent"
                    )
                ),
            }
            if (
                restored.status != original_report["status"]
                or restored.confirmation_type
                != original_report["confirmation_type"]
                or restored_counts["receipts"] != baseline_receipt_count
                or restored_counts["webhooks"] != baseline_webhook_count
                or restored_counts["briefings"] != baseline_briefing_count
            ):
                raise AssertionError((restored.status, restored_counts))
            output["rollback"] = {
                "report_status": restored.status,
                "report_confirmation_type": restored.confirmation_type,
                "temporary_rows_removed": True,
            }
    finally:
        await llm_client.close()

    print(json.dumps(output, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
