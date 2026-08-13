"""Run an isolated live evaluation through ``DeepSeekToolCallingAdapter``.

The provider is real, but the runtime is deliberately an in-memory recorder:

* it imports no database or DingTalk client;
* it never dispatches a production handler;
* every proposed write receives a ``no_op`` receipt and advances no version;
* ``commit_pending`` finalizes only the recorder's in-memory state.

This lets the evaluation observe the adapter's initial model call, independent
semantic review, optional zero-draft recovery, tool loop, and terminal reply
without changing business data or sending a message.
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
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.periodic_report_context import TrustedPeriodicReportContext
from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_system_prompt,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
    TrustedReportSnapshot,
    TrustedRuntimeIdentity,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekCanaryResult,
    DeepSeekToolCallingAdapter,
    DeepSeekToolCallingError,
    ModelTurnAudit,
)
from app.agent2.tool_calling.production_contracts import ProductionRuntimeResult
from app.agent2.tool_calling.registry import TOOL_REGISTRY, deepseek_tool_schemas
from app.agent2.tool_calling.validation import NativeToolCall
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
)
from tests.test_agent2_tri_domain_model_eval import (
    DAILY_REPORT_ID,
    DAILY_VERSION,
    MODEL_CASES,
    PERIODIC_REPORT_ID,
    PERIODIC_VERSION,
    TRI_DOMAIN_TOOL_NAMES,
    WEEKLY_PLAN_ID,
    WEEKLY_PLAN_VERSION,
    TriDomainModelCase,
    score_parameters,
    score_selection,
)

_EVALUATED_CASE_IDS = (
    "weekly_report_open",
    "daily_and_weekly_plan",
)
_USER_ID = UUID("10000000-0000-4000-8000-000000000001")
_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
_EVALUATION_NOW = datetime(2026, 8, 14, 17, 30, tzinfo=_LOCAL_TIMEZONE)


@dataclass(frozen=True)
class _RecordedBatch:
    tool_names: tuple[str, ...]
    calls: tuple[NativeToolCall, ...]
    defer_finalization: bool


class _ZeroWriteRuntime:
    """Record adapter calls without invoking a business handler or data store."""

    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self, context: TrustedContext) -> None:
        self._context = context
        self.batches: list[_RecordedBatch] = []
        self._pending_receipts: tuple[ToolReceipt, ...] | None = None
        self.commit_count = 0
        self.rollback_count = 0
        self.business_write_count = 0
        self.message_send_count = 0

    async def execute(
        self,
        calls: tuple[NativeToolCall, ...],
        *,
        defer_finalization: bool = False,
    ) -> ProductionRuntimeResult:
        if not calls or any(not isinstance(call, NativeToolCall) for call in calls):
            raise TypeError("evaluation runtime requires sealed native calls")
        has_write = any(
            TOOL_REGISTRY[call.tool_name].read_or_write == "write"
            for call in calls
        )
        if defer_finalization is not has_write:
            raise AssertionError("adapter used an unexpected finalization mode")
        if self._pending_receipts is not None:
            raise AssertionError("evaluation runtime already has a pending batch")

        batch = _RecordedBatch(
            tool_names=tuple(call.tool_name for call in calls),
            calls=calls,
            defer_finalization=defer_finalization,
        )
        self.batches.append(batch)
        receipts = tuple(self._receipt(call) for call in calls)
        if has_write:
            self._pending_receipts = receipts
        return ProductionRuntimeResult(
            status="success",
            receipts=receipts,
            transaction_opened=has_write,
            transaction_pending=has_write,
            committed_to_outer_transaction=False,
            handler_call_count=0,
            business_write_count=0,
            pending_write_count=0,
            memory_write_count=0,
            memory_audit_write_count=0,
            receipt_write_count=0,
        )

    async def commit_pending(self) -> ProductionRuntimeResult:
        if self._pending_receipts is None:
            raise AssertionError("evaluation runtime has no pending batch")
        receipts = self._pending_receipts
        self._pending_receipts = None
        self.commit_count += 1
        return ProductionRuntimeResult(
            status="success",
            receipts=receipts,
            transaction_opened=True,
            transaction_pending=False,
            committed_to_outer_transaction=True,
            handler_call_count=0,
            business_write_count=0,
            pending_write_count=0,
            memory_write_count=0,
            memory_audit_write_count=0,
            receipt_write_count=0,
        )

    async def rollback_pending(self) -> None:
        if self._pending_receipts is not None:
            self._pending_receipts = None
            self.rollback_count += 1

    @property
    def all_calls(self) -> tuple[NativeToolCall, ...]:
        return tuple(call for batch in self.batches for call in batch.calls)

    def assert_zero_side_effects(self) -> None:
        if self._pending_receipts is not None:
            raise AssertionError("evaluation left an in-memory batch pending")
        if self.business_write_count != 0 or self.message_send_count != 0:
            raise AssertionError("evaluation runtime reported a real side effect")

    def _receipt(self, call: NativeToolCall) -> ToolReceipt:
        is_write = TOOL_REGISTRY[call.tool_name].read_or_write == "write"
        target_type, target_id, version, read_facts = self._target(call.tool_name)
        return ToolReceipt(
            status=(ReceiptStatus.NO_OP if is_write else ReceiptStatus.SUCCESS),
            tool_name=call.tool_name,
            changed=False,
            target_type=target_type,
            target_id=target_id,
            before_version=version,
            after_version=version,
            safe_user_facts={
                "actual_write": False,
                "evaluation_only": True,
                "production_handler_called": False,
                **read_facts,
            },
            execution_mode=ExecutionMode.CANARY_EXECUTE,
        )

    def _target(
        self,
        tool_name: str,
    ) -> tuple[str, str, int, dict[str, Any]]:
        if "current_weekly_report" in tool_name:
            report = self._context.current_weekly_report
            assert report is not None
            facts = (
                {"current_weekly_report": report.safe_snapshot()}
                if tool_name == "query_current_weekly_report"
                else {}
            )
            return "periodic_report", str(report.report_id), report.version, facts
        if "weekly_plan" in tool_name:
            plan = self._context.weekly_plan
            assert plan is not None
            facts = (
                {"weekly_plan": plan.model_payload()}
                if tool_name == "query_next_weekly_plan"
                else {}
            )
            return "weekly_plan", plan.plan_id, plan.version, facts
        report = self._context.today_report
        assert report is not None
        facts = (
            {"today_report": report.safe_snapshot()}
            if tool_name == "query_today_report"
            else {}
        )
        return "daily_report", str(report.report_id), report.version, facts


def _trusted_context(round_number: int, case: TriDomainModelCase) -> TrustedContext:
    monday = date(2026, 8, 17)
    weekly_plan = TrustedWeeklyPlanContext(
        plan_id=WEEKLY_PLAN_ID,
        batch_id="5fa86875-6f8a-477b-b324-8602010d809b",
        tenant_id="eval-tenant",
        owner_user_id=str(_USER_ID),
        target_week_start=monday,
        version=WEEKLY_PLAN_VERSION,
        status="collecting",
        days=tuple(
            TrustedWeeklyPlanDay(
                day_id=f"weekly-day-{day.isoformat()}",
                plan_date=day,
                state="unfilled",
            )
            for day in (monday + timedelta(days=offset) for offset in range(6))
        ),
        roles=("active_collection", "natural_next"),
        natural_next_for_message_indexes=(1,),
    )
    principal = TrustedPrincipal(
        tenant_id="eval-tenant",
        user_id=_USER_ID,
        conversation_id=f"eval-{case.case_id}-{round_number}",
        source_message_id=f"eval-message-{case.case_id}-{round_number}",
        timezone="Asia/Shanghai",
        display_name="模型评测用户",
        conversation_kind="direct",
    )
    today_report = TrustedReportSnapshot(
        report_id=UUID(DAILY_REPORT_ID),
        tenant_id=principal.tenant_id,
        owner_user_id=principal.user_id,
        report_date=_EVALUATION_NOW.date(),
        version=DAILY_VERSION,
        status="collecting",
    )
    current_weekly_report = TrustedPeriodicReportContext(
        tenant_id=principal.tenant_id,
        owner_user_id=principal.user_id,
        report_id=UUID(PERIODIC_REPORT_ID),
        report_type="weekly",
        period_key="2026-W33",
        version=PERIODIC_VERSION,
        status="collecting",
    )
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=_EVALUATION_NOW,
        principal=principal,
        runtime_identity=TrustedRuntimeIdentity(
            provider_name="DeepSeek",
            model_name=CANARY_MODEL_NAME,
        ),
        today_report=today_report,
        current_weekly_report=current_weekly_report,
        weekly_plan=weekly_plan,
        weekly_plans=(weekly_plan,),
        allowed_tool_names=TRI_DOMAIN_TOOL_NAMES,
        gate_decisions={name: True for name in TRI_DOMAIN_TOOL_NAMES},
    )


def _raw_calls(turn: ModelTurnAudit) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for raw_call in turn.raw_assistant_message.get("tool_calls") or ():
        try:
            function = raw_call["function"]
            arguments = json.loads(function["arguments"])
            parsed.append(
                {
                    "name": function["name"],
                    "arguments": arguments,
                }
            )
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
    return parsed


def _scoring_calls(calls: tuple[NativeToolCall, ...]) -> list[dict[str, Any]]:
    return [
        {"name": call.tool_name, "arguments": call.arguments}
        for call in calls
    ]


def _semantic_payload(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"name": call.get("name"), "arguments": call.get("arguments")}
        for call in calls
    ]


def _turn_summary(turn: ModelTurnAudit) -> dict[str, Any]:
    metadata = turn.response_metadata
    return {
        "iteration": turn.iteration,
        "tool_names": [call["name"] for call in _raw_calls(turn)],
        "semantic_review": bool(
            metadata.get("daily_weekly_write_semantic_review")
        ),
        "zero_draft_confirmation": bool(
            metadata.get("daily_weekly_zero_draft_write_confirmation")
        ),
        "argument_repair": bool(
            metadata.get("pre_execution_tool_argument_repair")
        ),
        "request_attempt_count": int(
            metadata.get("request_attempt_count", 1)
        ),
        "transport_retry_count": int(
            metadata.get("transport_retry_count", 0)
        ),
        "elapsed_seconds": metadata.get("elapsed_seconds"),
        "assistant_message_sha256": turn.assistant_message_sha256,
    }


def _zero_write_assertions(
    runtime: _ZeroWriteRuntime,
    result: DeepSeekCanaryResult,
) -> None:
    runtime.assert_zero_side_effects()
    if any(receipt.changed for receipt in result.receipts):
        raise AssertionError("evaluation receipt claimed a business change")
    for runtime_result in result.runtime_results:
        if (
            runtime_result.actual_write
            or runtime_result.business_write_count
            or runtime_result.pending_write_count
            or runtime_result.memory_write_count
            or runtime_result.memory_audit_write_count
            or runtime_result.receipt_write_count
            or runtime_result.handler_call_count
        ):
            raise AssertionError("evaluation runtime result claimed a side effect")


async def _evaluate_one(
    *,
    adapter: DeepSeekToolCallingAdapter,
    case: TriDomainModelCase,
    round_number: int,
) -> dict[str, Any]:
    context = _trusted_context(round_number, case)
    runtime = _ZeroWriteRuntime(context)
    started = perf_counter()
    try:
        result = await adapter.run_canary_turn(
            system_prompt=canary_system_prompt(),
            user_text=case.user_text,
            context=context,
            runtime_session=runtime,
            thinking_enabled=True,
        )
    except DeepSeekToolCallingError as exc:
        runtime.assert_zero_side_effects()
        return {
            "round": round_number,
            "case_id": case.case_id,
            "expected_tools": sorted(case.expected_tools),
            "valid_adapter_result": False,
            "overall_pass": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
            "model_turns": [_turn_summary(turn) for turn in exc.model_turns],
            "runtime_batches": [list(batch.tool_names) for batch in runtime.batches],
            "zero_business_writes": True,
            "messages_sent": False,
        }

    _zero_write_assertions(runtime, result)
    final_calls = _scoring_calls(runtime.all_calls)
    first_calls = _raw_calls(result.model_turns[0])
    selection_pass, selection_reason = score_selection(
        case,
        tool_calls=final_calls,
        assistant_content=result.final_content,
    )
    arguments_valid, argument_errors = score_parameters(
        case,
        tool_calls=final_calls,
    )
    initial_selection_pass, initial_selection_reason = score_selection(
        case,
        tool_calls=first_calls,
        assistant_content=(
            result.model_turns[0].raw_assistant_message.get("content")
        ),
    )
    initial_arguments_valid, initial_argument_errors = score_parameters(
        case,
        tool_calls=first_calls,
    )
    review_turns = tuple(
        turn
        for turn in result.model_turns
        if turn.response_metadata.get("daily_weekly_write_semantic_review")
    )
    review_changed_draft = (
        _semantic_payload(first_calls) != _semantic_payload(final_calls)
    )
    overall_pass = bool(selection_pass and arguments_valid and review_turns)
    return {
        "round": round_number,
        "case_id": case.case_id,
        "user_text": case.user_text,
        "expected_tools": sorted(case.expected_tools),
        "valid_adapter_result": True,
        "initial_tool_names": sorted(
            str(call.get("name") or "") for call in first_calls
        ),
        "initial_selection_pass": initial_selection_pass,
        "initial_selection_reason": initial_selection_reason,
        "initial_arguments_valid": initial_arguments_valid,
        "initial_argument_errors": list(initial_argument_errors),
        "final_tool_names": sorted(call["name"] for call in final_calls),
        "final_calls": final_calls,
        "selection_pass": selection_pass,
        "selection_reason": selection_reason,
        "arguments_valid": arguments_valid,
        "argument_errors": list(argument_errors),
        "semantic_review_count": len(review_turns),
        "review_changed_draft": review_changed_draft,
        "recovered_by_review": bool(
            overall_pass
            and review_changed_draft
            and not (initial_selection_pass and initial_arguments_valid)
        ),
        "runtime_batches": [list(batch.tool_names) for batch in runtime.batches],
        "in_memory_commit_count": runtime.commit_count,
        "in_memory_rollback_count": runtime.rollback_count,
        "zero_business_writes": True,
        "production_handlers_called": False,
        "messages_sent": False,
        "final_content_sha256": result.model_content_sha256,
        "model_turns": [_turn_summary(turn) for turn in result.model_turns],
        "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        "overall_pass": overall_pass,
        "failure_reason": (
            None
            if overall_pass
            else "; ".join(
                value
                for value in (
                    None if selection_pass else selection_reason,
                    None if arguments_valid else "; ".join(argument_errors),
                    None if review_turns else "adapter semantic review did not run",
                )
                if value
            )
        ),
    }


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _run(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    base_url = args.base_url.rstrip("/")
    endpoint = base_url + "/chat/completions"
    endpoint_parts = urlsplit(endpoint)
    if endpoint_parts.scheme not in {"http", "https"} or not endpoint_parts.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")

    selected = {
        case.case_id: case
        for case in MODEL_CASES
        if case.case_id in _EVALUATED_CASE_IDS
    }
    if tuple(selected) != _EVALUATED_CASE_IDS:
        raise RuntimeError("required tri-domain cases are unavailable")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    results: list[dict[str, Any]] = []
    timeout = httpx.Timeout(args.timeout_seconds)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        adapter = DeepSeekToolCallingAdapter(
            http_client=client,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            max_tool_loops=args.max_tool_loops,
            max_request_attempts=args.max_attempts,
            endpoint=endpoint,
        )
        for round_number in range(1, args.rounds + 1):
            for case_id in _EVALUATED_CASE_IDS:
                result = await _evaluate_one(
                    adapter=adapter,
                    case=selected[case_id],
                    round_number=round_number,
                )
                results.append(result)
                print(
                    json.dumps(
                        {
                            "round": round_number,
                            "case_id": case_id,
                            "initial_tool_names": result.get(
                                "initial_tool_names", []
                            ),
                            "final_tool_names": result.get("final_tool_names", []),
                            "semantic_review_count": result.get(
                                "semantic_review_count", 0
                            ),
                            "recovered_by_review": result.get(
                                "recovered_by_review", False
                            ),
                            "overall_pass": result["overall_pass"],
                            "failure_reason": result.get("failure_reason")
                            or result.get("error"),
                            "elapsed_ms": result["elapsed_ms"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )

    passed = sum(bool(item["overall_pass"]) for item in results)
    recovered = sum(bool(item.get("recovered_by_review")) for item in results)
    summary = {
        "requested_case_runs": len(results),
        "passed_case_runs": passed,
        "failed_case_runs": len(results) - passed,
        "strict_pass_rate": round(passed / len(results), 4),
        "semantic_review_run_count": sum(
            int(item.get("semantic_review_count", 0)) for item in results
        ),
        "review_recovery_count": recovered,
        "zero_business_write_assertions_passed": all(
            item.get("zero_business_writes") is True for item in results
        ),
    }
    artifact = {
        "schema_version": "agent2.tri-domain.full-adapter-live-eval.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "rounds": args.rounds,
        "evaluated_case_ids": list(_EVALUATED_CASE_IDS),
        "adapter_path": "DeepSeekToolCallingAdapter.run_canary_turn",
        "runtime": {
            "kind": "ephemeral_in_memory_no_op_recorder",
            "production_handlers_called": False,
            "business_database_connected": False,
            "business_data_written": False,
            "receipt_store_written": False,
            "messages_sent": False,
        },
        "provider_endpoint_origin_sha256": hashlib.sha256(
            f"{endpoint_parts.scheme}://{endpoint_parts.netloc}".encode()
        ).hexdigest(),
        "system_prompt_sha256": hashlib.sha256(
            canary_system_prompt().encode("utf-8")
        ).hexdigest(),
        "tool_schemas_sha256": _sha256_json(
            deepseek_tool_schemas(TRI_DOMAIN_TOOL_NAMES)
        ),
        "context_sha256": _sha256_json(
            _trusted_context(1, selected[_EVALUATED_CASE_IDS[0]]).model_payload()
        ),
        "evaluated_source_sha256": {
            "app/agent2/tool_calling/canary_config.py": _sha256_file(
                Path(__file__).resolve().parents[1]
                / "app/agent2/tool_calling/canary_config.py"
            ),
            "app/agent2/tool_calling/deepseek_adapter.py": _sha256_file(
                Path(__file__).resolve().parents[1]
                / "app/agent2/tool_calling/deepseek_adapter.py"
            ),
        },
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
    return 0 if passed == len(results) else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run two high-risk tri-domain cases through the full adapter with "
            "a zero-write in-memory runtime."
        )
    )
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-tool-loops", type=int, default=4)
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
        default="artifacts/tri_domain_full_adapter_eval_2rounds.json",
    )
    args = parser.parse_args()
    if args.rounds < 2:
        parser.error("rounds must be at least 2")
    if args.max_attempts < 1 or args.max_tool_loops < 1:
        parser.error("max-attempts and max-tool-loops must be positive")
    if args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
