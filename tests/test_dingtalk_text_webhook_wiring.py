from pathlib import Path


WEBHOOK = Path(__file__).resolve().parents[1] / "app" / "api" / "webhook.py"
CANARY_SERVICE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "agent2"
    / "tool_calling"
    / "canary_service.py"
)


def test_webhook_routes_report_queries_through_formatted_agent2_reply():
    source = WEBHOOK.read_text(encoding="utf-8")
    canary_source = CANARY_SERVICE.read_text(encoding="utf-8")

    assert "from app.utils.dingtalk_text import format_dingtalk_plain_text" in source
    assert "process_tool_call_canary_ingress(" in source
    assert "conversation_kind=incoming.conversation_kind" in source
    assert "message_occurred_at=event.received_at" in source
    assert "report_insight_answer" not in source
    assert "formatted_message = format_dingtalk_plain_text(final_content)" in canary_source
    assert "message=formatted_message" in canary_source
