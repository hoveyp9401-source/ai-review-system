"""Three-case real-DeepSeek gate for receipt-bound date correction.

The adapter, prompt, model, argument validation, and binder are real.  The
runtime is in-memory only: it opens no database, invokes no production handler,
and sends no message.  Every accepted write is represented as a verified no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_agent2_tri_domain_full_adapter_matrix_live import (
    _BinderZeroWriteRuntime,
)

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_system_prompt,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedDateCorrectionReference,
    TrustedPrincipal,
    TrustedRecentMessage,
    TrustedRecentOperation,
    TrustedReportItem,
    TrustedReportReference,
    TrustedReportSnapshot,
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
from app.agent2.tool_calling.turn_batching import (
    canonical_turn_batch_source_id,
)
from app.agent2.tool_calling.validation import NativeToolCall

TENANT_ID = "legal-daily-production-v1"
USER_ID = UUID("11111111-1111-4111-8111-111111111111")
REPORT_ID = UUID("22222222-2222-4222-8222-222222222222")
CONVERSATION_ID = "date-correction-live-eval"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 14, 22, 5, tzinfo=LOCAL_TZ)
SOURCE_DATE = date(2026, 8, 14)
TARGET_DATE = date(2026, 8, 13)
WRITE_PROVIDER_ID = "dingtalk:live-eval-prior-write"
WRITE_TURN_ID = canonical_turn_batch_source_id((WRITE_PROVIDER_ID,))
TOOLS = frozenset({"correct_daily_report_date"})
CaseKind = Literal["fresh_move", "ordinary_lock", "idempotent_replay"]


@dataclass(frozen=True)
class LiveCase:
    case_id: CaseKind
    user_text: str
    source_message_id: str


CASES = (
    LiveCase(
        case_id="fresh_move",
        user_text="以上内容是8月13日的。",
        source_message_id="live-fresh-correction",
    ),
    LiveCase(
        case_id="ordinary_lock",
        user_text="把今天这份日报改到8月13日。",
        source_message_id="live-ordinary-history-lock",
    ),
    LiveCase(
        case_id="idempotent_replay",
        user_text="以上内容是8月13日的。",
        source_message_id="live-replayed-correction",
    ),
)


def _report(
    *,
    report_date: date,
    version: int,
    correction_source_message_id: str | None = None,
) -> TrustedReportSnapshot:
    reference = (
        TrustedDateCorrectionReference(
            report_id=REPORT_ID,
            source_message_id=correction_source_message_id,
            source_report_date=SOURCE_DATE,
            target_report_date=TARGET_DATE,
        )
        if correction_source_message_id is not None
        else None
    )
    return TrustedReportSnapshot(
        report_id=REPORT_ID,
        tenant_id=TENANT_ID,
        owner_user_id=USER_ID,
        report_date=report_date,
        version=version,
        status="collecting",
        items=tuple(
            TrustedReportItem(
                item_id=f"today-{index}",
                field="today_work",
                content=f"脱敏工作事项{index}",
                report_id=REPORT_ID,
                report_version=version,
            )
            for index in range(1, 9)
        ),
        date_correction_reference=reference,
    )


def _recent_write(report: TrustedReportSnapshot) -> TrustedRecentOperation:
    return TrustedRecentOperation(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        source_message_id=WRITE_TURN_ID,
        tool_call_id="prior-add-eight-items",
        tool_name="add_daily_items",
        status="success",
        changed=True,
        target_type="daily_report",
        target_id=str(REPORT_ID),
        before_version=report.version - 1,
        after_version=report.version,
        report_reference=TrustedReportReference(
            report_id=REPORT_ID,
            report_date=report.report_date,
            report_version=report.version,
            report_status=report.status,
            report_state_sha256=report.state_sha256,
        ),
        occurred_at=NOW - timedelta(minutes=1),
    )


def _recent_messages() -> tuple[TrustedRecentMessage, ...]:
    return (
        TrustedRecentMessage(
            role="user",
            content="今日工作共八项，内容已脱敏。",
            source_message_id=WRITE_PROVIDER_ID,
        ),
        TrustedRecentMessage(
            role="assistant",
            content="八项今日工作已保存。",
            source_message_id=f"{WRITE_PROVIDER_ID}:assistant",
            source_turn_id=WRITE_TURN_ID,
        ),
    )


def _context(case: LiveCase) -> TrustedContext:
    source = _report(report_date=SOURCE_DATE, version=9)
    principal = TrustedPrincipal(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        source_message_id=case.source_message_id,
        timezone="Asia/Shanghai",
        display_name="测试用户",
        conversation_kind="direct",
    )
    common: dict[str, Any] = {
        "namespace": CANARY_STATE_NAMESPACE,
        "now": NOW,
        "principal": principal,
        "allowed_tool_names": TOOLS,
        "gate_decisions": {"correct_daily_report_date": True},
    }
    if case.case_id == "ordinary_lock":
        return TrustedContext(today_report=source, **common)
    if case.case_id == "fresh_move":
        return TrustedContext(
            today_report=source,
            recent_messages=_recent_messages(),
            recent_operations=(_recent_write(source),),
            **common,
        )
    moved = _report(
        report_date=TARGET_DATE,
        version=10,
        correction_source_message_id=case.source_message_id,
    )
    stripped_operation = _recent_write(source).model_copy(
        update={"report_reference": None}
    )
    return TrustedContext(
        historical_reports=(moved,),
        recent_messages=_recent_messages(),
        recent_operations=(stripped_operation,),
        **common,
    )


class _DateCorrectionZeroWriteRuntime(_BinderZeroWriteRuntime):
    """Use the real binder, then turn an accepted correction into a no-op."""

    def _receipt(self, call: NativeToolCall) -> ToolReceipt:
        if call.tool_name != "correct_daily_report_date":
            return super()._receipt(call)
        report = self.context.today_report or next(
            iter(self.context.historical_reports),
            None,
        )
        if report is None:
            raise AssertionError("date-correction evaluation report is missing")
        return ToolReceipt(
            status=ReceiptStatus.NO_OP,
            tool_name=call.tool_name,
            changed=False,
            target_type="daily_report",
            target_id=str(report.report_id),
            before_version=report.version,
            after_version=report.version,
            safe_user_facts={
                "actual_write": False,
                "evaluation_only": True,
                "production_handler_called": False,
                "source_report_date": SOURCE_DATE.isoformat(),
                "target_report_date": TARGET_DATE.isoformat(),
            },
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )


def _all_attempted_calls(
    runtime: _DateCorrectionZeroWriteRuntime,
) -> tuple[NativeToolCall, ...]:
    return (
        *runtime.all_calls,
        *(
            call
            for batch in runtime.blocked_attempts
            for call in batch
        ),
    )


def _score(
    case: LiveCase,
    *,
    runtime: _DateCorrectionZeroWriteRuntime,
    final_content: str,
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    calls = _all_attempted_calls(runtime)
    if len(calls) != 1 or calls[0].tool_name != "correct_daily_report_date":
        errors.append(
            "expected exactly one correct_daily_report_date attempt, got "
            + ",".join(call.tool_name for call in calls)
        )
        return False, tuple(errors)
    arguments = calls[0].arguments
    if arguments.get("proposed_source_date") != SOURCE_DATE.isoformat():
        errors.append("source date was not 2026-08-14")
    if arguments.get("proposed_target_date") != TARGET_DATE.isoformat():
        errors.append("target date was not 2026-08-13")
    if case.case_id in {"fresh_move", "ordinary_lock"} and (
        arguments.get("submit_after_correction") is not False
        or arguments.get("acknowledged_empty_fields") not in ([], ())
    ):
        errors.append("model added an unauthorized confirmation or empty-section change")
    if not final_content.strip():
        errors.append("final reply was blank")
    if "{{historical_report_lock_facts}}" in final_content:
        errors.append("server facts token leaked to final reply")
    if case.case_id == "ordinary_lock":
        expected = (
            "2026-08-13 的日报已在当日 09:00 后锁定，不会在之后自动解锁；"
            "目前只可查询，不能修改或移动。"
        )
        if final_content != expected:
            errors.append("ordinary historical lock was not server-rendered exactly")
        if not runtime.blocked_attempts:
            errors.append("ordinary historical correction was not blocked")
    elif not runtime.batches:
        errors.append("authorized correction did not reach the zero-write runtime")
    return not errors, tuple(errors)


async def _evaluate_one(
    adapter: DeepSeekToolCallingAdapter,
    case: LiveCase,
) -> dict[str, Any]:
    context = _context(case)
    runtime = _DateCorrectionZeroWriteRuntime(context, case.user_text)
    started = perf_counter()
    try:
        result = await adapter.run_canary_turn(
            system_prompt=canary_system_prompt(allowed_tool_names=TOOLS),
            user_text=case.user_text,
            context=context,
            runtime_session=runtime,
            thinking_enabled=True,
        )
        runtime.assert_zero_side_effects()
        if any(receipt.changed for receipt in result.receipts):
            raise AssertionError("evaluation receipt claimed a business write")
        if any(
            item.actual_write
            or item.business_write_count
            or item.handler_call_count
            or item.receipt_write_count
            for item in result.runtime_results
        ):
            raise AssertionError("evaluation runtime claimed an external side effect")
        passed, errors = _score(
            case,
            runtime=runtime,
            final_content=result.final_content,
        )
        return {
            "case_id": case.case_id,
            "overall_pass": passed,
            "errors": list(errors),
            "calls": [
                {"name": call.tool_name, "arguments": call.arguments}
                for call in _all_attempted_calls(runtime)
            ],
            "final_content": result.final_content,
            "zero_business_writes": runtime.business_write_count == 0,
            "production_handlers_called": False,
            "business_database_connected": False,
            "messages_sent": runtime.message_send_count != 0,
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }
    except (DeepSeekToolCallingError, AssertionError, ValueError) as exc:
        await runtime.rollback_pending()
        runtime.assert_zero_side_effects()
        return {
            "case_id": case.case_id,
            "overall_pass": False,
            "error": f"{type(exc).__name__}: {exc}",
            "calls": [
                {"name": call.tool_name, "arguments": call.arguments}
                for call in _all_attempted_calls(runtime)
            ],
            "zero_business_writes": runtime.business_write_count == 0,
            "production_handlers_called": False,
            "business_database_connected": False,
            "messages_sent": runtime.message_send_count != 0,
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
        for case in CASES:
            result = await _evaluate_one(adapter, case)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)

    passed = sum(bool(item.get("overall_pass")) for item in results)
    summary = {
        "requested_case_runs": 3,
        "passed_case_runs": passed,
        "failed_case_runs": 3 - passed,
        "zero_business_write_assertions_passed": all(
            item.get("zero_business_writes") is True for item in results
        ),
        "production_handlers_called": False,
        "business_database_connected": False,
        "messages_sent": False,
    }
    artifact = {
        "schema_version": "agent2.recent-date-correction.live-eval.v1",
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
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"output": str(output), "summary": summary},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if passed == 3 else 2


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
    parser.add_argument(
        "--output",
        default="artifacts/recent_date_correction_live_3cases.json",
    )
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
