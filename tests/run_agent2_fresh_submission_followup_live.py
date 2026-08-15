"""Real-model, zero-write gate for mutable submission-status follow-ups.

The DeepSeek adapter and model are real. The runtime is an in-memory read
recorder: it opens no business database, invokes no production handler, and
sends no message.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
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
    TrustedRecentMessage,
    TrustedRecentOperation,
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

_NOW = datetime(2026, 8, 16, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
_REPORT_DATE = "2026-08-15"
_MEMBER_NAME = "测试甲"
_USER_ID = UUID("11111111-1111-1111-1111-111111111111")
_TOOLS = frozenset({"query_managed_daily_reports"})
SubmissionState = Literal["missing", "completed"]


class _ReadOnlyRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self, state: SubmissionState) -> None:
        self.state = state
        self.calls: list[NativeToolCall] = []
        self.business_write_count = 0
        self.message_send_count = 0

    async def execute(
        self,
        calls: tuple[NativeToolCall, ...],
        *,
        defer_finalization: bool = False,
    ) -> ProductionRuntimeResult:
        if not calls or defer_finalization:
            raise AssertionError("freshness evaluation accepts read calls only")
        self.calls.extend(calls)
        receipts = tuple(self._receipt(call) for call in calls)
        return ProductionRuntimeResult(
            status="success",
            receipts=receipts,
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
        if call.tool_name != "query_managed_daily_reports":
            raise AssertionError("freshness evaluation received another tool")
        report_date = str(
            call.arguments.get("proposed_report_date") or _NOW.date()
        )
        view = str(call.arguments.get("view") or "")
        if view == "member_report":
            facts = {
                "query_kind": "member_report",
                "report_date": report_date,
                "member": {
                    "name": _MEMBER_NAME,
                    "team_name": "综合管理部",
                    "team_label": "法务合约中心 / 综合管理部",
                },
                "submission": {
                    "status": (
                        "已完成" if self.state == "completed" else "未填写"
                    ),
                    "submitted_at": (
                        "2026-08-16T08:50:00+08:00"
                        if self.state == "completed"
                        else None
                    ),
                    "confirmation": (
                        "自动提交" if self.state == "completed" else "未确认"
                    ),
                },
                "report": None,
            }
        else:
            completed = (
                [{"name": _MEMBER_NAME, "team_name": "综合管理部"}]
                if self.state == "completed"
                else []
            )
            missing = (
                []
                if self.state == "completed"
                else [{"name": _MEMBER_NAME, "team_name": "综合管理部"}]
            )
            facts = {
                "query_kind": "missing_submissions",
                "report_date": report_date,
                "scope_name": "法务合约中心",
                "completed_members": completed,
                "partial_members": [],
                "not_filled_members": missing,
            }
        return ToolReceipt(
            status=ReceiptStatus.SUCCESS,
            tool_name=call.tool_name,
            changed=False,
            target_type="managed_daily_report",
            target_id=f"evaluation:{report_date}:{view}",
            safe_user_facts={
                "actual_write": False,
                "evaluation_only": True,
                "production_handler_called": False,
                "managed_daily_query": facts,
            },
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )


def _context(
    *,
    round_number: int,
    source_message_id: str,
    prior_answer: str | None = None,
) -> TrustedContext:
    recent_messages: tuple[TrustedRecentMessage, ...] = ()
    recent_operations: tuple[TrustedRecentOperation, ...] = ()
    if prior_answer is not None:
        recent_messages = (
            TrustedRecentMessage(
                role="user",
                content="昨天测试甲的日报交了吗？",
                source_message_id=f"round-{round_number}-prior-query",
            ),
            TrustedRecentMessage(
                role="assistant",
                content=prior_answer,
                source_message_id=(
                    f"round-{round_number}-prior-query:assistant"
                ),
                fact_time_scope="past_snapshot",
            ),
        )
        recent_operations = (
            TrustedRecentOperation(
                tenant_id="eval-tenant",
                user_id=_USER_ID,
                conversation_id=f"eval-conversation-{round_number}",
                source_message_id=f"round-{round_number}-prior-query",
                tool_call_id=f"round-{round_number}-prior-call",
                tool_name="query_managed_daily_reports",
                status="success",
                changed=False,
                target_type="managed_daily_report",
                target_id=f"evaluation:{_REPORT_DATE}:member_report",
                occurred_at=_NOW - timedelta(minutes=5),
            ),
        )
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=_NOW,
        principal=TrustedPrincipal(
            tenant_id="eval-tenant",
            user_id=_USER_ID,
            conversation_id=f"eval-conversation-{round_number}",
            source_message_id=source_message_id,
            timezone="Asia/Shanghai",
            display_name="评测用户",
            conversation_kind="direct",
        ),
        runtime_identity=TrustedRuntimeIdentity(
            provider_name="DeepSeek",
            model_name=CANARY_MODEL_NAME,
        ),
        recent_messages=recent_messages,
        recent_operations=recent_operations,
        allowed_tool_names=_TOOLS,
        gate_decisions={name: True for name in _TOOLS},
    )


def _call_payload(call: NativeToolCall) -> dict[str, Any]:
    return {"name": call.tool_name, "arguments": call.arguments}


def _is_exact_member_date_query(call: NativeToolCall) -> bool:
    arguments = call.arguments
    return bool(
        call.tool_name == "query_managed_daily_reports"
        and arguments.get("view") == "member_report"
        and arguments.get("member_name") == _MEMBER_NAME
        and str(arguments.get("proposed_report_date") or "")
        == _REPORT_DATE
        and str(arguments.get("report_date_expression") or "").strip()
    )


async def _run_branch(
    *,
    adapter: DeepSeekToolCallingAdapter,
    round_number: int,
    prior_answer: str,
    branch: Literal["fresh", "historical"],
) -> dict[str, Any]:
    if branch == "fresh":
        user_text = "他现在还是没交吗？请按当前实际状态回答。"
        runtime = _ReadOnlyRuntime("completed")
    else:
        user_text = "你刚才那次查询里，当时说测试甲是什么状态？"
        runtime = _ReadOnlyRuntime("completed")
    started = perf_counter()
    try:
        result = await adapter.run_canary_turn(
            system_prompt=canary_system_prompt(
                allowed_tool_names=_TOOLS
            ),
            user_text=user_text,
            context=_context(
                round_number=round_number,
                source_message_id=f"round-{round_number}-{branch}",
                prior_answer=prior_answer,
            ),
            runtime_session=runtime,
        )
    except (DeepSeekToolCallingError, AssertionError, ValueError) as exc:
        return {
            "branch": branch,
            "overall_pass": False,
            "error": f"{type(exc).__name__}: {exc}",
            "calls": [_call_payload(call) for call in runtime.calls],
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }

    calls = list(runtime.calls)
    content = result.final_content.strip()
    if branch == "fresh":
        passed = bool(
            len(calls) == 1
            and _is_exact_member_date_query(calls[0])
            and _MEMBER_NAME in content
            and any(marker in content for marker in ("已完成", "已提交", "已经交"))
        )
    else:
        passed = bool(
            not calls
            and _MEMBER_NAME in content
            and any(marker in content for marker in ("未填写", "没交", "未提交"))
        )
    return {
        "branch": branch,
        "overall_pass": passed,
        "calls": [_call_payload(call) for call in calls],
        "final_content": content,
        "zero_business_writes": runtime.business_write_count == 0,
        "messages_sent": runtime.message_send_count != 0,
        "elapsed_ms": round((perf_counter() - started) * 1000, 1),
    }


async def _evaluate_round(
    adapter: DeepSeekToolCallingAdapter,
    round_number: int,
) -> dict[str, Any]:
    first_text = "昨天测试甲的日报交了吗？"
    first_runtime = _ReadOnlyRuntime("missing")
    started = perf_counter()
    try:
        first = await adapter.run_canary_turn(
            system_prompt=canary_system_prompt(
                allowed_tool_names=_TOOLS
            ),
            user_text=first_text,
            context=_context(
                round_number=round_number,
                source_message_id=f"round-{round_number}-prior-query",
            ),
            runtime_session=first_runtime,
        )
    except (DeepSeekToolCallingError, AssertionError, ValueError) as exc:
        return {
            "round": round_number,
            "overall_pass": False,
            "error": f"{type(exc).__name__}: {exc}",
            "first_calls": [
                _call_payload(call) for call in first_runtime.calls
            ],
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }

    prior_answer = first.final_content.strip()
    first_pass = bool(
        len(first_runtime.calls) == 1
        and _is_exact_member_date_query(first_runtime.calls[0])
        and _MEMBER_NAME in prior_answer
        and any(
            marker in prior_answer
            for marker in ("未填写", "没交", "未提交")
        )
    )
    fresh = await _run_branch(
        adapter=adapter,
        round_number=round_number,
        prior_answer=prior_answer,
        branch="fresh",
    )
    historical = await _run_branch(
        adapter=adapter,
        round_number=round_number,
        prior_answer=prior_answer,
        branch="historical",
    )
    return {
        "round": round_number,
        "first_query_pass": first_pass,
        "first_calls": [
            _call_payload(call) for call in first_runtime.calls
        ],
        "first_content": prior_answer,
        "fresh_followup": fresh,
        "historical_followup": historical,
        "overall_pass": bool(
            first_pass
            and fresh.get("overall_pass")
            and historical.get("overall_pass")
        ),
        "zero_business_writes": bool(
            fresh.get("zero_business_writes") is True
            and historical.get("zero_business_writes") is True
        ),
        "production_handlers_called": False,
        "business_database_connected": False,
        "messages_sent": False,
        "elapsed_ms": round((perf_counter() - started) * 1000, 1),
    }


async def _run(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        headers=headers,
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
        for round_number in range(1, args.rounds + 1):
            result = await _evaluate_round(adapter, round_number)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)

    passed = sum(bool(item.get("overall_pass")) for item in results)
    summary = {
        "requested_rounds": len(results),
        "passed_rounds": passed,
        "failed_rounds": len(results) - passed,
        "zero_business_write_assertions_passed": all(
            item.get("zero_business_writes") is True for item in results
        ),
        "production_handlers_called": False,
        "business_database_connected": False,
        "messages_sent": False,
    }
    artifact = {
        "schema_version": "agent2.fresh-submission-followup.live-eval.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "provider_endpoint_origin_sha256": hashlib.sha256(
            f"{parts.scheme}://{parts.netloc}".encode()
        ).hexdigest(),
        "summary": summary,
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"output": str(output), "summary": summary},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if passed == len(results) else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=2)
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
    parser.add_argument(
        "--output",
        default="artifacts/fresh_submission_followup_live_2rounds.json",
    )
    args = parser.parse_args()
    if args.rounds < 1 or args.timeout_seconds <= 0:
        parser.error("rounds and timeout-seconds must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
