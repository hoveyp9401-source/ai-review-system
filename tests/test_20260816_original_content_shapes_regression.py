"""Isolated regressions for the redacted Daily Report incident shapes.

The deterministic checks enter through Agent2's public canary ingress.  They
replace only the database and model network seams with in-memory adapters; no
business database, production handler, or message sender is reachable.

Run the optional real-DeepSeek gate with ``python <this-file> --live``.  That
gate uses the same public ingress and in-memory conversation runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import pytest

import run_agent2_daily_retry_live as retry_live
import test_agent2_daily_weekly_cross_turn_handoff as retry_harness
import test_agent2_tri_domain_ingress_atomicity as ingress_harness

from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
from app.agent2.tool_calling.production_daily_executor import (
    ProductionHandlerOutcome,
)


ReportField = Literal["today_work", "problems", "tomorrow_plan"]


@dataclass(frozen=True)
class ExpectedItem:
    field: ReportField
    exact_quote: str


@dataclass(frozen=True)
class ShapeCase:
    case_id: str
    user_text: str
    expected_items: tuple[ExpectedItem, ...]
    expected_empty_fields: frozenset[ReportField] = frozenset()


COMPLETE_FIVE_NONE_FOUR = ShapeCase(
    case_id="complete_five_none_four",
    user_text=(
        "今日工作：完成海滨项目合同复核；更新两份用印台账；与财务核对付款节点；"
        "起草补充协议；回复业务部门法律咨询。"
        "问题风险：暂无。"
        "明日计划：跟进海滨项目签署；整理诉讼材料；复核采购模板；"
        "向项目组反馈风险意见。"
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
)

SIX_WORK_ITEMS = ShapeCase(
    case_id="six_work_items",
    user_text=(
        "今日工作：审阅建设工程合同；核对两份用印申请；起草付款条款修改意见；"
        "参加项目风险讨论；更新诉讼案件台账；回复业务部门法律咨询。"
    ),
    expected_items=(
        ExpectedItem("today_work", "审阅建设工程合同"),
        ExpectedItem("today_work", "核对两份用印申请"),
        ExpectedItem("today_work", "起草付款条款修改意见"),
        ExpectedItem("today_work", "参加项目风险讨论"),
        ExpectedItem("today_work", "更新诉讼案件台账"),
        ExpectedItem("today_work", "回复业务部门法律咨询"),
    ),
)

LONG_RISK_STAYS_IN_RISK = ShapeCase(
    case_id="long_risk_stays_in_risk",
    user_text=(
        "今日工作：完成南港项目投标文件复核。"
        "问题风险：若投标报价超过核算上限，项目收益将无法覆盖履约成本，"
        "价格风险高不建议参与，需与投标及核算沟通后再决定是否报名。"
        "明日计划：更新合同台账。"
    ),
    expected_items=(
        ExpectedItem("today_work", "完成南港项目投标文件复核"),
        ExpectedItem(
            "problems",
            (
                "若投标报价超过核算上限，项目收益将无法覆盖履约成本，"
                "价格风险高不建议参与，需与投标及核算沟通后再决定是否报名"
            ),
        ),
        ExpectedItem("tomorrow_plan", "更新合同台账"),
    ),
)

COMMUNICATION_EXPLICITLY_TOMORROW = ShapeCase(
    case_id="communication_explicitly_tomorrow",
    user_text=(
        "今日工作：完成南港项目成本测算。"
        "问题风险：当前预算口径仍待确认。"
        "明日计划：需与投标及核算沟通后确认是否参与。"
    ),
    expected_items=(
        ExpectedItem("today_work", "完成南港项目成本测算"),
        ExpectedItem("problems", "当前预算口径仍待确认"),
        ExpectedItem(
            "tomorrow_plan",
            "需与投标及核算沟通后确认是否参与",
        ),
    ),
)

FRESH_LIVE_CASES = (
    COMPLETE_FIVE_NONE_FOUR,
    SIX_WORK_ITEMS,
    LONG_RISK_STAYS_IN_RISK,
)


def _daily_call(case: ShapeCase, *, call_id: str) -> dict[str, Any]:
    return ingress_harness._native_call(
        call_id,
        "add_daily_items",
        {
            "date_selection": "server_default",
            "items": [
                {
                    "field": item.field,
                    "content": f"模型解释-{index}",
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
        },
    )


class _StructuredDailyExecutor:
    """In-memory database seam that records the server-bound public request."""

    def __init__(self, *, session, **_kwargs) -> None:
        self._session = session

    async def add_daily_items(self, request) -> ProductionHandlerOutcome:
        self._session.attempted_calls.append(request.tool_name)
        batch = {
            "items": [
                {"field": item.field, "content": item.content}
                for item in request.arguments.items
            ],
            "acknowledged_empty_fields": sorted(
                request.arguments.acknowledged_empty_fields
            ),
        }
        self._session.working.setdefault("daily_shape_batches", []).append(batch)
        self._session.working["daily_reports"].extend(
            item["content"] for item in batch["items"]
        )
        return ingress_harness._write_outcome(
            target_type="daily_report",
            target_id=f"daily:{ingress_harness.NOW.date().isoformat()}",
        )


async def _run_scripted_shape(
    monkeypatch: pytest.MonkeyPatch,
    case: ShapeCase,
):
    monkeypatch.setattr(
        ingress_harness,
        "_ObservableDailyExecutor",
        _StructuredDailyExecutor,
    )
    draft = _daily_call(case, call_id=f"draft-{case.case_id}")
    reviewed = _daily_call(case, call_id=f"reviewed-{case.case_id}")
    return await ingress_harness._run_ingress(
        monkeypatch,
        user_text=case.user_text,
        first_calls=(draft,),
        reviewed_calls=(reviewed,),
        terminal=ingress_harness._write_terminal(),
    )


def _expected_batch(case: ShapeCase) -> dict[str, Any]:
    return {
        "items": [
            {"field": item.field, "content": item.exact_quote}
            for item in case.expected_items
        ],
        "acknowledged_empty_fields": sorted(case.expected_empty_fields),
    }


@pytest.mark.asyncio
async def test_complete_five_none_four_is_one_exact_atomic_public_ingress_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, session, _ = await _run_scripted_shape(
        monkeypatch,
        COMPLETE_FIVE_NONE_FOUR,
    )

    assert outcome.owner == "tool_call_core"
    assert outcome.actual_write is True
    assert outcome.tool_success_count == 1
    assert session.attempted_calls == ["add_daily_items"]
    assert session.committed["daily_shape_batches"] == [
        _expected_batch(COMPLETE_FIVE_NONE_FOUR)
    ]
    assert len(session.committed["receipts"]) == 1
    assert session.outer_commit_count == 1
    assert session.outer_rollback_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        SIX_WORK_ITEMS,
        LONG_RISK_STAYS_IN_RISK,
        COMMUNICATION_EXPLICITLY_TOMORROW,
    ),
    ids=lambda case: case.case_id,
)
async def test_each_original_matter_keeps_its_exact_section_in_one_public_write(
    monkeypatch: pytest.MonkeyPatch,
    case: ShapeCase,
) -> None:
    outcome, session, _ = await _run_scripted_shape(monkeypatch, case)

    assert outcome.actual_write is True
    assert outcome.tool_success_count == 1
    assert session.attempted_calls == ["add_daily_items"]
    assert session.committed["daily_shape_batches"] == [_expected_batch(case)]
    assert len(session.committed["receipts"]) == 1
    assert session.outer_commit_count == 1
    assert session.outer_rollback_count == 0


@pytest.mark.asyncio
async def test_retry_after_source_block_consumes_one_trusted_candidate_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = retry_harness._ConversationSession()
    original = "今天核对了两份用印申请并保留原审批意见"
    unsafe_rewrite = "完成两份用印申请复核"
    blocked_at = datetime(2026, 8, 15, 21, 36, tzinfo=retry_harness.SHANGHAI)
    unsafe = retry_harness._daily_call(
        "shape-blocked-draft",
        content=unsafe_rewrite,
    )
    reviewed_unsafe = json.loads(json.dumps(unsafe, ensure_ascii=False))
    reviewed_unsafe["id"] = "shape-blocked-reviewed"

    blocked, _ = await retry_harness._run_turn(
        monkeypatch,
        session,
        source_message_id="shape-blocked-source",
        user_text=original,
        now=blocked_at,
        model_messages=[
            retry_harness._assistant_tools(unsafe),
            retry_harness._assistant_tools(reviewed_unsafe),
            retry_harness._terminal(
                reply="这次没有写入日报。",
                actual_write=False,
                outcome="not_executed",
            ),
        ],
    )
    assert blocked.actual_write is False
    assert blocked.tool_blocked_count == 1
    assert session.committed["daily"] == []

    retried, _ = await retry_harness._run_turn(
        monkeypatch,
        session,
        source_message_id="shape-trusted-retry",
        user_text="再试试",
        now=blocked_at + timedelta(minutes=1),
        model_client=retry_harness._RetrySelectingModel(original_text=original),
    )

    assert retried.actual_write is True
    assert retried.tool_success_count == 1
    assert session.committed["daily"] == [original]
    assert session.committed["daily_version"] == 1
    assert len(session.committed["receipts"]) == 1

    repeated, repeated_model = await retry_harness._run_turn(
        monkeypatch,
        session,
        source_message_id="shape-repeat-after-success",
        user_text="再试试",
        now=blocked_at + timedelta(minutes=2),
        model_messages=[
            {
                "role": "assistant",
                "content": "刚才已经写入成功，没有重复写入。",
            },
            {
                "role": "assistant",
                "content": json.dumps({"decision": "keep_original"}),
            },
        ],
    )

    assert repeated.actual_write is False
    assert retry_harness._first_context(repeated_model).get(
        "retryable_daily_write"
    ) is None
    assert session.committed["daily"] == [original]
    assert session.committed["daily_version"] == 1
    assert len(session.committed["receipts"]) == 1


class _LiveRecordingClient:
    """Real network seam that records only test-case tool decisions."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.records: list[dict[str, Any]] = []

    async def post(self, endpoint: str, *, json: dict[str, Any], timeout: Any):
        context = retry_live._trusted_context(json)
        candidate = context.get("retryable_daily_write")
        candidate_id = (
            str(candidate.get("candidate_id") or "")
            if isinstance(candidate, dict)
            else ""
        )
        response = await self._client.post(endpoint, json=json, timeout=timeout)
        try:
            body = response.json()
        except (TypeError, ValueError, json.JSONDecodeError):
            body = {}
        choices = body.get("choices") if isinstance(body, dict) else None
        message = (
            choices[0].get("message")
            if isinstance(choices, list)
            and choices
            and isinstance(choices[0], dict)
            and isinstance(choices[0].get("message"), dict)
            else {}
        )
        calls = retry_live._tool_calls(message)
        self.records.append(
            {
                "request_kind": retry_live._request_kind(json),
                "http_status": response.status_code,
                "candidate_present": bool(candidate_id),
                "weekly_write_tool_offered": any(
                    isinstance(tool, dict)
                    and isinstance(tool.get("function"), dict)
                    and tool["function"].get("name") == "apply_next_weekly_plan"
                    for tool in json.get("tools") or ()
                ),
                "response_calls": [
                    {
                        "name": call["name"],
                        "date_selection": call["arguments"].get(
                            "date_selection"
                        ),
                        "retry_candidate_matches_context": bool(
                            candidate_id
                            and call["arguments"].get("retry_candidate_id")
                            == candidate_id
                        ),
                        "item_fields": [
                            str(item.get("field") or "")
                            for item in call["arguments"].get("items") or ()
                            if isinstance(item, dict)
                        ],
                        "item_contents": [
                            str(item.get("content") or "")
                            for item in call["arguments"].get("items") or ()
                            if isinstance(item, dict)
                        ],
                        "item_exact_quotes": [
                            str((item.get("source_evidence") or {}).get(
                                "exact_quote"
                            ) or "")
                            for item in call["arguments"].get("items") or ()
                            if isinstance(item, dict)
                        ],
                        "acknowledged_empty_fields": sorted(
                            str(value)
                            for value in call["arguments"].get(
                                "acknowledged_empty_fields"
                            )
                            or ()
                        ),
                        "empty_evidence_fields": sorted(
                            str(item.get("field") or "")
                            for item in call["arguments"].get(
                                "empty_field_evidence"
                            )
                            or ()
                            if isinstance(item, dict)
                        ),
                    }
                    for call in calls
                ],
            }
        )
        return response

    def mark(self) -> int:
        return len(self.records)

    def since(self, mark: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.records[mark:]]


def _score_live_daily_call(
    case: ShapeCase,
    call: dict[str, Any],
    persisted: list[str],
) -> list[str]:
    errors: list[str] = []
    fields = list(call.get("item_fields") or ())
    quotes = list(call.get("item_exact_quotes") or ())
    expected = [(item.field, item.exact_quote) for item in case.expected_items]
    actual = list(zip(fields, quotes, strict=False))
    if len(actual) != len(expected):
        errors.append(
            f"expected {len(expected)} Daily items, got {len(actual)}"
        )
    matched_indexes: list[int] = []
    for expected_field, core_quote in expected:
        matches = [
            index
            for index, (field, quote) in enumerate(actual)
            if field == expected_field and core_quote in quote
        ]
        if len(matches) != 1:
            errors.append(
                "one original matter was missing, merged, or moved: "
                f"{expected_field}={core_quote!r}, matches={matches}"
            )
        else:
            matched_indexes.append(matches[0])
    if len(set(matched_indexes)) != len(matched_indexes):
        errors.append("one model quote merged multiple independent matters")
    if any(not quote or quote not in case.user_text for quote in quotes):
        errors.append("one persisted quote was not contiguous original text")
    if persisted != quotes:
        errors.append("server-bound contents differ from reviewed exact quotes")
    expected_empty = sorted(case.expected_empty_fields)
    if list(call.get("acknowledged_empty_fields") or ()) != expected_empty:
        errors.append("explicit empty fields were not preserved")
    if list(call.get("empty_evidence_fields") or ()) != expected_empty:
        errors.append("explicit empty-field evidence was not preserved")
    return errors


async def _evaluate_live_shape(
    monkeypatch: pytest.MonkeyPatch,
    client: _LiveRecordingClient,
    case: ShapeCase,
    *,
    offset_minutes: int,
) -> dict[str, Any]:
    session = retry_harness._ConversationSession()
    now = datetime(
        2026,
        8,
        16,
        19,
        0,
        tzinfo=retry_harness.SHANGHAI,
    ) + timedelta(minutes=offset_minutes)
    mark = client.mark()
    try:
        outcome, _ = await retry_harness._run_turn(
            monkeypatch,
            session,
            source_message_id=f"live-shape-{case.case_id}",
            user_text=case.user_text,
            now=now,
            model_client=client,
        )
    except Exception as exc:  # pragma: no cover - live diagnostic path
        return {
            "category": case.case_id,
            "pass": False,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }

    records = client.since(mark)
    main_calls = retry_live._calls_by_kind(records, "main_agent")
    review_calls = retry_live._calls_by_kind(
        records,
        "independent_semantic_review",
    )
    errors: list[str] = []
    selected: dict[str, dict[str, Any]] = {}
    for label, calls in (("main", main_calls), ("review", review_calls)):
        daily = [call for call in calls if call.get("name") == "add_daily_items"]
        if len(daily) != 1:
            errors.append(f"{label} produced {len(daily)} Daily write calls")
        else:
            selected[label] = daily[0]
            if daily[0].get("date_selection") != "server_default":
                errors.append(f"{label} did not use the server-default report date")

    persisted = list(session.committed["daily"])
    reviewed = selected.get("review")
    if reviewed is not None:
        errors.extend(_score_live_daily_call(case, reviewed, persisted))
    if (
        not outcome.actual_write
        or outcome.tool_success_count != 1
        or session.committed["daily_version"] != 1
        or len(session.committed["receipts"]) != 1
    ):
        errors.append("public ingress did not commit exactly one atomic Daily write")
    if session.committed["weekly_plan"] is not None:
        errors.append("Daily content crossed into the weekly-plan domain")
    result = {
        "category": case.case_id,
        "pass": not errors,
        "errors": errors,
        "daily_item_count": len(persisted),
        "daily_version": session.committed["daily_version"],
        "tool_success_count": outcome.tool_success_count,
        "model_request_count": len(records),
    }
    if errors:
        result["diagnostic"] = {
            "outcome": {
                "owner": outcome.owner,
                "reason": outcome.reason,
                "model_result_status": outcome.model_result_status,
                "model_call_count": outcome.model_call_count,
                "model_request_attempt_count": (
                    outcome.model_request_attempt_count
                ),
                "tool_blocked_count": outcome.tool_blocked_count,
                "tool_failure_count": outcome.tool_failure_count,
                "tool_receipt_count": outcome.tool_receipt_count,
                "user_visible_result": outcome.user_visible_result,
            },
            "model_trace": records,
        }
    return result


async def _run_live(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"missing credential in {args.api_key_env}")
    base_url = args.base_url.rstrip("/")
    endpoint = base_url + "/chat/completions"
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")

    base_settings = vars(retry_harness._settings()).copy()
    base_settings["llm_base_url"] = base_url
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        retry_harness,
        "_settings",
        lambda: SimpleNamespace(**base_settings),
    )
    results: list[dict[str, Any]] = []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(args.timeout_seconds),
        ) as raw_client:
            client = _LiveRecordingClient(raw_client)
            selected_categories = set(args.category or ())
            selected_fresh_cases = tuple(
                case
                for case in FRESH_LIVE_CASES
                if not selected_categories or case.case_id in selected_categories
            )
            for index, case in enumerate(selected_fresh_cases, start=1):
                result = await _evaluate_live_shape(
                    monkeypatch,
                    client,
                    case,
                    offset_minutes=index * 5,
                )
                results.append(result)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)

            if not selected_categories or "retry_lifecycle" in selected_categories:
                retry_result = await retry_live._evaluate_lifecycle(
                    monkeypatch,
                    client,
                    round_number=1,
                )
                results.append(retry_result)
                print(
                    json.dumps(
                        {
                            "category": retry_result["category"],
                            "pass": retry_result["pass"],
                            "errors": retry_result["errors"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )
    finally:
        monkeypatch.undo()

    passed = sum(bool(result.get("pass")) for result in results)
    summary = {
        "model": CANARY_MODEL_NAME,
        "case_runs": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "all_passed": passed == len(results),
        "public_agent2_ingress": True,
        "business_database_connected": False,
        "production_handlers_called": False,
        "messages_sent": False,
        "business_data_written": False,
        "in_memory_harness_only": True,
    }
    print(json.dumps({"summary": summary}, ensure_ascii=False, sort_keys=True))
    return 0 if summary["all_passed"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run isolated real-model Daily content-shape regressions"
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--api-key-env",
        default="DEEPSEEK_API_KEY",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DEEPSEEK_API_BASE") or "https://api.deepseek.com/v1",
    )
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--category",
        action="append",
        choices=(
            "complete_five_none_four",
            "six_work_items",
            "long_risk_stays_in_risk",
            "retry_lifecycle",
        ),
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("pass --live to run the isolated real-model gate")
    return asyncio.run(_run_live(args))


if __name__ == "__main__":
    raise SystemExit(main())
