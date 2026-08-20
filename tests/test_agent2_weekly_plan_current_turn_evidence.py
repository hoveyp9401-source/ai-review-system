import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent2.tool_calling.current_turn_source import (
    CurrentTurnSource,
    CurrentTurnSourceEvidenceError,
)


def test_current_turn_source_keeps_one_authoritative_time_per_message():
    first = datetime(2026, 8, 16, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    second = datetime(2026, 8, 17, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    source = CurrentTurnSource(
        ("第一句", "第二句"),
        occurred_at=(first, second),
    )

    assert source.occurred_at_for(1) == first
    assert source.occurred_at_for(2) == second
    assert source.occurred_at_for(3) is None

    with pytest.raises(ValueError, match="match current user messages"):
        CurrentTurnSource(("第一句", "第二句"), occurred_at=(first,))

    with pytest.raises(ValueError, match="timezone-aware"):
        CurrentTurnSource(
            ("第一句",),
            occurred_at=(datetime(2026, 8, 16, 23, 59),),
        )


def _args(content="整理甲项目材料", source_index=1):
    return {
        "plan_id": "84c6131f-9709-4f43-8719-6c8688c9ee0a",
        "expected_version": 0,
        "operations": [
            {
                "operation_id": "op-1",
                "operation": "add",
                "plan_date": "2026-08-17",
                "content": content,
                "source_evidence": {
                    "source_message_index": source_index,
                    "exact_clause_quote": "周一整理甲项目材料",
                },
            }
        ],
    }


def test_weekly_plan_content_must_be_an_exact_current_message_excerpt():
    source = CurrentTurnSource(("周一整理甲项目材料，周二去开庭",))

    source.validate_tool_arguments(
        "apply_next_weekly_plan",
        _args(),
    )

    with pytest.raises(
        CurrentTurnSourceEvidenceError,
        match="WEEKLY_PLAN_CONTENT_NOT_GROUNDED",
    ):
        source.validate_tool_arguments(
            "apply_next_weekly_plan",
            _args(content="完成甲项目材料"),
        )


def test_independently_reviewed_weekly_content_may_use_conservative_wording():
    source_text = (
        "下周三处理星河项目：只有收到补充材料后才发正式函；"
        "若金额超过180万元，先内部汇报，不直接承诺付款。"
    )
    source = CurrentTurnSource((source_text,))
    arguments = {
        "plan_id": "84c6131f-9709-4f43-8719-6c8688c9ee0a",
        "expected_version": 0,
        "content_reviewed": True,
        "operations": [
            {
                "operation_id": "op-reviewed",
                "operation": "add",
                "plan_date": "2026-08-19",
                "content": (
                    "处理星河项目：收到补充材料后才发正式函；"
                    "若金额超过180万元，先内部汇报，不直接承诺付款。"
                ),
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_clause_quote": source_text,
                },
            }
        ],
    }

    source.validate_tool_arguments(
        "apply_next_weekly_plan",
        arguments,
    )


def test_weekly_plan_binding_removes_only_a_redundant_model_added_year():
    source = CurrentTurnSource(("8月17日周一整理甲项目材料",))
    arguments = _args()
    arguments["operations"][0]["source_evidence"][
        "exact_clause_quote"
    ] = "2026年8月17日周一整理甲项目材料"

    bound = source.bind_tool_arguments(
        "apply_next_weekly_plan",
        arguments,
    )

    assert bound["operations"][0]["source_evidence"][
        "exact_clause_quote"
    ] == "8月17日周一整理甲项目材料"
    assert bound["operations"][0]["content"] == "整理甲项目材料"


def test_weekly_plan_binding_rejects_a_year_that_conflicts_with_plan_date():
    source = CurrentTurnSource(("8月17日周一整理甲项目材料",))
    arguments = _args()
    arguments["operations"][0]["source_evidence"][
        "exact_clause_quote"
    ] = "2027年8月17日周一整理甲项目材料"

    with pytest.raises(
        CurrentTurnSourceEvidenceError,
        match="WEEKLY_PLAN_DATE_EVIDENCE_MISMATCH",
    ):
        source.bind_tool_arguments(
            "apply_next_weekly_plan",
            arguments,
        )


def test_weekly_plan_evidence_cannot_point_outside_the_current_turn():
    source = CurrentTurnSource(("周一整理甲项目材料",))

    with pytest.raises(
        CurrentTurnSourceEvidenceError,
        match="CURRENT_MESSAGE_EVIDENCE_MISMATCH",
    ):
        source.validate_tool_arguments(
            "apply_next_weekly_plan",
            _args(source_index=2),
        )


def test_weekly_plan_submission_requires_current_turn_confirmation_evidence():
    source = CurrentTurnSource(("以上下周计划确认提交",))
    valid = {
        "plan_id": "84c6131f-9709-4f43-8719-6c8688c9ee0a",
        "expected_version": 6,
        "confirmation_evidence": {"source_message_index": 1},
    }

    source.validate_tool_arguments("submit_next_weekly_plan", valid)

    with pytest.raises(
        CurrentTurnSourceEvidenceError,
        match="CURRENT_MESSAGE_EVIDENCE_MISMATCH",
    ):
        source.validate_tool_arguments(
            "submit_next_weekly_plan",
            {
                **valid,
                "confirmation_evidence": {"source_message_index": 2},
            },
        )
