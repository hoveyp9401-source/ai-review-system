from pathlib import Path


STREAM_RUNNER = Path(__file__).resolve().parents[1] / "app" / "stream_runner.py"
CANARY_SERVICE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "agent2"
    / "tool_calling"
    / "canary_service.py"
)


def test_report_insight_uses_the_formatted_agent2_reply_path():
    source = STREAM_RUNNER.read_text(encoding="utf-8")
    canary_source = CANARY_SERVICE.read_text(encoding="utf-8")

    assert "from app.utils.dingtalk_text import format_dingtalk_plain_text" in source
    assert "is_report_insight_question(job.text)" not in source
    assert "process_tool_call_canary_ingress(" in source
    assert "reply_text = tool_call_canary.message" in source
    assert "formatted_message = format_dingtalk_plain_text(final_content)" in canary_source


def test_general_read_only_reply_is_formatted_before_it_is_persisted():
    source = STREAM_RUNNER.read_text(encoding="utf-8")

    read_only_position = source.index(
        '"Agent2 read-only outcome requires verified execution context"'
    )
    format_position = source.index(
        "reply_text = format_dingtalk_plain_text(reply_text)",
        read_only_position,
    )
    outcome_position = source.index(
        "text_outcome(\n                            reply_text,",
        format_position,
    )

    assert format_position < outcome_position
