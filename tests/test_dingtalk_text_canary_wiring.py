from pathlib import Path


CANARY_SERVICE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "agent2"
    / "tool_calling"
    / "canary_service.py"
)


def test_canary_formats_successful_reply_before_transport_receives_it():
    source = CANARY_SERVICE.read_text(encoding="utf-8")

    assert "from app.utils.dingtalk_text import format_dingtalk_plain_text" in source
    assert "formatted_message = format_dingtalk_plain_text(final_content)" in source
    assert "message=formatted_message" in source
