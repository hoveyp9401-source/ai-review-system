"""Run a strict, isolated Daily Report source-quote evaluation.

The evaluation uses the real DeepSeek Full-Adapter and the existing in-memory
zero-write harness.  It never opens a business database, invokes a production
handler, or sends a message.  Proposed ``add_daily_items`` calls are passed
through the real current-turn source binder, then recorded in memory so the
persistable content can be compared with the server-owned user message.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_agent2_tri_domain_full_adapter_matrix_live import (
    MatrixCase,
    _raw_calls,
    _turn_summary,
    _ZeroWriteRuntime,
)
from run_agent2_tri_domain_full_adapter_matrix_live import (
    _context as matrix_context,
)

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_system_prompt,
)
from app.agent2.tool_calling.context import TrustedContext
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.current_turn_source import (
    CurrentTurnSource,
    CurrentTurnSourceEvidenceError,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekToolCallingAdapter,
    DeepSeekToolCallingError,
)
from app.agent2.tool_calling.production_contracts import (
    ProductionRuntimeResult,
)
from app.agent2.tool_calling.validation import NativeToolCall

_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = frozenset({"add_daily_items"})
_DAILY_AND_WEEKLY_TOOLS = frozenset(
    {"add_daily_items", "apply_next_weekly_plan"}
)
ReportField = Literal["today_work", "problems", "tomorrow_plan"]


@dataclass(frozen=True)
class ExpectedItem:
    field: ReportField
    exact_quote: str


@dataclass(frozen=True)
class QuoteCase:
    case_id: str
    category: str
    user_text: str
    expected_items: tuple[ExpectedItem, ...]
    expected_empty_fields: frozenset[ReportField] = frozenset()
    allowed_tool_names: frozenset[str] = _TOOLS


CASES = (
    QuoteCase(
        case_id="pang_original_sentence",
        category="pang_exact",
        user_text="今天做了日报的基础功能优化",
        expected_items=(
            ExpectedItem("today_work", "做了日报的基础功能优化"),
        ),
        allowed_tool_names=_DAILY_AND_WEEKLY_TOOLS,
    ),
    QuoteCase(
        case_id="complete_three_sections_five_empty_four",
        category="complete_three_section",
        user_text=(
            "今日工作：完成海滨项目合同复核；更新两份用印台账；"
            "与财务核对付款节点；起草补充协议；回复业务部门法律咨询。"
            "问题风险：暂无。"
            "明日计划：跟进海滨项目签署；整理诉讼材料；"
            "复核采购模板；向项目组反馈风险意见。"
        ),
        expected_items=(
            ExpectedItem("today_work", "完成海滨项目合同复核"),
            ExpectedItem("today_work", "更新两份用印台账"),
            ExpectedItem("today_work", "与财务核对付款节点"),
            ExpectedItem("today_work", "起草补充协议"),
            ExpectedItem("today_work", "回复业务部门法律咨询"),
            ExpectedItem("tomorrow_plan", "跟进海滨项目签署"),
            ExpectedItem("tomorrow_plan", "整理诉讼材料"),
            ExpectedItem("tomorrow_plan", "复核采购模板"),
            ExpectedItem("tomorrow_plan", "向项目组反馈风险意见"),
        ),
        expected_empty_fields=frozenset({"problems"}),
    ),
    QuoteCase(
        case_id="long_risk_keeps_all_conditions",
        category="long_risk",
        user_text=(
            "今日工作：复核华东项目补充协议并标注修改意见。"
            "问题风险：供应商仍未提供盖章版授权文件，若明天中午前不能补齐，"
            "周一付款审批将无法按原计划发起，需项目负责人决定是否调整节点。"
            "明日计划：继续催收授权文件。"
        ),
        expected_items=(
            ExpectedItem(
                "today_work",
                "复核华东项目补充协议并标注修改意见",
            ),
            ExpectedItem(
                "problems",
                (
                    "供应商仍未提供盖章版授权文件，若明天中午前不能补齐，"
                    "周一付款审批将无法按原计划发起，需项目负责人决定是否调整节点"
                ),
            ),
            ExpectedItem("tomorrow_plan", "继续催收授权文件"),
        ),
    ),
    QuoteCase(
        case_id="multiple_items_split_by_field",
        category="multi_item_sections",
        user_text=(
            "工作方面，一是上午审核了三份用印申请，二是下午修改了两版保密协议，"
            "三是参加了项目风险讨论；风险方面没有新增风险；"
            "明天一是核对诉讼证据目录，二是整理本周待闭环事项。"
        ),
        expected_items=(
            ExpectedItem("today_work", "上午审核了三份用印申请"),
            ExpectedItem("today_work", "下午修改了两版保密协议"),
            ExpectedItem("today_work", "参加了项目风险讨论"),
            ExpectedItem("tomorrow_plan", "核对诉讼证据目录"),
            ExpectedItem("tomorrow_plan", "整理本周待闭环事项"),
        ),
        expected_empty_fields=frozenset({"problems"}),
    ),
    QuoteCase(
        case_id="natural_wording_likely_to_be_rephrased",
        category="natural_rephrase",
        user_text=(
            "今天主要是把付款节点跟财务又过了一遍，"
            "顺手补上合同里漏掉的发票要求；目前对方还没寄回盖章版；"
            "明天我去催办并同步项目组。"
        ),
        expected_items=(
            ExpectedItem("today_work", "把付款节点跟财务又过了一遍"),
            ExpectedItem("today_work", "顺手补上合同里漏掉的发票要求"),
            ExpectedItem("problems", "对方还没寄回盖章版"),
            ExpectedItem("tomorrow_plan", "催办"),
            ExpectedItem("tomorrow_plan", "同步项目组"),
        ),
    ),
    QuoteCase(
        case_id="negated_work_must_not_become_completed",
        category="negation_integrity",
        user_text="问题风险：合同复核尚未完成。明日计划：继续处理。",
        expected_items=(
            ExpectedItem("problems", "合同复核尚未完成"),
            ExpectedItem("tomorrow_plan", "继续处理"),
        ),
    ),
    QuoteCase(
        case_id="completion_with_unsubmitted_qualifier_stays_whole",
        category="qualifier_integrity",
        user_text="今日工作：完成合同复核但尚未提交系统。",
        expected_items=(
            ExpectedItem("today_work", "完成合同复核但尚未提交系统"),
        ),
    ),
)


def _native_payload(calls: tuple[NativeToolCall, ...]) -> list[dict[str, Any]]:
    return [
        {
            "name": call.tool_name,
            "arguments": call.arguments,
        }
        for call in calls
    ]


class _QuoteBindingZeroWriteRuntime(_ZeroWriteRuntime):
    """Materialize server-owned quotes, then use the shared no-op recorder."""

    def __init__(self, context: TrustedContext, user_text: str) -> None:
        super().__init__(context)
        self._source = CurrentTurnSource(
            (user_text,),
            occurred_at=(context.now,),
        )
        self.proposed_batches: list[tuple[NativeToolCall, ...]] = []
        self.bound_batches: list[tuple[NativeToolCall, ...]] = []
        self.binding_errors: list[str] = []

    async def execute(
        self,
        calls: tuple[NativeToolCall, ...],
        *,
        defer_finalization: bool = False,
    ) -> ProductionRuntimeResult:
        self.proposed_batches.append(calls)
        bound: list[NativeToolCall] = []
        try:
            for call in calls:
                arguments = self._source.bind_tool_arguments(
                    call.tool_name,
                    call.arguments,
                )
                bound.append(
                    NativeToolCall(
                        tool_call_id=call.tool_call_id,
                        tool_name=call.tool_name,
                        arguments=arguments,
                    )
                )
        except CurrentTurnSourceEvidenceError as exc:
            self.binding_errors.append(exc.code)
            receipts = tuple(
                ToolReceipt(
                    status=ReceiptStatus.BLOCKED,
                    tool_name=call.tool_name,
                    changed=False,
                    error_code=exc.code,
                    safe_user_facts={
                        "actual_write": False,
                        "evaluation_only": True,
                        "production_handler_called": False,
                    },
                    execution_mode=ExecutionMode.CANARY_EXECUTE,
                )
                for call in calls
            )
            return ProductionRuntimeResult(
                status="blocked",
                receipts=receipts,
                error_code=exc.code,
            )

        sealed = tuple(bound)
        self.bound_batches.append(sealed)
        return await super().execute(
            sealed,
            defer_finalization=defer_finalization,
        )


def _matrix_case(case: QuoteCase) -> MatrixCase:
    return MatrixCase(
        case_id=case.case_id,
        category=case.category,
        user_text=case.user_text,
        expected_tools=_TOOLS,
        expected_daily_fields=frozenset(
            item.field for item in case.expected_items
        ),
        note="Daily source-quote live evaluation",
    )


def _context(case: QuoteCase, round_number: int) -> TrustedContext:
    context = matrix_context(_matrix_case(case), round_number)
    return context.model_copy(
        update={
            "allowed_tool_names": case.allowed_tool_names,
            "gate_decisions": {
                name: True for name in case.allowed_tool_names
            },
        }
    )


def _expected_pairs(case: QuoteCase) -> Counter[tuple[str, str]]:
    return Counter(
        (item.field, item.exact_quote) for item in case.expected_items
    )


def _score_proposed_call(
    case: QuoteCase,
    calls: list[dict[str, Any]],
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    if len(calls) != 1 or calls[0].get("name") != "add_daily_items":
        return False, (
            "final proposal must contain exactly one add_daily_items call",
        )

    arguments = calls[0].get("arguments")
    if not isinstance(arguments, dict):
        return False, ("add_daily_items arguments must be an object",)
    if arguments.get("date_selection") != "server_default":
        errors.append("daily write must use server_default report date")

    items = arguments.get("items")
    if not isinstance(items, list):
        return False, ("add_daily_items.items must be an array",)

    actual_items: list[tuple[str, str]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"items[{index}] is not an object")
            continue
        field = item.get("field")
        evidence = item.get("source_evidence")
        if not isinstance(evidence, dict):
            errors.append(f"items[{index}] has no source_evidence")
            continue
        if evidence.get("source_message_index") != 1:
            errors.append(f"items[{index}] has the wrong source_message_index")
        exact_quote = evidence.get("exact_quote")
        if not isinstance(exact_quote, str) or not exact_quote:
            errors.append(f"items[{index}] has no exact_quote")
            continue
        if exact_quote not in case.user_text:
            errors.append(f"items[{index}] exact_quote is not contiguous source text")
        actual_items.append((str(field), exact_quote))

    expected_items = tuple(
        (item.field, item.exact_quote) for item in case.expected_items
    )
    matched_actual_indexes: list[int] = []
    for field, core_quote in expected_items:
        matches = [
            index
            for index, (actual_field, actual_quote) in enumerate(actual_items)
            if actual_field == field and core_quote in actual_quote
        ]
        if len(matches) != 1:
            errors.append(
                "each expected matter must be covered by exactly one source quote: "
                f"{field}={core_quote!r}, matches={matches}"
            )
        else:
            matched_actual_indexes.append(matches[0])
    if len(set(matched_actual_indexes)) != len(matched_actual_indexes):
        errors.append("one source quote merged more than one independent matter")
    if len(actual_items) != len(expected_items):
        errors.append(
            "daily item count differs: expected "
            f"{len(expected_items)}, got {len(actual_items)}"
        )
    for index, (field, actual_quote) in enumerate(actual_items):
        if not any(
            expected_field == field and core_quote in actual_quote
            for expected_field, core_quote in expected_items
        ):
            errors.append(
                "source quote does not cover an expected user-authored matter: "
                f"items[{index}]={field}:{actual_quote!r}"
            )

    acknowledged = frozenset(arguments.get("acknowledged_empty_fields") or ())
    if acknowledged != case.expected_empty_fields:
        errors.append(
            "acknowledged empty fields differ: expected "
            f"{sorted(case.expected_empty_fields)}, got {sorted(acknowledged)}"
        )
    empty_evidence = arguments.get("empty_field_evidence") or []
    if not isinstance(empty_evidence, list):
        errors.append("empty_field_evidence must be an array")
    else:
        actual_empty: Counter[str] = Counter()
        for index, item in enumerate(empty_evidence):
            if not isinstance(item, dict):
                errors.append(f"empty_field_evidence[{index}] is not an object")
                continue
            evidence = item.get("source_evidence")
            if not isinstance(evidence, dict) or evidence.get(
                "source_message_index"
            ) != 1:
                errors.append(
                    f"empty_field_evidence[{index}] has invalid current-message evidence"
                )
            actual_empty[str(item.get("field"))] += 1
        expected_empty = Counter(case.expected_empty_fields)
        if actual_empty != expected_empty:
            errors.append(
                "empty-field evidence differs: expected "
                f"{sorted(expected_empty.elements())}, got "
                f"{sorted(actual_empty.elements())}"
            )
    return not errors, tuple(errors)


def _score_bound_call(
    case: QuoteCase,
    calls: list[dict[str, Any]],
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    if len(calls) != 1 or calls[0].get("name") != "add_daily_items":
        return False, ("server binder did not produce one add_daily_items call",)
    arguments = calls[0].get("arguments")
    if not isinstance(arguments, dict):
        return False, ("server-bound add_daily_items arguments are invalid",)
    for index, item in enumerate(arguments.get("items") or []):
            if not isinstance(item, dict):
                errors.append(f"bound items[{index}] is not an object")
                continue
            evidence = item.get("source_evidence") or {}
            exact_quote = evidence.get("exact_quote")
            content = item.get("content")
            if isinstance(exact_quote, str) and content != exact_quote:
                errors.append(
                    f"bound items[{index}].content was not copied from exact_quote"
                )
            if isinstance(exact_quote, str) and exact_quote in case.user_text:
                start = case.user_text.index(exact_quote)
                server_slice = case.user_text[start : start + len(exact_quote)]
                if content != server_slice:
                    errors.append(
                        f"bound items[{index}].content contains model-authored text"
                    )
    return not errors, tuple(errors)


def _model_rephrase_count(calls: list[dict[str, Any]]) -> int:
    count = 0
    for call in calls:
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        for item in arguments.get("items") or []:
            if not isinstance(item, dict):
                continue
            evidence = item.get("source_evidence") or {}
            if item.get("content") != evidence.get("exact_quote"):
                count += 1
    return count


def _canonical_arguments(case: QuoteCase) -> dict[str, Any]:
    return {
        "date_selection": "server_default",
        "items": [
            {
                "field": item.field,
                "content": f"模型语义解释{index}",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_quote": item.exact_quote,
                },
            }
            for index, item in enumerate(case.expected_items, start=1)
        ],
        "acknowledged_empty_fields": sorted(case.expected_empty_fields),
        "empty_field_evidence": [
            {
                "field": field,
                "source_evidence": {"source_message_index": 1},
            }
            for field in sorted(case.expected_empty_fields)
        ],
    }


def _self_check() -> dict[str, Any]:
    if len(CASES) != 7 or len({case.category for case in CASES}) != 7:
        raise AssertionError("source-quote evaluation requires seven distinct categories")
    if len(CASES) != len({case.case_id for case in CASES}):
        raise AssertionError("source-quote case IDs must be unique")

    for case in CASES:
        for item in case.expected_items:
            if case.user_text.count(item.exact_quote) != 1:
                raise AssertionError(
                    f"{case.case_id}: expected quote must occur exactly once"
                )
        proposed = [
            {
                "name": "add_daily_items",
                "arguments": _canonical_arguments(case),
            }
        ]
        proposed_ok, proposed_errors = _score_proposed_call(case, proposed)
        if not proposed_ok:
            raise AssertionError((case.case_id, proposed_errors))

        source = CurrentTurnSource((case.user_text,))
        bound_arguments = source.bind_tool_arguments(
            "add_daily_items",
            proposed[0]["arguments"],
        )
        bound_ok, bound_errors = _score_bound_call(
            case,
            [{"name": "add_daily_items", "arguments": bound_arguments}],
        )
        if not bound_ok:
            raise AssertionError((case.case_id, bound_errors))

    return {
        "case_count": len(CASES),
        "categories": sorted(case.category for case in CASES),
        "minimum_rounds": 3,
        "source_quote_full_coverage_and_no_merge_check": True,
        "forged_quote_live_case": False,
        "forged_quote_validation": "covered_by_unit_tests",
        "business_database_connected": False,
        "production_handlers_called": False,
        "messages_sent": False,
    }


async def _evaluate_one(
    *,
    adapter: DeepSeekToolCallingAdapter,
    case: QuoteCase,
    round_number: int,
) -> dict[str, Any]:
    context = _context(case, round_number)
    runtime = _QuoteBindingZeroWriteRuntime(context, case.user_text)
    started = perf_counter()
    prompt = canary_system_prompt(allowed_tool_names=case.allowed_tool_names)
    try:
        result = await adapter.run_canary_turn(
            system_prompt=prompt,
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
            "category": case.category,
            "overall_pass": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "binding_errors": runtime.binding_errors,
            "model_turns": [_turn_summary(turn) for turn in exc.model_turns],
            "model_raw_calls": [_raw_calls(turn) for turn in exc.model_turns],
            "zero_business_writes": True,
            "production_handlers_called": False,
            "messages_sent": False,
            "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        }

    runtime.assert_zero_side_effects()
    if any(receipt.changed for receipt in result.receipts):
        raise AssertionError("evaluation receipt claimed a business change")
    if any(runtime_result.actual_write for runtime_result in result.runtime_results):
        raise AssertionError("evaluation runtime claimed a business write")

    initial_calls = _raw_calls(result.model_turns[0])
    proposed_calls = (
        _native_payload(runtime.proposed_batches[-1])
        if runtime.proposed_batches
        else []
    )
    bound_calls = _native_payload(runtime.all_calls)
    proposed_ok, proposed_errors = _score_proposed_call(case, proposed_calls)
    bound_ok, bound_errors = _score_bound_call(case, bound_calls)
    semantic_review_count = sum(
        bool(turn.response_metadata.get("daily_weekly_write_semantic_review"))
        for turn in result.model_turns
    )
    review_errors: tuple[str, ...] = ()
    if semantic_review_count != 1:
        review_errors = (
            (
                "daily-only write must receive exactly one semantic review: "
                f"got {semantic_review_count}"
            ),
        )
    overall = bool(
        proposed_ok
        and bound_ok
        and not runtime.binding_errors
        and not review_errors
    )
    return {
        "round": round_number,
        "case_id": case.case_id,
        "category": case.category,
        "user_text": case.user_text,
        "expected_items": [
            {"field": item.field, "exact_quote": item.exact_quote}
            for item in case.expected_items
        ],
        "expected_empty_fields": sorted(case.expected_empty_fields),
        "initial_calls": initial_calls,
        "final_proposed_calls": proposed_calls,
        "server_bound_calls": bound_calls,
        "final_proposal_pass": proposed_ok,
        "final_proposal_errors": list(proposed_errors),
        "server_binding_pass": bound_ok,
        "server_binding_errors": list(bound_errors),
        "server_bound_no_model_added_text": bound_ok,
        "model_rephrased_item_count": _model_rephrase_count(proposed_calls),
        "semantic_review_count": semantic_review_count,
        "binding_errors": runtime.binding_errors,
        "in_memory_commit_count": runtime.commit_count,
        "in_memory_rollback_count": runtime.rollback_count,
        "zero_business_writes": True,
        "production_handlers_called": False,
        "messages_sent": False,
        "model_turns": [_turn_summary(turn) for turn in result.model_turns],
        "final_content": result.final_content,
        "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        "overall_pass": overall,
        "failure_reason": None
        if overall
        else "; ".join(
            (
                *proposed_errors,
                *bound_errors,
                *runtime.binding_errors,
                *review_errors,
            )
        ),
    }


def _source_revision(explicit: str | None) -> tuple[str, bool | None]:
    if explicit:
        return explicit, None
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=_ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
        return commit, dirty
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("SOURCE_COMMIT", "unavailable"), None


async def _run(args: argparse.Namespace) -> int:
    self_check = _self_check()
    print(
        json.dumps({"self_check": self_check}, ensure_ascii=False, sort_keys=True),
        flush=True,
    )
    if args.self_check_only:
        return 0

    requested_case_ids = frozenset(args.case_id or ())
    selected_cases = tuple(
        case
        for case in CASES
        if not requested_case_ids or case.case_id in requested_case_ids
    )
    if requested_case_ids != frozenset(case.case_id for case in selected_cases):
        raise ValueError("unknown case ID requested")

    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")

    results: list[dict[str, Any]] = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(args.timeout_seconds),
    ) as client:
        adapter = DeepSeekToolCallingAdapter(
            http_client=client,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            max_tool_loops=args.max_tool_loops,
            max_request_attempts=args.max_attempts,
            endpoint=endpoint,
        )
        semaphore = asyncio.Semaphore(args.concurrency)

        async def evaluate(case: QuoteCase, round_number: int) -> dict[str, Any]:
            async with semaphore:
                return await _evaluate_one(
                    adapter=adapter,
                    case=case,
                    round_number=round_number,
                )

        for round_number in range(1, args.rounds + 1):
            round_items = await asyncio.gather(
                *(evaluate(case, round_number) for case in selected_cases)
            )
            for case, item in zip(selected_cases, round_items, strict=True):
                results.append(item)
                print(
                    json.dumps(
                        {
                            "round": round_number,
                            "case_id": case.case_id,
                            "proposal_pass": item.get("final_proposal_pass", False),
                            "binding_pass": item.get("server_binding_pass", False),
                            "semantic_review_count": item.get(
                                "semantic_review_count", 0
                            ),
                            "pass": item["overall_pass"],
                            "failure": item.get("failure_reason")
                            or item.get("error"),
                            "elapsed_ms": item["elapsed_ms"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )

    passed = sum(bool(item["overall_pass"]) for item in results)
    source_commit, source_dirty = _source_revision(args.source_commit)
    summary = {
        "requested_case_runs": len(results),
        "unique_cases": len(selected_cases),
        "unique_categories": len({case.category for case in selected_cases}),
        "rounds": args.rounds,
        "passed_case_runs": passed,
        "failed_case_runs": len(results) - passed,
        "strict_pass_rate": round(passed / len(results), 4),
        "all_final_items_cover_every_matter_without_merging": all(
            item.get("final_proposal_pass") is True for item in results
        ),
        "all_bound_content_is_server_owned": all(
            item.get("server_bound_no_model_added_text") is True
            for item in results
        ),
        "all_daily_only_writes_semantically_reviewed_once": all(
            item.get("semantic_review_count") == 1 for item in results
        ),
        "zero_business_write_assertions_passed": all(
            item.get("zero_business_writes") is True for item in results
        ),
        "production_handlers_called": False,
        "business_database_connected": False,
        "messages_sent": False,
    }
    artifact = {
        "schema_version": "agent2.daily-source-quote.full-adapter-live-eval.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": source_commit,
        "source_checkout_has_tracked_changes": source_dirty,
        "model": args.model,
        "adapter_path": "DeepSeekToolCallingAdapter.run_canary_turn",
        "runtime": {
            "kind": "ephemeral_in_memory_source_binder_and_no_op_recorder",
            "production_handlers_called": False,
            "business_database_connected": False,
            "business_data_written": False,
            "receipt_store_written": False,
            "messages_sent": False,
        },
        "self_check": self_check,
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
        description="Strict isolated Daily Report source-quote live evaluation"
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-tool-loops", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DEEPSEEK_API_BASE")
        or os.environ.get("LLM_BASE_URL")
        or "https://api.deepseek.com/v1",
    )
    parser.add_argument("--model", default=CANARY_MODEL_NAME)
    parser.add_argument(
        "--output",
        default="artifacts/daily_source_quote_live_3rounds.json",
    )
    parser.add_argument("--source-commit")
    parser.add_argument("--self-check-only", action="store_true")
    args = parser.parse_args()
    if args.rounds < 3:
        parser.error("rounds must be at least 3")
    if (
        args.timeout_seconds <= 0
        or args.max_attempts < 1
        or args.max_tool_loops < 1
        or args.concurrency < 1
    ):
        parser.error("timeouts, attempts, and tool loops must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
