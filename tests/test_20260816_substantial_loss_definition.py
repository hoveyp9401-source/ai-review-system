from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.agent2.tool_calling.canary_service import (
    _select_trusted_read_response,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    QueryDefendantPerformanceArgs,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.production_handlers import (
    ProductionHandlerRequest,
    execute_query_defendant_performance,
)
from app.agent2.tool_calling.production_performance_executor import (
    ProductionPerformanceExecutor,
)


async def _published_performance_loader(**_kwargs):
    return SimpleNamespace(
        facts={
            "source_status_label": "当前已发布底表",
            "rule_version": "rule-2026-08",
            "reports": {
                "month": {
                    "period": {
                        "view": "month",
                        "label": "2026年8月",
                        "cutoff_date": "2026-08-16",
                        "comparison_label": "上月末",
                    },
                    "scopes": [
                        {
                            "scope_key": "overall",
                            "scope_name": "整体",
                            "scope_type": "overall",
                            "loss_metrics": {
                                "substantial_loss_amount": {
                                    "status": "calculated",
                                    "display": "12.34万元",
                                    "eligible_case_count": 2,
                                }
                            },
                        }
                    ],
                }
            },
        }
    )


async def _query_performance_outcome():
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        principal=TrustedPrincipal(
            tenant_id="tenant-1",
            user_id=UUID("11111111-1111-1111-1111-111111111111"),
            conversation_id="conversation-1",
            source_message_id="message-1",
            timezone="Asia/Shanghai",
        ),
    )
    executor = ProductionPerformanceExecutor(
        session=object(),
        user=SimpleNamespace(id="user-1", name="测试用户"),
        context=context,
        settings=SimpleNamespace(),
        performance_loader=_published_performance_loader,
    )
    request = ProductionHandlerRequest(
        tool_call_id="call-1",
        tool_name="query_defendant_performance",
        arguments=QueryDefendantPerformanceArgs(
            view="month",
            scope_type="department",
            mode="explain_substantial",
        ),
        executor=SimpleNamespace(),
        memory_executor=SimpleNamespace(),
        performance_executor=executor,
    )

    return await execute_query_defendant_performance(request)


def _receipt_from_outcome(outcome) -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.SUCCESS,
        tool_name="query_defendant_performance",
        changed=False,
        target_type=outcome.target_type,
        target_id=outcome.target_id,
        safe_user_facts=outcome.safe_user_facts,
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


def _substantial_definition_claim(catalog):
    matches = [
        (claim_id, claim)
        for claim_id, claim in catalog.items()
        if claim.get("kind") == "definition"
        and claim.get("term") == "实质减损金额"
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.asyncio
async def test_public_performance_query_exposes_substantial_definition_separately_from_amount() -> None:
    outcome = await _query_performance_outcome()

    assert outcome.safe_user_facts["performance_query"]["mode"] == (
        "explain_substantial"
    )
    catalog = outcome.safe_user_facts["performance_facts"]["claim_catalog"]
    _, definition = _substantial_definition_claim(catalog)
    assert definition["validation_policy"] == "exact_canonical_text"
    assert "年初至统计截止日" in definition["canonical_text"]
    assert "供应商或班组" in definition["canonical_text"]
    assert "无争议-债权债务明确" in definition["canonical_text"]
    assert "汇总这些案件的减损金额" in definition["canonical_text"]

    amount = catalog["scope.substantial_loss_amount"]
    assert amount["kind"] == "loss"
    assert amount["value"]["display"] == "12.34万元"
    assert "canonical_text" not in amount


@pytest.mark.asyncio
async def test_public_performance_facts_explain_exact_canonical_reply_protocol() -> None:
    outcome = await _query_performance_outcome()
    safe_facts = outcome.safe_user_facts
    selection_contract = safe_facts["performance_facts"]["reply_guidance"][
        "selection_contract"
    ]

    for protocol_text in (safe_facts["回复要求"], selection_contract):
        assert "validation_policy=exact_canonical_text" in protocol_text
        assert "canonical_text" in protocol_text
        assert "标题、前缀、后缀" in protocol_text
        assert "同一行其他事实" in protocol_text


@pytest.mark.asyncio
async def test_public_performance_facts_keep_definition_only_answers_in_scope() -> None:
    outcome = await _query_performance_outcome()
    safe_facts = outcome.safe_user_facts
    answer_scope = safe_facts["performance_facts"]["reply_guidance"][
        "answer_scope"
    ]

    for protocol_text in (safe_facts["回复要求"], answer_scope):
        assert "只询问某个指标的定义或计算口径" in protocol_text
        assert "只回答对应定义或口径" in protocol_text
        assert "当前数值、件数、截止日期或其他报表事实" in protocol_text
        assert "同时明确询问当前结果" in protocol_text
        assert "独立事实" in protocol_text


@pytest.mark.asyncio
async def test_public_performance_facts_define_unambiguous_claim_id_citation_protocol() -> None:
    outcome = await _query_performance_outcome()
    safe_facts = outcome.safe_user_facts
    guidance = safe_facts["performance_facts"]["reply_guidance"]

    assert guidance["citation_format"] == "[依据:<claim_id>]"
    assert "claim_catalog 对象的键原文" in guidance["claim_id_source"]
    for protocol_text in (
        safe_facts["回复要求"],
        guidance["selection_contract"],
    ):
        assert "[依据:<claim_id>]" in protocol_text
        assert "claim_catalog 对象的键原文" in protocol_text
        assert "[依据:definition.7]" in protocol_text
        assert "claim_catalog中的" in protocol_text
        assert "中文类别名" in protocol_text
        assert "翻译" in protocol_text


@pytest.mark.asyncio
async def test_public_performance_facts_describe_validation_without_reply_rewriting() -> None:
    outcome = await _query_performance_outcome()
    safe_facts = outcome.safe_user_facts
    selection_contract = safe_facts["performance_facts"]["reply_guidance"][
        "selection_contract"
    ]

    for protocol_text in (safe_facts["回复要求"], selection_contract):
        assert (
            "系统会按编号核验事实并隐藏编号，不会重写用户可见文字"
            in protocol_text
        )
        assert "按编号重新生成用户可见文字" not in protocol_text


@pytest.mark.asyncio
async def test_final_read_reply_rejects_substantial_definition_that_omits_positive_amount_condition() -> None:
    outcome = await _query_performance_outcome()
    catalog = outcome.safe_user_facts["performance_facts"]["claim_catalog"]
    claim_id, definition = _substantial_definition_claim(catalog)
    incomplete = definition["canonical_text"].replace(
        "、减损金额大于0",
        "",
    )

    selected = _select_trusted_read_response(
        model_content=f"{incomplete}[依据:{claim_id}]",
        receipts=(_receipt_from_outcome(outcome),),
    )

    assert selected != incomplete
    assert incomplete not in selected


@pytest.mark.asyncio
async def test_final_read_reply_accepts_complete_canonical_definition_with_outer_markdown() -> None:
    outcome = await _query_performance_outcome()
    catalog = outcome.safe_user_facts["performance_facts"]["claim_catalog"]
    claim_id, definition = _substantial_definition_claim(catalog)
    canonical = definition["canonical_text"]

    selected = _select_trusted_read_response(
        model_content=f"- **{canonical}**[依据:{claim_id}]",
        receipts=(_receipt_from_outcome(outcome),),
    )

    assert canonical in selected
    assert "[依据:" not in selected


@pytest.mark.asyncio
async def test_final_read_reply_accepts_complete_canonical_definition_with_ascii_quotes() -> None:
    outcome = await _query_performance_outcome()
    catalog = outcome.safe_user_facts["performance_facts"]["claim_catalog"]
    claim_id, definition = _substantial_definition_claim(catalog)
    ascii_quotes = definition["canonical_text"].replace("“", '"').replace(
        "”",
        '"',
    )

    selected = _select_trusted_read_response(
        model_content=f"{ascii_quotes}[依据:{claim_id}]",
        receipts=(_receipt_from_outcome(outcome),),
    )

    assert selected == ascii_quotes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original", "reversed_condition"),
    [
        (
            "对方单位性质为供应商或班组",
            "对方单位性质不是供应商或班组",
        ),
        (
            "案情原因为“无争议-债权债务明确”",
            "案情原因不是“无争议-债权债务明确”",
        ),
    ],
)
async def test_final_read_reply_rejects_reversed_substantial_definition_condition(
    original: str,
    reversed_condition: str,
) -> None:
    outcome = await _query_performance_outcome()
    catalog = outcome.safe_user_facts["performance_facts"]["claim_catalog"]
    claim_id, definition = _substantial_definition_claim(catalog)
    reversed_definition = definition["canonical_text"].replace(
        original,
        reversed_condition,
    )

    selected = _select_trusted_read_response(
        model_content=f"{reversed_definition}[依据:{claim_id}]",
        receipts=(_receipt_from_outcome(outcome),),
    )

    assert selected != reversed_definition
    assert reversed_definition not in selected


@pytest.mark.asyncio
async def test_final_read_reply_rejects_canonical_definition_with_contradictory_suffix() -> None:
    outcome = await _query_performance_outcome()
    catalog = outcome.safe_user_facts["performance_facts"]["claim_catalog"]
    claim_id, definition = _substantial_definition_claim(catalog)
    contradictory = (
        f'{definition["canonical_text"]}但供应商案件不纳入。'
    )

    selected = _select_trusted_read_response(
        model_content=f"{contradictory}[依据:{claim_id}]",
        receipts=(_receipt_from_outcome(outcome),),
    )

    assert selected != contradictory
    assert contradictory not in selected


@pytest.mark.asyncio
async def test_final_read_reply_rejects_amount_supported_only_by_definition_fact() -> None:
    outcome = await _query_performance_outcome()
    catalog = outcome.safe_user_facts["performance_facts"]["claim_catalog"]
    definition_id, _ = _substantial_definition_claim(catalog)
    unsupported_amount = "法务部门整体实质减损金额为12.34万元。"

    selected = _select_trusted_read_response(
        model_content=f"{unsupported_amount}[依据:{definition_id}]",
        receipts=(_receipt_from_outcome(outcome),),
    )

    assert selected != unsupported_amount
    assert "12.34万元" not in selected


@pytest.mark.asyncio
async def test_final_read_reply_accepts_amount_supported_by_independent_loss_fact() -> None:
    outcome = await _query_performance_outcome()
    supported_amount = "法务部门整体实质减损金额为12.34万元。"

    selected = _select_trusted_read_response(
        model_content=(
            f"{supported_amount}[依据:scope.substantial_loss_amount]"
        ),
        receipts=(_receipt_from_outcome(outcome),),
    )

    assert selected == supported_amount
