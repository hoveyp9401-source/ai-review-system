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
