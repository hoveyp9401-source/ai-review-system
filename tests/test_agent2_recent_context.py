from types import SimpleNamespace

from app.agent2.recent_context import looks_like_recent_case_context_text, webhook_event_text


def test_webhook_event_text_reads_manual_raw_input_payload():
    event = SimpleNamespace(payload={"raw_input": "法务一部目前被告存量多少？"})

    assert webhook_event_text(event) == "法务一部目前被告存量多少？"


def test_recent_case_context_recognizes_defendant_metric_questions():
    assert looks_like_recent_case_context_text("法务一部目前被告存量多少？")
    assert looks_like_recent_case_context_text("发我被告二季度新增同比数据")
    assert not looks_like_recent_case_context_text("今天完成合同评审")
