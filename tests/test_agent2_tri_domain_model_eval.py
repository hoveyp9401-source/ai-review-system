from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_system_prompt,
)
from app.agent2.tool_calling.contracts import ExecutionMode
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    deepseek_tool_schemas,
    validate_tool_arguments,
)


DAILY_REPORT_ID = "a9bb2812-89ed-4d2c-b2c4-ed2476744963"
PERIODIC_REPORT_ID = "b9bb2812-89ed-4d2c-b2c4-ed2476744963"
WEEKLY_PLAN_ID = "4fa86875-6f8a-477b-b324-8602010d809b"
DAILY_VERSION = 2
PERIODIC_VERSION = 3
WEEKLY_PLAN_VERSION = 4


@dataclass(frozen=True)
class TriDomainModelCase:
    case_id: str
    category: str
    user_text: str
    expected_tools: frozenset[str]
    expected_daily_fields: frozenset[str] = frozenset()
    expected_periodic_fields: frozenset[str] = frozenset()
    expected_weekly_dates: frozenset[str] = frozenset()
    requires_clarification: bool = False
    note: str = ""


MODEL_CASES = (
    TriDomainModelCase(
        case_id="weekly_report_open",
        category="pure_periodic_weekly_report",
        user_text="我想写本周周报。",
        expected_tools=frozenset({"query_current_weekly_report"}),
        note="Entering the retrospective report must not open a Weekly Work Plan.",
    ),
    TriDomainModelCase(
        case_id="weekly_report_three_sections",
        category="pure_periodic_weekly_report",
        user_text=(
            "补充本周周报：本周完成合同复核；风险是付款材料还没齐；"
            "下周计划是周一继续向财务催材料。"
        ),
        expected_tools=frozenset({"apply_current_weekly_report"}),
        expected_periodic_fields=frozenset(
            {"accomplishments", "risks", "next_plan"}
        ),
        note=(
            "A next-plan section explicitly framed inside 本周周报 remains in the "
            "retrospective Weekly Report; it is not a dated Weekly Work Plan item."
        ),
    ),
    TriDomainModelCase(
        case_id="weekly_report_submit",
        category="pure_periodic_weekly_report",
        user_text="我已经看过完整预览，确认提交本周周报。",
        expected_tools=frozenset({"submit_current_weekly_report"}),
    ),
    TriDomainModelCase(
        case_id="daily_mentions_weekly_report_summary",
        category="daily_phrase_collision",
        user_text="写进今天日报：完成本周周报汇总。",
        expected_tools=frozenset({"add_daily_items"}),
        expected_daily_fields=frozenset({"today_work"}),
        note=(
            "完成本周周报汇总 is today's work content, not an instruction to edit "
            "the retrospective Weekly Report."
        ),
    ),
    TriDomainModelCase(
        case_id="next_friday_plan",
        category="pure_weekly_work_plan",
        user_text="下周五计划向负责人甲汇报案件进展。",
        expected_tools=frozenset({"apply_next_weekly_plan"}),
        expected_weekly_dates=frozenset({"2026-08-21"}),
    ),
    TriDomainModelCase(
        case_id="daily_and_weekly_plan",
        category="two_domain_daily_plan",
        user_text="写进今天日报：完成合同初稿；下周五计划和业务确认条款。",
        expected_tools=frozenset(
            {"add_daily_items", "apply_next_weekly_plan"}
        ),
        expected_daily_fields=frozenset({"today_work"}),
        expected_weekly_dates=frozenset({"2026-08-21"}),
    ),
    TriDomainModelCase(
        case_id="weekly_report_and_weekly_plan",
        category="two_domain_report_plan",
        user_text=(
            "补充本周周报：本周完成合同复核；另外下周五工作计划是"
            "向负责人甲汇报案件进展。"
        ),
        expected_tools=frozenset(
            {"apply_current_weekly_report", "apply_next_weekly_plan"}
        ),
        expected_periodic_fields=frozenset({"accomplishments"}),
        expected_weekly_dates=frozenset({"2026-08-21"}),
    ),
    TriDomainModelCase(
        case_id="all_three_domains",
        category="three_domain",
        user_text=(
            "写进今天日报：今天完成台账核对；补充本周周报：本周完成合同复核；"
            "下周五工作计划向负责人甲汇报案件进展。"
        ),
        expected_tools=frozenset(
            {
                "add_daily_items",
                "apply_current_weekly_report",
                "apply_next_weekly_plan",
            }
        ),
        expected_daily_fields=frozenset({"today_work"}),
        expected_periodic_fields=frozenset({"accomplishments"}),
        expected_weekly_dates=frozenset({"2026-08-21"}),
    ),
    TriDomainModelCase(
        case_id="bare_friday_requires_clarification",
        category="friday_ambiguity",
        user_text="周五做A。",
        expected_tools=frozenset(),
        requires_clarification=True,
        note=(
            "On Friday, a bare 周五 has neither completion tense nor next-week "
            "scope, so the model must ask rather than guess daily versus plan."
        ),
    ),
)


TRI_DOMAIN_TOOL_NAMES = frozenset(
    {
        "query_today_report",
        "add_daily_items",
        "confirm_report",
        "query_current_weekly_report",
        "apply_current_weekly_report",
        "submit_current_weekly_report",
        "query_next_weekly_plan",
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
    }
)


def _day_payload(day: date) -> dict[str, Any]:
    return {
        "day_id": f"weekly-day-{day.isoformat()}",
        "plan_date": day.isoformat(),
        "state": "unfilled",
        "items": [],
    }


def _weekly_plan_payload() -> dict[str, Any]:
    monday = date(2026, 8, 17)
    return {
        "plan_id": WEEKLY_PLAN_ID,
        "batch_id": "weekly-batch-2026-08-17",
        "target_week_start": monday.isoformat(),
        "version": WEEKLY_PLAN_VERSION,
        "status": "collecting",
        "days": [_day_payload(monday + timedelta(days=offset)) for offset in range(6)],
        "suggestions": [],
        "roles": ["active_collection", "natural_next"],
        "natural_next_for_message_indexes": [1],
        "provenance": "server_weekly_plan",
    }


def trusted_context_payload() -> dict[str, Any]:
    now = datetime(2026, 8, 14, 17, 30, tzinfo=timezone(timedelta(hours=8)))
    weekly_plan = _weekly_plan_payload()
    return {
        "current_time": now.isoformat(),
        "timezone": "Asia/Shanghai",
        "daily_reporting_context": {
            "local_date": "2026-08-14",
            "local_time": now.isoformat(),
            "default_report_date": "2026-08-14",
            "morning_cutoff": "09:00",
            "default_is_prior_not_lock": True,
            "safe_semantic_date_candidates": ["2026-08-14"],
        },
        "today_report": {
            "report_id": DAILY_REPORT_ID,
            "report_date": "2026-08-14",
            "version": DAILY_VERSION,
            "status": "collecting",
            "fields": {
                "today_work": [],
                "problems": [],
                "tomorrow_plan": [],
            },
            "acknowledged_empty_fields": [],
            "provenance": "trusted_context",
        },
        "current_weekly_report": {
            "report_id": PERIODIC_REPORT_ID,
            "report_type": "weekly",
            "period_key": "2026-W33",
            "version": PERIODIC_VERSION,
            "status": "collecting",
            "sections": {
                "accomplishments": [],
                "risks": [],
                "next_plan": [],
                "metrics": [],
            },
        },
        "weekly_plan": weekly_plan,
        "weekly_plan_targets": [weekly_plan],
        "historical_reports": [],
        "active_clear_pending": None,
        "recent_messages": [],
        "recent_operations": [],
        "resource_namespace": "agent2.tool_calling.canary.v1",
        "runtime_identity": {
            "provider_name": "DeepSeek",
            "model_name": CANARY_MODEL_NAME,
            "provenance": "server_runtime",
        },
        "authenticated_user": {
            "display_name": "模型评测用户",
            "provenance": "server_identity",
        },
    }


def _trusted_context_for_case(case: TriDomainModelCase) -> dict[str, Any]:
    context = trusted_context_payload()
    if case.case_id == "weekly_report_submit":
        context["current_weekly_report"] = {
            **context["current_weekly_report"],
            "sections": {
                "accomplishments": [
                    {
                        "item_id": "periodic-accomplishment-1",
                        "content": "完成合同复核",
                    }
                ],
                "risks": [
                    {
                        "item_id": "periodic-risk-1",
                        "content": "付款材料还没齐",
                    }
                ],
                "next_plan": [
                    {
                        "item_id": "periodic-next-plan-1",
                        "content": "下周继续向财务催材料",
                    }
                ],
                "metrics": [],
            },
        }
    return context


def request_payload(case: TriDomainModelCase) -> dict[str, Any]:
    return {
        "model": CANARY_MODEL_NAME,
        "messages": [
            {"role": "system", "content": canary_system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "user_message": case.user_text,
                        "trusted_context": _trusted_context_for_case(case),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ],
        "temperature": 0,
        "tools": deepseek_tool_schemas(
            TRI_DOMAIN_TOOL_NAMES,
            mode=ExecutionMode.CANARY_EXECUTE,
        ),
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


def evaluation_bundle(case_ids: set[str] | None = None) -> dict[str, Any]:
    selected = [
        case for case in MODEL_CASES
        if case_ids is None or case.case_id in case_ids
    ]
    prompt_lower = canary_system_prompt().lower()
    explicit_periodic_tokens = (
        "query_current_weekly_report",
        "apply_current_weekly_report",
        "submit_current_weekly_report",
        "accomplishments",
        "risks",
        "next_plan",
        "metrics",
    )
    return {
        "schema_version": "agent2.tri-domain.model-eval.v1",
        "zero_write": True,
        "model_layer_only": True,
        "prompt_audit": {
            "has_daily_weekly_plan_boundary": (
                "weekly work plan" in prompt_lower
                and "weekly report is a current-week review" in prompt_lower
            ),
            "explicit_periodic_guidance_tokens": {
                token: token in prompt_lower for token in explicit_periodic_tokens
            },
        },
        "cases": [
            {
                "case_id": case.case_id,
                "category": case.category,
                "user_text": case.user_text,
                "expected_tools": sorted(case.expected_tools),
                "expected_daily_fields": sorted(case.expected_daily_fields),
                "expected_periodic_fields": sorted(case.expected_periodic_fields),
                "expected_weekly_dates": sorted(case.expected_weekly_dates),
                "requires_clarification": case.requires_clarification,
                "note": case.note,
                "request": request_payload(case),
            }
            for case in selected
        ],
    }


def _actual_tool_names(tool_calls: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(str(item.get("name") or "") for item in tool_calls)


def score_selection(
    case: TriDomainModelCase,
    *,
    tool_calls: list[dict[str, Any]],
    assistant_content: str | None,
) -> tuple[bool, str]:
    actual = _actual_tool_names(tool_calls)
    if actual != case.expected_tools:
        return False, f"unexpected tools: {sorted(actual)}"
    if not case.requires_clarification:
        return True, "matched"
    reply = (assistant_content or "").strip()
    if not reply:
        return False, "clarification case returned no question"
    if not any(token in reply for token in ("今天", "下周", "日报", "工作计划")):
        return False, "clarification did not distinguish daily versus weekly plan"
    return True, "matched"


def score_parameters(
    case: TriDomainModelCase,
    *,
    tool_calls: list[dict[str, Any]],
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    source = CurrentTurnSource((case.user_text,))
    calls_by_name: dict[str, list[dict[str, Any]]] = {}
    for index, call in enumerate(tool_calls):
        name = str(call.get("name") or "")
        raw_arguments = call.get("arguments")
        if name not in TRI_DOMAIN_TOOL_NAMES:
            errors.append(f"call[{index}] unknown or out-of-scope tool: {name}")
            continue
        if not isinstance(raw_arguments, dict):
            errors.append(f"call[{index}] arguments are not an object")
            continue
        try:
            arguments = validate_tool_arguments(name, raw_arguments)
            source.validate_tool_arguments(name, arguments)
        except Exception as exc:  # the artifact must retain the exact contract failure
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        calls_by_name.setdefault(name, []).append(arguments)

    for name, calls in calls_by_name.items():
        if len(calls) > 1:
            errors.append(f"{name}: duplicate calls are not one atomic domain batch")

    daily_calls = calls_by_name.get("add_daily_items", [])
    if daily_calls:
        fields = frozenset(
            item["field"] for item in daily_calls[0].get("items", [])
        )
        if fields != case.expected_daily_fields:
            errors.append(
                f"add_daily_items: expected fields {sorted(case.expected_daily_fields)}, "
                f"got {sorted(fields)}"
            )
        if daily_calls[0].get("date_selection") == "trusted_report" and (
            daily_calls[0].get("report_id") != DAILY_REPORT_ID
            or daily_calls[0].get("expected_version") != DAILY_VERSION
        ):
            errors.append("add_daily_items: wrong trusted daily report binding")

    periodic_calls = calls_by_name.get("apply_current_weekly_report", [])
    if periodic_calls:
        periodic = periodic_calls[0]
        if (
            periodic.get("report_id") != PERIODIC_REPORT_ID
            or periodic.get("expected_version") != PERIODIC_VERSION
        ):
            errors.append("apply_current_weekly_report: wrong trusted report binding")
        fields = frozenset(
            item["field"]
            for item in periodic.get("operations", [])
            if item.get("operation") == "append"
        )
        if fields != case.expected_periodic_fields:
            errors.append(
                "apply_current_weekly_report: expected fields "
                f"{sorted(case.expected_periodic_fields)}, got {sorted(fields)}"
            )

    periodic_submit_calls = calls_by_name.get("submit_current_weekly_report", [])
    if periodic_submit_calls:
        submit = periodic_submit_calls[0]
        if (
            submit.get("report_id") != PERIODIC_REPORT_ID
            or submit.get("expected_version") != PERIODIC_VERSION
        ):
            errors.append("submit_current_weekly_report: wrong trusted report binding")

    weekly_calls = calls_by_name.get("apply_next_weekly_plan", [])
    if weekly_calls:
        weekly = weekly_calls[0]
        if (
            weekly.get("plan_id") != WEEKLY_PLAN_ID
            or weekly.get("expected_version") != WEEKLY_PLAN_VERSION
        ):
            errors.append("apply_next_weekly_plan: wrong trusted plan binding")
        dates = frozenset(
            str(item["plan_date"])
            for item in weekly.get("operations", [])
            if item.get("operation") in {"add", "set_day_empty"}
        )
        if dates != case.expected_weekly_dates:
            errors.append(
                f"apply_next_weekly_plan: expected dates {sorted(case.expected_weekly_dates)}, "
                f"got {sorted(dates)}"
            )

    return not errors, tuple(errors)


def test_tri_domain_corpus_covers_every_required_collision() -> None:
    assert len({case.case_id for case in MODEL_CASES}) == len(MODEL_CASES)
    assert {
        "weekly_report_open",
        "weekly_report_three_sections",
        "weekly_report_submit",
        "daily_mentions_weekly_report_summary",
        "next_friday_plan",
        "daily_and_weekly_plan",
        "weekly_report_and_weekly_plan",
        "all_three_domains",
        "bare_friday_requires_clarification",
    } == {case.case_id for case in MODEL_CASES}


def test_tri_domain_requests_use_current_prompt_and_zero_execution_schema() -> None:
    bundle = evaluation_bundle()
    assert bundle["zero_write"] is True
    assert bundle["model_layer_only"] is True
    assert bundle["prompt_audit"]["has_daily_weekly_plan_boundary"] is True
    for item in bundle["cases"]:
        request = item["request"]
        assert request["model"] == CANARY_MODEL_NAME
        assert request["messages"][0]["content"] == canary_system_prompt()
        assert "tool_choice" not in request
        assert {
            tool["function"]["name"] for tool in request["tools"]
        } == TRI_DOMAIN_TOOL_NAMES


def test_current_canary_schemas_expose_three_distinct_domains() -> None:
    schemas = {
        item["function"]["name"]: item["function"]
        for item in deepseek_tool_schemas(
            TRI_DOMAIN_TOOL_NAMES,
            mode=ExecutionMode.CANARY_EXECUTE,
        )
    }
    assert set(schemas) == TRI_DOMAIN_TOOL_NAMES
    assert "retrospective Weekly Report" in schemas[
        "apply_current_weekly_report"
    ]["description"]
    assert "weekly-plan target" in schemas["apply_next_weekly_plan"][
        "description"
    ]
    assert TOOL_REGISTRY[
        "add_daily_items"
    ].transaction_target_policy == "resolved_report"
    assert TOOL_REGISTRY[
        "apply_current_weekly_report"
    ].transaction_target_policy == "periodic_report"
    assert TOOL_REGISTRY[
        "apply_next_weekly_plan"
    ].transaction_target_policy == "weekly_plan"


def test_trusted_context_keeps_three_records_and_dates_separate() -> None:
    context = trusted_context_payload()
    assert context["today_report"]["report_date"] == "2026-08-14"
    assert context["current_weekly_report"]["period_key"] == "2026-W33"
    assert context["weekly_plan"]["target_week_start"] == "2026-08-17"
    assert context["weekly_plan"]["days"][4]["plan_date"] == "2026-08-21"
    assert len(
        {
            context["today_report"]["report_id"],
            context["current_weekly_report"]["report_id"],
            context["weekly_plan"]["plan_id"],
        }
    ) == 3


def test_scoring_rejects_cross_domain_selection_and_illegal_parameters() -> None:
    collision = next(
        case for case in MODEL_CASES
        if case.case_id == "daily_mentions_weekly_report_summary"
    )
    selection_ok, _ = score_selection(
        collision,
        tool_calls=[{"name": "apply_current_weekly_report", "arguments": {}}],
        assistant_content=None,
    )
    parameter_ok, errors = score_parameters(
        next(case for case in MODEL_CASES if case.case_id == "next_friday_plan"),
        tool_calls=[
            {
                "name": "apply_next_weekly_plan",
                "arguments": {
                    "plan_id": WEEKLY_PLAN_ID,
                    "expected_version": WEEKLY_PLAN_VERSION,
                    "operations": [
                        {
                            "operation_id": "add-1",
                            "operation": "add",
                            "plan_date": "2026-08-22",
                            "content": "向负责人甲汇报案件进展",
                            "source_evidence": {
                                "source_message_index": 1,
                                "exact_clause_quote": "下周五计划向负责人甲汇报案件进展。",
                            },
                        }
                    ],
                },
            }
        ],
    )
    assert selection_ok is False
    assert parameter_ok is False
    assert any("expected dates" in error for error in errors)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--emit-all", action="store_true")
    args = parser.parse_args()
    if not args.emit_all:
        parser.error("choose --emit-all")
    print(json.dumps(evaluation_bundle(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
