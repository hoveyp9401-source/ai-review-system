from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.agent2.report_insight_query import StructuredReportInsightQuery
from app.agent2.report_insights import (
    InMemoryReportInsightRepository,
    ReportInsightModule,
)
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.contracts import QueryReportInsightsArgs


def _requester() -> SimpleNamespace:
    return SimpleNamespace(
        id="u-manager",
        name="庞浩",
        dingtalk_user_id="40842",
        team_id="team-admin",
        role="department_head",
    )


def _repository(*, plan_count: int, include_old_plan: bool = False):
    plans = [f"推进专项事项{index:02d}" for index in range(plan_count)]
    reports: list[dict[str, object]] = [
        {
            "id": "r-plan",
            "user_id": "u-member",
            "team_id": "team-admin",
            "date": "2026-08-01",
            "status": "completed",
            "tomorrow_plan": plans,
        },
        {
            "id": "r-evidence",
            "user_id": "u-member",
            "team_id": "team-admin",
            "date": "2026-08-09",
            "status": "completed",
            "today_work": ["处理其他日常工作"],
        },
    ]
    if include_old_plan:
        reports.insert(
            0,
            {
                "id": "r-old",
                "user_id": "u-member",
                "team_id": "team-admin",
                "date": "2026-06-20",
                "status": "completed",
                "tomorrow_plan": ["推进历史专项事项"],
            },
        )
    return InMemoryReportInsightRepository(
        teams=[
            {
                "id": "team-admin",
                "name": "综合管理部",
                "department_name": "法务合约中心",
            }
        ],
        users=[
            {
                "id": "u-manager",
                "name": "庞浩",
                "team_id": "team-admin",
                "role": "department_head",
            },
            {
                "id": "u-member",
                "name": "刘聪",
                "team_id": "team-admin",
                "role": "member",
            },
        ],
        reports=reports,
    )


def _query(period_type: str) -> StructuredReportInsightQuery:
    return StructuredReportInsightQuery(
        query="看下综合管理部没闭环的工作",
        query_kind="unclosed_work",
        scope_type="organization",
        scope_name="综合管理部",
        period_type=period_type,
    )


def test_unclosed_tool_distinguishes_omitted_all_history_and_recent_month() -> None:
    unspecified = QueryReportInsightsArgs(
        query_kind="unclosed_work",
        scope_type="organization",
        scope_name="综合管理部",
        period_type="unspecified",
    )
    recent_month = QueryReportInsightsArgs(
        query_kind="unclosed_work",
        scope_type="organization",
        scope_name="综合管理部",
        period_type="recent_30_days",
    )

    assert unspecified.period_type == "unspecified"
    assert recent_month.period_type == "recent_30_days"


@pytest.mark.asyncio
async def test_large_unbounded_unclosed_result_returns_counts_then_requests_scope() -> None:
    answer = await ReportInsightModule(
        _repository(plan_count=21)
    ).answer_query(
        _query("unspecified"),
        requester=_requester(),
        current_date=date(2026, 8, 9),
    )

    assert answer is not None
    facts = answer.evidence.facts
    assert facts["unclosed_count"] == 21
    assert facts["needs_time_scope"] is True
    assert facts["period_type"] == "unspecified"
    assert facts["evaluated_period_type"] == "all_history"
    assert facts["unclosed_items"] == []
    assert facts["unclosed_items_withheld_count"] == 21
    assert "followed_up_count" not in facts
    assert "in_progress_count" not in facts
    assert "completed_count" not in facts
    assert "pending_evidence_count" not in facts
    assert facts["classification_counts_withheld_for_scope_choice"] is True
    assert facts["available_period_types"] == [
        "recent_7_days",
        "recent_30_days",
        "all_history",
    ]


@pytest.mark.asyncio
async def test_small_unbounded_unclosed_result_keeps_details_and_date_scope() -> None:
    answer = await ReportInsightModule(
        _repository(plan_count=2)
    ).answer_query(
        _query("unspecified"),
        requester=_requester(),
        current_date=date(2026, 8, 9),
    )

    assert answer is not None
    facts = answer.evidence.facts
    assert facts["unclosed_count"] == 2
    assert facts["needs_time_scope"] is False
    assert len(facts["unclosed_items"]) == 2
    assert facts["period_start"] == "2026-08-01"
    assert facts["period_end"] == "2026-08-09"


@pytest.mark.asyncio
async def test_explicit_recent_month_excludes_older_plans() -> None:
    answer = await ReportInsightModule(
        _repository(plan_count=2, include_old_plan=True)
    ).answer_query(
        _query("recent_30_days"),
        requester=_requester(),
        current_date=date(2026, 8, 9),
    )

    assert answer is not None
    facts = answer.evidence.facts
    assert facts["period_start"] == "2026-07-11"
    assert facts["period_end"] == "2026-08-09"
    assert facts["unclosed_count"] == 2
    assert all(
        item["plan_text"] != "推进历史专项事项"
        for item in facts["unclosed_items"]
    )


@pytest.mark.asyncio
async def test_explicit_large_result_discloses_that_the_text_is_a_preview() -> None:
    answer = await ReportInsightModule(
        _repository(plan_count=35)
    ).answer_query(
        _query("recent_30_days"),
        requester=_requester(),
        current_date=date(2026, 8, 9),
    )

    assert answer is not None
    facts = answer.evidence.facts
    assert facts["unclosed_count"] == 35
    assert facts["unclosed_preview_count"] == 30
    assert facts["unclosed_preview_truncated"] is True
    assert facts["unclosed_remaining_count"] == 5
    assert "共35项" in "".join(answer.text.split())
    assert "前30项" in "".join(answer.text.split())


def test_agent2_prompt_owns_the_scope_follow_up_instead_of_the_program() -> None:
    prompt = canary_system_prompt()

    assert "period_type=unspecified" in prompt
    assert "period_type=recent_30_days" in prompt
    assert "needs_time_scope" in prompt
    assert "由你自然追问" in prompt
