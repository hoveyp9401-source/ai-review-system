from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app.agent2.tool_calling import canary_service
from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_prompt_sha256,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.production_store import ToolCallCanaryReceipt
from app.agent2.tool_calling.registry import runtime_registry_contract_digest
from app.config import get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.models import DailyReport, User, WebhookEvent

AUDIT_USER_ID = str(os.getenv("AUDIT_USER_ID", "")).strip()
SOURCE_DATE = date.fromisoformat(
    str(os.getenv("AUDIT_SOURCE_DATE", "2026-08-07"))
)
TARGET_DATE = date.fromisoformat(
    str(os.getenv("AUDIT_TARGET_DATE", "2026-08-08"))
)
RAW_ERROR_MARKERS = (
    "DATE_EXPRESSION_UNRESOLVED",
    "SOURCE_REPORT_DATE_MISMATCH",
    "ProductionRuntimeExecutionError",
    "REPORT_INCOMPLETE",
)


class _StaticHttpClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = iter(responses)

    async def post(self, url, **_kwargs):
        body = next(self._responses)
        return httpx.Response(
            200,
            json=body,
            request=httpx.Request("POST", url),
        )


class _StaticLlmClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.native_http_client = _StaticHttpClient(responses)


def forced_atomic_failure_llm() -> _StaticLlmClient:
    return _StaticLlmClient(
        [
            {
                "id": "forced-atomic-failure",
                "model": CANARY_MODEL_NAME,
                "created": 1,
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "forced-copy",
                                    "type": "function",
                                    "function": {
                                        "name": "copy_previous_to_today",
                                        "arguments": json.dumps(
                                            {
                                                "source_date_expression": "昨天",
                                                "proposed_source_date": (
                                                    SOURCE_DATE.isoformat()
                                                ),
                                            },
                                            ensure_ascii=False,
                                        ),
                                    },
                                },
                                {
                                    "id": "forced-invalid-followup",
                                    "type": "function",
                                    "function": {
                                        "name": "add_daily_items",
                                        "arguments": json.dumps(
                                            {
                                                "date_expression": "今天",
                                                "proposed_date": (
                                                    TARGET_DATE.isoformat()
                                                ),
                                                "items": [],
                                                "acknowledged_empty_fields": [
                                                    "today_work"
                                                ],
                                                "empty_field_evidence": [
                                                    {
                                                        "field": "today_work",
                                                        "source_evidence": {
                                                            "source_message_index": 1,
                                                        },
                                                    }
                                                ],
                                            },
                                            ensure_ascii=False,
                                        ),
                                    },
                                },
                            ],
                        },
                    }
                ],
                "usage": {},
            }
        ]
    )


def report_snapshot(report: DailyReport) -> dict[str, object]:
    return {
        "today_work": list(report.today_work or []),
        "problems": list(report.problems or []),
        "tomorrow_plan": list(report.tomorrow_plan or []),
        "status": str(report.status or ""),
        "confirmation_type": str(report.confirmation_type or ""),
        "confirmed_by_user": bool(report.confirmed_by_user),
        "submitted_at": (
            report.submitted_at.isoformat() if report.submitted_at else None
        ),
        "section_status": dict(report.section_status or {}),
        "version": int(getattr(report, "version", 0) or 0),
    }


def clear_report(report: DailyReport) -> None:
    report.today_work = []
    report.problems = []
    report.tomorrow_plan = []
    report.raw_input = ""
    report.input_fragments = []
    report.section_status = {}
    report.status = "collecting"
    report.confirmation_type = "none"
    report.confirmed_by_user = False
    report.submitted_at = None
    report.pending_confirmation_at = None
    report.auto_submit_at = None


def prepare_confirmation(report: DailyReport) -> None:
    if not report.today_work:
        report.today_work = ["完成历史日报确认回滚演练"]
    if not report.tomorrow_plan:
        report.tomorrow_plan = ["继续跟进历史日报确认回滚演练"]
    report.problems = []
    report.section_status = {
        **dict(report.section_status or {}),
        "problems_acknowledged_empty": True,
    }
    report.status = "pending_confirmation"
    report.confirmation_type = "none"
    report.confirmed_by_user = False
    report.submitted_at = None


def reopen_for_audit(report: DailyReport) -> None:
    report.status = "collecting"
    report.confirmation_type = "none"
    report.confirmed_by_user = False
    report.submitted_at = None
    report.pending_confirmation_at = None
    report.auto_submit_at = None


def history_event(
    *,
    dingtalk_user_id: str,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    now: datetime,
) -> WebhookEvent:
    source_id = f"full-audit-history-{uuid4()}"
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


async def load_receipts(session, source_message_id: str):
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


def safe_terminal_shape(raw_message: object) -> dict[str, object]:
    if not isinstance(raw_message, dict):
        return {"kind": "not_a_message"}
    content = raw_message.get("content")
    tool_calls = raw_message.get("tool_calls")
    if tool_calls:
        names = []
        raw_argument_previews = []
        for item in tool_calls if isinstance(tool_calls, list) else []:
            if not isinstance(item, dict):
                continue
            function = item.get("function")
            if isinstance(function, dict):
                names.append(str(function.get("name") or ""))
                raw_argument_previews.append(
                    str(function.get("arguments") or "")[:2000]
                )
        return {
            "kind": "tool_calls",
            "tool_names": names,
            "raw_argument_previews": raw_argument_previews,
        }
    if not isinstance(content, str):
        return {"kind": "no_text"}
    try:
        decoded = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {"kind": "non_json_text", "length": len(content)}
    if not isinstance(decoded, dict):
        return {"kind": "json_non_object"}
    reply = decoded.get("reply")
    return {
        "kind": "json_object",
        "keys": sorted(str(key) for key in decoded),
        "actual_write": decoded.get("actual_write"),
        "operation_outcome": decoded.get("operation_outcome"),
        "reply_length": len(reply) if isinstance(reply, str) else None,
    }


def safe_model_failure(payload: dict[str, object]) -> dict[str, object]:
    turns = payload.get("model_turns")
    return {
        "error_type": payload.get("error_type"),
        "error_message": payload.get("error_message"),
        "turns": [
            {
                "iteration": turn.get("iteration"),
                "write_reply_validation": (
                    turn.get("response_metadata", {}).get(
                        "write_reply_validation"
                    )
                    if isinstance(turn.get("response_metadata"), dict)
                    else None
                ),
                "write_reply_retry": (
                    turn.get("response_metadata", {}).get("write_reply_retry")
                    if isinstance(turn.get("response_metadata"), dict)
                    else None
                ),
                "daily_briefing_reply_validation": (
                    turn.get("response_metadata", {}).get(
                        "daily_briefing_reply_validation"
                    )
                    if isinstance(turn.get("response_metadata"), dict)
                    else None
                ),
                "daily_briefing_reply_retry": (
                    turn.get("response_metadata", {}).get(
                        "daily_briefing_reply_retry"
                    )
                    if isinstance(turn.get("response_metadata"), dict)
                    else None
                ),
                "daily_briefing_reply_review": (
                    turn.get("response_metadata", {}).get(
                        "daily_briefing_reply_review"
                    )
                    if isinstance(turn.get("response_metadata"), dict)
                    else None
                ),
                "terminal_reply_validation": (
                    turn.get("response_metadata", {}).get(
                        "terminal_reply_validation"
                    )
                    if isinstance(turn.get("response_metadata"), dict)
                    else None
                ),
                "response_shape": safe_terminal_shape(
                    turn.get("raw_assistant_message")
                ),
            }
            for turn in turns
            if isinstance(turn, dict)
        ]
        if isinstance(turns, list)
        else [],
    }


def receipt_summary(receipts) -> list[dict[str, object]]:
    return [
        {
            "tool": row.tool_name,
            "status": row.status,
            "changed": bool(row.changed),
            "error_code": str(row.error_code or ""),
        }
        for row in receipts
    ]


async def run_case(
    *,
    name: str,
    text: str,
    llm_client: LLMClient,
    now: datetime,
    mode: str,
    history: tuple[str, str] | None = None,
    required_written_fragments: tuple[str, ...] = (),
    required_empty_fields: tuple[str, ...] = (),
) -> dict[str, object]:
    settings = get_settings()
    case_now = (
        datetime.combine(
            SOURCE_DATE + timedelta(days=1),
            time(hour=8, minute=30),
            tzinfo=now.tzinfo,
        )
        if mode == "confirmation"
        else now
    )
    async with AsyncSessionLocal() as session:
        user = await session.scalar(select(User).where(User.id == AUDIT_USER_ID))
        if user is None:
            raise AssertionError("audit user missing")
        control = await session.scalar(
            select(ToolCallCanaryControl).where(
                ToolCallCanaryControl.user_id == str(user.id)
            )
        )
        if control is None:
            raise AssertionError("audit user Agent2 control missing")
        control.registry_digest = runtime_registry_contract_digest(settings)
        control.prompt_sha256 = canary_prompt_sha256()
        control.model_name = CANARY_MODEL_NAME
        source_report = await session.scalar(
            select(DailyReport).where(
                DailyReport.user_id == user.id,
                DailyReport.report_date == SOURCE_DATE,
            )
        )
        target_report = await session.scalar(
            select(DailyReport).where(
                DailyReport.user_id == user.id,
                DailyReport.report_date == TARGET_DATE,
            )
        )
        if source_report is None or target_report is None:
            raise AssertionError("daily report fixtures missing")
        source_production_before = report_snapshot(source_report)
        target_production_before = report_snapshot(target_report)
        source_snapshot = report_snapshot(source_report)
        if mode == "confirmation":
            prepare_confirmation(source_report)
            tested_report = source_report
        elif mode in {
            "copy",
            "copy_add",
            "copy_missing",
            "copy_ambiguous",
            "forced_atomic_failure",
            "conditional",
            "quote",
            "source_fidelity",
        }:
            clear_report(target_report)
            tested_report = target_report
        elif mode == "copy_existing":
            reopen_for_audit(target_report)
            tested_report = target_report
        elif mode == "copy_noop":
            clear_report(target_report)
            target_report.today_work = list(source_snapshot["today_work"])
            target_report.problems = list(source_snapshot["problems"])
            target_report.tomorrow_plan = list(source_snapshot["tomorrow_plan"])
            tested_report = target_report
        else:
            tested_report = target_report
        prepared_before = report_snapshot(tested_report)
        conversation_id = f"full-audit-{name}-{uuid4()}"
        if history is not None:
            session.add(
                history_event(
                    dingtalk_user_id=user.dingtalk_user_id,
                    conversation_id=conversation_id,
                    user_text=history[0],
                    assistant_text=history[1],
                    now=case_now,
                )
            )
        await session.flush()
        source_message_id = f"full-audit-{name}-{uuid4()}"
        model_failures: list[dict[str, object]] = []
        model_audits: list[dict[str, object]] = []
        original_audit_recorder = canary_service._record_model_audit_safely

        def capture_model_audit(payload):
            if isinstance(payload, dict):
                safe_audit = {
                    "status": payload.get("status"),
                    **safe_model_failure(payload),
                }
                model_audits.append(safe_audit)
                if payload.get("status") == "failed":
                    model_failures.append(safe_audit)
            original_audit_recorder(payload)

        canary_service._record_model_audit_safely = capture_model_audit
        try:
            outcome = await canary_service.process_tool_call_canary_ingress(
                session,
                user=user,
                dingtalk_user_id=user.dingtalk_user_id,
                user_text=text,
                source_channel="full_audit_rollback",
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                settings=settings,
                llm_client=(
                    forced_atomic_failure_llm()
                    if mode == "forced_atomic_failure"
                    else llm_client
                ),
                now=case_now,
            )
        finally:
            canary_service._record_model_audit_safely = original_audit_recorder
        await session.flush()
        await session.refresh(tested_report)
        receipts = await load_receipts(session, source_message_id)
        after = report_snapshot(tested_report)
        reply = str(outcome.message or "")
        result: dict[str, object] = {
            "name": name,
            "mode": mode,
            "input": text,
            "owner": outcome.owner,
            "reason": outcome.reason,
            "handled": bool(outcome.handled),
            "actual_write": bool(outcome.actual_write),
            "reply": reply,
            "receipts": receipt_summary(receipts),
            "model_call_count": int(outcome.model_call_count),
            "model_request_attempt_count": int(
                outcome.model_request_attempt_count
            ),
            "model_transport_retry_count": int(
                outcome.model_transport_retry_count
            ),
            "model_elapsed_seconds": float(outcome.model_elapsed_seconds),
            "model_result_status": outcome.model_result_status,
            "model_audits": model_audits,
            "tool_status_counts": {
                "success": int(outcome.tool_success_count),
                "no_op": int(outcome.tool_no_op_count),
                "clarification_required": int(
                    outcome.tool_clarification_count
                ),
                "blocked": int(outcome.tool_blocked_count),
                "failed": int(outcome.tool_failure_count),
            },
            "model_failures": model_failures,
            "prepared_changed": after != prepared_before,
            "raw_error_exposed": any(
                marker in reply for marker in RAW_ERROR_MARKERS
            ),
        }
        if mode == "confirmation":
            result["expected"] = "confirm_report writes and completes source date"
            result["passed"] = bool(
                outcome.actual_write
                and any(
                    row.tool_name == "confirm_report"
                    and row.status == "success"
                    and row.changed
                    for row in receipts
                )
                and after["status"] == "completed"
                and after["confirmed_by_user"] is True
                and not result["raw_error_exposed"]
            )
        elif mode == "copy":
            result["expected"] = "copy yesterday into today without raw error"
            same_content = all(
                after[key] == source_snapshot[key]
                for key in ("today_work", "problems", "tomorrow_plan")
            )
            result["copied_content_matches"] = same_content
            result["passed"] = bool(
                outcome.actual_write
                and same_content
                and not result["raw_error_exposed"]
            )
        elif mode == "copy_add":
            source_matches = all(
                all(value in after[key] for value in source_snapshot[key])
                for key in ("today_work", "problems", "tomorrow_plan")
            )
            extra_preserved = "整理测试材料" in after["today_work"]
            result["expected"] = "copy and add commit together"
            result["source_content_preserved"] = source_matches
            result["extra_item_preserved"] = extra_preserved
            result["passed"] = bool(
                outcome.actual_write
                and source_matches
                and extra_preserved
                and not result["raw_error_exposed"]
            )
        elif mode == "copy_existing":
            source_preserved = all(
                all(value in after[key] for value in source_snapshot[key])
                for key in ("today_work", "problems", "tomorrow_plan")
            )
            existing_preserved = all(
                all(value in after[key] for value in prepared_before[key])
                for key in ("today_work", "problems", "tomorrow_plan")
            )
            result["expected"] = "merge source without deleting existing items"
            result["source_content_preserved"] = source_preserved
            result["existing_content_preserved"] = existing_preserved
            result["passed"] = bool(
                source_preserved
                and existing_preserved
                and not result["raw_error_exposed"]
            )
        elif mode == "copy_noop":
            result["expected"] = "repeat copy is a truthful no-op"
            result["passed"] = bool(
                not outcome.actual_write
                and after == prepared_before
                and outcome.tool_no_op_count == 1
                and not result["raw_error_exposed"]
            )
        elif mode in {"copy_missing", "copy_ambiguous"}:
            result["expected"] = "do not write when the source is not uniquely bound"
            result["passed"] = bool(
                not outcome.actual_write
                and after == prepared_before
                and outcome.owner == "tool_call_core"
                and not result["raw_error_exposed"]
            )
        elif mode == "forced_atomic_failure":
            result["expected"] = "a later dependent failure rolls back the copy"
            result["passed"] = bool(
                not outcome.actual_write
                and outcome.owner == "blocked"
                and after == prepared_before
                and not result["raw_error_exposed"]
            )
        elif mode == "conditional":
            result["expected"] = "do not execute a future conditional immediately"
            result["passed"] = bool(
                not outcome.actual_write
                and after == prepared_before
                and not result["raw_error_exposed"]
            )
        elif mode in {"quote", "source_fidelity"}:
            result["expected"] = (
                "preserve every required source detail without asking the user to repeat"
            )
            written_text = "\n".join(
                str(item)
                for field in ("today_work", "problems", "tomorrow_plan")
                for item in after[field]
            )
            missing_fragments = [
                fragment
                for fragment in required_written_fragments
                if fragment not in written_text
            ]
            missing_empty_fields = [
                field
                for field in required_empty_fields
                if not bool(
                    after["section_status"].get(
                        f"{field}_acknowledged_empty"
                    )
                )
            ]
            result["written_today_work"] = list(after["today_work"])
            result["written_report_fields"] = {
                field: list(after[field])
                for field in ("today_work", "problems", "tomorrow_plan")
            }
            result["required_written_fragments"] = list(
                required_written_fragments
            )
            result["missing_written_fragments"] = missing_fragments
            result["required_empty_fields"] = list(required_empty_fields)
            result["missing_empty_fields"] = missing_empty_fields
            result["passed"] = bool(
                outcome.actual_write
                and after != prepared_before
                and not missing_fragments
                and not missing_empty_fields
                and not result["raw_error_exposed"]
                and "再发一次" not in reply
            )
        elif mode == "incident":
            briefing_receipts = [
                row
                for row in receipts
                if row.tool_name == "query_daily_briefing_facts"
            ]
            briefing_fact_payloads = [
                dict(row.safe_user_facts or {}).get(
                    "daily_briefing_facts"
                )
                for row in briefing_receipts
            ]
            evidence_limits = [
                str(limit)
                for payload in briefing_fact_payloads
                if isinstance(payload, dict)
                for limit in payload.get("evidence_limits", [])
            ]
            recorded_events = [
                event
                for payload in briefing_fact_payloads
                if isinstance(payload, dict)
                for event in payload.get("recorded_briefings", [])
                if isinstance(event, dict)
            ]
            recorded_message_texts = [
                str(event.get("message_text") or "")
                for event in recorded_events
                if str(event.get("message_text") or "")
            ]
            quoted_fragments = re.findall(r"“([^”]+)”", reply)
            recorded_quote_grounded = any(
                quote in recorded_text
                for quote in quoted_fragments
                for recorded_text in recorded_message_texts
            )
            premise_corrected = (
                "系统保存的这份晨报原文与问题中的情况不一致。"
                in reply
            )
            recorded_times_localized = bool(recorded_events) and all(
                _same_utc_offset(
                    str(event.get("sent_at") or ""),
                    now,
                )
                for event in recorded_events
            )
            reply_uses_local_time = "UTC" not in reply and "01:00" not in reply
            submission_overclaim = bool(
                re.search(
                    r"(?:庞浩|你|您).{0,16}"
                    r"(?:被|已)?(?:列入|列为|归入).{0,6}(?:已交|已提交)|"
                    r"(?:被|已)(?:列入|列为|归入).{0,6}(?:已交|已提交)名单",
                    reply,
                )
            )
            speculative = bool(
                re.search(
                    r"快照.*(之后|延迟|时间差|尚未提交)|提交.*晚于.*晨报|统计.*延迟|统计.*早于.*提交|统计已经跑完",
                    reply,
                )
            )
            speculative = speculative or bool(
                re.search(
                    r"(可能|也许|或许|推测|猜测|会不会|是不是|或者|或是)"
                    r".{0,80}"
                    r"(原因|导致|因为|由于|同一份|版本|转发|误解|偏差|"
                    r"顺序|先后|延迟|故障|抓取)",
                    reply,
                )
            )
            speculative = speculative or bool(
                re.search(
                    r"(说明|表明).{0,80}(晨报生成时|生成时|当时)"
                    r".{0,80}(已提交|未提交|提交状态)",
                    reply,
                )
            )
            incomplete_reply = (
                outcome.owner != "tool_call_core"
                or "没有处理完整" in reply
                or "请再发一次" in reply
            )
            limitation_explained = (
                not evidence_limits
                or all(limit in reply for limit in evidence_limits)
            )
            result["expected"] = "confirm facts but do not invent an incident cause"
            result["speculative_cause"] = speculative
            result["incomplete_reply"] = incomplete_reply
            result["briefing_fact_receipt_count"] = len(
                briefing_receipts
            )
            result["total_receipt_count"] = len(receipts)
            result["evidence_limit_count"] = len(evidence_limits)
            result["limitation_explained"] = limitation_explained
            result["premise_corrected_from_recorded_copy"] = premise_corrected
            result["recorded_quote_grounded"] = recorded_quote_grounded
            result["recorded_times_localized"] = recorded_times_localized
            result["reply_uses_local_time"] = reply_uses_local_time
            result["submission_overclaim"] = submission_overclaim
            result["passed"] = bool(
                not outcome.actual_write
                and len(briefing_receipts) == 1
                and len(receipts) == 1
                and briefing_receipts[0].status == "success"
                and not briefing_receipts[0].changed
                and not speculative
                and not incomplete_reply
                and limitation_explained
                and premise_corrected
                and recorded_quote_grounded
                and recorded_times_localized
                and reply_uses_local_time
                and not submission_overclaim
                and not result["raw_error_exposed"]
            )
        await session.rollback()

    async with AsyncSessionLocal() as verification:
        restored_source = await verification.scalar(
            select(DailyReport).where(
                DailyReport.user_id == AUDIT_USER_ID,
                DailyReport.report_date == SOURCE_DATE,
            )
        )
        restored_target = await verification.scalar(
            select(DailyReport).where(
                DailyReport.user_id == AUDIT_USER_ID,
                DailyReport.report_date == TARGET_DATE,
            )
        )
        if restored_source is None or restored_target is None:
            raise AssertionError("report disappeared after rollback")
        result["rollback_verified"] = (
            report_snapshot(restored_source) == source_production_before
            and report_snapshot(restored_target) == target_production_before
        )
    return result


def _same_utc_offset(value: str, expected: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.utcoffset() == expected.utcoffset()


async def main() -> None:
    if not AUDIT_USER_ID:
        raise RuntimeError("AUDIT_USER_ID is required")
    settings = get_settings()
    llm_client = LLMClient(settings)
    tz = ZoneInfo(settings.timezone)
    now = datetime.combine(
        TARGET_DATE,
        time(hour=16),
        tzinfo=tz,
    )
    cases = [
        {
            "name": "confirm_yesterday_short",
            "text": "昨天的",
            "mode": "confirmation",
            "history": ("可以，提交", "你要提交哪一天的日报？"),
        },
        {
            "name": "confirm_yesterday_explicit",
            "text": "就提交昨天那份",
            "mode": "confirmation",
            "history": ("确认提交", "请告诉我要提交哪一天。"),
        },
        {
            "name": "confirm_date_explicit",
            "text": f"提交{SOURCE_DATE.month}月{SOURCE_DATE.day}日的日报",
            "mode": "confirmation",
            "history": ("可以提交吗", "你指哪一天的日报？"),
        },
        {
            "name": "copy_yesterday_1",
            "text": "把昨天日报复制到今天",
            "mode": "copy",
        },
        {
            "name": "copy_yesterday_2",
            "text": "今天沿用昨天的日报",
            "mode": "copy",
        },
        {
            "name": "copy_yesterday_3",
            "text": "昨天那份整篇复用到今天",
            "mode": "copy",
        },
        {
            "name": "copy_and_add",
            "text": "把昨天日报复制到今天，再补一项今日工作：整理测试材料。",
            "mode": "copy_add",
        },
        {
            "name": "copy_with_existing_target",
            "text": "把昨天日报复制到今天，保留今天已经写的内容。",
            "mode": "copy_existing",
        },
        {
            "name": "copy_repeat_noop",
            "text": "再把昨天日报复制到今天一次。",
            "mode": "copy_noop",
        },
        {
            "name": "copy_missing_source",
            "text": "把8月2日的日报复制到今天。",
            "mode": "copy_missing",
        },
        {
            "name": "copy_ambiguous_source",
            "text": "把前几天那份日报复制到今天。",
            "mode": "copy_ambiguous",
        },
        {
            "name": "forced_atomic_followup_failure",
            "text": "强制事务回滚审计",
            "mode": "forced_atomic_failure",
        },
        {
            "name": "conditional_copy_1",
            "text": "如果我今晚12点前没有发新内容，就用昨天的日报。",
            "mode": "conditional",
        },
        {
            "name": "conditional_copy_2",
            "text": "今晚没更新的话，零点时自动把昨天日报复制到今天。",
            "mode": "conditional",
        },
        {
            "name": "conditional_copy_3",
            "text": "先别复制；如果今天结束前我没有补充，再沿用昨天日报。",
            "mode": "conditional",
        },
        {
            "name": "quoted_content_short_curly",
            "text": "今天记录经营会，老板原话是“要么降薪，要么裁员”；风险暂无；明天继续跟进降本方案。",
            "mode": "quote",
            "required_written_fragments": ("降薪", "裁员"),
            "required_empty_fields": ("problems",),
        },
        {
            "name": "quoted_content_short_ascii",
            "text": "今天记录经营会，老板原话是\"要么降薪，要么裁员\"；风险暂无；明天继续跟进降本方案。",
            "mode": "quote",
            "required_written_fragments": ("降薪", "裁员"),
            "required_empty_fields": ("problems",),
        },
        {
            "name": "quoted_content_long",
            "text": (
                "下午参加公司的经营管理会议。老板主要讲了几个方面。"
                "第一，关于人员，各部门负责人不要忽视人才培养，在八月底前要么招聘，"
                "要么内部一定要有确定的人员储备。第二，关于成本，行政部成本从原来9.1"
                "上升到14点几，所以老板的原话是要么降薪，要么裁员。也提到了施工成本"
                "和海外项目。今天内容的核心一是人才，二是降成本。风险暂无，明天继续跟进。"
            ),
            "mode": "quote",
            "required_written_fragments": (
                "八月底",
                "招聘",
                "人员储备",
                "9.1",
                "14",
                "降薪",
                "裁员",
                "施工成本",
                "海外项目",
                "人才",
                "降成本",
            ),
            "required_empty_fields": ("problems",),
        },
        {
            "name": "long_voice_unquoted_details",
            "text": (
                "今天下午参加经营管理会议，第一项是人才培养，各部门负责人要在八月底前"
                "完成招聘或明确内部人员储备；第二项是成本，行政成本从9.1升到14点几，"
                "还讨论了施工成本和海外项目。风险是降本措施还没明确，明天继续梳理。"
            ),
            "mode": "source_fidelity",
            "required_written_fragments": (
                "八月底",
                "招聘",
                "人员储备",
                "9.1",
                "14",
                "施工成本",
                "海外项目",
                "降本措施",
                "继续梳理",
            ),
        },
        {
            "name": "numbered_daily_items",
            "text": (
                "今天工作：1.完成合同模板复核；2.和法务三部沟通印章权限；"
                "3.整理海外项目台账。问题：费用口径尚未统一。明天计划：跟财务确认口径。"
            ),
            "mode": "source_fidelity",
            "required_written_fragments": (
                "合同模板",
                "法务三部",
                "印章权限",
                "海外项目",
                "费用口径",
                "财务",
            ),
        },
        {
            "name": "explicit_verbatim_daily_item",
            "text": (
                "请原样记录今天工作：各部门负责人须在8月31日前完成招聘，"
                "否则必须明确内部人员储备，不要概括。风险暂无，明天继续跟进。"
            ),
            "mode": "source_fidelity",
            "required_written_fragments": (
                "8月31日",
                "招聘",
                "内部人员储备",
                "继续跟进",
            ),
            "required_empty_fields": ("problems",),
        },
        {
            "name": "explicit_summary_permission",
            "text": (
                "今天参加经营会，会上讨论了人才储备、施工成本和海外项目，"
                "细节不用逐字保留，请概括成一项工作。风险暂无，明天跟进会议事项。"
            ),
            "mode": "source_fidelity",
            "required_written_fragments": (
                "经营会",
                "人才储备",
                "施工成本",
                "海外项目",
                "跟进",
                "会议",
            ),
            "required_empty_fields": ("problems",),
        },
        {
            "name": "incident_cause_1",
            "text": "我昨天明明交了日报，为什么晨报说我没交？",
            "mode": "incident",
        },
        {
            "name": "incident_cause_2",
            "text": "昨天日报已经提交，早上的统计却把我列为未交，是什么原因？",
            "mode": "incident",
        },
        {
            "name": "incident_cause_3",
            "text": "帮我查清楚为什么晨报漏掉了我昨天的日报，不要猜。",
            "mode": "incident",
        },
    ]
    selected_modes = {
        item.strip()
        for item in str(os.getenv("AUDIT_MODES", "") or "").split(",")
        if item.strip()
    }
    if selected_modes:
        cases = [case for case in cases if case["mode"] in selected_modes]
    selected_names = {
        item.strip()
        for item in str(os.getenv("AUDIT_CASE_NAMES", "") or "").split(",")
        if item.strip()
    }
    if selected_names:
        cases = [case for case in cases if case["name"] in selected_names]
    results = []
    try:
        for case in cases:
            try:
                result = await run_case(
                    llm_client=llm_client,
                    now=now,
                    **case,
                )
            except Exception as exc:
                result = {
                    "name": case["name"],
                    "mode": case["mode"],
                    "input": case["text"],
                    "passed": False,
                    "exception": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)
            print(
                json.dumps(
                    {
                        "progress": result.get("name"),
                        "passed": result.get("passed"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        await llm_client.close()
        await engine.dispose()
    output = {
        "status": "pass" if all(item.get("passed") for item in results) else "issues_found",
        "case_count": len(results),
        "passed_count": sum(bool(item.get("passed")) for item in results),
        "failed_count": sum(not bool(item.get("passed")) for item in results),
        "all_rollbacks_verified": all(
            item.get("rollback_verified") is True
            for item in results
            if "exception" not in item
        ),
        "dingtalk_send_calls": 0,
        "results": results,
    }
    print("FULL_AUDIT_DIALOGUE_RESULTS_START")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print("FULL_AUDIT_DIALOGUE_RESULTS_END")


if __name__ == "__main__":
    asyncio.run(main())
