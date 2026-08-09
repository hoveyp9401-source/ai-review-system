from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.agent2.report_insights import (
    InMemoryReportInsightRepository,
    ReportInsightModule,
)
from app.agent2.tool_calling.canary_config import canary_system_prompt


def _requester() -> SimpleNamespace:
    return SimpleNamespace(
        id="u-manager",
        name="庞浩",
        dingtalk_user_id="40842",
        team_id="team-admin",
        role="department_head",
    )


def _repository(reports: list[dict[str, object]]) -> InMemoryReportInsightRepository:
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
                "id": "u-weng",
                "name": "翁亚兰",
                "team_id": "team-admin",
                "role": "member",
            },
        ],
        reports=reports,
    )


@pytest.mark.asyncio
async def test_previous_week_unclosed_collapses_repeated_workstreams_and_scans_all_later_work():
    reports = [
        {
            "id": "r-0727",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-07-27",
            "status": "completed",
            "today_work": ["完成领导报销单提报一部分"],
            "tomorrow_plan": ["继续完成领导报销单", "跟进绩效考核回顾面谈表收集"],
        },
        {
            "id": "r-0728",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-07-28",
            "status": "completed",
            "today_work": ["完成个人绩效回顾表提报", "报销事项对接"],
            "tomorrow_plan": [
                "收集各团队绩效回顾表",
                "清理15楼余下丢弃资料",
                "提交领导报销单",
                "沟通汇报今年演讲比赛事项",
            ],
        },
        {
            "id": "r-0729",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-07-29",
            "status": "completed",
            "today_work": [
                "完成15楼资料和办公用品清理",
                "完成领导报销单",
                "完成团队绩效回顾面谈表（曹俊团队）",
            ],
            "tomorrow_plan": ["15楼资料归类整理", "团队绩效回顾面谈表收集"],
        },
        {
            "id": "r-0730",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-07-30",
            "status": "completed",
            "today_work": ["完成绩效面谈一部、五部"],
            "tomorrow_plan": ["完成绩效面谈回顾表", "15楼资料整理", "参加部门周会"],
        },
        {
            "id": "r-0731",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-07-31",
            "status": "completed",
            "today_work": [
                "完成法务二部绩效回顾面谈表收集（还剩四部、三部和综合部）",
                "完成15楼资料梳理50%",
                "完成领导报销对接",
            ],
            "problems": ["绩效回顾面谈表四部、三部、综合部尚未收集完成"],
            "tomorrow_plan": [
                "继续收集四部、三部、综合部绩效回顾面谈表，收齐后发人力中心闭环",
                "对接领导报销事宜",
            ],
        },
        {
            "id": "r-0803",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-08-03",
            "status": "completed",
            "today_work": [
                "完成了绩效考核回顾表的催收，已催收完毕",
                "完成了十五楼资料的整理",
            ],
        },
        {
            "id": "r-0804",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-08-04",
            "status": "completed",
            "today_work": ["绩效回顾表扫描归档"],
        },
        {
            "id": "r-0805",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-08-05",
            "status": "completed",
            "today_work": ["绩效面谈表整理扫描"],
        },
    ]

    answer = await ReportInsightModule(_repository(reports)).answer_query(
        SimpleNamespace(
            query_kind="unclosed_work",
            scope_type="person",
            scope_name="翁亚兰",
            period_type="previous_week",
            status_filter="all_saved",
        ),
        requester=_requester(),
        current_date=date(2026, 8, 6),
    )

    assert answer is not None
    facts = answer.evidence.facts
    assert facts["unclosed_count"] == 3
    assert [item["plan_text"] for item in facts["unclosed_items"]] == [
        "沟通汇报今年演讲比赛事项",
        "参加部门周会",
        "对接领导报销事宜",
    ]
    assert "15楼资料" not in answer.text
    assert "绩效回顾" not in answer.text


@pytest.mark.asyncio
async def test_recent_attention_suppresses_problem_and_plan_completed_later():
    reports = [
        {
            "id": "r-0731",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-07-31",
            "status": "completed",
            "problems": ["绩效回顾面谈表四部、三部、综合部尚未收集完成"],
            "tomorrow_plan": [
                "继续收集四部、三部、综合部绩效回顾面谈表，收齐后发人力中心闭环"
            ],
        },
        {
            "id": "r-0803",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-08-03",
            "status": "completed",
            "today_work": ["完成了绩效考核回顾表的催收，已催收完毕"],
        },
        {
            "id": "r-0805",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-08-05",
            "status": "completed",
            "problems": ["预算审批仍在等待"],
            "tomorrow_plan": ["继续跟进预算审批"],
        },
        {
            "id": "r-0806",
            "user_id": "u-weng",
            "team_id": "team-admin",
            "date": "2026-08-06",
            "status": "collecting",
            "today_work": ["预算审批仍在推进"],
            "tomorrow_plan": ["确认招聘面试安排"],
        },
    ]

    answer = await ReportInsightModule(_repository(reports)).answer_query(
        SimpleNamespace(
            query="综合管理部最近有什么需要关注",
            query_kind="recent_attention",
            scope_type="organization",
            scope_name="综合管理部",
            period_type="recent_7_days",
            status_filter="all_saved",
        ),
        requester=_requester(),
        current_date=date(2026, 8, 6),
    )

    assert answer is not None
    facts = answer.evidence.facts
    assert facts["resolved_problem_count"] == 1
    assert all("绩效" not in item["text"] for item in facts["problem_items"])
    assert all("绩效" not in item["text"] for item in facts["plan_items"])
    assert any("预算审批" in item["text"] for item in facts["problem_items"])
    assert any("确认招聘面试安排" in item["text"] for item in facts["plan_items"])
    assert "绩效回顾" not in answer.text


@pytest.mark.asyncio
async def test_later_in_progress_entry_is_followup_not_missing_followup():
    answer = await ReportInsightModule(
        _repository(
            [
                {
                    "id": "r-1",
                    "user_id": "u-weng",
                    "team_id": "team-admin",
                    "date": "2026-08-01",
                    "status": "completed",
                    "tomorrow_plan": ["跟进预算审批"],
                },
                {
                    "id": "r-2",
                    "user_id": "u-weng",
                    "team_id": "team-admin",
                    "date": "2026-08-02",
                    "status": "completed",
                    "today_work": ["预算审批仍在推进"],
                },
            ]
        )
    ).answer(
        "看下翁亚兰有什么没闭环的工作",
        requester=_requester(),
        current_date=date(2026, 8, 6),
    )

    assert answer is not None
    assert answer.evidence.facts["unclosed_count"] == 0
    assert answer.evidence.facts["in_progress_count"] == 1


@pytest.mark.asyncio
async def test_plan_without_any_later_report_is_insufficient_evidence_not_unclosed():
    answer = await ReportInsightModule(
        _repository(
            [
                {
                    "id": "r-1",
                    "user_id": "u-weng",
                    "team_id": "team-admin",
                    "date": "2026-08-05",
                    "status": "completed",
                    "tomorrow_plan": ["准备月度例会"],
                }
            ]
        )
    ).answer(
        "看下翁亚兰有什么没闭环的工作",
        requester=_requester(),
        current_date=date(2026, 8, 6),
    )

    assert answer is not None
    assert answer.evidence.facts["unclosed_count"] == 0
    assert answer.evidence.facts["pending_evidence_count"] == 1


def test_report_insight_prompt_forbids_nested_or_invented_item_numbers():
    prompt = canary_system_prompt()

    assert "exactly one visible numbering layer" in prompt
    assert "Do not invent internal item IDs" in prompt
