from pathlib import Path


WEBHOOK = Path(__file__).resolve().parents[1] / "app" / "api" / "webhook.py"


def test_webhook_formats_direct_and_general_read_only_replies():
    source = WEBHOOK.read_text(encoding="utf-8")

    assert "from app.utils.dingtalk_text import format_dingtalk_plain_text" in source
    assert "message=format_dingtalk_plain_text(report_insight_answer.text)" in source
    assert "message = format_dingtalk_plain_text(message)" in source
