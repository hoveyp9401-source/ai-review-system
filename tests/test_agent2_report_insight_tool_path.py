from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent2.report_insights import InMemoryReportInsightRepository
from app.agent2.tool_calling.contracts import QueryReportInsightsArgs
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.production_daily_executor import ProductionDailyExecutor
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.registry import TOOL_REGISTRY, validate_tool_arguments


def test_report_insights_are_exposed_as_one_model_selected_read_tool() -> None:
    definition = TOOL_REGISTRY["query_report_insights"]

    assert definition.read_or_write == "read"
    assert definition.transaction_target_policy == "read_only"
    assert definition.permission_policy == "authenticated_tenant_daily_read"
    managed_description = TOOL_REGISTRY["query_managed_daily_reports"].description
    assert "exactly one calendar date" in managed_description
    assert "use query_report_insights" in managed_description
    prompt = canary_system_prompt()
    assert "query_report_insights` is required" in prompt
    assert "Do not substitute a one-date managed-daily query" in prompt


@pytest.mark.parametrize(
    "arguments",
    (
        {
            "query_kind": "report_count",
            "scope_type": "person",
            "scope_name": "庞浩",
            "period_type": "all_history",
            "status_filter": "all_saved",
        },
        {
            "query_kind": "recent_work",
            "scope_type": "person",
            "scope_name": "庞浩",
            "period_type": "recent_7_days",
            "status_filter": "all_saved",
        },
        {
            "query_kind": "period_work",
            "scope_type": "organization",
            "scope_name": "综合管理部",
            "period_type": "current_week",
            "status_filter": "all_saved",
        },
        {
            "query_kind": "recent_attention",
            "scope_type": "organization",
            "scope_name": "综合管理部",
            "period_type": "recent_7_days",
            "status_filter": "all_saved",
        },
        {
            "query_kind": "unclosed_work",
            "scope_type": "person",
            "scope_name": "刘聪",
            "period_type": "all_history",
            "status_filter": "all_saved",
        },
        {
            "query_kind": "unclosed_work",
            "scope_type": "organization",
            "scope_name": "综合管理部",
            "period_type": "all_history",
            "status_filter": "all_saved",
        },
    ),
)
def test_report_insight_tool_contract_accepts_supported_queries(
    arguments: dict[str, str],
) -> None:
    validated = validate_tool_arguments("query_report_insights", arguments)

    assert QueryReportInsightsArgs.model_validate(validated)


def test_stream_has_no_pre_model_report_insight_language_route() -> None:
    source = Path("app/stream_runner.py").read_text(encoding="utf-8")
    webhook_source = Path("app/api/webhook.py").read_text(encoding="utf-8")

    assert "if is_report_insight_question(job.text):" not in source
    assert "load_live_report_insight_answer(" not in source
    assert "is_report_insight_question" not in webhook_source
    assert "load_live_report_insight_answer(" not in webhook_source


@pytest.mark.asyncio
async def test_typed_report_insight_execution_is_read_only_and_uses_live_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_id = str(uuid4())
    requester_id = uuid4()
    target_id = str(uuid4())
    repository = InMemoryReportInsightRepository(
        teams=[
            {
                "id": team_id,
                "name": "综合管理部",
                "department_name": "法务合约中心",
            }
        ],
        users=[
            {
                "id": str(requester_id),
                "name": "庞浩",
                "team_id": team_id,
                "role": "member",
            },
            {
                "id": target_id,
                "name": "刘聪",
                "team_id": team_id,
                "role": "member",
            },
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": target_id,
                "team_id": team_id,
                "date": "2026-08-01",
                "status": "completed",
                "tomorrow_plan": ["完成预算审批"],
            },
            {
                "id": "r-2",
                "user_id": target_id,
                "team_id": team_id,
                "date": "2026-08-03",
                "status": "completed",
                "today_work": ["整理部门档案"],
            },
        ],
    )
    monkeypatch.setattr(
        "app.agent2.tool_calling.production_daily_executor.SqlReportInsightRepository",
        lambda *_args, **_kwargs: repository,
    )
    principal = SimpleNamespace(
        tenant_id="tenant-legal",
        user_id=requester_id,
        timezone="Asia/Shanghai",
    )
    context = SimpleNamespace(
        principal=principal,
        now=datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc),
        business_glossary={},
    )
    user = SimpleNamespace(
        id=requester_id,
        name="庞浩",
        dingtalk_user_id="dt-pang",
        team_id=team_id,
        role="member",
    )
    executor = ProductionDailyExecutor(
        session=object(),
        user=user,
        context=context,
        settings=SimpleNamespace(
            legal_daily_dashboard_tenant_id="tenant-legal"
        ),
        bound_calls={},
        source_channel="test",
        source_text_hash="0" * 64,
        date_resolver=object(),
    )
    request = ProductionHandlerRequest(
        tool_call_id="call-1",
        tool_name="query_report_insights",
        arguments=QueryReportInsightsArgs(
            query_kind="unclosed_work",
            scope_type="person",
            scope_name="刘聪",
            period_type="all_history",
        ),
        executor=executor,
        memory_executor=object(),
    )

    outcome = await executor.query_report_insights(request)

    assert outcome.status_if_unchanged.value == "success"
    assert outcome.target_type == "daily_report_insight"
    assert outcome.before_report is None
    assert outcome.after_report is None
    assert outcome.safe_user_facts is not None
    assert outcome.safe_user_facts["actual_write"] is False
    facts = outcome.safe_user_facts["report_insight"]["facts"]
    assert facts["scope_label"] == "刘聪"
    assert facts["unclosed_count"] == 1
    assert facts["unclosed_items"][0]["plan_text"] == "完成预算审批"
