from datetime import date
from time import monotonic
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.agent2.assistant_responder import AssistantReply
from app.agent2.assistant_tools import build_tool_assisted_reply
from app.agent2.cognitive_reply_v3 import build_cognitive_side_reply_v3
from app.agent2.context_pack import KnowledgeEvidenceFrame, build_agent2_context_pack
from app.agent2.knowledge_resolver import KnowledgeQuery
from app.agent2.report_insight_intent import is_report_insight_question
from app.agent2.report_insight_query import StructuredReportInsightQuery
from app.agent2.report_insights import (
    InMemoryReportInsightRepository,
    ReportInsightAnswer,
    ReportInsightModule,
    SqlReportInsightRepository,
    load_live_report_insight_adapter,
)
from app.workflows.intake import IncomingMessageEnvelope


@pytest.mark.asyncio
async def test_department_head_can_count_named_users_daily_reports():
    repository = InMemoryReportInsightRepository(
        teams=[
            {
                "id": "team-admin",
                "name": "综合管理部",
                "department_name": "法务中心",
            }
        ],
        users=[
            {
                "id": "u-manager",
                "name": "负责人",
                "team_id": "team-admin",
                "role": "department_head",
            },
            {
                "id": "u-pang",
                "name": "庞浩",
                "team_id": "team-admin",
                "role": "member",
            },
        ],
        reports=[
            {"id": "r-1", "user_id": "u-pang", "date": "2026-07-31", "status": "completed"},
            {"id": "r-2", "user_id": "u-pang", "date": "2026-08-01", "status": "completed"},
            {"id": "r-3", "user_id": "u-pang", "date": "2026-08-05", "status": "collecting"},
            {"id": "other", "user_id": "u-manager", "date": "2026-08-05", "status": "completed"},
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await module.answer(
        "庞浩目前有多少份日报了？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["report_count"] == 3
    assert answer.evidence.facts["status_counts"] == {"collecting": 1, "completed": 2}
    assert "庞浩目前共有 3 份日报" in answer.text
    assert "已完成 2 份" in answer.text
    assert "填写中 1 份" in answer.text

    completed_answer = await module.answer(
        "庞浩已经完成了多少份日报？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert completed_answer is not None
    assert completed_answer.evidence.facts["query_kind"] == "completed_report_count"
    assert completed_answer.evidence.facts["report_count"] == 2
    assert "庞浩已完成 2 份日报" in completed_answer.text


@pytest.mark.asyncio
async def test_department_head_can_summarize_named_users_recent_work():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合管理部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-pang", "name": "庞浩", "team_id": "team-admin", "role": "member"},
            {"id": "u-other", "name": "其他人", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-pang",
                "date": "2026-08-05",
                "status": "completed",
                "today_work": ["完成供应商合同审查", "推进档案系统上线"],
                "problems": ["档案系统权限仍待确认"],
                "tomorrow_plan": ["跟进权限开通"],
            },
            {
                "id": "r-2",
                "user_id": "u-pang",
                "date": "2026-08-01",
                "status": "completed",
                "today_work": ["组织月度经营会议"],
            },
            {
                "id": "old",
                "user_id": "u-pang",
                "date": "2026-07-28",
                "status": "completed",
                "today_work": ["过期工作不应出现"],
            },
            {
                "id": "other",
                "user_id": "u-other",
                "date": "2026-08-04",
                "status": "completed",
                "today_work": ["其他人的工作不应出现"],
            },
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await module.answer(
        "总结下庞浩最近的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["period_start"] == "2026-07-30"
    assert answer.evidence.facts["period_end"] == "2026-08-05"
    assert answer.evidence.facts["report_count"] == 2
    assert "完成供应商合同审查" in answer.text
    assert "推进档案系统上线" in answer.text
    assert "组织月度经营会议" in answer.text
    assert "档案系统权限仍待确认" in answer.text
    assert "过期工作不应出现" not in answer.text
    assert "其他人的工作不应出现" not in answer.text


@pytest.mark.asyncio
async def test_department_head_can_summarize_named_team_current_week_work():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-admin", "name": "综合管理部", "department_name": "法务中心"},
            {"id": "team-legal", "name": "法务一部", "department_name": "法务中心"},
        ],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-pang", "name": "庞浩", "team_id": "team-admin", "role": "member"},
            {"id": "u-chen", "name": "陈晨", "team_id": "team-admin", "role": "member"},
            {"id": "u-other", "name": "其他部门员工", "team_id": "team-legal", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-pang",
                "date": "2026-08-03",
                "status": "completed",
                "today_work": ["完成供应商准入复核"],
            },
            {
                "id": "r-2",
                "user_id": "u-chen",
                "date": "2026-08-04",
                "status": "completed",
                "today_work": ["组织办公区域安全检查"],
            },
            {
                "id": "old",
                "user_id": "u-pang",
                "date": "2026-08-02",
                "status": "completed",
                "today_work": ["上周工作不应出现"],
            },
            {
                "id": "other-team",
                "user_id": "u-other",
                "date": "2026-08-04",
                "status": "completed",
                "today_work": ["其他部门工作不应出现"],
            },
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await module.answer(
        "总结下综合管理部本周都做了什么",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["period_start"] == "2026-08-03"
    assert answer.evidence.facts["period_end"] == "2026-08-05"
    assert answer.evidence.facts["report_count"] == 2
    assert answer.evidence.facts["reporter_count"] == 2
    assert "庞浩：完成供应商准入复核" in answer.text
    assert "陈晨：组织办公区域安全检查" in answer.text
    assert "上周工作不应出现" not in answer.text
    assert "其他部门工作不应出现" not in answer.text


@pytest.mark.asyncio
async def test_team_week_summary_keeps_the_same_work_recorded_on_different_days():
    repository = InMemoryReportInsightRepository(
        teams=[
            {
                "id": "team-admin",
                "name": "综合管理部",
                "department_name": "法务中心",
            }
        ],
        users=[
            {
                "id": "u-manager",
                "name": "负责人",
                "team_id": "team-admin",
                "role": "department_head",
            },
            {
                "id": "u-pang",
                "name": "庞浩",
                "team_id": "team-admin",
                "role": "member",
            },
        ],
        reports=[
            {
                "id": "monday",
                "user_id": "u-pang",
                "date": "2026-08-10",
                "status": "completed",
                "today_work": ["日常用印审核"],
            },
            {
                "id": "tuesday",
                "user_id": "u-pang",
                "date": "2026-08-11",
                "status": "completed",
                "today_work": ["日常用印审核"],
            },
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await module.answer_query(
        StructuredReportInsightQuery(
            query="综合部本周都做了什么？",
            query_kind="period_work",
            scope_type="organization",
            scope_name="综合管理部",
            period_type="current_week",
        ),
        requester=requester,
        current_date=date(2026, 8, 14),
    )

    assert answer is not None
    assert [item["date"] for item in answer.evidence.facts["work_items"]] == [
        "2026-08-11",
        "2026-08-10",
    ]
    assert "2026-08-11（共 1 项）：" in answer.text
    assert "2026-08-10（共 1 项）：" in answer.text
    assert answer.text.count("庞浩：日常用印审核") == 2


@pytest.mark.asyncio
async def test_team_week_summary_covers_every_day_when_work_exceeds_the_old_preview_limit():
    report_dates = (
        "2026-08-10",
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
        "2026-08-14",
    )
    repository = InMemoryReportInsightRepository(
        teams=[
            {
                "id": "team-admin",
                "name": "综合管理部",
                "department_name": "法务中心",
            }
        ],
        users=[
            {
                "id": "u-manager",
                "name": "负责人",
                "team_id": "team-admin",
                "role": "department_head",
            },
            {
                "id": "u-pang",
                "name": "庞浩",
                "team_id": "team-admin",
                "role": "member",
            },
        ],
        reports=[
            {
                "id": f"report-{report_date}",
                "user_id": "u-pang",
                "date": report_date,
                "status": "completed",
                "today_work": [
                    f"{report_date}工作事项{index}" for index in range(1, 5)
                ],
            }
            for report_date in report_dates
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await module.answer_query(
        StructuredReportInsightQuery(
            query="综合部本周都做了什么？",
            query_kind="period_work",
            scope_type="organization",
            scope_name="综合管理部",
            period_type="current_week",
        ),
        requester=requester,
        current_date=date(2026, 8, 14),
    )

    assert answer is not None
    assert len(answer.evidence.facts["work_items"]) == 20
    for report_date in report_dates:
        assert report_date in answer.text
        assert f"{report_date}工作事项1" in answer.text
    assert "共 20 项" in answer.text
    assert "本次展示 16 项，另有 4 项未展开" in answer.text


@pytest.mark.asyncio
async def test_person_week_summary_keeps_repeated_work_and_covers_every_recorded_day():
    repository = InMemoryReportInsightRepository(
        teams=[
            {
                "id": "team-admin",
                "name": "综合管理部",
                "department_name": "法务中心",
            }
        ],
        users=[
            {
                "id": "u-pang",
                "name": "庞浩",
                "team_id": "team-admin",
                "role": "member",
            }
        ],
        reports=[
            {
                "id": "wednesday",
                "user_id": "u-pang",
                "date": "2026-08-12",
                "status": "completed",
                "today_work": [
                    "日常用印审核",
                    *[f"周三工作事项{index}" for index in range(1, 10)],
                ],
            },
            {
                "id": "tuesday",
                "user_id": "u-pang",
                "date": "2026-08-11",
                "status": "completed",
                "today_work": ["日常用印审核"],
            },
            {
                "id": "monday",
                "user_id": "u-pang",
                "date": "2026-08-10",
                "status": "completed",
                "today_work": ["日常用印审核"],
            },
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-pang",
        name="庞浩",
        dingtalk_user_id="dt-pang",
        team_id="team-admin",
        role="member",
    )

    answer = await module.answer_query(
        StructuredReportInsightQuery(
            query="我本周都做了什么？",
            query_kind="recent_work",
            scope_type="person",
            scope_name="庞浩",
            period_type="current_week",
        ),
        requester=requester,
        current_date=date(2026, 8, 14),
    )

    assert answer is not None
    assert len(answer.evidence.facts["work_items"]) == 12
    assert "2026-08-11（共 1 项）：" in answer.text
    assert "2026-08-10（共 1 项）：" in answer.text
    assert answer.text.count("日常用印审核") == 3
    assert "共 12 项" in answer.text
    assert "本次展示 10 项，另有 2 项未展开" in answer.text


@pytest.mark.asyncio
async def test_department_head_can_summarize_named_team_previous_week_work():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合管理部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-pang", "name": "庞浩", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "monday",
                "user_id": "u-pang",
                "date": "2026-07-27",
                "status": "completed",
                "today_work": ["完成会议室改造验收"],
            },
            {
                "id": "sunday",
                "user_id": "u-pang",
                "date": "2026-08-02",
                "status": "completed",
                "today_work": ["完成周末值班安排"],
            },
            {
                "id": "before",
                "user_id": "u-pang",
                "date": "2026-07-26",
                "status": "completed",
                "today_work": ["更早工作不应出现"],
            },
            {
                "id": "current",
                "user_id": "u-pang",
                "date": "2026-08-03",
                "status": "completed",
                "today_work": ["本周工作不应出现"],
            },
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await module.answer(
        "总结下综合管理部上周都做了什么",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["period_start"] == "2026-07-27"
    assert answer.evidence.facts["period_end"] == "2026-08-02"
    assert answer.evidence.facts["period_type"] == "previous_week"
    assert "综合管理部上周" in answer.text
    assert "完成会议室改造验收" in answer.text
    assert "完成周末值班安排" in answer.text
    assert "更早工作不应出现" not in answer.text
    assert "本周工作不应出现" not in answer.text


@pytest.mark.asyncio
async def test_department_head_can_review_recent_department_attention_items():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-admin", "name": "综合管理部", "department_name": "法务中心"},
            {"id": "team-legal", "name": "法务一部", "department_name": "法务中心"},
            {"id": "team-external", "name": "外部部门", "department_name": "其他中心"},
        ],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-pang", "name": "庞浩", "team_id": "team-admin", "role": "member"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-legal", "role": "member"},
            {"id": "u-other", "name": "外部人员", "team_id": "team-external", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-pang",
                "date": "2026-08-05",
                "status": "completed",
                "problems": ["档案系统权限仍待确认", "暂无问题"],
                "tomorrow_plan": ["跟进权限开通", "完成合同归档"],
            },
            {
                "id": "r-2",
                "user_id": "u-liu",
                "date": "2026-08-04",
                "status": "completed",
                "problems": ["重大诉讼材料尚未齐备"],
            },
            {
                "id": "old",
                "user_id": "u-liu",
                "date": "2026-07-29",
                "status": "completed",
                "problems": ["过期风险不应出现"],
            },
            {
                "id": "other-department",
                "user_id": "u-other",
                "date": "2026-08-04",
                "status": "completed",
                "problems": ["其他中心风险不应出现"],
            },
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await module.answer(
        "最近部门有什么重点需要关注的事情吗？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["scope_type"] == "department"
    assert answer.evidence.facts["scope_label"] == "法务中心"
    assert answer.evidence.facts["period_start"] == "2026-07-30"
    assert answer.evidence.facts["period_end"] == "2026-08-05"
    assert "档案系统权限仍待确认" in answer.text
    assert "重大诉讼材料尚未齐备" in answer.text
    assert "跟进权限开通" in answer.text
    assert "暂无问题" not in answer.text
    assert "完成合同归档" not in answer.text
    assert "过期风险不应出现" not in answer.text
    assert "其他中心风险不应出现" not in answer.text


@pytest.mark.asyncio
async def test_regular_member_cannot_read_another_members_reports():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合管理部", "department_name": "法务中心"}],
        users=[
            {"id": "u-member", "name": "普通员工", "team_id": "team-admin", "role": "member"},
            {"id": "u-pang", "name": "庞浩", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "secret",
                "user_id": "u-pang",
                "date": "2026-08-05",
                "status": "completed",
                "today_work": ["不应泄露的工作内容"],
            }
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-member",
        name="普通员工",
        dingtalk_user_id="dt-member",
        team_id="team-admin",
        role="member",
    )

    answer = await module.answer(
        "总结下庞浩最近的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["permission_allowed"] is False
    assert "没有权限查看庞浩的日报" in answer.text
    assert "不应泄露的工作内容" not in answer.text


@pytest.mark.asyncio
async def test_display_name_cannot_grant_all_department_access():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-a", "name": "一组", "department_name": "法务中心"},
            {"id": "team-b", "name": "二组", "department_name": "综合管理部"},
        ],
        users=[
            {"id": "u-impostor", "name": "庞浩", "team_id": "team-a", "role": "member"},
            {"id": "u-target", "name": "赵小明", "team_id": "team-b", "role": "member"},
        ],
        reports=[
            {
                "id": "secret",
                "user_id": "u-target",
                "team_id": "team-b",
                "date": "2026-08-05",
                "status": "completed",
                "today_work": ["不应泄露的跨部门内容"],
            }
        ],
    )
    requester = SimpleNamespace(
        id="u-impostor",
        name="庞浩",
        dingtalk_user_id="not-the-privileged-id",
        team_id="team-a",
        role="member",
    )

    answer = await ReportInsightModule(repository).answer(
        "总结下赵小明最近的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["permission_allowed"] is False
    assert "不应泄露的跨部门内容" not in answer.text


@pytest.mark.asyncio
async def test_named_department_week_summary_combines_all_teams_in_department():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-admin-1", "name": "行政组", "department_name": "综合管理部"},
            {"id": "team-admin-2", "name": "信息化组", "department_name": "综合管理部"},
        ],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin-1", "role": "department_head"},
            {"id": "u-a", "name": "行政同事", "team_id": "team-admin-1", "role": "member"},
            {"id": "u-b", "name": "信息化同事", "team_id": "team-admin-2", "role": "member"},
        ],
        reports=[
            {
                "id": "r-a",
                "user_id": "u-a",
                "date": "2026-08-03",
                "status": "completed",
                "today_work": ["完成办公用品盘点"],
            },
            {
                "id": "r-b",
                "user_id": "u-b",
                "date": "2026-08-04",
                "status": "completed",
                "today_work": ["完成网络设备巡检"],
            },
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin-1",
        role="department_head",
    )

    answer = await module.answer(
        "总结下综合管理部本周都做了什么",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["scope_type"] == "department"
    assert answer.evidence.facts["scope_label"] == "综合管理部"
    assert set(answer.evidence.facts["target_team_ids"]) == {"team-admin-1", "team-admin-2"}
    assert "行政同事：完成办公用品盘点" in answer.text
    assert "信息化同事：完成网络设备巡检" in answer.text


@pytest.mark.asyncio
async def test_report_count_returns_a_real_zero_when_no_reports_exist():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合管理部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-pang", "name": "庞浩", "team_id": "team-admin", "role": "member"},
        ],
        reports=[],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await module.answer(
        "庞浩目前有多少份日报了？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["report_count"] == 0
    assert "庞浩目前共有 0 份日报" in answer.text


@pytest.mark.asyncio
async def test_team_leader_recent_department_attention_is_limited_to_own_team():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-a", "name": "综合管理一组", "department_name": "综合管理部"},
            {"id": "team-b", "name": "综合管理二组", "department_name": "综合管理部"},
        ],
        users=[
            {"id": "u-leader", "name": "一组负责人", "team_id": "team-a", "role": "team_leader"},
            {"id": "u-a", "name": "一组员工", "team_id": "team-a", "role": "member"},
            {"id": "u-b", "name": "二组员工", "team_id": "team-b", "role": "member"},
        ],
        reports=[
            {
                "id": "r-a",
                "user_id": "u-a",
                "date": "2026-08-05",
                "status": "completed",
                "problems": ["一组系统权限待确认"],
            },
            {
                "id": "r-b",
                "user_id": "u-b",
                "date": "2026-08-05",
                "status": "completed",
                "problems": ["二组信息不应出现"],
            },
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-leader",
        name="一组负责人",
        dingtalk_user_id="dt-leader",
        team_id="team-a",
        role="team_leader",
    )

    answer = await module.answer(
        "最近部门有什么重点需要关注的事情吗？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["permission_allowed"] is True
    assert answer.evidence.facts["scope_type"] == "team"
    assert answer.evidence.facts["scope_label"] == "综合管理一组"
    assert "一组系统权限待确认" in answer.text
    assert "二组信息不应出现" not in answer.text


@pytest.mark.asyncio
async def test_all_access_user_can_name_another_department_for_recent_attention():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-legal", "name": "法务一组", "department_name": "法务中心"},
            {"id": "team-admin", "name": "行政组", "department_name": "综合管理部"},
        ],
        users=[
            {"id": "u-admin", "name": "系统管理员", "team_id": "team-legal", "role": "member"},
            {"id": "u-target", "name": "行政同事", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "target-risk",
                "user_id": "u-target",
                "team_id": "team-admin",
                "date": "2026-08-05",
                "status": "completed",
                "problems": ["办公系统权限需要协调"],
            }
        ],
    )
    requester = SimpleNamespace(
        id="u-admin",
        name="系统管理员",
        dingtalk_user_id="0515246015778891",
        team_id="team-legal",
        role="member",
    )

    answer = await ReportInsightModule(repository).answer(
        "最近综合管理部有什么重点需要关注？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["scope_type"] == "department"
    assert answer.evidence.facts["scope_label"] == "综合管理部"
    assert "办公系统权限需要协调" in answer.text


@pytest.mark.asyncio
async def test_team_summary_uses_report_team_at_submission_time_after_user_transfer():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-new", "name": "综合管理部", "department_name": "管理中心"},
            {"id": "team-old", "name": "原工作组", "department_name": "其他中心"},
        ],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-new", "role": "department_head"},
            {"id": "u-transfer", "name": "欧阳小明", "team_id": "team-new", "role": "member"},
        ],
        reports=[
            {
                "id": "new-team-report",
                "user_id": "u-transfer",
                "team_id": "team-new",
                "date": "2026-08-04",
                "status": "completed",
                "today_work": ["调组后的新部门工作"],
            },
            {
                "id": "old-team-report",
                "user_id": "u-transfer",
                "team_id": "team-old",
                "date": "2026-08-03",
                "status": "completed",
                "today_work": ["调组前的旧部门工作不应出现"],
            },
        ],
    )
    module = ReportInsightModule(repository)
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-new",
        role="department_head",
    )

    answer = await module.answer(
        "总结下综合管理部本周都做了什么",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["report_count"] == 1
    assert "调组后的新部门工作" in answer.text
    assert "调组前的旧部门工作不应出现" not in answer.text


@pytest.mark.asyncio
async def test_old_team_summary_keeps_report_submitted_before_user_transfer():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-new", "name": "新工作组", "department_name": "管理中心"},
            {"id": "team-old", "name": "原工作组", "department_name": "其他中心"},
        ],
        users=[
            {"id": "u-old-manager", "name": "原组负责人", "team_id": "team-old", "role": "department_head"},
            {"id": "u-transfer", "name": "欧阳小明", "team_id": "team-new", "role": "member"},
        ],
        reports=[
            {
                "id": "old-team-report",
                "user_id": "u-transfer",
                "team_id": "team-old",
                "date": "2026-08-03",
                "status": "completed",
                "today_work": ["调组前为原工作组完成的工作"],
            },
        ],
    )
    requester = SimpleNamespace(
        id="u-old-manager",
        name="原组负责人",
        dingtalk_user_id="dt-old-manager",
        team_id="team-old",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "总结下原工作组本周都做了什么",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["report_count"] == 1
    assert "调组前为原工作组完成的工作" in answer.text


@pytest.mark.asyncio
async def test_new_department_head_sees_only_post_transfer_reports_in_person_summary():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-new", "name": "新工作组", "department_name": "管理中心"},
            {"id": "team-old", "name": "原工作组", "department_name": "其他中心"},
        ],
        users=[
            {"id": "u-manager", "name": "新组负责人", "team_id": "team-new", "role": "department_head"},
            {"id": "u-transfer", "name": "欧阳小明", "team_id": "team-new", "role": "member"},
        ],
        reports=[
            {
                "id": "new-team-report",
                "user_id": "u-transfer",
                "team_id": "team-new",
                "date": "2026-08-05",
                "status": "completed",
                "today_work": ["调组后可查看的工作"],
            },
            {
                "id": "old-team-report",
                "user_id": "u-transfer",
                "team_id": "team-old",
                "date": "2026-08-03",
                "status": "completed",
                "today_work": ["调组前不可查看的旧部门工作"],
            },
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="新组负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-new",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "总结下欧阳小明最近的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["report_count"] == 1
    assert answer.evidence.facts["permission_scope_team_ids"] == ["team-new"]
    assert "调组后可查看的工作" in answer.text
    assert "调组前不可查看的旧部门工作" not in answer.text


@pytest.mark.asyncio
async def test_new_department_head_counts_only_post_transfer_reports():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-new", "name": "新工作组", "department_name": "管理中心"},
            {"id": "team-old", "name": "原工作组", "department_name": "其他中心"},
        ],
        users=[
            {"id": "u-manager", "name": "新组负责人", "team_id": "team-new", "role": "department_head"},
            {"id": "u-transfer", "name": "欧阳小明", "team_id": "team-new", "role": "member"},
        ],
        reports=[
            {"id": "new", "user_id": "u-transfer", "team_id": "team-new", "date": "2026-08-05", "status": "completed"},
            {"id": "old", "user_id": "u-transfer", "team_id": "team-old", "date": "2026-08-03", "status": "completed"},
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="新组负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-new",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "请查欧阳小明目前有多少份日报了？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["report_count"] == 1
    assert answer.evidence.facts["permission_scope_team_ids"] == ["team-new"]
    assert "可查看范围内" in answer.text


@pytest.mark.asyncio
async def test_unknown_person_returns_a_safe_clarification_instead_of_falling_back():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合管理部", "department_name": "法务中心"}],
        users=[{"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"}],
        reports=[],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "王五目前有多少份日报了？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["resolution_status"] == "not_found"
    assert "没有找到" in answer.text
    assert "请确认姓名" in answer.text


@pytest.mark.asyncio
async def test_duplicate_person_name_returns_a_safe_clarification():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-a", "name": "一组", "department_name": "法务中心"},
            {"id": "team-b", "name": "二组", "department_name": "法务中心"},
        ],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-a", "role": "department_head"},
            {"id": "u-zhang-a", "name": "张伟", "team_id": "team-a", "role": "member"},
            {"id": "u-zhang-b", "name": "张伟", "team_id": "team-b", "role": "member"},
        ],
        reports=[],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-a",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "张伟目前有多少份日报了？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["resolution_status"] == "ambiguous"
    assert "多位同名人员" in answer.text
    assert "部门或团队" in answer.text


@pytest.mark.asyncio
async def test_duplicate_person_name_can_be_disambiguated_by_team():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-a", "name": "法务一组", "department_name": "法务中心"},
            {"id": "team-b", "name": "法务二组", "department_name": "法务中心"},
        ],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-a", "role": "department_head"},
            {"id": "u-zhang-a", "name": "张伟", "team_id": "team-a", "role": "member"},
            {"id": "u-zhang-b", "name": "张伟", "team_id": "team-b", "role": "member"},
        ],
        reports=[
            {"id": "a-1", "user_id": "u-zhang-a", "team_id": "team-a", "date": "2026-08-05", "status": "completed"},
            {"id": "b-1", "user_id": "u-zhang-b", "team_id": "team-b", "date": "2026-08-05", "status": "completed"},
            {"id": "b-2", "user_id": "u-zhang-b", "team_id": "team-b", "date": "2026-08-04", "status": "completed"},
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-a",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "法务一组的张伟目前有多少份日报了？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["target_user_ids"] == ["u-zhang-a"]
    assert answer.evidence.facts["report_count"] == 1


@pytest.mark.asyncio
async def test_person_query_rejects_extra_text_prepended_to_a_real_scope():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-a", "name": "法务一组", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-a", "role": "department_head"},
            {"id": "u-zhang", "name": "张伟", "team_id": "team-a", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-zhang",
                "team_id": "team-a",
                "date": "2026-08-05",
                "status": "completed",
            }
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-a",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "查合同法务一组的张伟目前有多少份日报了？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is None


@pytest.mark.asyncio
async def test_unknown_team_returns_a_safe_clarification():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合管理部", "department_name": "法务中心"}],
        users=[{"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"}],
        reports=[],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "总结下不存在部门本周都做了什么",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["resolution_status"] == "not_found"
    assert "没有找到" in answer.text
    assert "部门或团队" in answer.text


@pytest.mark.asyncio
async def test_longest_person_name_wins_over_a_shorter_substring_name():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合管理部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-pang", "name": "庞浩", "team_id": "team-admin", "role": "member"},
            {"id": "u-pang-long", "name": "庞浩然", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {"id": "short", "user_id": "u-pang", "date": "2026-08-05", "status": "completed"},
            {"id": "long", "user_id": "u-pang-long", "date": "2026-08-05", "status": "completed"},
            {"id": "long-2", "user_id": "u-pang-long", "date": "2026-08-04", "status": "completed"},
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "庞浩然目前有多少份日报了？",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["scope_label"] == "庞浩然"
    assert answer.evidence.facts["report_count"] == 2


@pytest.mark.asyncio
async def test_live_report_insight_adapter_answers_count_from_database_rows(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "app.agent2.report_insights.get_settings",
        lambda: SimpleNamespace(legal_daily_dashboard_tenant_id=""),
    )
    team_id = UUID("10000000-0000-0000-0000-000000000001")
    manager_id = UUID("20000000-0000-0000-0000-000000000001")
    pang_id = UUID("20000000-0000-0000-0000-000000000002")
    team = SimpleNamespace(id=team_id, name="综合管理部", department_name="法务中心")
    manager = SimpleNamespace(
        id=manager_id,
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id=team_id,
        team=team,
        role="department_head",
    )
    pang = SimpleNamespace(
        id=pang_id,
        name="庞浩",
        dingtalk_user_id="dt-pang",
        team_id=team_id,
        team=team,
        role="member",
    )

    class _Scalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    class _Result:
        def __init__(self, *, values=(), rows=()):
            self._values = values
            self._rows = rows

        def scalars(self):
            return _Scalars(self._values)

        def all(self):
            return self._rows

    class _Session:
        def __init__(self):
            self.results = [
                _Result(values=[manager, pang]),
                _Result(values=[team]),
                _Result(rows=[("completed", 2), ("collecting", 1)]),
            ]

        async def execute(self, statement):
            statement.compile(compile_kwargs={"literal_binds": True})
            return self.results.pop(0)

    text = "庞浩目前有多少份日报了？"
    adapter = await load_live_report_insight_adapter(
        _Session(),
        requester=manager,
        text=text,
        current_date=date(2026, 8, 5),
    )
    evidence = adapter.resolve(KnowledgeQuery(text=text, user_id=str(manager_id)))

    assert len(evidence) == 1
    assert evidence[0].facts["report_count"] == 3
    assert "庞浩目前共有 3 份日报" in evidence[0].summary


@pytest.mark.asyncio
async def test_sql_report_insight_repository_filters_by_report_team_id():
    user_id = UUID("20000000-0000-0000-0000-000000000002")
    team_id = UUID("10000000-0000-0000-0000-000000000001")

    class _Scalars:
        def all(self):
            return []

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result()

    session = _Session()
    reports = await SqlReportInsightRepository(session).list_reports(
        user_ids=[str(user_id)],
        team_ids=[str(team_id)],
        start_date=date(2026, 8, 3),
        end_date=date(2026, 8, 5),
    )

    compiled = str(session.statement.compile(compile_kwargs={"literal_binds": True}))
    assert reports == ()
    assert "daily_reports.team_id IN" in compiled
    assert team_id.hex in compiled
    assert "daily_reports.user_id IN" in compiled
    assert user_id.hex in compiled


@pytest.mark.asyncio
async def test_sql_repository_uses_only_the_formal_roster_and_keeps_center_direct_members(
    monkeypatch: pytest.MonkeyPatch,
):
    outside_user = SimpleNamespace(
        id=UUID("20000000-0000-0000-0000-000000000001"),
        name="名单外启用账号",
        team_id=UUID("10000000-0000-0000-0000-000000000001"),
        active=True,
    )
    roster_user = SimpleNamespace(
        id=UUID("20000000-0000-0000-0000-000000000002"),
        name="名单成员",
        team_id=UUID("10000000-0000-0000-0000-000000000002"),
        active=False,
    )
    center_user = SimpleNamespace(
        id=UUID("20000000-0000-0000-0000-000000000003"),
        name="赵卫中",
        team_id=UUID("10000000-0000-0000-0000-000000000003"),
        active=True,
    )

    async def fake_get_active_users(_session):
        return [outside_user]

    monkeypatch.setattr(
        "app.agent2.report_insights.get_active_users",
        fake_get_active_users,
    )

    async def fake_load_roster(*_args, **_kwargs):
        return SimpleNamespace(
            user_ids=(str(roster_user.id), str(center_user.id)),
            team_ids=(str(roster_user.team_id), str(center_user.team_id)),
            member_count=2,
        )

    monkeypatch.setattr(
        "app.agent2.report_insights.load_formal_legal_daily_roster",
        fake_load_roster,
    )

    class _Scalars:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class _Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return _Scalars(self.values)

    class _Session:
        def __init__(self):
            self.calls = []

        async def execute(self, statement, params=None):
            self.calls.append((statement, params))
            return _Result([roster_user, center_user])

    session = _Session()
    users = await SqlReportInsightRepository(
        session,
        roster_date=date(2026, 8, 6),
        roster_tenant_id="legal-daily-production-v1",
    ).list_users()

    assert [user.id for user in users] == [roster_user.id, center_user.id]
    assert roster_user.active is False
    assert center_user.name == "赵卫中"
    user_query = str(session.calls[0][0])
    assert "users.id IN" in user_query
    assert "WHERE users.active" not in user_query


@pytest.mark.asyncio
async def test_sql_repository_includes_center_holder_for_department_scope(
    monkeypatch: pytest.MonkeyPatch,
):
    child_team_id = UUID("10000000-0000-0000-0000-000000000002")
    center_team_id = UUID("10000000-0000-0000-0000-000000000003")
    child_team = SimpleNamespace(
        id=child_team_id,
        name="法务二部",
        department_name="法务合约中心",
        code="monthly-law-2",
        active=True,
    )
    center_team = SimpleNamespace(
        id=center_team_id,
        name="法务合约中心（中心层级）",
        department_name="法务合约中心",
        code="legal-center",
        active=False,
    )

    async def fake_load_roster(*_args, **_kwargs):
        return SimpleNamespace(
            user_ids=(),
            team_ids=(str(child_team_id), str(center_team_id)),
            member_count=0,
        )

    monkeypatch.setattr(
        "app.agent2.report_insights.load_formal_legal_daily_roster",
        fake_load_roster,
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [child_team, center_team]

    class _Session:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result()

    session = _Session()
    teams = await SqlReportInsightRepository(
        session,
        roster_date=date(2026, 8, 6),
        roster_tenant_id="legal-daily-production-v1",
    ).list_teams()

    assert [team.name for team in teams] == [
        "法务二部",
        "法务合约中心（中心层级）",
    ]
    compiled = str(session.statement)
    assert "teams.id IN" in compiled
    assert "WHERE teams.active" not in compiled


@pytest.mark.asyncio
async def test_sql_person_count_filters_by_authorized_submission_teams():
    user_id = UUID("20000000-0000-0000-0000-000000000002")
    team_id = UUID("10000000-0000-0000-0000-000000000001")

    class _Result:
        def all(self):
            return [("completed", 1)]

    class _Session:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result()

    session = _Session()
    counts = await SqlReportInsightRepository(session).count_reports(
        user_ids=[str(user_id)],
        team_ids=[str(team_id)],
        end_date=date(2026, 8, 5),
    )

    compiled = str(session.statement.compile(compile_kwargs={"literal_binds": True}))
    assert counts == {"completed": 1}
    assert "daily_reports.team_id IN" in compiled
    assert team_id.hex in compiled
    assert "daily_reports.user_id IN" in compiled
    assert user_id.hex in compiled


@pytest.mark.asyncio
async def test_sql_team_summary_uses_submission_team_without_current_user_filter():
    team_id = UUID("10000000-0000-0000-0000-000000000001")

    class _Scalars:
        def all(self):
            return []

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result()

    session = _Session()
    await SqlReportInsightRepository(session).list_reports(
        user_ids=None,
        team_ids=[str(team_id)],
        start_date=date(2026, 8, 3),
        end_date=date(2026, 8, 5),
    )

    compiled = str(session.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "daily_reports.team_id IN" in compiled
    assert team_id.hex in compiled
    assert "daily_reports.user_id IN" not in compiled


@pytest.mark.parametrize(
    "statement",
    [
        "今天完成了日报数量统计",
        "最近这部分工作有问题",
        "上周工作挺忙",
        "今天完成了上周工作总结",
        "整理了综合管理部本周工作总结",
        "综合管理部本周放假吗？",
        "综合管理部上周有人请假吗？",
        "庞浩本周请假吗？",
    ],
)
def test_non_question_work_statements_are_not_report_insight_queries(statement: str):
    assert is_report_insight_question(statement) is False


@pytest.mark.parametrize(
    "question",
    [
        "庞浩已经完成了多少份日报？",
        "请总结一下庞浩最近已完成的工作",
        "庞浩最近完成了哪些工作？",
        "综合管理部本周完成了什么？",
        "看下刘聪有什么没闭环的工作。",
        "看下综合部没闭环的工作",
    ],
)
def test_natural_report_insight_question_forms_are_supported(question: str):
    assert is_report_insight_question(question) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "expected_kind"),
    [
        ("庞浩已经完成了多少份日报？", "completed_report_count"),
        ("请总结一下庞浩最近已完成的工作", "recent_work"),
        ("庞浩最近完成了哪些工作？", "recent_work"),
        ("综合管理部本周完成了什么？", "team_work_summary"),
    ],
)
async def test_natural_query_forms_reach_the_grounded_report_module(
    question: str,
    expected_kind: str,
):
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合管理部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-pang", "name": "庞浩", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-pang",
                "team_id": "team-admin",
                "date": "2026-08-05",
                "status": "completed",
                "today_work": ["完成供应商合同审查"],
            }
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        question,
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["query_kind"] == expected_kind


@pytest.mark.asyncio
async def test_person_unclosed_work_compares_plans_only_with_later_work_entries():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": ["跟进档案系统权限开通", "核对供应商台账", "跟进预算审批A"],
            },
            {
                "id": "r-2",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-02",
                "today_work": ["完成档案系统权限开通", "完成预算审批B"],
            },
            {
                "id": "r-3",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-03",
                "tomorrow_plan": ["继续核对供应商台账"],
            },
            {
                "id": "r-4",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-04",
                "today_work": ["准备月度例会"],
                "tomorrow_plan": ["准备月度例会"],
            },
            {
                "id": "r-5",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-05",
                "today_work": ["整理会议纪要"],
                "tomorrow_plan": ["仅今天新增计划"],
            },
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下刘聪有什么没闭环的工作。",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["query_kind"] == "person_unclosed_work"
    assert answer.evidence.facts["unclosed_count"] == 3
    assert [item["plan_text"] for item in answer.evidence.facts["unclosed_items"]] == [
        "跟进预算审批A",
        "继续核对供应商台账",
        "准备月度例会",
    ]
    assert answer.evidence.facts["unclosed_items"][1]["first_plan_date"] == "2026-08-01"
    assert answer.evidence.facts["unclosed_items"][1]["last_plan_date"] == "2026-08-03"
    assert "档案系统权限开通" not in answer.text
    assert "仅今天新增计划" not in answer.text
    assert "3 项" in answer.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "later_work",
    [
        "继续跟进预算审批，尚未完成",
        "预算审批仍待处理",
        "预算审批尚未开始",
        "预算审批未通过",
        "预算审批仍需跟进",
        "预算审批待处理",
        "预算审批被退回",
        "预算审批进行中",
        "需继续跟进预算审批",
        "预算审批未能完成",
        "预算审批无法完成",
        "预算审批需进一步跟进",
        "预算审批需要后续跟进",
        "预算审批正在协调",
        "预算审批沟通中",
        "预算审批，当前仍在推进",
        "预算审批，目前尚未完成",
        "预算审批，后续还需跟进",
        "预算审批已完成后被退回",
        "预算审批未获通过",
        "预算审批尚未获批",
        "预算审批没有取得进展",
        "预算审批不通过",
        "预算审批未能取得进展",
        "预算审批需要后续继续跟进",
        "预算审批已完成，后又被退回",
        "预算审批，目前处于推进中",
        "预算审批尚未全部完成",
        "预算审批还没有彻底完成",
        "预算审批仅完成一半",
        "预算审批只完成了部分工作",
        "预算审批完成了一半",
        "预算审批部分工作已完成",
        "预算审批失败",
        "预算审批受阻",
        "继续推进预算审批",
        "预算审批持续跟进",
        "预算审批未成功",
        "预算审批完成八成",
        "预算审批完成三分之二",
        "预算审批，整体仍在推进",
        "预算审批需补充材料",
        "预算审批需修改后重新提交",
        "预算审批需返工",
        "尚未解决预算审批受阻问题",
        "正在处理预算审批受阻问题",
        "继续处理预算审批受阻问题",
        "待处理预算审批受阻问题",
    ],
)
async def test_explicitly_unfinished_later_work_does_not_close_a_plan(later_work: str):
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": ["跟进预算审批"],
            },
            {
                "id": "r-2",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-02",
                "today_work": [later_work],
            },
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下刘聪有什么没闭环的工作。",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["unclosed_count"] == 1
    assert answer.evidence.facts["unclosed_items"][0]["plan_text"] == "跟进预算审批"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_text", "later_work"),
    [
        ("跟进预算审批", "预算审批已完成，不需要继续跟进"),
        ("跟进预算审批", "预算审批被退回后现已通过"),
        ("跟进预算审批", "预算审批不再需要继续跟进"),
        ("跟进预算审批", "预算审批被退回，后来已经通过"),
        ("跟进预算审批", "预算审批没有未完成事项"),
        ("跟进预算审批", "预算审批没有需要继续跟进的事项"),
        ("跟进预算审批", "预算审批不需要进一步审批"),
        ("跟进预算审批", "已解决预算审批受阻问题"),
        ("跟进合同归档", "完成合同归档，预算审批仍待处理"),
        ("协调预算审批", "沟通预算审批"),
    ],
)
async def test_completed_matching_clause_closes_without_being_blocked_by_other_text(
    plan_text: str,
    later_work: str,
):
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": [plan_text],
            },
            {
                "id": "r-2",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-02",
                "today_work": [later_work],
            },
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下刘聪有什么没闭环的工作。",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["unclosed_count"] == 0


@pytest.mark.asyncio
async def test_different_chinese_scope_qualifiers_do_not_close_each_other():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": ["跟进南区预算审批"],
            },
            {
                "id": "r-2",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-02",
                "today_work": ["完成北区预算审批"],
            },
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下刘聪有什么没闭环的工作。",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["unclosed_count"] == 1
    assert answer.evidence.facts["unclosed_items"][0]["plan_text"] == "跟进南区预算审批"


@pytest.mark.asyncio
async def test_unclosed_work_matching_scales_for_many_unique_plans():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-many",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": [f"核对唯一事项{index:04d}" for index in range(4000)],
            }
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    started_at = monotonic()
    answer = await ReportInsightModule(repository).answer(
        "看下刘聪有什么没闭环的工作。",
        requester=requester,
        current_date=date(2026, 8, 5),
    )
    elapsed = monotonic() - started_at

    assert answer is not None
    assert answer.evidence.facts["unclosed_count"] == 4000
    assert elapsed < 3.0


@pytest.mark.asyncio
async def test_unclosed_work_matching_scales_across_many_people():
    users = [
        {"id": "u-admin", "name": "系统管理员", "team_id": "team-admin", "role": "member"},
        *[
            {
                "id": f"u-{user_index:03d}",
                "name": f"成员{user_index:03d}",
                "team_id": "team-admin",
                "role": "member",
            }
            for user_index in range(100)
        ],
    ]
    reports = [
        {
            "id": f"r-{user_index:03d}",
            "user_id": f"u-{user_index:03d}",
            "team_id": "team-admin",
            "date": "2026-08-01",
            "tomorrow_plan": [
                f"核对成员{user_index:03d}唯一事项{plan_index:03d}"
                for plan_index in range(80)
            ],
        }
        for user_index in range(100)
    ]
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=users,
        reports=reports,
    )
    requester = SimpleNamespace(
        id="u-admin",
        name="系统管理员",
        dingtalk_user_id="0515246015778891",
        team_id="team-admin",
        role="member",
    )

    started_at = monotonic()
    answer = await ReportInsightModule(repository).answer(
        "看下综合部没闭环的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )
    elapsed = monotonic() - started_at

    assert answer is not None
    assert answer.evidence.facts["unclosed_count"] == 8000
    assert elapsed < 6.0


@pytest.mark.asyncio
async def test_multi_item_work_entry_still_closes_each_matching_plan():
    extra_plans = [f"整理合同材料{index}" for index in range(80)]
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=[
            {"id": "u-admin", "name": "系统管理员", "team_id": "team-admin", "role": "member"},
            {"id": "u-a", "name": "成员甲", "team_id": "team-admin", "role": "member"},
            {"id": "u-b", "name": "成员乙", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-a",
                "user_id": "u-a",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": ["跟进预算审批", *extra_plans],
            },
            {
                "id": "r-b",
                "user_id": "u-b",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": ["跟进预算审批"],
            },
            {
                "id": "r-close",
                "user_id": "u-b",
                "team_id": "team-admin",
                "date": "2026-08-02",
                "today_work": ["已完成预算审批并整理合同材料并发送邮件"],
            },
        ],
    )
    requester = SimpleNamespace(
        id="u-admin",
        name="系统管理员",
        dingtalk_user_id="0515246015778891",
        team_id="team-admin",
        role="member",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下综合部没闭环的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert all(
        item["plan_text"] != "跟进预算审批"
        for item in answer.evidence.facts["unclosed_items"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan_items", "later_work", "expected_unclosed"),
    [
        (
            ["跟进合同归档", "跟进预算审批"],
            "合同归档仍待处理同时预算审批已完成",
            ["跟进合同归档"],
        ),
        (
            ["跟进合同归档", "跟进预算审批"],
            "合同归档仍待处理和预算审批已完成",
            ["跟进合同归档"],
        ),
        (
            ["跟进合同归档", "跟进预算审批"],
            "合同归档已完成、预算审批仍待处理",
            ["跟进预算审批"],
        ),
        (
            ["跟进合同归档", "跟进预算审批"],
            "合同归档仍待处理，同时已完成预算审批",
            ["跟进合同归档"],
        ),
        (
            ["跟进和解协议", "跟进预算审批"],
            "和解协议仍待处理，同时预算审批已完成",
            ["跟进和解协议"],
        ),
    ],
)
async def test_status_in_one_work_item_does_not_leak_into_another_item(
    plan_items: list[str],
    later_work: str,
    expected_unclosed: list[str],
):
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=[
            {"id": "u-admin", "name": "系统管理员", "team_id": "team-admin", "role": "member"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-plan",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": plan_items,
            },
            {
                "id": "r-work",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-02",
                "today_work": [later_work],
            },
        ],
    )
    requester = SimpleNamespace(
        id="u-admin",
        name="系统管理员",
        dingtalk_user_id="0515246015778891",
        team_id="team-admin",
        role="member",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下综合部没闭环的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert [
        item["plan_text"] for item in answer.evidence.facts["unclosed_items"]
    ] == expected_unclosed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_name", "question"),
    [
        ("叶浩", "叶浩目前有多少份日报了？"),
        ("覃敏", "总结下覃敏最近的工作"),
        ("黎明", "看下黎明有什么没闭环的工作"),
        ("肖明", "肖明目前有多少份日报了？"),
        ("白雪", "总结下白雪最近的工作"),
        ("侯军", "看下侯军有什么没闭环的工作"),
        ("叶青", "总结下叶青最近的工作"),
        ("覃伟", "覃伟目前有多少份日报了？"),
    ],
)
async def test_directory_names_are_not_rejected_by_a_built_in_surname_list(
    target_name: str,
    question: str,
):
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-target", "name": target_name, "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-target",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "status": "completed",
                "today_work": ["完成合同归档"],
                "tomorrow_plan": ["跟进预算审批"],
            }
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        question,
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["scope_label"] == target_name


@pytest.mark.asyncio
async def test_team_unclosed_work_can_be_closed_by_a_later_entry_from_another_member():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-admin", "role": "member"},
            {"id": "u-zhang", "name": "张敏", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": ["整理培训材料", "跟进预算审批"],
            },
            {
                "id": "r-2",
                "user_id": "u-zhang",
                "team_id": "team-admin",
                "date": "2026-08-02",
                "today_work": ["完成培训材料整理"],
            },
            {
                "id": "r-3",
                "user_id": "u-zhang",
                "team_id": "team-admin",
                "date": "2026-08-03",
                "tomorrow_plan": ["复核供应商名单"],
            },
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下综合部没闭环的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["query_kind"] == "organization_unclosed_work"
    assert answer.evidence.facts["unclosed_count"] == 2
    assert {item["plan_text"] for item in answer.evidence.facts["unclosed_items"]} == {
        "跟进预算审批",
        "复核供应商名单",
    }
    assert "培训材料" not in answer.text
    assert "刘聪：跟进预算审批" in answer.text
    assert "张敏：复核供应商名单" in answer.text


@pytest.mark.asyncio
async def test_department_closes_same_item_for_more_than_sixty_four_members():
    plan_users = [
        {
            "id": f"u-member-{index}",
            "name": f"成员{index}",
            "team_id": "team-admin",
            "role": "member",
        }
        for index in range(65)
    ]
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合部", "department_name": "法务中心"}],
        users=[
            {"id": "u-admin", "name": "系统管理员", "team_id": "team-admin", "role": "member"},
            *plan_users,
            {"id": "u-closer", "name": "闭环人", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            *[
                {
                    "id": f"r-plan-{index}",
                    "user_id": f"u-member-{index}",
                    "team_id": "team-admin",
                    "date": "2026-08-01",
                    "tomorrow_plan": ["整理培训材料"],
                }
                for index in range(65)
            ],
            {
                "id": "r-close",
                "user_id": "u-closer",
                "team_id": "team-admin",
                "date": "2026-08-02",
                "today_work": ["完成培训材料整理"],
            },
        ],
    )
    requester = SimpleNamespace(
        id="u-admin",
        name="系统管理员",
        dingtalk_user_id="0515246015778891",
        team_id="team-admin",
        role="member",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下综合部没闭环的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["unclosed_count"] == 0


@pytest.mark.asyncio
async def test_team_unclosed_work_keeps_the_existing_organization_permission_boundary():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-admin", "name": "综合部", "department_name": "职能中心"},
            {"id": "team-legal", "name": "法务一组", "department_name": "法务中心"},
        ],
        users=[
            {"id": "u-leader", "name": "法务负责人", "team_id": "team-legal", "role": "team_leader"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": ["跟进预算审批"],
            }
        ],
    )
    requester = SimpleNamespace(
        id="u-leader",
        name="法务负责人",
        dingtalk_user_id="dt-legal-leader",
        team_id="team-legal",
        role="team_leader",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下综合部没闭环的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["query_kind"] == "permission_denied"
    assert answer.evidence.facts["permission_allowed"] is False
    assert "没有权限" in answer.text


@pytest.mark.asyncio
async def test_unclosed_work_resolves_the_common_short_name_for_comprehensive_management():
    repository = InMemoryReportInsightRepository(
        teams=[
            {"id": "team-legal", "name": "法务一组", "department_name": "法务中心"},
            {"id": "team-admin", "name": "行政组", "department_name": "综合管理部"},
        ],
        users=[
            {"id": "u-admin", "name": "系统管理员", "team_id": "team-legal", "role": "member"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": ["跟进预算审批"],
            }
        ],
    )
    requester = SimpleNamespace(
        id="u-admin",
        name="系统管理员",
        dingtalk_user_id="0515246015778891",
        team_id="team-legal",
        role="member",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下综合部没闭环的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["scope_label"] == "综合管理部"
    assert answer.evidence.facts["target_team_ids"] == ["team-admin"]
    assert "跟进预算审批" in answer.text


@pytest.mark.asyncio
async def test_exact_department_name_wins_over_another_teams_short_name():
    repository = InMemoryReportInsightRepository(
        teams=[
            {
                "id": "team-management",
                "name": "综合管理部",
                "department_name": "职能中心",
            },
            {
                "id": "team-general",
                "name": "行政组",
                "department_name": "综合部",
            },
        ],
        users=[
            {
                "id": "u-admin",
                "name": "系统管理员",
                "team_id": "team-management",
                "role": "member",
            },
            {
                "id": "u-liu",
                "name": "刘聪",
                "team_id": "team-general",
                "role": "member",
            },
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-liu",
                "team_id": "team-general",
                "date": "2026-08-01",
                "tomorrow_plan": ["跟进预算审批"],
            }
        ],
    )
    requester = SimpleNamespace(
        id="u-admin",
        name="系统管理员",
        dingtalk_user_id="0515246015778891",
        team_id="team-management",
        role="member",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下综合部没闭环的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["scope_label"] == "综合部"
    assert answer.evidence.facts["target_team_ids"] == ["team-general"]
    assert "跟进预算审批" in answer.text


@pytest.mark.asyncio
async def test_unknown_organization_in_unclosed_query_does_not_fall_back_to_requesters_scope():
    repository = InMemoryReportInsightRepository(
        teams=[{"id": "team-admin", "name": "综合管理部", "department_name": "职能中心"}],
        users=[
            {"id": "u-manager", "name": "负责人", "team_id": "team-admin", "role": "department_head"},
            {"id": "u-liu", "name": "刘聪", "team_id": "team-admin", "role": "member"},
        ],
        reports=[
            {
                "id": "r-1",
                "user_id": "u-liu",
                "team_id": "team-admin",
                "date": "2026-08-01",
                "tomorrow_plan": ["不应泄露的本部门计划"],
            }
        ],
    )
    requester = SimpleNamespace(
        id="u-manager",
        name="负责人",
        dingtalk_user_id="dt-manager",
        team_id="team-admin",
        role="department_head",
    )

    answer = await ReportInsightModule(repository).answer(
        "看下不存在综合部没闭环的工作",
        requester=requester,
        current_date=date(2026, 8, 5),
    )

    assert answer is not None
    assert answer.evidence.facts["query_kind"] == "target_resolution"
    assert answer.evidence.facts["resolution_status"] == "not_found"
    assert "不应泄露" not in answer.text


@pytest.mark.parametrize(
    "message",
    [
        "今天完成合同审查；顺便庞浩有多少份日报？",
        "今天完成合同审查，顺便庞浩有多少份日报？",
        "今天完成合同审查，庞浩有多少份日报？",
        "今天完成合同审查顺便庞浩有多少份日报？",
        "今天审核了合同然后庞浩有多少份日报？",
        "今天起草了合同还有庞浩有多少份日报？",
        "今天完成合同审查，顺便看下刘聪有什么没闭环的工作。",
        "今天完成合同审查看下刘聪有什么没闭环的工作",
        "今天审核了合同看下刘聪有什么没闭环的工作",
        "总结下恒大案最近的工作",
        "总结下合同审查最近的工作",
        "总结下审计最近的工作",
        "总结下安全最近的工作",
        "总结下文秘最近的工作",
        "看下恒大案还有哪些未闭环事项",
        "看下预算审批有什么未闭环事项",
        "看下预算有什么未闭环事项",
        "看下纪检有什么未闭环事项",
        "看下安保有什么未闭环事项",
        "看下开完会刘聪有什么没闭环的工作",
        "今天核对目前有多少份日报",
        "审了合同庞浩有多少份日报？",
        "合同审完庞浩有多少份日报？",
        "开完会庞浩有多少份日报？",
    ],
)
def test_multi_intent_message_is_not_a_standalone_report_insight_query(message: str):
    assert is_report_insight_question(message) is False


def test_polite_query_preamble_before_comma_is_still_supported():
    assert is_report_insight_question("帮我查一下，庞浩目前有多少份日报？") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "庞浩目前有多少份日报了？",
        "庞浩已经完成了多少份日报？",
        "总结下庞浩最近的工作",
        "请总结一下庞浩最近已完成的工作",
        "庞浩最近完成了哪些工作？",
        "总结下综合管理部本周都做了什么",
        "综合管理部本周完成了什么？",
        "总结下综合管理部上周都做了什么",
        "最近部门有什么重点需要关注的事情吗？",
        "看下刘聪有什么没闭环的工作。",
        "看下综合部没闭环的工作",
    ],
)
async def test_webhook_entrypoint_returns_report_insight_before_legacy_daily(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
):
    from app.api import webhook

    async def resolve_entrypoint(*_args, **_kwargs):
        return SimpleNamespace(decision=SimpleNamespace(route="agent1"), binding=None)

    async def persist_owner(*_args, **_kwargs):
        return None

    answer = ReportInsightAnswer(
        text="根据日报台账，庞浩目前共有 3 份日报。",
        evidence=KnowledgeEvidenceFrame(
            source_type="daily_report_insight",
            source_id="count",
            title="日报数量",
            summary="根据日报台账，庞浩目前共有 3 份日报。",
            facts={"report_count": 3},
            confidence=1.0,
            freshness="2026-08-05",
        ),
    )

    async def load_answer(*_args, **_kwargs):
        return answer

    monkeypatch.setattr(webhook, "resolve_agent2_entrypoint", resolve_entrypoint)
    monkeypatch.setattr(webhook, "persist_runtime_owner_claim", persist_owner)
    monkeypatch.setattr(webhook, "decide_runtime_owner", lambda *_args, **_kwargs: "agent1")
    monkeypatch.setattr(webhook, "load_live_report_insight_answer", load_answer)

    result = await webhook._submit_webhook_agent2_if_enabled(
        session=SimpleNamespace(),
        user=SimpleNamespace(id="u-manager", name="负责人", dingtalk_user_id="dt-manager"),
        incoming=SimpleNamespace(
            dingtalk_user_id="dt-manager",
            source="dingtalk_webhook",
            text=question,
            conversation_id="conversation-1",
        ),
        settings=SimpleNamespace(timezone="Asia/Shanghai", agent2_business_phase2_enabled=False),
        message_id="message-1",
        llm_client=None,
    )

    assert result is not None
    assert result.read_only is True
    assert result.report_saved is False
    assert result.message == answer.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "今天完成合同审查；顺便庞浩有多少份日报？",
        "今天完成合同审查，顺便庞浩有多少份日报？",
        "今天完成合同审查，庞浩有多少份日报？",
        "今天完成合同审查顺便庞浩有多少份日报？",
        "今天审核了合同然后庞浩有多少份日报？",
        "今天起草了合同还有庞浩有多少份日报？",
        "今天完成合同审查，顺便看下刘聪有什么没闭环的工作。",
        "今天完成合同审查看下刘聪有什么没闭环的工作",
        "今天审核了合同看下刘聪有什么没闭环的工作",
        "总结下恒大案最近的工作",
        "总结下合同审查最近的工作",
        "总结下审计最近的工作",
        "总结下安全最近的工作",
        "总结下文秘最近的工作",
        "看下恒大案还有哪些未闭环事项",
        "看下预算审批有什么未闭环事项",
        "看下预算有什么未闭环事项",
        "看下纪检有什么未闭环事项",
        "看下安保有什么未闭环事项",
        "看下开完会刘聪有什么没闭环的工作",
        "今天核对目前有多少份日报",
        "审了合同庞浩有多少份日报？",
        "合同审完庞浩有多少份日报？",
        "开完会庞浩有多少份日报？",
        "综合管理部本周放假吗？",
        "综合管理部上周有人请假吗？",
        "庞浩本周请假吗？",
    ],
)
async def test_webhook_does_not_short_circuit_a_multi_intent_message(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
):
    from app.api import webhook

    async def resolve_entrypoint(*_args, **_kwargs):
        return SimpleNamespace(decision=SimpleNamespace(route="agent1"), binding=None)

    async def persist_owner(*_args, **_kwargs):
        return None

    async def unexpected_load(*_args, **_kwargs):
        raise AssertionError("multi-intent message must continue to the normal write-aware route")

    monkeypatch.setattr(webhook, "resolve_agent2_entrypoint", resolve_entrypoint)
    monkeypatch.setattr(webhook, "persist_runtime_owner_claim", persist_owner)
    monkeypatch.setattr(webhook, "decide_runtime_owner", lambda *_args, **_kwargs: "agent1")
    monkeypatch.setattr(webhook, "load_live_report_insight_answer", unexpected_load)

    result = await webhook._submit_webhook_agent2_if_enabled(
        session=SimpleNamespace(),
        user=SimpleNamespace(id="u-manager", name="负责人", dingtalk_user_id="dt-manager"),
        incoming=SimpleNamespace(
            dingtalk_user_id="dt-manager",
            source="dingtalk_webhook",
            text=message,
            conversation_id="conversation-1",
        ),
        settings=SimpleNamespace(timezone="Asia/Shanghai", agent2_business_phase2_enabled=False),
        message_id="message-1",
        llm_client=None,
    )

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entrypoint_route", "runtime_owner"),
    [("agent1", "agent1"), ("agent2_shadow", "agent2_daily")],
)
async def test_stream_agent1_and_shadow_entrypoints_return_report_insight_directly(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint_route: str,
    runtime_owner: str,
):
    import app.stream_runner as stream_runner

    async def resolve_entrypoint(*_args, **_kwargs):
        return SimpleNamespace(decision=SimpleNamespace(route=entrypoint_route), binding=None)

    async def persist_owner(*_args, **_kwargs):
        return None

    answer = ReportInsightAnswer(
        text="根据日报台账，庞浩目前共有 3 份日报。",
        evidence=KnowledgeEvidenceFrame(
            source_type="daily_report_insight",
            source_id="count",
            title="日报数量",
            summary="根据日报台账，庞浩目前共有 3 份日报。",
            facts={"report_count": 3},
            confidence=1.0,
            freshness="2026-08-05",
        ),
    )

    async def load_answer(*_args, **_kwargs):
        return answer

    processed = {}

    async def mark_processed(*_args, **kwargs):
        processed.update(kwargs)

    async def reply(*_args, **_kwargs):
        return 0.0

    class _Session:
        committed = False

        async def commit(self):
            self.committed = True

    session = _Session()
    monkeypatch.setattr(stream_runner, "resolve_agent2_entrypoint", resolve_entrypoint)
    monkeypatch.setattr(stream_runner, "persist_runtime_owner_claim", persist_owner)
    monkeypatch.setattr(stream_runner, "decide_runtime_owner", lambda *_args, **_kwargs: runtime_owner)
    monkeypatch.setattr(stream_runner, "load_live_report_insight_answer", load_answer)
    monkeypatch.setattr(stream_runner, "mark_webhook_event_processed", mark_processed)
    monkeypatch.setattr(stream_runner, "_reply", reply)
    monkeypatch.setattr(stream_runner, "_stream_source_message_id", lambda *_args: "source-1")

    result = await stream_runner._process_stream_agent2_daily_if_enabled(
        session=session,
        user=SimpleNamespace(id="u-manager", name="负责人", dingtalk_user_id="dt-manager"),
        event=SimpleNamespace(),
        job=SimpleNamespace(
            text="庞浩目前有多少份日报了？",
            message=SimpleNamespace(conversation_id="conversation-1"),
        ),
        handler=SimpleNamespace(),
        robot=SimpleNamespace(),
        llm_client=SimpleNamespace(),
        settings=SimpleNamespace(timezone="Asia/Shanghai", agent2_business_phase2_enabled=False),
        performance_service=SimpleNamespace(),
        timings={},
    )

    assert result == "daily_report_insight_processed"
    assert processed["response_payload"]["text"]["content"] == answer.text
    assert session.committed is True


@pytest.mark.asyncio
async def test_tool_assisted_reply_uses_report_insight_without_asking_the_llm():
    text = "庞浩目前有多少份日报了？"
    reply_text = "根据日报台账，庞浩目前共有 3 份日报，其中已完成 2 份、填写中 1 份。"
    context_pack = build_agent2_context_pack(
        IncomingMessageEnvelope(
            sender_id="u-manager",
            sender_name="负责人",
            dingtalk_user_id="dt-manager",
            source="test",
            raw_text=text,
        ),
        knowledge=[
            KnowledgeEvidenceFrame(
                source_type="daily_report_insight",
                source_id="daily_report_count:u-pang:2026-08-05",
                title="庞浩日报数量",
                summary=reply_text,
                facts={"query_kind": "report_count", "report_count": 3, "permission_allowed": True},
                confidence=0.99,
                freshness="2026-08-05",
            )
        ],
    )

    class _UnexpectedLlm:
        async def complete_json(self, **kwargs):
            raise AssertionError("structured report insight must not call the LLM")

    result = await build_tool_assisted_reply(
        raw_text=text,
        assistant_reply=AssistantReply(
            reply_type="internal_qa",
            workflow="internal_qa",
            text="fallback",
        ),
        llm_client=_UnexpectedLlm(),
        context_pack=context_pack,
    )

    assert result.source == "report_insight"
    assert result.fallback_used is False
    assert result.text == reply_text


@pytest.mark.asyncio
async def test_cognitive_reply_returns_report_insight_even_when_semantic_intent_is_unknown():
    text = "总结下庞浩最近的工作"
    reply_text = "根据近 7 天日报，庞浩主要完成了供应商合同审查。"
    context_pack = build_agent2_context_pack(
        IncomingMessageEnvelope(
            sender_id="u-manager",
            sender_name="负责人",
            dingtalk_user_id="dt-manager",
            source="test",
            raw_text=text,
        ),
        knowledge=[
            KnowledgeEvidenceFrame(
                source_type="daily_report_insight",
                source_id="daily_recent_work:u-pang:2026-07-30:2026-08-05",
                title="庞浩近期工作",
                summary=reply_text,
                facts={"query_kind": "recent_work", "permission_allowed": True},
                confidence=0.98,
                freshness="2026-08-05",
            )
        ],
    )

    class _UnexpectedLlm:
        async def complete_json(self, **kwargs):
            raise AssertionError("structured report insight must not call the LLM")

    reply = await build_cognitive_side_reply_v3(
        decision=SimpleNamespace(segments=()),
        llm_client=_UnexpectedLlm(),
        context_pack=context_pack,
    )

    assert reply == reply_text
