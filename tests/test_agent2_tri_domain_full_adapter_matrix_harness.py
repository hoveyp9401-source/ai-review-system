from __future__ import annotations

from tests.run_agent2_tri_domain_full_adapter_matrix_live import (
    CASES,
    _FRIDAY_PLAN_ID,
    _FRIDAY_PLAN_VERSION,
    _score,
    _self_check,
)


def test_full_adapter_matrix_self_check_covers_required_private_domains() -> None:
    checked = _self_check()

    assert checked["case_count"] >= 22
    assert checked["private_chat_only"] is True
    assert checked["business_database_imported"] is False
    assert checked["message_provider_imported"] is False
    assert checked["strict_negative_control_passed"] is True


def test_strict_scorer_rejects_weekly_report_next_plan_leaking_to_plan() -> None:
    case = next(
        item for item in CASES
        if item.case_id == "periodic_next_plan_stays_periodic"
    )

    passed, errors = _score(
        case,
        calls=[
            {
                "name": "apply_next_weekly_plan",
                "arguments": {
                    "plan_id": str(_FRIDAY_PLAN_ID),
                    "expected_version": _FRIDAY_PLAN_VERSION,
                    "operations": [
                        {
                            "operation_id": "wrong-domain",
                            "operation": "add",
                            "plan_date": "2026-08-17",
                            "content": "继续向财务催付款材料",
                            "source_evidence": {
                                "source_message_index": 1,
                                "exact_clause_quote": "下周周一继续向财务催付款材料。",
                            },
                        }
                    ],
                },
            }
        ],
        assistant_content=None,
    )

    assert passed is False
    assert any("expected tools" in error for error in errors)


def test_strict_scorer_accepts_no_tool_monday_week_clarification() -> None:
    case = next(
        item for item in CASES
        if item.case_id == "monday_bare_weekday_ambiguous"
    )

    passed, errors = _score(
        case,
        calls=[],
        assistant_content=(
            "你说的周三，是补本周工作计划里的周三，"
            "还是安排到下周工作计划里的周三？"
        ),
    )

    assert passed is True
    assert errors == ()
