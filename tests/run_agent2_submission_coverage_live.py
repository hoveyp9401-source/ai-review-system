"""Two-case real-DeepSeek gate for weekly submission coverage.

The model and adapter are real. Business facts come from an in-memory read
runtime: no database is opened, no production handler runs, and no message is
sent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_system_prompt,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
    TrustedRuntimeIdentity,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekToolCallingAdapter,
    DeepSeekToolCallingError,
)
from app.agent2.tool_calling.production_contracts import (
    ProductionRuntimeResult,
)
from app.agent2.tool_calling.runtime import NativeToolCall

_NOW = datetime(2026, 8, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
_USER_ID = UUID("55555555-5555-5555-5555-555555555555")
_TOOLS = frozenset({"query_report_insights"})
Period = Literal["current_week", "previous_week"]


class _ReadOnlyCoverageRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self, period_type: Period) -> None:
        self.period_type = period_type
        self.calls: list[NativeToolCall] = []
        self.business_write_count = 0
        self.handler_call_count = 0
        self.message_send_count = 0

    async def execute(
        self,
        calls: tuple[NativeToolCall, ...],
        *,
        defer_finalization: bool = False,
    ) -> ProductionRuntimeResult:
        if not calls or defer_finalization:
            raise AssertionError("coverage evaluation accepts read calls only")
        self.calls.extend(calls)
        return ProductionRuntimeResult(
            status="success",
            receipts=tuple(self._receipt(call) for call in calls),
            transaction_opened=False,
            transaction_pending=False,
            committed_to_outer_transaction=False,
            handler_call_count=0,
            business_write_count=0,
            pending_write_count=0,
            memory_write_count=0,
            memory_audit_write_count=0,
            receipt_write_count=0,
        )

    def _receipt(self, call: NativeToolCall) -> ToolReceipt:
        if call.tool_name != "query_report_insights":
            raise AssertionError("coverage evaluation received another tool")
        facts = _coverage_facts(self.period_type)
        return ToolReceipt(
            status=ReceiptStatus.SUCCESS,
            tool_name=call.tool_name,
            changed=False,
            target_type="daily_report_insight",
            target_id=f"coverage-evaluation:{self.period_type}",
            safe_user_facts={
                "actual_write": False,
                "model_composition_allowed": True,
                "evaluation_only": True,
                "production_handler_called": False,
                "report_insight": {
                    "answer_text": "",
                    "title": f"法务五部{self.period_type}提交情况",
                    "facts": facts,
                    "freshness": "2026-08-16",
                },
                "回复要求": (
                    "直接按结构化事实回答。必须说明起止日期、责任数据是否完整；"
                    "计数单位是人次。expected_count、report_count、completed_count"
                    "和 pending_confirmation_count 必须分别说明；责任待核不能说成"
                    "未交，截止前不能说成逾期，待确认不能说成已提交；本周结果"
                    "还必须说明 queried_at 查询时点。"
                ),
            },
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )


def _coverage_facts(period_type: Period) -> dict[str, Any]:
    if period_type == "current_week":
        start_date, end_date = "2026-08-10", "2026-08-16"
        completed_count, pending_count, not_filled_count = 0, 0, 10
        reporter_count, report_count = 0, 0
    else:
        start_date, end_date = "2026-08-03", "2026-08-09"
        completed_count, pending_count, not_filled_count = 7, 1, 2
        reporter_count, report_count = 2, 8
    period_start = date.fromisoformat(start_date)
    calendar_dates = tuple(
        period_start + timedelta(days=offset) for offset in range(7)
    )
    reporting_dates = tuple(
        value for value in calendar_dates if value.weekday() < 5
    )

    daily_breakdown: list[dict[str, Any]] = []
    for index, report_date in enumerate(calendar_dates):
        completed_members: list[dict[str, str]] = []
        pending_members: list[dict[str, str]] = []
        not_filled_members: list[dict[str, str]] = []
        if report_date in reporting_dates:
            if period_type == "current_week":
                not_filled_members = [
                    {"member_name": name, "submission_state": "overdue"}
                    for name in ("测试甲", "测试乙")
                ]
            else:
                completed_members.append(
                    {"member_name": "测试甲", "submission_state": "submitted"}
                )
                if index < 2:
                    completed_members.append(
                        {"member_name": "测试乙", "submission_state": "submitted"}
                    )
                elif index == 2:
                    pending_members.append(
                        {
                            "member_name": "测试乙",
                            "submission_state": "pending_confirmation",
                        }
                    )
                else:
                    not_filled_members.append(
                        {"member_name": "测试乙", "submission_state": "overdue"}
                    )
        daily_breakdown.append(
            {
                "report_date": report_date.isoformat(),
                "responsibility_data_complete": True,
                "completed_members": completed_members,
                "pending_confirmation_members": pending_members,
                "partial_members": [],
                "not_filled_members": not_filled_members,
                "exempt_members": [],
                "responsibility_unknown_members": [],
            }
        )

    if period_type == "current_week":
        member_breakdown = [
            {
                "member_name": name,
                "team_name": "法务五部",
                "completed_dates": [],
                "pending_confirmation_dates": [],
                "partial_dates": [],
                "not_filled_dates": [
                    {
                        "report_date": value.isoformat(),
                        "submission_state": "overdue",
                    }
                    for value in reporting_dates
                ],
                "exempt_dates": [],
                "responsibility_unknown_dates": [],
            }
            for name in ("测试甲", "测试乙")
        ]
    else:
        member_breakdown = [
            {
                "member_name": "测试甲",
                "team_name": "法务五部",
                "completed_dates": [
                    value.isoformat() for value in reporting_dates
                ],
                "pending_confirmation_dates": [],
                "partial_dates": [],
                "not_filled_dates": [],
                "exempt_dates": [],
                "responsibility_unknown_dates": [],
            },
            {
                "member_name": "测试乙",
                "team_name": "法务五部",
                "completed_dates": [
                    value.isoformat() for value in reporting_dates[:2]
                ],
                "pending_confirmation_dates": [
                    reporting_dates[2].isoformat()
                ],
                "partial_dates": [],
                "not_filled_dates": [
                    {
                        "report_date": value.isoformat(),
                        "submission_state": "overdue",
                    }
                    for value in reporting_dates[3:]
                ],
                "exempt_dates": [],
                "responsibility_unknown_dates": [],
            },
        ]
    return {
        "query_kind": "submission_coverage",
        "scope_type": "team",
        "scope_label": "法务五部",
        "period_type": period_type,
        "period_start": start_date,
        "period_end": end_date,
        "default_reporting_dates": [
            value.isoformat() for value in reporting_dates
        ],
        "default_non_reporting_dates": [
            value.isoformat()
            for value in calendar_dates
            if value.weekday() >= 5
        ],
        "queried_at": _NOW.isoformat(),
        "count_unit": "member_day",
        "member_day_count": 10,
        "responsibility_data_complete": True,
        "responsibility_known_count": 10,
        "responsibility_unknown_count": 0,
        "expected_count": 10,
        "expected_known_count": 10,
        "reporter_count": reporter_count,
        "report_count": report_count,
        "completed_count": completed_count,
        "pending_confirmation_count": pending_count,
        "partial_count": 0,
        "not_filled_count": not_filled_count,
        "exempt_count": 0,
        "overdue_count": not_filled_count,
        "not_yet_due_count": 0,
        "daily_breakdown": daily_breakdown,
        "member_breakdown": member_breakdown,
        "permission_allowed": True,
    }


def _context(case_number: int) -> TrustedContext:
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=_NOW,
        principal=TrustedPrincipal(
            tenant_id="coverage-evaluation-tenant",
            user_id=_USER_ID,
            conversation_id=f"coverage-evaluation-{case_number}",
            source_message_id=f"coverage-evaluation-{case_number}-message",
            timezone="Asia/Shanghai",
            display_name="评测用户",
            conversation_kind="direct",
        ),
        runtime_identity=TrustedRuntimeIdentity(
            provider_name="DeepSeek",
            model_name=CANARY_MODEL_NAME,
        ),
        allowed_tool_names=_TOOLS,
        gate_decisions={"query_report_insights": True},
    )


def _exact_call(call: NativeToolCall, period_type: Period) -> bool:
    arguments = call.arguments
    return bool(
        call.tool_name == "query_report_insights"
        and arguments.get("query_kind") == "submission_coverage"
        and arguments.get("scope_type") == "organization"
        and arguments.get("scope_name") == "法务五部"
        and arguments.get("period_type") == period_type
        and arguments.get("status_filter", "all_saved") == "all_saved"
    )


def _mentions_period(content: str, period_type: Period) -> bool:
    if period_type == "current_week":
        return bool(
            ("2026-08-10" in content and "2026-08-16" in content)
            or ("8月10日" in content and "8月16日" in content)
        )
    return bool(
        ("2026-08-03" in content and "2026-08-09" in content)
        or ("8月3日" in content and "8月9日" in content)
    )


def _mentions_labeled_count(
    content: str,
    *,
    label_pattern: str,
    value: int,
) -> bool:
    return bool(
        re.search(
            rf"(?:{label_pattern})[^。；;\n]{{0,48}}(?<!\d){value}(?!\d)",
            content,
        )
    )


def _preserves_coverage_counts(content: str, period_type: Period) -> bool:
    expected_is_ten = _mentions_labeled_count(
        content,
        label_pattern=r"应交|应提交|应填|expected_count",
        value=10,
    )
    expected_is_not_zero = not _mentions_labeled_count(
        content,
        label_pattern=r"应交|应提交|应填|expected_count",
        value=0,
    )
    if period_type == "current_week":
        report_count_is_zero = _mentions_labeled_count(
            content,
            label_pattern=(
                r"日报记录|日报份数|报告数|已保存|实际报告|report_count"
            ),
            value=0,
        )
        return expected_is_ten and expected_is_not_zero and report_count_is_zero
    pending_is_one = _mentions_labeled_count(
        content,
        label_pattern=r"待确认|未最终确认|pending_confirmation_count",
        value=1,
    )
    submitted_is_seven = _mentions_labeled_count(
        content,
        label_pattern=r"已提交|已完成|完成/提交|completed_count",
        value=7,
    )
    return expected_is_ten and expected_is_not_zero and pending_is_one and submitted_is_seven


def _mentions_query_time(content: str, period_type: Period) -> bool:
    if period_type == "previous_week":
        return True
    return "10:00" in content or "10点" in content


async def _run_case(
    adapter: DeepSeekToolCallingAdapter,
    *,
    case_number: int,
    period_type: Period,
) -> dict[str, Any]:
    runtime = _ReadOnlyCoverageRuntime(period_type)
    user_text = (
        "帮我查一下法务五部本周的日报提交情况。"
        if period_type == "current_week"
        else "帮我查一下法务五部上周的日报提交情况。"
    )
    try:
        result = await adapter.run_canary_turn(
            system_prompt=canary_system_prompt(allowed_tool_names=_TOOLS),
            user_text=user_text,
            context=_context(case_number),
            runtime_session=runtime,
        )
    except (DeepSeekToolCallingError, AssertionError, ValueError) as exc:
        return {
            "case": case_number,
            "period_type": period_type,
            "overall_pass": False,
            "error": f"{type(exc).__name__}: {exc}",
            "calls": [
                {"name": call.tool_name, "arguments": call.arguments}
                for call in runtime.calls
            ],
        }
    content = result.final_content.strip()
    exact_calls = len(runtime.calls) == 1 and _exact_call(
        runtime.calls[0], period_type
    )
    zero_side_effects = bool(
        runtime.business_write_count == 0
        and runtime.handler_call_count == 0
        and runtime.message_send_count == 0
    )
    checks = {
        "exact_calls": exact_calls,
        "zero_side_effects": zero_side_effects,
        "scope_mentioned": "法务五部" in content,
        "period_mentioned": _mentions_period(content, period_type),
        "responsibility_completeness_mentioned": (
            "责任" in content and "完整" in content
        ),
        "coverage_counts_preserved": _preserves_coverage_counts(
            content,
            period_type,
        ),
        "query_time_mentioned": _mentions_query_time(content, period_type),
    }
    passed = all(checks.values())
    return {
        "case": case_number,
        "period_type": period_type,
        "overall_pass": passed,
        "calls": [
            {"name": call.tool_name, "arguments": call.arguments}
            for call in runtime.calls
        ],
        "final_content": content,
        "checks": checks,
        "zero_business_writes": runtime.business_write_count == 0,
        "production_handlers_called": runtime.handler_call_count != 0,
        "messages_sent": runtime.message_send_count != 0,
    }


async def _run(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")
    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(args.timeout_seconds),
    ) as client:
        adapter = DeepSeekToolCallingAdapter(
            http_client=client,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            max_tool_loops=4,
            max_request_attempts=1,
            endpoint=endpoint,
        )
        results = [
            await _run_case(
                adapter,
                case_number=1,
                period_type="current_week",
            ),
            await _run_case(
                adapter,
                case_number=2,
                period_type="previous_week",
            ),
        ]
    summary = {
        "requested_cases": 2,
        "passed_cases": sum(bool(item["overall_pass"]) for item in results),
        "failed_cases": sum(not bool(item["overall_pass"]) for item in results),
        "business_database_connected": False,
        "production_handlers_called": any(
            bool(item.get("production_handlers_called")) for item in results
        ),
        "messages_sent": any(bool(item.get("messages_sent")) for item in results),
        "zero_business_writes": all(
            item.get("zero_business_writes") is True for item in results
        ),
    }
    print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False))
    return 0 if summary["failed_cases"] == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument(
        "--base-url",
        default=(
            os.environ.get("DEEPSEEK_API_BASE")
            or os.environ.get("LLM_BASE_URL")
            or "https://api.deepseek.com/v1"
        ),
    )
    parser.add_argument("--model", default=CANARY_MODEL_NAME)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
