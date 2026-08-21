from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from app.agent2.business.models import Agent2IdentityBinding
from app.agent2.tool_calling.production_store import (
    ToolCallCanaryReceipt,
    trusted_snapshot_from_report,
)
from app.agent2.typed_daily_executor import TYPED_REPORT_VERSION_KEY
from app.agent2.weekly_plan_store import _plans as weekly_plan_rows
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import DailyReport, User, WebhookEvent
from scripts.smoke_20260811_overnight_daily_rollback import (
    PANG_USER_ID,
    _turn,
    _user_and_control,
)


RUN_ID = f"daily-focus-weekly-submit-{uuid4()}"
REPORT_DATE = date(2026, 8, 21)
TARGET_WEEK_START = date(2026, 8, 24)
CASES = (
    ("bare", "没问题 提交", "daily"),
    ("natural", "问题风险没有，提交吧", "daily"),
    ("followup", "没有问题，按这个提交", "daily"),
    (
        "daily_plan_followup",
        (
            "你写的没有问题，不改。计划：跟进苏宁38家债权，"
            "确认优先债权部分情况；准备齐河智慧产业园上诉状答辩资料。"
        ),
        "daily_plan_update",
    ),
    (
        "explicit_weekly_switch",
        "这次提交周工作计划",
        "weekly",
    ),
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_failure(name: str, exc: Exception) -> dict[str, object]:
    failure: dict[str, object] = {
        "name": name,
        "error_type": type(exc).__name__,
    }
    raw = exc.args[0] if exc.args else None
    if isinstance(raw, str):
        failure["reason"] = raw[:300]
    elif isinstance(raw, dict):
        for key in (
            "reason",
            "business_result",
            "release_blockers",
            "daily_date_votes",
            "model_flow",
            "result_receipts",
            "unexpected_tools",
        ):
            if key in raw:
                failure[key] = raw[key]
    return failure


def _daily_state(report: DailyReport) -> dict[str, object]:
    return {
        "today_work": list(report.today_work or ()),
        "problems": list(report.problems or ()),
        "tomorrow_plan": list(report.tomorrow_plan or ()),
        "section_status": dict(report.section_status or {}),
        "status": report.status,
        "confirmation_type": report.confirmation_type,
        "confirmed_by_user": bool(report.confirmed_by_user),
        "submitted_at": report.submitted_at,
    }


async def _load_weekly_row(session, *, user_id: str):
    return (
        await session.execute(
            select(weekly_plan_rows).where(
                weekly_plan_rows.c.owner_user_id == user_id,
                weekly_plan_rows.c.target_week_start == TARGET_WEEK_START,
            )
        )
    ).mappings().one()


def _weekly_state(row) -> dict[str, object]:
    return {
        "plan_id": str(row["plan_id"]),
        "status": row["status"],
        "version": int(row["version"]),
        "submitted_at": row["submitted_at"],
    }


async def _prepare_recent_daily_focus(
    session,
    *,
    user,
    tenant_id: str,
    conversation_id: str,
    now: datetime,
    case_name: str,
) -> tuple[DailyReport, str]:
    report = await session.scalar(
        select(DailyReport).where(
            DailyReport.user_id == user.id,
            DailyReport.report_date == REPORT_DATE,
        )
    )
    if report is None:
        raise AssertionError("expected current Daily Report is missing")
    report.today_work = [
        "完成被告案件未结案件签阅单",
        "日报助手上线周计划功能",
        "参加团队例会",
    ]
    report.problems = []
    report.tomorrow_plan = ["研究绩效维度指标周完成情况填写"]
    report.section_status = {
        TYPED_REPORT_VERSION_KEY: 40,
        "_draft_item_ids": {
            "today_work": [
                f"{RUN_ID}-{case_name}-today-{index}"
                for index in range(1, 4)
            ],
            "problems": [],
            "tomorrow_plan": [f"{RUN_ID}-{case_name}-tomorrow-1"],
        },
    }
    report.completeness_score = Decimal("0.67")
    report.status = "collecting"
    report.confirmation_type = "none"
    report.confirmed_by_user = False
    report.submitted_at = None
    report.pending_confirmation_at = None
    report.auto_submit_at = None
    await session.flush()

    trusted = trusted_snapshot_from_report(
        user=user,
        tenant_id=tenant_id,
        report_date=REPORT_DATE,
        report=report,
    )
    previous_source_id = f"{RUN_ID}-{case_name}-previous"
    assistant_reply = (
        "已记录今日日报，问题风险未填写。\n\n"
        "2026-08-21 日报\n今日工作\n1. 完成被告案件未结案件签阅单\n"
        "2. 日报助手上线周计划功能\n3. 参加团队例会\n\n"
        "问题风险\n（未填写）\n\n明日计划\n"
        "1. 研究绩效维度指标周完成情况填写\n\n状态：收集整理中"
    )
    session.add(
        WebhookEvent(
            idempotency_key=previous_source_id,
            external_message_id=previous_source_id,
            dingtalk_user_id=user.dingtalk_user_id,
            report_id=report.id,
            payload={
                "conversationId": conversation_id,
                "text": {
                    "content": (
                        "今天完成三项工作，明天研究绩效维度指标周完成情况填写"
                    )
                },
            },
            response_payload={
                "msgtype": "text",
                "text": {"content": assistant_reply},
            },
            status="processed",
            received_at=now - timedelta(minutes=1),
            processed_at=now - timedelta(minutes=1),
        )
    )
    snapshot = {
        **trusted.safe_snapshot(),
        "report_state_sha256": trusted.state_sha256,
    }
    session.add(
        ToolCallCanaryReceipt(
            receipt_id=uuid4(),
            tenant_id=tenant_id,
            user_id=str(user.id),
            conversation_id=conversation_id,
            source_message_id=previous_source_id,
            tool_call_id=f"{RUN_ID}-{case_name}-daily-add",
            tool_name="add_daily_items",
            idempotency_key=f"{RUN_ID}-{case_name}-daily-add-key",
            canonical_arguments_hash=_sha(f"{RUN_ID}-{case_name}-arguments"),
            request_fingerprint=_sha(f"{RUN_ID}-{case_name}-request"),
            operation_fingerprint=_sha(f"{RUN_ID}-{case_name}-operation"),
            status="success",
            changed=True,
            target_type="daily_report",
            target_id=str(report.id),
            before_version=trusted.version - 1,
            after_version=trusted.version,
            affected_item_ids=[item.item_id for item in trusted.items],
            safe_user_facts={
                "actual_write": True,
                "report_snapshot": snapshot,
            },
            before_state_hash=_sha(f"{RUN_ID}-{case_name}-before"),
            after_state_hash=trusted.state_sha256,
            typed_receipt_ids=[],
            execution_mode="canary_execute",
            created_at=now - timedelta(minutes=1),
        )
    )
    await session.flush()
    return report, previous_source_id


async def _run_case(
    llm_client: LLMClient,
    *,
    name: str,
    text: str,
    expected_domain: str,
):
    now = datetime(2026, 8, 21, 22, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
    conversation_id = f"{RUN_ID}-{name}-conversation"
    source_message_id = f"{RUN_ID}-{name}-current"
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            binding = await session.scalar(
                select(Agent2IdentityBinding).where(
                    Agent2IdentityBinding.user_id == str(user.id),
                    Agent2IdentityBinding.active.is_(True),
                )
            )
            if binding is None:
                raise AssertionError("Agent2 identity binding is missing")
            weekly_before = await _load_weekly_row(
                session,
                user_id=str(user.id),
            )
            await session.execute(
                update(weekly_plan_rows)
                .where(
                    weekly_plan_rows.c.plan_id == weekly_before["plan_id"]
                )
                .values(
                    status="pending_confirmation",
                    submitted_at=None,
                    updated_at=now,
                )
            )
            report, _previous_source_id = await _prepare_recent_daily_focus(
                session,
                user=user,
                tenant_id=binding.tenant_id,
                conversation_id=conversation_id,
                now=now,
                case_name=name,
            )
            outcome = await _turn(
                session,
                user=user,
                settings=settings,
                llm_client=llm_client,
                text=text,
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                now=now,
                conversation_kind="direct",
                message_occurred_at=now,
            )
            if outcome.messages_enabled:
                raise AssertionError("rollback smoke transport was enabled")
            await session.flush()
            receipts = list(
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
            weekly_after = await _load_weekly_row(
                session,
                user_id=str(user.id),
            )
            await session.refresh(report)
            tools = [row.tool_name for row in receipts]
            if expected_domain == "daily":
                if "submit_next_weekly_plan" in tools:
                    raise AssertionError("older Weekly Work Plan was submitted")
                if tools not in (["add_daily_items"], ["confirm_report"]):
                    raise AssertionError({"unexpected_tools": tools})
                if report.status != "completed" or not report.confirmed_by_user:
                    raise AssertionError("Daily Report was not submitted")
                if not bool(
                    dict(report.section_status or {}).get(
                        "problems_acknowledged_empty"
                    )
                ):
                    raise AssertionError(
                        "Daily risk field was not acknowledged empty"
                    )
                if weekly_after["status"] != "pending_confirmation":
                    raise AssertionError("Weekly Work Plan state changed")
            elif expected_domain == "weekly":
                if tools != ["submit_next_weekly_plan"]:
                    raise AssertionError({"unexpected_tools": tools})
                if weekly_after["status"] != "submitted":
                    raise AssertionError("explicit Weekly Work Plan was not submitted")
                if report.status != "collecting" or report.confirmed_by_user:
                    raise AssertionError("explicit Weekly switch changed Daily Report")
            elif expected_domain == "daily_plan_update":
                if "submit_next_weekly_plan" in tools:
                    raise AssertionError("Daily plan update submitted Weekly Work Plan")
                if tools != ["add_daily_items"]:
                    raise AssertionError({"unexpected_tools": tools})
                written_plans = "\n".join(report.tomorrow_plan or ())
                if not all(
                    fragment in written_plans
                    for fragment in (
                        "苏宁38家债权",
                        "齐河智慧产业园",
                    )
                ):
                    raise AssertionError("Daily tomorrow plan content is missing")
                if weekly_after["status"] != "pending_confirmation":
                    raise AssertionError("Daily plan update changed Weekly Work Plan")
            else:
                raise AssertionError("unknown expected domain")
            return {
                "name": name,
                "status": "pass",
                "tools": tools,
                "business_result": outcome.user_visible_result,
                "model_call_count": outcome.model_call_count,
                "daily_status": report.status,
                "weekly_status": weekly_after["status"],
                "transport_suppressed": not outcome.messages_enabled,
            }
        finally:
            await session.rollback()


async def _residue() -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        event_count = int(
            await session.scalar(
                select(func.count(WebhookEvent.id)).where(
                    WebhookEvent.idempotency_key.like(f"{RUN_ID}%")
                )
            )
            or 0
        )
        receipt_count = int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                    ToolCallCanaryReceipt.source_message_id.like(
                        f"{RUN_ID}%"
                    )
                )
            )
            or 0
        )
        await session.rollback()
        return {"events": event_count, "receipts": receipt_count}


async def _production_state() -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        user = await session.get(User, PANG_USER_ID)
        if user is None:
            raise AssertionError("rollback smoke user is missing")
        report = await session.scalar(
            select(DailyReport).where(
                DailyReport.user_id == user.id,
                DailyReport.report_date == REPORT_DATE,
            )
        )
        if report is None:
            raise AssertionError("production Daily Report is missing")
        weekly = await _load_weekly_row(
            session,
            user_id=str(user.id),
        )
        state = {
            "daily": _daily_state(report),
            "weekly": _weekly_state(weekly),
        }
        await session.rollback()
        return state


async def main() -> None:
    production_before = await _production_state()
    llm_client = LLMClient(get_settings())
    results = []
    failures = []
    selected = {
        value.strip()
        for value in os.getenv("SMOKE_CASE_NAMES", "").split(",")
        if value.strip()
    }
    try:
        for name, text, expected_domain in CASES:
            if selected and name not in selected:
                continue
            try:
                results.append(
                    await _run_case(
                        llm_client,
                        name=name,
                        text=text,
                        expected_domain=expected_domain,
                    )
                )
            except Exception as exc:
                failures.append(_safe_failure(name, exc))
    finally:
        await llm_client.close()
    residue = await _residue()
    production_after = await _production_state()
    production_state_changes = {
        "daily": int(
            production_before["daily"] != production_after["daily"]
        ),
        "weekly": int(
            production_before["weekly"] != production_after["weekly"]
        ),
    }
    transport_enabled_cases = sum(
        not bool(item.get("transport_suppressed"))
        for item in results
    )
    output = {
        "status": (
            "pass"
            if not failures
            and not any(residue.values())
            and not any(production_state_changes.values())
            and transport_enabled_cases == 0
            else "failed"
        ),
        "passed": len(results),
        "failed": len(failures),
        "dingtalk_send_calls": 0,
        "transport_enabled_cases": transport_enabled_cases,
        "rollback_residue": residue,
        "production_state_changes": production_state_changes,
        "results": results,
        "failures": failures,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    await engine.dispose()
    if output["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
