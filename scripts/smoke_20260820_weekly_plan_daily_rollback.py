from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta
from time import perf_counter
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.production_store import (
    ToolCallCanaryReceipt,
    capture_production_state,
)
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.agent2.weekly_plan_store import (
    SqlWeeklyPlanStore,
)
from app.agent2.weekly_plan_store import (
    _audits as weekly_audits,
)
from app.agent2.weekly_plan_store import (
    _receipts as weekly_receipts,
)
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import DailyReport, User
from scripts.smoke_20260811_overnight_daily_rollback import _turn

TARGET_WEEK_START = date(2026, 8, 24)
RUN_ID = f"weekly-daily-rollback-{uuid4().hex[:12]}"
CONVERSATION_ID = f"{RUN_ID}-conversation"


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _control_snapshot(control: ToolCallCanaryControl) -> dict[str, object]:
    return {
        "enabled": bool(control.enabled),
        "messages_enabled": bool(control.messages_enabled),
        "registry_digest": str(control.registry_digest),
        "prompt_sha256": str(control.prompt_sha256),
        "model_name": str(control.model_name),
        "version": int(control.version),
    }


async def _select_unused_weekday(session, *, user_id: UUID) -> date:
    candidate = date(2026, 1, 5)
    for offset in range(120):
        report_date = candidate + timedelta(days=offset)
        if report_date.weekday() >= 5:
            continue
        existing = await session.scalar(
            select(DailyReport.id).where(
                DailyReport.user_id == user_id,
                DailyReport.report_date == report_date,
            )
        )
        if existing is None:
            return report_date
    raise AssertionError("no unused rollback Daily Report date is available")


async def _select_empty_plan_user(
    session,
) -> tuple[User, ToolCallCanaryControl, object]:
    settings = get_settings()
    raw_user_ids = str(settings.agent2_weekly_plan_user_allowlist or "")
    user_ids = tuple(
        value.strip() for value in raw_user_ids.split(",") if value.strip()
    )
    if not 1 <= len(user_ids) <= 74 or len(set(user_ids)) != len(user_ids):
        raise AssertionError("weekly-plan rollback requires a valid enabled scope")
    tenant_id = str(settings.agent2_weekly_plan_tenant_allowlist or "")
    if not tenant_id:
        raise AssertionError("weekly-plan tenant canary is not configured")

    store = SqlWeeklyPlanStore(session)
    candidates: list[tuple[User, ToolCallCanaryControl]] = []
    for raw_user_id in user_ids:
        user = await session.get(User, UUID(raw_user_id))
        control = await session.scalar(
            select(ToolCallCanaryControl).where(
                ToolCallCanaryControl.tenant_id == tenant_id,
                ToolCallCanaryControl.user_id == raw_user_id,
            )
        )
        if user is None or control is None:
            raise AssertionError("weekly-plan canary identity binding is incomplete")
        plan = await store.load_plan_by_owner_week(
            tenant_id=tenant_id,
            owner_user_id=raw_user_id,
            target_week_start=TARGET_WEEK_START,
        )
        if plan is None:
            candidates.append((user, control))
    if not candidates:
        raise AssertionError("rollback requires one enabled user without a next-week plan")
    user, control = candidates[0]
    return user, control, settings


async def _canary_receipt_summary(session, source_message_id: str) -> list[dict]:
    rows = tuple(
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
    return [
        {
            "tool_name": str(row.tool_name),
            "status": str(row.status),
            "changed": bool(row.changed),
            "error_code": row.error_code,
        }
        for row in rows
    ]


async def _run_case(
    session,
    *,
    user: User,
    settings: object,
    client: LLMClient,
    name: str,
    text: str,
    now: datetime,
    model_audit_sink: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    source_message_id = f"{RUN_ID}-{name}"
    started = perf_counter()
    outcome = await _turn(
        session,
        user=user,
        settings=settings,
        llm_client=client,
        text=text,
        conversation_id=CONVERSATION_ID,
        source_message_id=source_message_id,
        now=now,
        conversation_kind="direct",
        message_occurred_at=now,
        accepted_business_results=frozenset({"success", "no_op"}),
        model_audit_sink=model_audit_sink,
    )
    await session.flush()
    receipts = await _canary_receipt_summary(session, source_message_id)
    if not receipts or any(row["error_code"] for row in receipts):
        raise AssertionError(
            {
                "case": name,
                "business_result": outcome.user_visible_result,
                "receipt_summary": receipts,
            }
        )
    return {
        "name": name,
        "business_result": outcome.user_visible_result,
        "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        "receipts": receipts,
    }


def _items_by_date(plan) -> dict[date, tuple[str, ...]]:
    return {
        day.plan_date: tuple(item.original_text for item in day.items)
        for day in plan.days
    }


def _weekly_review_audit_summary(
    audits: list[dict[str, object]],
) -> list[dict[str, object]]:
    summary: list[dict[str, object]] = []
    for audit in audits:
        turns = audit.get("turns")
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            message = turn.get("message")
            metadata = turn.get("response_metadata")
            calls = (
                message.get("calls")
                if isinstance(message, dict)
                and isinstance(message.get("calls"), list)
                else []
            )
            weekly_contents: list[str] = []
            tool_names: list[str] = []
            for call in calls:
                if not isinstance(call, dict):
                    continue
                tool_names.append(str(call.get("name") or ""))
                if call.get("name") != "apply_next_weekly_plan":
                    continue
                arguments = call.get("arguments")
                operations = (
                    arguments.get("operations")
                    if isinstance(arguments, dict)
                    else None
                )
                if isinstance(operations, list):
                    weekly_contents.extend(
                        str(operation.get("content") or "")
                        for operation in operations
                        if isinstance(operation, dict)
                        and operation.get("content")
                    )
            summary.append(
                {
                    "iteration": turn.get("iteration"),
                    "tool_names": tool_names,
                    "weekly_contents": weekly_contents,
                    "weekly_reclassification_focused_review": bool(
                        isinstance(metadata, dict)
                        and metadata.get(
                            "weekly_reclassification_focused_review"
                        )
                    ),
                }
            )
    return summary


async def main() -> None:
    client = LLMClient(get_settings())
    baseline_hash = ""
    baseline_control: dict[str, object] = {}
    tenant_id = ""
    user_id: UUID | None = None
    unused_report_date: date | None = None
    results: list[dict[str, object]] = []
    temporary_hash = ""
    final_plan_version = 0
    try:
        async with AsyncSessionLocal() as session:
            try:
                user, control, settings = await _select_empty_plan_user(session)
                user_id = user.id
                tenant_id = str(control.tenant_id)
                baseline_control = _control_snapshot(control)
                unused_report_date = await _select_unused_weekday(
                    session,
                    user_id=user.id,
                )
                baseline = await capture_production_state(
                    session,
                    tenant_id=tenant_id,
                    user_id=user.id,
                    conversation_id=CONVERSATION_ID,
                    include_weekly_plan=True,
                )
                baseline_hash = baseline.canonical_hash

                control.enabled = True
                control.messages_enabled = False
                control.registry_digest = runtime_registry_contract_digest(settings)
                control.prompt_sha256 = canary_prompt_sha256()
                control.model_name = CANARY_MODEL_NAME
                await session.flush()

                now = datetime(
                    2026,
                    8,
                    20,
                    19,
                    30,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                )
                first_case_audits: list[dict[str, object]] = []
                results.append(
                    await _run_case(
                        session,
                        user=user,
                        settings=settings,
                        client=client,
                        name="add_without_submit",
                        text=(
                            "下周三再增加一条：准备星河案证据清单，"
                            "先别提交。"
                        ),
                        now=now,
                        model_audit_sink=first_case_audits,
                    )
                )
                store = SqlWeeklyPlanStore(session)
                plan = await store.load_plan_by_owner_week(
                    tenant_id=tenant_id,
                    owner_user_id=str(user.id),
                    target_week_start=TARGET_WEEK_START,
                )
                if plan is None:
                    raise AssertionError("weekly plan was not created")
                wednesday_items = _items_by_date(plan)[date(2026, 8, 26)]
                matching_items = tuple(
                    item
                    for item in wednesday_items
                    if "星河案证据清单" in item
                )
                if not matching_items or any(
                    "提交" in item for item in matching_items
                ):
                    raise AssertionError(
                        {
                            "error": "operation control leaked into plan content",
                            "model_flow": _weekly_review_audit_summary(
                                first_case_audits
                            ),
                        }
                    )
                if plan.status == "submitted":
                    raise AssertionError("add-without-submit unexpectedly submitted")

                results.append(
                    await _run_case(
                        session,
                        user=user,
                        settings=settings,
                        client=client,
                        name="two_recurring_matters",
                        text="下周一到周五每天做档案催收和日常用印审核。",
                        now=now + timedelta(minutes=1),
                    )
                )
                plan = await store.load_plan_by_owner_week(
                    tenant_id=tenant_id,
                    owner_user_id=str(user.id),
                    target_week_start=TARGET_WEEK_START,
                )
                if plan is None:
                    raise AssertionError("weekly plan disappeared after recurrence")
                by_date = _items_by_date(plan)
                for offset in range(5):
                    body = "\n".join(
                        by_date[TARGET_WEEK_START + timedelta(days=offset)]
                    )
                    if "档案催收" not in body or "日常用印审核" not in body:
                        raise AssertionError(
                            {"recurrence_date_offset_missing": offset}
                        )

                results.append(
                    await _run_case(
                        session,
                        user=user,
                        settings=settings,
                        client=client,
                        name="saturday_empty",
                        text="下周六没安排，留空。",
                        now=now + timedelta(minutes=2),
                    )
                )
                plan = await store.load_plan_by_owner_week(
                    tenant_id=tenant_id,
                    owner_user_id=str(user.id),
                    target_week_start=TARGET_WEEK_START,
                )
                if plan is None or plan.days[5].state != "explicitly_empty":
                    raise AssertionError("Saturday was not explicitly left empty")

                report_label = (
                    f"{unused_report_date.year}年{unused_report_date.month}月"
                    f"{unused_report_date.day}日"
                )
                results.append(
                    await _run_case(
                        session,
                        user=user,
                        settings=settings,
                        client=client,
                        name="historical_daily_and_weekly_plan",
                        text=(
                            f"补写{report_label}日报：今日工作完成甲项目合同复核。"
                            "另记下周四工作计划：整理乙项目材料。"
                        ),
                        now=now + timedelta(minutes=3),
                    )
                )
                report = await session.scalar(
                    select(DailyReport).where(
                        DailyReport.user_id == user.id,
                        DailyReport.report_date == unused_report_date,
                    )
                )
                if report is None or "甲项目合同复核" not in "\n".join(
                    report.today_work or ()
                ):
                    raise AssertionError("mixed turn did not write the Daily Report")
                plan = await store.load_plan_by_owner_week(
                    tenant_id=tenant_id,
                    owner_user_id=str(user.id),
                    target_week_start=TARGET_WEEK_START,
                )
                if plan is None or "整理乙项目材料" not in "\n".join(
                    _items_by_date(plan)[date(2026, 8, 27)]
                ):
                    raise AssertionError("mixed turn did not write the Weekly Work Plan")

                results.append(
                    await _run_case(
                        session,
                        user=user,
                        settings=settings,
                        client=client,
                        name="edit_then_request_submit",
                        text=(
                            "把下周三的准备星河案证据清单改成"
                            "准备星河案开庭材料，然后提交下周工作计划。"
                        ),
                        now=now + timedelta(minutes=4),
                    )
                )
                plan = await store.load_plan_by_owner_week(
                    tenant_id=tenant_id,
                    owner_user_id=str(user.id),
                    target_week_start=TARGET_WEEK_START,
                )
                if plan is None:
                    raise AssertionError("weekly plan disappeared after edit")
                wednesday_body = "\n".join(
                    _items_by_date(plan)[date(2026, 8, 26)]
                )
                if (
                    "准备星河案开庭材料" not in wednesday_body
                    or "准备星河案证据清单" in wednesday_body
                    or plan.status == "submitted"
                ):
                    raise AssertionError("edit-before-submit boundary is incorrect")

                results.append(
                    await _run_case(
                        session,
                        user=user,
                        settings=settings,
                        client=client,
                        name="explicit_submit_after_preview",
                        text="确认提交下周工作计划。",
                        now=now + timedelta(minutes=5),
                    )
                )
                plan = await store.load_plan_by_owner_week(
                    tenant_id=tenant_id,
                    owner_user_id=str(user.id),
                    target_week_start=TARGET_WEEK_START,
                )
                if plan is None or plan.status != "submitted":
                    raise AssertionError("resolved Weekly Work Plan was not submitted")
                final_plan_version = plan.version

                temporary = await capture_production_state(
                    session,
                    tenant_id=tenant_id,
                    user_id=user.id,
                    conversation_id=CONVERSATION_ID,
                    include_weekly_plan=True,
                )
                temporary_hash = temporary.canonical_hash
                if temporary_hash == baseline_hash:
                    raise AssertionError("rollback smoke produced no temporary state change")
            finally:
                await session.rollback()
    finally:
        await client.close()

    if user_id is None or unused_report_date is None:
        raise AssertionError("rollback identity was not initialized")

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        control = await session.scalar(
            select(ToolCallCanaryControl).where(
                ToolCallCanaryControl.tenant_id == tenant_id,
                ToolCallCanaryControl.user_id == str(user_id),
            )
        )
        if user is None or control is None:
            raise AssertionError("rollback identity disappeared")
        restored = await capture_production_state(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=CONVERSATION_ID,
            include_weekly_plan=True,
        )
        canary_receipt_residue = int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                    ToolCallCanaryReceipt.source_message_id.like(f"{RUN_ID}%")
                )
            )
            or 0
        )
        weekly_receipt_residue = int(
            await session.scalar(
                select(func.count()).select_from(weekly_receipts).where(
                    weekly_receipts.c.source_message_id.like(f"{RUN_ID}%")
                )
            )
            or 0
        )
        weekly_audit_residue = int(
            await session.scalar(
                select(func.count()).select_from(weekly_audits).where(
                    weekly_audits.c.source_message_id.like(f"{RUN_ID}%")
                )
            )
            or 0
        )
        restored_report = await session.scalar(
            select(DailyReport.id).where(
                DailyReport.user_id == user_id,
                DailyReport.report_date == unused_report_date,
            )
        )
        restored_control = _control_snapshot(control)
        await session.rollback()

    residues = {
        "canary_receipts": canary_receipt_residue,
        "weekly_command_receipts": weekly_receipt_residue,
        "weekly_audits": weekly_audit_residue,
        "temporary_daily_report": int(restored_report is not None),
    }
    if restored.canonical_hash != baseline_hash:
        raise AssertionError("production state did not return to its baseline hash")
    if restored_control != baseline_control:
        raise AssertionError("Agent2 control did not return to its baseline")
    if any(residues.values()):
        raise AssertionError({"rollback_residue": residues})

    print(
        json.dumps(
            {
                "status": "pass",
                "model": CANARY_MODEL_NAME,
                "target_week_start": TARGET_WEEK_START.isoformat(),
                "cases_passed": len(results),
                "cases_failed": 0,
                "results": results,
                "temporary_state_changed": temporary_hash != baseline_hash,
                "final_plan_status_before_rollback": "submitted",
                "final_plan_version_before_rollback": final_plan_version,
                "baseline_restored": True,
                "rollback_residue": residues,
                "dingtalk_send_calls": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
