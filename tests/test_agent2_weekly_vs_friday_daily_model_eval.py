from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from typing import Any

from app.agent2.tool_calling.canary_config import (
    CANARY_MODEL_NAME,
    canary_system_prompt,
)
from app.agent2.tool_calling.contracts import ExecutionMode
from app.agent2.tool_calling.registry import deepseek_tool_schemas


@dataclass(frozen=True)
class WeeklyDailyModelCase:
    case_id: str
    category: str
    user_text: str
    expected_tool_sets: tuple[frozenset[str], ...]
    context_variant: str = "base"
    recent_messages: tuple[tuple[str, str], ...] = ()
    expected_weekly_operations: frozenset[str] | None = None
    note: str = ""


def _sets(*values: tuple[str, ...]) -> tuple[frozenset[str], ...]:
    return tuple(frozenset(value) for value in values)


MODEL_CASES = (
    WeeklyDailyModelCase(
        "daily_01",
        "pure_daily",
        "今天完成了华东区合同模板复核。",
        _sets(("add_daily_items",)),
    ),
    WeeklyDailyModelCase(
        "daily_02",
        "pure_daily",
        "今天完成保证金台账核对，目前没有问题，明日计划跟财务确认付款节点。",
        _sets(("add_daily_items",)),
    ),
    WeeklyDailyModelCase(
        "daily_03",
        "pure_daily",
        "今天没完成合同评审。",
        _sets(()),
        note="A negated event is neither completed work nor a weekly-plan commitment.",
    ),
    WeeklyDailyModelCase(
        "daily_04",
        "pure_daily",
        "今天周五，我完成了案件复盘并把纪要发给项目组。",
        _sets(("add_daily_items",)),
    ),
    WeeklyDailyModelCase(
        "weekly_01",
        "pure_weekly",
        "下周一整理XX案件材料，周二去上海开庭，周三优化合同评审技能，周四跟进XX项目，周五汇报中台，周六暂无安排。",
        _sets(("apply_next_weekly_plan",)),
    ),
    WeeklyDailyModelCase(
        "weekly_02",
        "pure_weekly",
        "下周二去上海参加庭审。",
        _sets(("apply_next_weekly_plan",)),
    ),
    WeeklyDailyModelCase(
        "weekly_03",
        "suggestion_routing",
        "下周继续完善合同评审规则。",
        _sets(("apply_next_weekly_plan",)),
        expected_weekly_operations=frozenset({"capture_suggestion"}),
        note="The operation must be capture_suggestion, never a daily tomorrow_plan.",
    ),
    WeeklyDailyModelCase(
        "weekly_04",
        "suggestion_routing",
        "下周二或者周三找一天整理案件证据。",
        _sets(("apply_next_weekly_plan",), ()),
        expected_weekly_operations=frozenset({"capture_suggestion"}),
        note="Accept capture_suggestion or a concise clarification; never guess one formal day.",
    ),
    WeeklyDailyModelCase(
        "weekly_05",
        "pure_weekly",
        "给我看看下周工作计划。",
        _sets(("query_next_weekly_plan",), ()),
        note=(
            "A direct answer from the already injected trusted weekly snapshot "
            "is equivalent to a redundant read call and is preferred when complete."
        ),
    ),
    WeeklyDailyModelCase(
        "dual_01",
        "dual_intent",
        "今天完成了合同模板初稿；下周三继续和业务确认条款。",
        _sets(("add_daily_items", "apply_next_weekly_plan")),
    ),
    WeeklyDailyModelCase(
        "dual_02",
        "dual_intent",
        "今天跟财务核对了保证金，明日计划补齐台账；下周五向负责人甲汇报案件进展。",
        _sets(("add_daily_items", "apply_next_weekly_plan")),
    ),
    WeeklyDailyModelCase(
        "dual_03",
        "friday_vs_next_friday",
        "周五的工作：完成案件复盘；下周五向负责人甲汇报案件进展。",
        _sets(("add_daily_items", "apply_next_weekly_plan")),
        note="Trusted local date is Friday, so the two Friday expressions target different domains.",
    ),
    WeeklyDailyModelCase(
        "dual_04",
        "dual_intent",
        "今天完成了制度初稿，下周继续征求业务意见。",
        _sets(("add_daily_items", "apply_next_weekly_plan")),
        expected_weekly_operations=frozenset({"capture_suggestion"}),
        note="The weekly operation must remain an undated suggestion.",
    ),
    WeeklyDailyModelCase(
        "tomorrow_01",
        "tomorrow_plan",
        "明日计划整理本周案件进展。",
        _sets(("add_daily_items",)),
    ),
    WeeklyDailyModelCase(
        "tomorrow_02",
        "tomorrow_plan",
        "明天整理开庭材料，下周一和律师复盘庭审。",
        _sets(("add_daily_items", "apply_next_weekly_plan")),
        note="On Friday, tomorrow is Saturday in the daily report; next Monday is the weekly plan.",
    ),
    WeeklyDailyModelCase(
        "friday_01",
        "friday_vs_next_friday",
        "下周五完成中台阶段汇报。",
        _sets(("apply_next_weekly_plan",)),
    ),
    WeeklyDailyModelCase(
        "friday_02",
        "friday_vs_next_friday",
        "周五汇报案件进展。",
        _sets(()),
        note="Without tense or week scope, the model should clarify instead of guessing today or next Friday.",
    ),
    WeeklyDailyModelCase(
        "confirm_01",
        "context_confirmation",
        "确认提交这份下周计划。",
        _sets(("submit_next_weekly_plan",)),
        context_variant="weekly_ready",
        recent_messages=(("assistant", "这是你的下周工作计划完整预览，确认后我再提交。"),),
    ),
    WeeklyDailyModelCase(
        "confirm_02",
        "context_confirmation",
        "确认提交这份日报。",
        _sets(("confirm_report",)),
        context_variant="daily_ready",
        recent_messages=(("assistant", "这是你今天的工作日报预览，确认后我再提交。"),),
    ),
    WeeklyDailyModelCase(
        "confirm_03",
        "context_confirmation",
        "确认提交。",
        _sets(()),
        context_variant="both_ready",
        recent_messages=(
            ("assistant", "今天的工作日报和下周工作计划都已经形成预览，你想提交哪一份？"),
        ),
        note="Both objects are live; an unqualified confirmation must be clarified.",
    ),
    WeeklyDailyModelCase(
        "modify_01",
        "context_modification",
        "把下周二开庭挪到下周三。",
        _sets(("apply_next_weekly_plan",)),
        context_variant="weekly_item",
    ),
    WeeklyDailyModelCase(
        "suggestion_01",
        "suggestion_routing",
        "放到下周四。",
        _sets(("apply_next_weekly_plan",)),
        context_variant="weekly_suggestion",
        recent_messages=(("assistant", "你本周提到过完善合同评审规则，要不要放到下周某一天？"),),
        expected_weekly_operations=frozenset({"accept_suggestion"}),
        note="The operation must accept the trusted suggestion into Thursday.",
    ),
    WeeklyDailyModelCase(
        "suggestion_02",
        "suggestion_routing",
        "这个已经做完了，不要放进下周计划。",
        _sets(("apply_next_weekly_plan",)),
        context_variant="weekly_suggestion",
        recent_messages=(("assistant", "你本周提到过完善合同评审规则，要不要放到下周某一天？"),),
        expected_weekly_operations=frozenset({"reject_suggestion"}),
        note="The operation must reject the trusted suggestion, not record completion in the plan.",
    ),
    WeeklyDailyModelCase(
        "guard_01",
        "attribution_guard",
        "同事说他下周二要去上海开庭。",
        _sets(()),
    ),
    WeeklyDailyModelCase(
        "guard_02",
        "conditional_guard",
        "如果下周二收到材料，我就复核合同。",
        _sets(()),
    ),
)


_TOOL_NAMES = frozenset(
    {
        "query_today_report",
        "query_next_weekly_plan",
        "add_daily_items",
        "edit_daily_items",
        "delete_daily_items",
        "move_daily_items",
        "confirm_report",
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
    }
)


def _day_payload(day: date, state: str = "unfilled", items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "day_id": f"weekly-day-{day.isoformat()}",
        "plan_date": day.isoformat(),
        "state": state,
        "items": items or [],
    }


def _weekly_payload(variant: str) -> dict[str, Any]:
    monday = date(2026, 8, 17)
    days = [_day_payload(monday + timedelta(days=offset)) for offset in range(6)]
    status = "collecting"
    suggestions: list[dict[str, Any]] = []
    if variant in {"weekly_ready", "both_ready"}:
        status = "pending_confirmation"
        days = [
            _day_payload(
                monday + timedelta(days=offset),
                "planned" if offset < 5 else "explicitly_empty",
                (
                    [
                        {
                            "item_id": f"weekly-item-{offset + 1}",
                            "original_text": f"下周{offset + 1}的已确认计划",
                            "source": "manual",
                        }
                    ]
                    if offset < 5
                    else []
                ),
            )
            for offset in range(6)
        ]
    if variant == "weekly_item":
        days[1] = _day_payload(
            date(2026, 8, 18),
            "planned",
            [
                {
                    "item_id": "weekly-item-hearing",
                    "original_text": "下周二开庭",
                    "source": "manual",
                }
            ],
        )
    if variant == "weekly_suggestion":
        suggestions = [
            {
                "suggestion_id": "weekly-suggestion-contract-review",
                "status": "available",
                "source_kind": "user_original_message",
                "source_ref": "source-message-prior",
                "source_version": "v1",
                "evidence_sha256": "a" * 64,
                "evidence_excerpt": "下周继续完善合同评审规则",
                "prompt": "你本周提到过完善合同评审规则，我暂时没找到后续记录。要不要把它放到下周某一天？",
                "is_formal_plan_item": False,
            }
        ]
    return {
        "plan_id": "4fa86875-6f8a-477b-b324-8602010d809b",
        "batch_id": "weekly-batch-2026-08-17",
        "target_week_start": monday.isoformat(),
        "version": 4,
        "status": status,
        "days": days,
        "suggestions": suggestions,
        "provenance": "server_weekly_plan",
    }


def _daily_payload(variant: str) -> dict[str, Any]:
    ready = variant in {"daily_ready", "both_ready"}
    return {
        "report_id": "a9bb2812-89ed-4d2c-b2c4-ed2476744963",
        "report_date": "2026-08-14",
        "version": 7 if ready else 2,
        "status": "pending_confirmation" if ready else "collecting",
        "fields": {
            "today_work": (
                [
                    {
                        "item_id": "daily-item-work",
                        "content": "完成案件复盘",
                        "provenance": "trusted_context",
                    }
                ]
                if ready
                else []
            ),
            "problems": [],
            "tomorrow_plan": (
                [
                    {
                        "item_id": "daily-item-plan",
                        "content": "整理案件材料",
                        "provenance": "trusted_context",
                    }
                ]
                if ready
                else []
            ),
        },
        "acknowledged_empty_fields": ["problems"] if ready else [],
        "provenance": "trusted_context",
    }


def trusted_context_payload(case: WeeklyDailyModelCase) -> dict[str, Any]:
    now = datetime(2026, 8, 14, 17, 30, tzinfo=timezone(timedelta(hours=8)))
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
        "today_report": _daily_payload(case.context_variant),
        "historical_reports": [],
        "active_clear_pending": None,
        "recent_messages": [
            {
                "role": role,
                "content": content,
                "source_message_id": f"recent-{index}",
            }
            for index, (role, content) in enumerate(case.recent_messages, start=1)
        ],
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
        "weekly_plan": _weekly_payload(case.context_variant),
    }


def request_payload(case: WeeklyDailyModelCase) -> dict[str, Any]:
    return {
        "model": CANARY_MODEL_NAME,
        "messages": [
            {"role": "system", "content": canary_system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "user_message": case.user_text,
                        "trusted_context": trusted_context_payload(case),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ],
        "temperature": 0,
        "tools": deepseek_tool_schemas(
            _TOOL_NAMES,
            mode=ExecutionMode.CANARY_EXECUTE,
        ),
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }


def evaluation_bundle(case_ids: set[str] | None = None) -> dict[str, Any]:
    selected = [case for case in MODEL_CASES if case_ids is None or case.case_id in case_ids]
    return {
        "schema_version": "agent2.weekly-vs-friday-daily.model-eval.v1",
        "zero_write": True,
        "cases": [
            {
                "case_id": case.case_id,
                "category": case.category,
                "user_text": case.user_text,
                "expected_tool_sets": [sorted(value) for value in case.expected_tool_sets],
                "expected_weekly_operations": (
                    sorted(case.expected_weekly_operations)
                    if case.expected_weekly_operations is not None
                    else None
                ),
                "note": case.note,
                "request": request_payload(case),
            }
            for case in selected
        ],
    }


def score_model_result(
    case: WeeklyDailyModelCase,
    *,
    tool_calls: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Score model output without executing any returned tool call."""

    names = frozenset(str(item.get("name") or "") for item in tool_calls)
    if names not in case.expected_tool_sets:
        return False, f"unexpected tools: {sorted(names)}"
    expected_operations = case.expected_weekly_operations
    if expected_operations is None or "apply_next_weekly_plan" not in names:
        return True, "matched"
    weekly_call = next(
        item for item in tool_calls if item.get("name") == "apply_next_weekly_plan"
    )
    arguments = weekly_call.get("arguments") or {}
    operations = arguments.get("operations") or []
    actual_operations = frozenset(
        str(operation.get("operation") or "") for operation in operations
    )
    if actual_operations != expected_operations:
        return False, f"unexpected weekly operations: {sorted(actual_operations)}"
    return True, "matched"


def test_model_eval_corpus_has_required_adversarial_coverage() -> None:
    assert len(MODEL_CASES) >= 20
    assert len({case.case_id for case in MODEL_CASES}) == len(MODEL_CASES)
    categories = {case.category for case in MODEL_CASES}
    assert {
        "pure_daily",
        "pure_weekly",
        "dual_intent",
        "tomorrow_plan",
        "friday_vs_next_friday",
        "context_confirmation",
        "context_modification",
        "suggestion_routing",
    }.issubset(categories)


def test_model_eval_requests_use_production_prompt_and_never_execute_tools() -> None:
    bundle = evaluation_bundle({"daily_01", "weekly_01", "dual_01"})
    assert bundle["zero_write"] is True
    for item in bundle["cases"]:
        request = item["request"]
        assert request["model"] == CANARY_MODEL_NAME
        assert request["messages"][0]["content"] == canary_system_prompt()
        assert "tool_choice" not in request
        assert {tool["function"]["name"] for tool in request["tools"]} == _TOOL_NAMES


def test_friday_context_binds_daily_and_next_week_to_different_dates() -> None:
    case = next(item for item in MODEL_CASES if item.case_id == "dual_03")
    context = trusted_context_payload(case)
    assert context["daily_reporting_context"]["local_date"] == "2026-08-14"
    assert context["weekly_plan"]["target_week_start"] == "2026-08-17"
    assert context["weekly_plan"]["days"][4]["plan_date"] == "2026-08-21"


def test_scoring_checks_tools_and_suggestion_zone_operations() -> None:
    suggestion = next(item for item in MODEL_CASES if item.case_id == "weekly_03")
    good, _ = score_model_result(
        suggestion,
        tool_calls=[
            {
                "name": "apply_next_weekly_plan",
                "arguments": {
                    "operations": [{"operation": "capture_suggestion"}]
                },
            }
        ],
    )
    wrong_operation, _ = score_model_result(
        suggestion,
        tool_calls=[
            {
                "name": "apply_next_weekly_plan",
                "arguments": {"operations": [{"operation": "add"}]},
            }
        ],
    )
    wrong_domain, _ = score_model_result(
        suggestion,
        tool_calls=[{"name": "add_daily_items", "arguments": {}}],
    )
    assert good is True
    assert wrong_operation is False
    assert wrong_domain is False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--emit-smoke", action="store_true")
    parser.add_argument("--emit-all", action="store_true")
    args = parser.parse_args()
    if args.emit_smoke:
        selected = {"daily_01", "weekly_01", "dual_01", "weekly_03", "dual_03"}
    elif args.emit_all:
        selected = None
    else:
        parser.error("choose --emit-smoke or --emit-all")
    print(json.dumps(evaluation_bundle(selected), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
