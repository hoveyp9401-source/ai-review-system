from __future__ import annotations

from types import SimpleNamespace

from app.agent2.tool_calling.canary_service import (
    _select_trusted_read_response,
)


def _receipt(*, catalog: dict[str, dict[str, object]]):
    return SimpleNamespace(
        tool_name="query_defendant_performance",
        changed=False,
        safe_user_facts={
            "actual_write": False,
            "model_composition_allowed": True,
            "response_text": "旧程序固定答案，不应成为回退。",
            "performance_facts": {"claim_catalog": catalog},
        },
    )


def test_grounded_performance_reply_preserves_agent2_wording() -> None:
    receipt = _receipt(
        catalog={
            "metric.stock": {
                "kind": "metric",
                "metric_key": "stock_count",
                "value": 12,
                "entities": [],
            }
        }
    )
    model_content = "当前存量案件为12件。[依据:metric.stock]"

    result = _select_trusted_read_response(
        model_content=model_content,
        receipts=(receipt,),
    )

    assert result == "当前存量案件为12件。"
    assert "旧程序固定答案" not in result


def test_invalid_performance_fact_fails_closed_without_fixed_business_answer() -> None:
    receipt = _receipt(
        catalog={
            "metric.stock": {
                "kind": "metric",
                "metric_key": "stock_count",
                "value": 12,
                "entities": [],
            }
        }
    )

    result = _select_trusted_read_response(
        model_content="当前存量案件为99件。[依据:metric.stock]",
        receipts=(receipt,),
    )

    assert "旧程序固定答案" not in result
    assert "99" not in result
    assert "没有写入" in result or "再发一次" in result


def test_performance_answer_selection_does_not_accept_user_query() -> None:
    import inspect

    parameters = inspect.signature(_select_trusted_read_response).parameters

    assert "user_query" not in parameters


def test_mixed_performance_reads_fail_closed_when_other_claim_is_uncited() -> None:
    performance = _receipt(
        catalog={
            "metric.stock": {
                "kind": "metric",
                "metric_key": "stock_count",
                "value": 12,
                "entities": [],
            }
        }
    )
    another_read = SimpleNamespace(
        tool_name="query_report_by_date",
        changed=False,
        safe_user_facts={
            "actual_write": False,
            "authoritative_read_response": True,
            "response_text": "program-rendered daily answer",
        },
    )

    model_content = (
        "当前存量案件为12件。[依据:metric.stock]\n"
        "庞浩昨天的日报有2项工作。"
    )
    result = _select_trusted_read_response(
        model_content=model_content,
        receipts=(performance, another_read),
    )

    assert "program-rendered" not in result
    assert "庞浩昨天的日报有2项工作" not in result
    assert "没有写入" in result or "再发一次" in result


def test_mixed_performance_reads_fail_closed_on_wrong_performance_claim() -> None:
    performance = _receipt(
        catalog={
            "metric.stock": {
                "kind": "metric",
                "metric_key": "stock_count",
                "value": 12,
                "entities": [],
            }
        }
    )
    another_read = SimpleNamespace(
        tool_name="query_report_by_date",
        changed=False,
        safe_user_facts={
            "actual_write": False,
            "authoritative_read_response": True,
            "response_text": "program-rendered daily answer",
        },
    )

    result = _select_trusted_read_response(
        model_content=(
            "当前存量案件为99件。[依据:metric.stock]\n"
            "庞浩昨天的日报有2项工作。"
        ),
        receipts=(performance, another_read),
    )

    assert "program-rendered" not in result
    assert "99" not in result
    assert "庞浩昨天" not in result
    assert "没有写入" in result or "再发一次" in result


def test_mixed_performance_reads_reject_uncited_non_numeric_business_claims() -> None:
    performance = _receipt(
        catalog={
            "metric.stock": {
                "kind": "metric",
                "metric_key": "stock_count",
                "value": 12,
                "entities": [],
            }
        }
    )
    another_read = SimpleNamespace(
        tool_name="query_report_by_date",
        changed=False,
        safe_user_facts={
            "actual_write": False,
            "authoritative_read_response": True,
            "response_text": "program-rendered daily answer",
        },
    )

    for uncited_claim in (
        "庞浩昨天没有提交日报。",
        "庞浩昨天有未闭环事项。",
        "综合管理部最近需关注合同风险。",
    ):
        result = _select_trusted_read_response(
            model_content=(
                "当前存量案件为12件。[依据:metric.stock]\n" + uncited_claim
            ),
            receipts=(performance, another_read),
        )

        assert uncited_claim not in result
        assert "program-rendered" not in result
        assert "没有写入" in result or "再发一次" in result
