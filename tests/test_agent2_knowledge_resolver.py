from datetime import date

from app.agent2.context_pack import KnowledgeEvidenceFrame, build_agent2_context_pack
from app.agent2.knowledge_resolver import (
    CaseRegistryRecord,
    DailyReportHistoryRecord,
    InMemoryDailyReportHistoryAdapter,
    InMemoryCaseRegistryAdapter,
    InMemoryOrgDirectoryAdapter,
    KnowledgeQuery,
    OrgTeamRecord,
    OrgUserRecord,
    resolve_knowledge,
)
from app.workflows.intake import IncomingMessageEnvelope


class StaticAdapter:
    def __init__(self, source_type, evidence):
        self.source_type = source_type
        self._evidence = tuple(evidence)

    def resolve(self, query):
        return self._evidence


def test_case_registry_resolves_active_cases_as_structured_evidence():
    resolution = resolve_knowledge(
        KnowledgeQuery(text="我手底下有几个案子？", user_id="user-1", dingtalk_user_id="dt-1"),
        [
            InMemoryCaseRegistryAdapter(
                [
                    CaseRegistryRecord(
                        case_id="case-1",
                        case_name="A项目诉讼案",
                        assignee_user_id="user-1",
                        status="在办",
                        updated_at=date(2026, 7, 1),
                    ),
                    CaseRegistryRecord(
                        case_id="case-2",
                        case_name="B项目执行案",
                        assignee_dingtalk_user_id="dt-1",
                        status="active",
                        updated_at=date(2026, 7, 3),
                    ),
                    CaseRegistryRecord(
                        case_id="closed-1",
                        case_name="已结案件",
                        assignee_user_id="user-1",
                        status="closed",
                        updated_at=date(2026, 6, 1),
                    ),
                ]
            )
        ],
    )

    assert resolution.status == "available"
    assert resolution.evidence[0].source_type == "case_registry"
    assert resolution.evidence[0].facts["active_case_count"] == 2
    assert resolution.evidence[0].facts["case_names"] == ["A项目诉讼案", "B项目执行案"]
    assert resolution.evidence[0].freshness == "2026-07-03"


def test_case_registry_returns_zero_count_when_registry_is_reliably_queried():
    resolution = resolve_knowledge(
        KnowledgeQuery(text="我名下几件在办案件？", user_id="user-empty"),
        [InMemoryCaseRegistryAdapter([])],
    )

    assert resolution.status == "available"
    assert resolution.evidence[0].facts["active_case_count"] == 0
    assert "0 件在办案件" in resolution.evidence[0].summary


def test_resolver_reports_no_reliable_evidence_when_no_adapter_matches():
    resolution = resolve_knowledge(
        KnowledgeQuery(text="印章流程是什么？", user_id="user-1"),
        [InMemoryCaseRegistryAdapter([])],
    )

    assert resolution.status == "no_reliable_evidence"
    assert resolution.evidence == ()
    assert resolution.warnings == ("no_reliable_evidence",)


def test_resolver_prioritizes_structured_sources_over_vector_rag():
    vector = KnowledgeEvidenceFrame(
        source_type="vector_rag",
        source_id="doc-1",
        title="日报片段",
        summary="某日报里提到用户可能有 9 个案子。",
        confidence=0.99,
    )
    registry = KnowledgeEvidenceFrame(
        source_type="case_registry",
        source_id="assignee:user-1:active",
        title="案件台账",
        summary="案件台账显示该用户有 3 件在办案件。",
        facts={"active_case_count": 3},
        confidence=0.8,
    )

    resolution = resolve_knowledge(
        KnowledgeQuery(text="我手底下有几个案子？", user_id="user-1"),
        [StaticAdapter("vector_rag", [vector]), StaticAdapter("case_registry", [registry])],
    )

    assert [item.source_type for item in resolution.evidence] == ["case_registry", "vector_rag"]


def test_resolution_evidence_can_be_attached_to_context_pack_without_daily_write():
    resolution = resolve_knowledge(
        KnowledgeQuery(text="我手底下有几个案子？", user_id="user-1"),
        [
            InMemoryCaseRegistryAdapter(
                [
                    {
                        "case_id": "case-1",
                        "case_name": "A项目诉讼案",
                        "user_id": "user-1",
                        "status": "active",
                    }
                ]
            )
        ],
    )
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="庞浩",
        dingtalk_user_id="dt-1",
        source="dingtalk_stream_text",
        raw_text="我手底下有几个案子？",
    )

    pack = build_agent2_context_pack(envelope, knowledge=resolution.evidence)
    payload = pack.as_payload()

    assert payload["daily_draft"] is None
    assert payload["knowledge_status"] == "available"
    assert payload["knowledge"][0]["facts"]["active_case_count"] == 1


def test_org_directory_resolves_current_user_team_and_team_leader():
    resolution = resolve_knowledge(
        KnowledgeQuery(text="我属于哪个团队？", user_id="u-1", dingtalk_user_id="dt-1"),
        [
            InMemoryOrgDirectoryAdapter(
                teams=[OrgTeamRecord(team_id="team-2", team_name="法务二部", department_name="法务合约中心")],
                users=[
                    OrgUserRecord(
                        user_id="u-1",
                        name="庞浩",
                        dingtalk_user_id="dt-1",
                        employee_no="00020271",
                        team_id="team-2",
                        role="member",
                    ),
                    OrgUserRecord(
                        user_id="leader-2",
                        name="丁益明",
                        dingtalk_user_id="dt-leader-2",
                        team_id="team-2",
                        role="team_leader",
                    ),
                ],
            )
        ],
    )

    assert resolution.status == "available"
    evidence = resolution.evidence[0]
    assert evidence.source_type == "org_directory"
    assert evidence.facts["team_name"] == "法务二部"
    assert evidence.facts["department_name"] == "法务合约中心"
    assert evidence.facts["team_leaders"][0]["name"] == "丁益明"


def test_org_directory_resolves_named_team_members_and_leaders():
    resolution = resolve_knowledge(
        KnowledgeQuery(text="法务二部负责人是谁？"),
        [
            InMemoryOrgDirectoryAdapter(
                teams=[{"id": "team-2", "name": "法务二部", "department_name": "法务合约中心"}],
                users=[
                    {"id": "u-1", "name": "庞浩", "team_id": "team-2", "role": "member"},
                    {"id": "u-2", "name": "丁益明", "team_id": "team-2", "role": "负责人"},
                    {"id": "u-3", "name": "离职人员", "team_id": "team-2", "role": "负责人", "active": False},
                ],
            )
        ],
    )

    assert resolution.status == "available"
    team = resolution.evidence[0].facts
    assert team["team_name"] == "法务二部"
    assert team["member_count"] == 2
    assert [item["name"] for item in team["leaders"]] == ["丁益明"]


def test_org_directory_resolves_department_heads():
    resolution = resolve_knowledge(
        KnowledgeQuery(text="部门负责人是谁？"),
        [
            InMemoryOrgDirectoryAdapter(
                teams=[
                    OrgTeamRecord(team_id="team-1", team_name="法务一部", department_name="法务合约中心"),
                    OrgTeamRecord(team_id="team-2", team_name="法务二部", department_name="法务合约中心"),
                ],
                users=[
                    OrgUserRecord(user_id="head-1", name="赵卫中", team_id="team-1", role="department_head"),
                    OrgUserRecord(user_id="u-2", name="庞浩", team_id="team-2", role="member"),
                ],
            )
        ],
    )

    assert resolution.status == "available"
    assert resolution.evidence[0].source_id == "department:heads"
    assert resolution.evidence[0].facts["department_heads"][0]["name"] == "赵卫中"


def test_org_directory_ignores_non_org_questions():
    resolution = resolve_knowledge(
        KnowledgeQuery(text="今天完成合同审核", user_id="u-1"),
        [
            InMemoryOrgDirectoryAdapter(
                teams=[OrgTeamRecord(team_id="team-2", team_name="法务二部")],
                users=[OrgUserRecord(user_id="u-1", name="庞浩", team_id="team-2")],
            )
        ],
    )

    assert resolution.status == "no_reliable_evidence"
    assert resolution.evidence == ()


def test_org_directory_accepts_orm_like_user_and_team_objects():
    class TeamObj:
        id = "team-2"
        name = "法务二部"
        department_name = "法务合约中心"
        code = "legal-2"
        active = True

    class UserObj:
        id = "u-1"
        name = "庞浩"
        dingtalk_user_id = "dt-1"
        employee_no = "00020271"
        team_id = "team-2"
        role = "team_leader"
        active = True
        team = TeamObj()

    resolution = resolve_knowledge(
        KnowledgeQuery(text="我是谁？", dingtalk_user_id="dt-1"),
        [InMemoryOrgDirectoryAdapter(teams=[TeamObj()], users=[UserObj()])],
    )
    pack = build_agent2_context_pack(
        IncomingMessageEnvelope(sender_id="u-1", sender_name="庞浩", dingtalk_user_id="dt-1", source="test", raw_text="我是谁？"),
        knowledge=resolution.evidence,
    )

    payload = pack.as_payload()
    assert payload["knowledge_status"] == "available"
    assert payload["knowledge"][0]["facts"]["name"] == "庞浩"
    assert payload["knowledge"][0]["facts"]["team_name"] == "法务二部"


def test_daily_history_resolves_yesterday_report_for_copy_context():
    resolution = resolve_knowledge(
        KnowledgeQuery(
            text="复制昨天的日报",
            user_id="u-1",
            dingtalk_user_id="dt-1",
            metadata={"current_date": "2026-07-03"},
        ),
        [
            InMemoryDailyReportHistoryAdapter(
                [
                    DailyReportHistoryRecord(
                        report_id="r-0702",
                        user_id="u-1",
                        dingtalk_user_id="dt-1",
                        report_date=date(2026, 7, 2),
                        status="completed",
                        today_work=("合同审核",),
                        problems=("暂无明显问题",),
                        tomorrow_plan=("南京出差盖章",),
                        updated_at=date(2026, 7, 2),
                    )
                ]
            )
        ],
    )

    assert resolution.status == "available"
    evidence = resolution.evidence[0]
    assert evidence.source_type == "daily_report_history"
    assert evidence.facts["relation"] == "yesterday"
    assert evidence.facts["report_date"] == "2026-07-02"
    assert evidence.facts["can_copy_to_today"] is True
    assert evidence.facts["tomorrow_plan"] == ["南京出差盖章"]


def test_daily_history_resolves_previous_plan_completion_candidates():
    resolution = resolve_knowledge(
        KnowledgeQuery(
            text="昨天的计划都完成了",
            user_id="u-1",
            metadata={"current_date": date(2026, 7, 3)},
        ),
        [
            InMemoryDailyReportHistoryAdapter(
                [
                    {
                        "id": "r-0702",
                        "user_id": "u-1",
                        "date": "2026-07-02",
                        "status": "completed",
                        "tomorrow_plan": ["处理南京出差盖章", "整理案件材料"],
                    }
                ]
            )
        ],
    )

    evidence = resolution.evidence[0]
    assert evidence.source_id == "daily_previous_plan:u-1:2026-07-02"
    assert evidence.facts["suggested_daily_operation"] == "complete_previous_plan_items"
    assert evidence.facts["suggested_target_field"] == "today_work"
    assert evidence.facts["today_work_candidates"] == ["处理南京出差盖章", "整理案件材料"]


def test_daily_history_resolves_recent_report_summary_sorted_newest_first():
    resolution = resolve_knowledge(
        KnowledgeQuery(
            text="看看我最近几天日报",
            user_id="u-1",
            metadata={"current_date": "2026-07-04"},
        ),
        [
            InMemoryDailyReportHistoryAdapter(
                [
                    {"id": "r-0701", "user_id": "u-1", "date": "2026-07-01", "today_work": ["项目沟通"]},
                    {"id": "r-0703", "user_id": "u-1", "date": "2026-07-03", "today_work": ["合同审核"]},
                    {"id": "other", "user_id": "u-2", "date": "2026-07-03", "today_work": ["别人日报"]},
                ]
            )
        ],
    )

    reports = resolution.evidence[0].facts["reports"]
    assert [item["report_date"] for item in reports] == ["2026-07-03", "2026-07-01"]
    assert reports[0]["today_work"] == ["合同审核"]


def test_daily_history_ignores_plain_daily_write_message():
    resolution = resolve_knowledge(
        KnowledgeQuery(text="今天完成合同审核，明天去南京", user_id="u-1"),
        [
            InMemoryDailyReportHistoryAdapter(
                [
                    {"id": "r-0702", "user_id": "u-1", "date": "2026-07-02", "tomorrow_plan": ["南京出差"]},
                ]
            )
        ],
    )

    assert resolution.status == "no_reliable_evidence"
    assert resolution.evidence == ()


def test_daily_history_accepts_orm_like_report_and_attaches_to_context_pack():
    class ReportObj:
        id = "r-0702"
        user_id = "u-1"
        dingtalk_user_id = "dt-1"
        report_date = date(2026, 7, 2)
        status = "completed"
        today_work = ["合同审核"]
        problems = ["暂无明显问题"]
        tomorrow_plan = ["南京出差"]
        updated_at = date(2026, 7, 2)

    resolution = resolve_knowledge(
        KnowledgeQuery(text="昨天明日计划是什么？", user_id="u-1", metadata={"current_date": "2026-07-03"}),
        [InMemoryDailyReportHistoryAdapter([ReportObj()])],
    )
    pack = build_agent2_context_pack(
        IncomingMessageEnvelope(sender_id="u-1", sender_name="庞浩", dingtalk_user_id="dt-1", source="test", raw_text="昨天明日计划是什么？"),
        knowledge=resolution.evidence,
    )

    payload = pack.as_payload()
    assert payload["knowledge_status"] == "available"
    assert payload["knowledge"][0]["facts"]["tomorrow_plan"] == ["南京出差"]
