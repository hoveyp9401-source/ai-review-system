from pathlib import Path


STREAM_RUNNER = Path(__file__).resolve().parents[1] / "app" / "stream_runner.py"


def test_report_insight_direct_reply_is_formatted_before_it_is_sent():
    source = STREAM_RUNNER.read_text(encoding="utf-8")

    assert "from app.utils.dingtalk_text import format_dingtalk_plain_text" in source
    insight_position = source.index("if is_report_insight_question(job.text):")
    format_position = source.index(
        "reply_text = format_dingtalk_plain_text(reply_text)",
        insight_position,
    )
    payload_position = source.index(
        'response_payload = {"msgtype": "text", "text": {"content": reply_text}}',
        format_position,
    )

    assert format_position < payload_position


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
