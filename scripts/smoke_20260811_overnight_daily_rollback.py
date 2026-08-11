from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.agent2.tool_calling import canary_service
from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.canary_service import (
    build_canary_persisted_response_payload,
    process_tool_call_canary_ingress,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.agent2.tool_calling.release_gate import assess_release_turn
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import DailyReport, User, WebhookEvent

PANG_USER_ID = "222b1eeb-4faa-40cf-a193-e1892c9377b0"
SOURCE_PREFIX = f"overnight-v22-rollback-{uuid4()}"
OBSERVATION_KEY = "_agent2_turn_observation_v1"
SELECTED_CASE_NAMES = frozenset(
    value.strip()
    for value in os.getenv("SMOKE_CASE_NAMES", "").split(",")
    if value.strip()
)
INCLUDE_MODEL_AUDIT = os.getenv("SMOKE_INCLUDE_MODEL_AUDIT", "").strip() == "1"


UNDATED_CASES = (
    (
        date(2026, 1, 10),
        "今天完成合同审核；问题暂无；明天整理证据材料。请提交日报。",
        {"today_work": ("合同",), "tomorrow_plan": ("证据",)},
    ),
    (
        date(2026, 1, 12),
        "日报内容：今日核对付款节点，没什么问题；明日继续跟进回款，请直接提交。",
        {"today_work": ("付款",), "tomorrow_plan": ("回款",)},
    ),
    (
        date(2026, 1, 14),
        "我今天完成用印检查，目前没有风险，明天继续梳理台账，帮我提交日报。",
        {"today_work": ("用印",), "tomorrow_plan": ("台账",)},
    ),
)


EXPLICIT_TODAY_CASES = (
    (
        date(2026, 1, 16),
        "这是今天的日报：完成合同归档；问题暂无；明天复核清单。请提交。",
        {"today_work": ("合同", "归档"), "tomorrow_plan": ("清单",)},
    ),
    (
        date(2026, 1, 18),
        "我要写今天（1月18日）的日报：核对付款资料；没有问题；明天跟进签章。直接提交。",
        {"today_work": ("付款",), "tomorrow_plan": ("签章",)},
    ),
    (
        date(2026, 1, 20),
        "日报日期就是2026年1月20日。今日完成台账检查，风险暂无，明日补充证据，请提交。",
        {"today_work": ("台账",), "tomorrow_plan": ("证据",)},
    ),
)


CORRECTION_CASES = (
    (
        date(2026, 1, 22),
        "这是1月22日的日报：今日完成合同复核；明日整理附件。",
        "刚才那份日报日期写错了，实际是昨天的；问题暂无，请提交。",
        ("合同", "附件"),
    ),
    (
        date(2026, 1, 24),
        "记录1月24日日报：今天核对付款节点；明天跟进回款。",
        "更正一下：上一份应归到1月23日，不是1月24日；没有风险，直接提交。",
        ("付款", "回款"),
    ),
    (
        date(2026, 1, 26),
        "这是2026年1月26日的日报，今日完成用印检查，明日继续梳理台账。",
        "把刚记录的1月26日日报改成1月25日，问题没有，提交。",
        ("用印", "台账"),
    ),
)


CONFLICT_CASES = (
    (
        date(2026, 1, 28),
        "这是1月28日的日报：今日完成合同编号核对；明日整理目录。",
        "刚才那份其实是1月27日的，问题暂无，请提交。",
    ),
    (
        date(2026, 1, 30),
        "记录1月30日日报：今日复核付款清单；明日跟进发票。",
        "更正日报日期到1月29日，没有风险并提交。",
    ),
    (
        date(2026, 2, 1),
        "这是2月1日的日报：今天检查用印材料；明天补齐附件。",
        "把刚才的日报改成1月31日，问题没有，请提交。",
    ),
)


def _selected(case_name: str) -> bool:
    return not SELECTED_CASE_NAMES or case_name in SELECTED_CASE_NAMES


def _safe_message_summary(raw_message: object) -> dict[str, object]:
    if not isinstance(raw_message, dict):
        return {"kind": "invalid_message"}
    tool_calls = raw_message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        calls: list[dict[str, object]] = []
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            function = item.get("function")
            if not isinstance(function, dict):
                continue
            raw_arguments = str(function.get("arguments") or "")
            try:
                arguments: object = json.loads(raw_arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {"unparsed_preview": raw_arguments[:2000]}
            calls.append(
                {
                    "name": str(function.get("name") or ""),
                    "arguments": arguments,
                }
            )
        return {"kind": "tool_calls", "calls": calls}
    content = raw_message.get("content")
    return {
        "kind": "terminal_text" if isinstance(content, str) else "no_content",
        "content_preview": content[:2000] if isinstance(content, str) else None,
    }


def _safe_model_audit(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {"kind": "invalid_audit"}
    turns = payload.get("model_turns")
    return {
        "status": payload.get("status"),
        "error_type": payload.get("error_type"),
        "error_message": payload.get("error_message"),
        "failure_reason": payload.get("failure_reason"),
        "turns": [
            {
                "iteration": turn.get("iteration"),
                "message": _safe_message_summary(turn.get("raw_assistant_message")),
                "response_metadata": turn.get("response_metadata"),
                "tool_results": turn.get("tool_results"),
            }
            for turn in turns
            if isinstance(turn, dict)
        ]
        if isinstance(turns, list)
        else [],
    }


def _compact_daily_date_votes(
    model_audits: list[dict[str, object]],
) -> list[dict[str, object]]:
    votes: list[dict[str, object]] = []
    for audit in model_audits:
        turns = audit.get("turns")
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            metadata = turn.get("response_metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            message = turn.get("message")
            if not isinstance(message, dict) or message.get("kind") != "tool_calls":
                continue
            calls = message.get("calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                arguments = call.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                if call.get("name") == "add_daily_items":
                    votes.append(
                        {
                            "iteration": turn.get("iteration"),
                            "review_attempt": None,
                            "kind": "draft",
                            "date_selection": arguments.get("date_selection"),
                            "date_expression": arguments.get("date_expression"),
                            "proposed_date": arguments.get("proposed_date"),
                        }
                    )
                    continue
                if call.get("name") != "review_daily_report_dates":
                    continue
                decisions = arguments.get("decisions")
                if not isinstance(decisions, list):
                    continue
                for decision in decisions:
                    if not isinstance(decision, dict):
                        continue
                    votes.append(
                        {
                            "iteration": turn.get("iteration"),
                            "review_attempt": metadata.get(
                                "daily_write_date_semantic_review_attempt"
                            ),
                            "kind": "semantic_review",
                            "binding": decision.get("binding"),
                            "evidence": decision.get("evidence"),
                            "observed_time_expression": decision.get(
                                "observed_time_expression"
                            ),
                            "proposed_report_date": decision.get(
                                "proposed_report_date"
                            ),
                        }
                    )
    return votes


def _compact_model_flow(
    model_audits: list[dict[str, object]],
) -> list[dict[str, object]]:
    flow: list[dict[str, object]] = []
    selected_argument_keys = {
        "date_selection",
        "date_expression",
        "proposed_date",
        "source_date_expression",
        "proposed_source_date",
        "target_date_expression",
        "proposed_target_date",
        "acknowledged_empty_fields",
        "submit_after_write",
        "submit_after_correction",
        "decisions",
    }
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
            row: dict[str, object] = {
                "iteration": turn.get("iteration"),
                "kind": message.get("kind"),
            }
            if message.get("kind") == "tool_calls":
                calls = message.get("calls")
                row["calls"] = [
                    {
                        "name": call.get("name"),
                        "arguments": {
                            key: value
                            for key, value in arguments.items()
                            if key in selected_argument_keys
                        },
                    }
                    for call in calls
                    if isinstance(call, dict)
                    and isinstance((arguments := call.get("arguments")), dict)
                ] if isinstance(calls, list) else []
            else:
                row["content_preview"] = message.get("content_preview")
            flow.append(row)
    return flow


async def _user_and_control(session):
    user = await session.get(User, PANG_USER_ID)
    if user is None:
        raise AssertionError("rollback smoke user is missing")
    control = await session.scalar(
        select(ToolCallCanaryControl).where(
            ToolCallCanaryControl.user_id == PANG_USER_ID
        )
    )
    if control is None:
        raise AssertionError("rollback smoke Agent2 control is missing")
    settings = get_settings()
    control.enabled = True
    control.messages_enabled = False
    control.registry_digest = runtime_registry_contract_digest(settings)
    control.prompt_sha256 = canary_prompt_sha256()
    control.model_name = CANARY_MODEL_NAME
    await session.flush()
    return user, settings


async def _turn(
    session,
    *,
    user,
    settings,
    llm_client,
    text: str,
    conversation_id: str,
    source_message_id: str,
    now: datetime,
    accepted_business_results: frozenset[str] = frozenset({"success"}),
    model_audit_sink: list[dict[str, object]] | None = None,
):
    model_audits = model_audit_sink if model_audit_sink is not None else []
    original_audit_recorder = canary_service._record_model_audit_safely
    original_run_canary_turn = canary_service.DeepSeekToolCallingAdapter.run_canary_turn

    def capture_model_audit(payload):
        model_audits.append(_safe_model_audit(payload))
        original_audit_recorder(payload)

    async def capture_successful_model_turns(adapter, *args, **kwargs):
        result = await original_run_canary_turn(adapter, *args, **kwargs)
        model_audits.append(
            {
                "status": "success",
                "turns": [
                    {
                        "iteration": turn.iteration,
                        "message": _safe_message_summary(turn.raw_assistant_message),
                        "response_metadata": turn.response_metadata,
                        "tool_results": turn.tool_results,
                    }
                    for turn in result.model_turns
                ],
            }
        )
        return result

    canary_service._record_model_audit_safely = capture_model_audit
    canary_service.DeepSeekToolCallingAdapter.run_canary_turn = (
        capture_successful_model_turns
    )
    try:
        outcome = await process_tool_call_canary_ingress(
            session,
            user=user,
            dingtalk_user_id=user.dingtalk_user_id,
            user_text=text,
            source_channel="overnight_v22_rollback_smoke",
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            settings=settings,
            llm_client=llm_client,
            now=now,
        )
    except Exception as exc:
        raise AssertionError(
            {
                "source_message_id": source_message_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "model_audits": model_audits,
            }
        ) from exc
    finally:
        canary_service._record_model_audit_safely = original_audit_recorder
        canary_service.DeepSeekToolCallingAdapter.run_canary_turn = (
            original_run_canary_turn
        )
    observation = build_canary_persisted_response_payload(outcome)[OBSERVATION_KEY]
    assessment = assess_release_turn(
        webhook_status="processed",
        observation=observation,
        accepted_business_results=accepted_business_results,
    )
    if not assessment.releasable:
        raise AssertionError(
            {
                "source_message_id": source_message_id,
                "reason": outcome.reason,
                "business_result": outcome.user_visible_result,
                "release_blockers": assessment.blockers,
                "daily_date_votes": _compact_daily_date_votes(model_audits),
                "model_flow": _compact_model_flow(model_audits),
            }
        )
    if outcome.messages_enabled:
        raise AssertionError("rollback smoke unexpectedly enabled transport")
    return outcome


async def _receipts(session, source_message_id: str):
    return list(
        (
            await session.scalars(
                select(ToolCallCanaryReceipt)
                .where(ToolCallCanaryReceipt.source_message_id == source_message_id)
                .order_by(ToolCallCanaryReceipt.created_at)
            )
        ).all()
    )


def _assert_report(
    report: DailyReport | None,
    *,
    expected_date: date,
    required_fragments: dict[str, tuple[str, ...]],
    completed: bool,
) -> None:
    if report is None or report.report_date != expected_date:
        raise AssertionError("expected report date was not written")
    for field_name, fragments in required_fragments.items():
        written = "\n".join(str(item) for item in getattr(report, field_name))
        missing = [fragment for fragment in fragments if fragment not in written]
        if missing:
            raise AssertionError({"field": field_name, "missing": missing})
    if completed and report.status != "completed":
        raise AssertionError({"report_status": report.status})
    if completed and not bool(
        dict(report.section_status or {}).get("problems_acknowledged_empty")
    ):
        raise AssertionError("explicitly empty problems were not acknowledged")


def _history_event(
    *,
    user,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    now: datetime,
) -> WebhookEvent:
    source_id = f"{SOURCE_PREFIX}-history-{uuid4()}"
    return WebhookEvent(
        idempotency_key=source_id,
        external_message_id=source_id,
        dingtalk_user_id=user.dingtalk_user_id,
        payload={
            "conversationId": conversation_id,
            "text": {"content": user_text},
        },
        response_payload={
            "msgtype": "text",
            "text": {"content": assistant_text},
        },
        status="processed",
        received_at=now - timedelta(minutes=1),
        processed_at=now - timedelta(minutes=1),
    )


async def _run_write_case(
    *,
    llm_client,
    case_name: str,
    local_date: date,
    text: str,
    expected_date: date,
    required_fragments: dict[str, tuple[str, ...]],
) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            now = datetime.combine(
                local_date,
                datetime.min.time().replace(hour=8, minute=20),
                tzinfo=ZoneInfo(user.timezone or settings.timezone),
            )
            conversation_id = f"{SOURCE_PREFIX}-{case_name}"
            source_message_id = f"{conversation_id}-message"
            model_audits: list[dict[str, object]] = []
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
            await session.flush()
            report = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == expected_date,
                )
            )
            if report is None:
                actual_reports = list(
                    (
                        await session.scalars(
                            select(DailyReport).where(
                                DailyReport.user_id == user.id,
                                DailyReport.report_date.in_(
                                    {
                                        local_date,
                                        local_date - timedelta(days=1),
                                    }
                                ),
                            )
                        )
                    ).all()
                )
                raise AssertionError(
                    {
                        "expected_report_date": expected_date.isoformat(),
                        "actual_report_dates": sorted(
                            item.report_date.isoformat() for item in actual_reports
                        ),
                        "daily_date_votes": _compact_daily_date_votes(model_audits),
                    }
                )
            _assert_report(
                report,
                expected_date=expected_date,
                required_fragments=required_fragments,
                completed=True,
            )
            unexpected_date = (
                local_date
                if expected_date != local_date
                else local_date - timedelta(days=1)
            )
            unexpected = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == unexpected_date,
                )
            )
            if unexpected is not None:
                raise AssertionError(
                    {"unexpected_report_date": unexpected_date.isoformat()}
                )
            receipts = await _receipts(session, source_message_id)
            if [row.tool_name for row in receipts] != ["add_daily_items"]:
                raise AssertionError(
                    {"unexpected_tools": [row.tool_name for row in receipts]}
                )
            if receipts[0].status != "success" or not receipts[0].changed:
                raise AssertionError(
                    {
                        "receipt_status": receipts[0].status,
                        "changed": receipts[0].changed,
                    }
                )
            result = {
                "name": case_name,
                "status": "pass",
                "report_date": expected_date.isoformat(),
                "business_result": outcome.user_visible_result,
                "model_calls": outcome.model_call_count,
                "tools": [row.tool_name for row in receipts],
            }
            if INCLUDE_MODEL_AUDIT:
                result["daily_date_votes"] = _compact_daily_date_votes(model_audits)
            return result
        finally:
            await session.rollback()


async def _run_correction_case(
    *,
    llm_client,
    case_name: str,
    source_date: date,
    first_text: str,
    correction_text: str,
    fragments: tuple[str, ...],
    target_conflict: bool,
) -> dict[str, object]:
    target_date = source_date - timedelta(days=1)
    async with AsyncSessionLocal() as session:
        try:
            user, settings = await _user_and_control(session)
            now = datetime.combine(
                source_date,
                datetime.min.time().replace(hour=8, minute=20),
                tzinfo=ZoneInfo(user.timezone or settings.timezone),
            )
            conversation_id = f"{SOURCE_PREFIX}-{case_name}"
            first_source = f"{conversation_id}-first"
            first = await _turn(
                session,
                user=user,
                settings=settings,
                llm_client=llm_client,
                text=first_text,
                conversation_id=conversation_id,
                source_message_id=first_source,
                now=now,
            )
            await session.flush()
            source = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == source_date,
                )
            )
            _assert_report(
                source,
                expected_date=source_date,
                required_fragments={
                    "today_work": fragments[:1],
                    "tomorrow_plan": fragments[1:],
                },
                completed=False,
            )
            if source.status == "completed":
                raise AssertionError("pre-correction report was unexpectedly submitted")
            session.add(
                _history_event(
                    user=user,
                    conversation_id=conversation_id,
                    user_text=first_text,
                    assistant_text=first.message,
                    now=now,
                )
            )
            if target_conflict:
                session.add(
                    DailyReport(
                        user_id=user.id,
                        team_id=user.team_id,
                        report_date=target_date,
                        today_work=["目标日期已有日报"],
                        problems=[],
                        tomorrow_plan=["目标日期已有计划"],
                        section_status={"_agent2_report_version": 1},
                        completeness_score=Decimal("0.6667"),
                        status="collecting",
                        source="overnight_v22_rollback_smoke",
                    )
                )
            await session.flush()
            source_before = (
                list(source.today_work),
                list(source.problems),
                list(source.tomorrow_plan),
                source.status,
                dict(source.section_status or {}),
            )
            target_before = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == target_date,
                )
            )
            target_before_snapshot = (
                (
                    list(target_before.today_work),
                    list(target_before.problems),
                    list(target_before.tomorrow_plan),
                    target_before.status,
                    dict(target_before.section_status or {}),
                )
                if target_before is not None
                else None
            )
            correction_source = f"{conversation_id}-correction"
            correction_audits: list[dict[str, object]] = []
            correction = await _turn(
                session,
                user=user,
                settings=settings,
                llm_client=llm_client,
                text=correction_text,
                conversation_id=conversation_id,
                source_message_id=correction_source,
                now=now + timedelta(minutes=2),
                accepted_business_results=(
                    frozenset({"clarification"})
                    if target_conflict
                    else frozenset({"success"})
                ),
                model_audit_sink=correction_audits,
            )
            await session.flush()
            receipts = await _receipts(session, correction_source)
            if [row.tool_name for row in receipts] != ["correct_daily_report_date"]:
                raise AssertionError(
                    {
                        "unexpected_tools": [row.tool_name for row in receipts],
                        "model_flow": _compact_model_flow(correction_audits),
                    }
                )

            await session.refresh(source)
            live_target = await session.scalar(
                select(DailyReport).where(
                    DailyReport.user_id == user.id,
                    DailyReport.report_date == target_date,
                )
            )
            if target_conflict:
                if correction.actual_write:
                    raise AssertionError("occupied target unexpectedly changed")
                if receipts[0].status != "clarification_required":
                    raise AssertionError({"receipt_status": receipts[0].status})
                if source.report_date != source_date:
                    raise AssertionError("source moved despite target conflict")
                current_source = (
                    list(source.today_work),
                    list(source.problems),
                    list(source.tomorrow_plan),
                    source.status,
                    dict(source.section_status or {}),
                )
                current_target = (
                    list(live_target.today_work),
                    list(live_target.problems),
                    list(live_target.tomorrow_plan),
                    live_target.status,
                    dict(live_target.section_status or {}),
                )
                if (
                    current_source != source_before
                    or current_target != target_before_snapshot
                ):
                    raise AssertionError("target conflict changed a report")
            else:
                if receipts[0].status != "success" or not receipts[0].changed:
                    raise AssertionError({"receipt_status": receipts[0].status})
                if source.report_date != target_date or live_target.id != source.id:
                    raise AssertionError("report was not relocated to the target date")
                _assert_report(
                    source,
                    expected_date=target_date,
                    required_fragments={
                        "today_work": fragments[:1],
                        "tomorrow_plan": fragments[1:],
                    },
                    completed=True,
                )
                if source_before[0] != list(source.today_work) or source_before[
                    2
                ] != list(source.tomorrow_plan):
                    raise AssertionError("date correction changed report content")
            return {
                "name": case_name,
                "status": "pass",
                "source_date": source_date.isoformat(),
                "target_date": target_date.isoformat(),
                "business_result": correction.user_visible_result,
                "model_calls": first.model_call_count + correction.model_call_count,
                "tools": [row.tool_name for row in receipts],
            }
        finally:
            await session.rollback()


async def _verify_clean() -> dict[str, int]:
    all_dates = {
        *(value[0] for value in UNDATED_CASES),
        *(value[0] - timedelta(days=1) for value in UNDATED_CASES),
        *(value[0] for value in EXPLICIT_TODAY_CASES),
        *(value[0] - timedelta(days=1) for value in EXPLICIT_TODAY_CASES),
        *(value[0] for value in CORRECTION_CASES),
        *(value[0] - timedelta(days=1) for value in CORRECTION_CASES),
        *(value[0] for value in CONFLICT_CASES),
        *(value[0] - timedelta(days=1) for value in CONFLICT_CASES),
    }
    async with AsyncSessionLocal() as session:
        report_count = int(
            await session.scalar(
                select(func.count(DailyReport.id)).where(
                    DailyReport.user_id == PANG_USER_ID,
                    DailyReport.report_date.in_(all_dates),
                )
            )
            or 0
        )
        receipt_count = int(
            await session.scalar(
                select(func.count(ToolCallCanaryReceipt.receipt_id)).where(
                    ToolCallCanaryReceipt.source_message_id.like(f"{SOURCE_PREFIX}%")
                )
            )
            or 0
        )
        webhook_count = int(
            await session.scalar(
                select(func.count(WebhookEvent.id)).where(
                    WebhookEvent.external_message_id.like(f"{SOURCE_PREFIX}%")
                )
            )
            or 0
        )
        await session.rollback()
    return {
        "reports": report_count,
        "receipts": receipt_count,
        "webhooks": webhook_count,
    }


async def main() -> None:
    initial = await _verify_clean()
    if any(initial.values()):
        raise AssertionError({"preexisting_test_residue": initial})
    settings = get_settings()
    llm_client = LLMClient(settings)
    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    try:
        for index, (local_date, text, fragments) in enumerate(UNDATED_CASES, start=1):
            case_name = f"undated_before_nine_{index}"
            if not _selected(case_name):
                continue
            try:
                results.append(
                    await _run_write_case(
                        llm_client=llm_client,
                        case_name=case_name,
                        local_date=local_date,
                        text=text,
                        expected_date=local_date - timedelta(days=1),
                        required_fragments=fragments,
                    )
                )
            except Exception as exc:
                failures.append(
                    {"name": case_name, "error": f"{type(exc).__name__}: {exc}"}
                )
        for index, (local_date, text, fragments) in enumerate(
            EXPLICIT_TODAY_CASES, start=1
        ):
            case_name = f"explicit_today_{index}"
            if not _selected(case_name):
                continue
            try:
                results.append(
                    await _run_write_case(
                        llm_client=llm_client,
                        case_name=case_name,
                        local_date=local_date,
                        text=text,
                        expected_date=local_date,
                        required_fragments=fragments,
                    )
                )
            except Exception as exc:
                failures.append(
                    {"name": case_name, "error": f"{type(exc).__name__}: {exc}"}
                )
        for index, (source_date, first_text, correction_text, fragments) in enumerate(
            CORRECTION_CASES, start=1
        ):
            case_name = f"date_correction_{index}"
            if not _selected(case_name):
                continue
            try:
                results.append(
                    await _run_correction_case(
                        llm_client=llm_client,
                        case_name=case_name,
                        source_date=source_date,
                        first_text=first_text,
                        correction_text=correction_text,
                        fragments=fragments,
                        target_conflict=False,
                    )
                )
            except Exception as exc:
                failures.append(
                    {"name": case_name, "error": f"{type(exc).__name__}: {exc}"}
                )
        for index, (source_date, first_text, correction_text) in enumerate(
            CONFLICT_CASES, start=1
        ):
            case_name = f"occupied_target_{index}"
            if not _selected(case_name):
                continue
            try:
                results.append(
                    await _run_correction_case(
                        llm_client=llm_client,
                        case_name=case_name,
                        source_date=source_date,
                        first_text=first_text,
                        correction_text=correction_text,
                        fragments=("", ""),
                        target_conflict=True,
                    )
                )
            except Exception as exc:
                failures.append(
                    {"name": case_name, "error": f"{type(exc).__name__}: {exc}"}
                )
    finally:
        await llm_client.close()
    final = await _verify_clean()
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
    print(json.dumps(output, ensure_ascii=False, indent=2))
    await engine.dispose()
    if output["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
