from __future__ import annotations

import pytest

from app.agent2.tool_calling.current_turn_source import (
    CurrentTurnSource,
    CurrentTurnSourceEvidenceError,
)


def test_periodic_weekly_append_content_must_come_from_current_message():
    source = CurrentTurnSource(("补充本周周报：本周完成合同复核",))
    arguments = {
        "report_id": "10000000-0000-4000-8000-000000000001",
        "expected_version": 0,
        "operations": [
            {
                "operation_id": "append-1",
                "operation": "append",
                "field": "accomplishments",
                "content": "完成合同复核",
                "source_evidence": {"source_message_index": 1},
            }
        ],
    }

    source.validate_tool_arguments("apply_current_weekly_report", arguments)

    arguments["operations"][0]["content"] = "模型凭历史补出的内容"
    with pytest.raises(
        CurrentTurnSourceEvidenceError,
        match="PERIODIC_REPORT_CONTENT_NOT_GROUNDED",
    ):
        source.validate_tool_arguments("apply_current_weekly_report", arguments)


def test_periodic_weekly_binding_removes_only_extra_terminal_punctuation():
    source = CurrentTurnSource(("本周完成合同复核；另外下周五汇报进展",))
    arguments = {
        "report_id": "10000000-0000-4000-8000-000000000001",
        "expected_version": 3,
        "operations": [
            {
                "operation_id": "append-accomplishment",
                "operation": "append",
                "field": "accomplishments",
                "content": "本周完成合同复核。",
                "source_evidence": {"source_message_index": 1},
            }
        ],
    }

    bound = source.bind_tool_arguments(
        "apply_current_weekly_report",
        arguments,
    )

    assert bound["operations"][0]["content"] == "本周完成合同复核"


def test_periodic_weekly_binding_still_rejects_changed_wording():
    source = CurrentTurnSource(("本周完成合同复核；另外下周五汇报进展",))
    arguments = {
        "report_id": "10000000-0000-4000-8000-000000000001",
        "expected_version": 3,
        "operations": [
            {
                "operation_id": "append-accomplishment",
                "operation": "append",
                "field": "accomplishments",
                "content": "本周完成合同终审。",
                "source_evidence": {"source_message_index": 1},
            }
        ],
    }

    with pytest.raises(
        CurrentTurnSourceEvidenceError,
        match="PERIODIC_REPORT_CONTENT_NOT_GROUNDED",
    ):
        source.bind_tool_arguments(
            "apply_current_weekly_report",
            arguments,
        )


def test_independently_reviewed_periodic_wording_may_remove_oral_repetition():
    source = CurrentTurnSource(
        ("补充本周周报的下周计划：下周周一继续向财务催付款材料。",)
    )
    arguments = {
        "report_id": "10000000-0000-4000-8000-000000000001",
        "expected_version": 3,
        "content_reviewed": True,
        "operations": [
            {
                "operation_id": "append-next-plan",
                "operation": "append",
                "field": "next_plan",
                "content": "下周一继续向财务催付款材料",
                "source_evidence": {"source_message_index": 1},
            }
        ],
    }

    source.bind_tool_arguments(
        "apply_current_weekly_report",
        arguments,
    )


def test_periodic_weekly_submit_requires_current_message_confirmation():
    source = CurrentTurnSource(("确认提交本周周报",))
    source.validate_tool_arguments(
        "submit_current_weekly_report",
        {
            "report_id": "10000000-0000-4000-8000-000000000001",
            "expected_version": 1,
            "confirmation_evidence": {"source_message_index": 1},
        },
    )

    with pytest.raises(
        CurrentTurnSourceEvidenceError,
        match="CURRENT_MESSAGE_EVIDENCE_MISMATCH",
    ):
        source.validate_tool_arguments(
            "submit_current_weekly_report",
            {
                "report_id": "10000000-0000-4000-8000-000000000001",
                "expected_version": 1,
                "confirmation_evidence": {"source_message_index": 2},
            },
        )
