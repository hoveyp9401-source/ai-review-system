from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import UUID
from zoneinfo import ZoneInfo

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
    TrustedReportItem,
    TrustedReportReference,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.deepseek_adapter import DeepSeekToolCallingAdapter
from app.agent2.tool_calling.production_contracts import ProductionRuntimeResult
from app.agent2.tool_calling.validation import NativeToolCall
from app.config import get_settings
from app.llm.client import LLMClient


USER_ID = UUID("10000000-0000-4000-8000-000000000001")
REPORT_ID = UUID("20000000-0000-4000-8000-000000000009")
REPORT_DATE = date(2026, 8, 18)
COMBINED = (
    "明天计划继续找可以做成网页端的agent技能然后"
    "被告案件进行通报与未结案案件的签约"
)
SOURCE = (
    "1. 明天计划继续找可以做成网页端的agent技能\n"
    "2.被告案件进行通报与未结案案件的签约"
)
EXPECTED = (
    "明天计划继续找可以做成网页端的agent技能",
    "被告案件进行通报与未结案案件的签约",
)
ALLOWED = frozenset({"add_daily_items", "delete_daily_items"})


def _context(round_number: int) -> TrustedContext:
    now = datetime(2026, 8, 18, 19, round_number, tzinfo=ZoneInfo("Asia/Shanghai"))
    report = TrustedReportSnapshot(
        report_id=REPORT_ID,
        tenant_id="test-tenant",
        owner_user_id=USER_ID,
        report_date=REPORT_DATE,
        version=9,
        status="pending_confirmation",
        items=(
            TrustedReportItem(
                item_id="tomorrow-combined",
                field="tomorrow_plan",
                content=COMBINED,
                report_id=REPORT_ID,
                report_version=9,
            ),
        ),
    )
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=now,
        principal=TrustedPrincipal(
            tenant_id="test-tenant",
            user_id=USER_ID,
            conversation_id=f"followup-live-{round_number}",
            source_message_id=f"followup-live-current-{round_number}",
            timezone="Asia/Shanghai",
            display_name="测试用户",
            conversation_kind="direct",
        ),
        today_report=report,
        recent_messages=(
            TrustedRecentMessage(
                role="user",
                content="明日计划是两条",
                source_message_id="previous-correction",
            ),
            TrustedRecentMessage(
                role="assistant",
                content="请把两条明日计划分别发给我，我来调整。",
                source_message_id="previous-correction:assistant",
            ),
            TrustedRecentMessage(
                role="user",
                content="咋不回复了？",
                source_message_id="queued-followup",
            ),
        ),
        recent_operations=(
            TrustedRecentOperation(
                tenant_id="test-tenant",
                user_id=USER_ID,
                conversation_id=f"followup-live-{round_number}",
                source_message_id="original-daily-message",
                tool_call_id="original-daily-add",
                tool_name="add_daily_items",
                status=ReceiptStatus.SUCCESS,
                changed=True,
                target_type="daily_report",
                target_id=str(REPORT_ID),
                before_version=8,
                after_version=9,
                affected_item_ids=("tomorrow-combined",),
                report_reference=TrustedReportReference(
                    report_id=REPORT_ID,
                    report_date=REPORT_DATE,
                    report_version=9,
                    report_status="pending_confirmation",
                    report_state_sha256=report.state_sha256,
                ),
                occurred_at=now - timedelta(minutes=2),
            ),
        ),
        allowed_tool_names=ALLOWED,
        gate_decisions={name: True for name in ALLOWED},
    )


class _ReplacementRuntime:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self) -> None:
        self.calls: tuple[NativeToolCall, ...] = ()
        self.receipts: tuple[ToolReceipt, ...] = ()
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, calls, *, defer_finalization=False):
        assert defer_finalization is True
        self.calls = calls
        actual_tools = [call.tool_name for call in calls]
        assert actual_tools == [
            "delete_daily_items",
            "add_daily_items",
        ], actual_tools
        assert calls[0].arguments["target_item_ids"] == [
            "tomorrow-combined"
        ], calls[0].arguments
        bound_add = CurrentTurnSource((SOURCE,)).bind_tool_arguments(
            "add_daily_items",
            calls[1].arguments,
        )
        values = tuple(item["content"] for item in bound_add["items"])
        assert values == EXPECTED, values
        snapshot = {
            "report_date": REPORT_DATE.isoformat(),
            "status": "pending_confirmation",
            "fields": {
                "today_work": [],
                "problems": [],
                "tomorrow_plan": [
                    {"item_id": f"replacement-{index}", "content": content}
                    for index, content in enumerate(values, start=1)
                ],
            },
            "acknowledged_empty_fields": [],
        }
        self.receipts = tuple(
            ToolReceipt(
                status=ReceiptStatus.SUCCESS,
                tool_name=call.tool_name,
                changed=True,
                target_type="daily_report",
                target_id=str(REPORT_ID),
                before_version=9 + index,
                after_version=10 + index,
                affected_item_ids=(
                    ("tomorrow-combined",)
                    if call.tool_name == "delete_daily_items"
                    else ("replacement-1", "replacement-2")
                ),
                safe_user_facts={
                    "actual_write": True,
                    "report_snapshot": snapshot,
                },
                execution_mode=ExecutionMode.CANARY_EXECUTE,
            )
            for index, call in enumerate(calls)
        )
        return ProductionRuntimeResult(
            status="success",
            receipts=self.receipts,
            transaction_opened=True,
            transaction_pending=True,
            handler_call_count=2,
        )

    async def commit_pending(self):
        self.commit_count += 1
        return ProductionRuntimeResult(
            status="success",
            receipts=self.receipts,
            transaction_opened=True,
            committed_to_outer_transaction=True,
            handler_call_count=2,
            business_write_count=2,
            receipt_write_count=2,
        )

    async def rollback_pending(self):
        self.rollback_count += 1


async def _run(rounds: int, output: Path) -> int:
    settings = get_settings()
    client = LLMClient(settings)
    results = []
    try:
        adapter = DeepSeekToolCallingAdapter(
            http_client=client.native_http_client,
            model=CANARY_MODEL_NAME,
            timeout_seconds=120,
            max_tool_loops=4,
            max_request_attempts=1,
            endpoint=settings.llm_base_url.rstrip("/") + "/chat/completions",
        )
        for round_number in range(1, rounds + 1):
            runtime = _ReplacementRuntime()
            started = perf_counter()
            try:
                result = await adapter.run_canary_turn(
                    system_prompt=canary_system_prompt(
                        allowed_tool_names=ALLOWED
                    ),
                    user_text=SOURCE,
                    context=_context(round_number),
                    runtime_session=runtime,
                    thinking_enabled=True,
                )
                passed = (
                    runtime.commit_count == 1
                    and runtime.rollback_count == 0
                    and all(content in result.final_content for content in EXPECTED)
                    and COMBINED not in result.final_content
                )
                item = {
                    "round": round_number,
                    "pass": passed,
                    "tools": [call.tool_name for call in runtime.calls],
                    "final_content": result.final_content,
                    "model_calls": len(result.model_turns),
                    "elapsed_ms": round((perf_counter() - started) * 1000, 1),
                }
            except Exception as exc:
                item = {
                    "round": round_number,
                    "pass": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "tools": [call.tool_name for call in runtime.calls],
                    "arguments": [call.arguments for call in runtime.calls],
                    "elapsed_ms": round((perf_counter() - started) * 1000, 1),
                }
            results.append(item)
            print(json.dumps(item, ensure_ascii=False, sort_keys=True), flush=True)
    finally:
        await client.close()
    payload = {
        "status": "pass" if all(item["pass"] for item in results) else "failed",
        "passed": sum(bool(item["pass"]) for item in results),
        "failed": sum(not bool(item["pass"]) for item in results),
        "rounds": rounds,
        "business_database_connected": False,
        "messages_sent": False,
        "results": results,
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return 0 if payload["status"] == "pass" else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 3:
        parser.error("rounds must be at least 3")
    return asyncio.run(_run(args.rounds, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
