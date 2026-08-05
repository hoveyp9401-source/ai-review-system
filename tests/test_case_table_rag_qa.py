from __future__ import annotations

import pytest
import asyncio

from app.agent2.assistant_responder import AssistantReply
from app.agent2.assistant_tools import build_tool_assisted_reply
from app.agent2.case_table_rag import CaseTableDocument, CaseTableRagAdapter, write_case_table_index
from app.agent2.context_pack import Agent2ContextPack, KnowledgeEvidenceFrame, MessageContextFrame, UserContextFrame
from app.agent2.knowledge_resolver import KnowledgeQuery
from app.agent2.rag_qa import build_rag_qa_reply


def _build_index(tmp_path):
    documents = [
        CaseTableDocument(
            doc_id="d1",
            source_type="case_table_rag",
            source_file="defendant.xlsx",
            sheet_name="Sheet1",
            row_number=2,
            table_type="defendant_case_table",
            case_name="\u738b\u559c\u6848\u4ef61",
            department="\u6cd5\u52a1\u4e8c\u90e8",
            assignee_name="(001)\u738b\u559c",
            status="\u53d7\u7406",
            text="\u6848\u4ef6\u540d\u79f0: \u738b\u559c\u6848\u4ef61\uff1b\u6cd5\u52a1\u90e8\u95e8: \u6cd5\u52a1\u4e8c\u90e8\uff1b\u6cd5\u52a1\u88ab\u544a\u6848\u4ef6\u8d1f\u8d23\u4eba: (001)\u738b\u559c",
            facts={"\u662f\u5426\u7ed3\u6848": "\u5426", "\u7cfb\u7edf\u767b\u8bb0\u65e5\u671f": "2026-06-10"},
        ),
        CaseTableDocument(
            doc_id="d2",
            source_type="case_table_rag",
            source_file="defendant.xlsx",
            sheet_name="Sheet1",
            row_number=3,
            table_type="defendant_case_table",
            case_name="\u738b\u559c\u6848\u4ef62",
            department="\u6cd5\u52a1\u4e8c\u90e8",
            assignee_name="(001)\u738b\u559c",
            status="\u6267\u884c",
            text="\u6848\u4ef6\u540d\u79f0: \u738b\u559c\u6848\u4ef62\uff1b\u6cd5\u52a1\u90e8\u95e8: \u6cd5\u52a1\u4e8c\u90e8\uff1b\u6cd5\u52a1\u88ab\u544a\u6848\u4ef6\u8d1f\u8d23\u4eba: (001)\u738b\u559c",
            facts={"\u662f\u5426\u7ed3\u6848": "\u5426", "\u7cfb\u7edf\u767b\u8bb0\u65e5\u671f": "2026-05-10"},
        ),
        CaseTableDocument(
            doc_id="d3",
            source_type="case_table_rag",
            source_file="defendant.xlsx",
            sheet_name="Sheet1",
            row_number=4,
            table_type="defendant_case_table",
            case_name="\u738b\u559c\u5df2\u7ed3\u6848",
            department="\u6cd5\u52a1\u4e8c\u90e8",
            assignee_name="(001)\u738b\u559c",
            status="\u5df2\u7ed3\u6848",
            text="\u6848\u4ef6\u540d\u79f0: \u738b\u559c\u5df2\u7ed3\u6848\uff1b\u6cd5\u52a1\u90e8\u95e8: \u6cd5\u52a1\u4e8c\u90e8\uff1b\u6cd5\u52a1\u88ab\u544a\u6848\u4ef6\u8d1f\u8d23\u4eba: (001)\u738b\u559c",
            facts={
                "\u662f\u5426\u7ed3\u6848": "\u662f",
                "\u7cfb\u7edf\u767b\u8bb0\u65e5\u671f": "2026-04-10",
                "\u7ed3\u6848\u65e5\u671f": "2026-06-05",
            },
        ),
        CaseTableDocument(
            doc_id="d4",
            source_type="case_table_rag",
            source_file="defendant.xlsx",
            sheet_name="Sheet1",
            row_number=5,
            table_type="defendant_case_table",
            case_name="\u5218\u6ce2\u6848\u4ef6",
            department="\u6cd5\u52a1\u4e09\u90e8",
            assignee_name="(002)\u5218\u6ce2",
            status="\u53d7\u7406",
            text="\u6848\u4ef6\u540d\u79f0: \u5218\u6ce2\u6848\u4ef6\uff1b\u6cd5\u52a1\u90e8\u95e8: \u6cd5\u52a1\u4e09\u90e8\uff1b\u6cd5\u52a1\u88ab\u544a\u6848\u4ef6\u8d1f\u8d23\u4eba: (002)\u5218\u6ce2",
            facts={"\u662f\u5426\u7ed3\u6848": "\u5426", "\u7cfb\u7edf\u767b\u8bb0\u65e5\u671f": "2026-06-02"},
        ),
    ]
    sqlite_path = tmp_path / "case_index.sqlite"
    write_case_table_index(documents, sqlite_path=sqlite_path)
    return sqlite_path


def _build_quarter_index(tmp_path):
    documents = [
        CaseTableDocument(
            doc_id=f"q2-2026-{index}",
            source_type="case_table_rag",
            source_file="defendant.xlsx",
            sheet_name="Sheet1",
            row_number=index,
            table_type="defendant_case_table",
            case_name=f"2026\u4e8c\u5b63\u5ea6\u65b0\u589e\u6848\u4ef6{index}",
            department="\u6cd5\u52a1\u4e8c\u90e8",
            assignee_name="(001)\u738b\u559c",
            status="\u53d7\u7406",
            text=f"\u6848\u4ef6\u540d\u79f0: 2026\u4e8c\u5b63\u5ea6\u65b0\u589e\u6848\u4ef6{index}",
            facts={"\u662f\u5426\u7ed3\u6848": "\u5426", "\u7cfb\u7edf\u767b\u8bb0\u65e5\u671f": day},
        )
        for index, day in enumerate(("2026-04-10", "2026-05-10", "2026-06-10"), start=1)
    ]
    documents.extend(
        [
            CaseTableDocument(
                doc_id=f"q2-2025-{index}",
                source_type="case_table_rag",
                source_file="defendant.xlsx",
                sheet_name="Sheet1",
                row_number=10 + index,
                table_type="defendant_case_table",
                case_name=f"2025\u4e8c\u5b63\u5ea6\u65b0\u589e\u6848\u4ef6{index}",
                department="\u6cd5\u52a1\u4e8c\u90e8",
                assignee_name="(001)\u738b\u559c",
                status="\u53d7\u7406",
                text=f"\u6848\u4ef6\u540d\u79f0: 2025\u4e8c\u5b63\u5ea6\u65b0\u589e\u6848\u4ef6{index}",
                facts={"\u662f\u5426\u7ed3\u6848": "\u5426", "\u7cfb\u7edf\u767b\u8bb0\u65e5\u671f": day},
            )
            for index, day in enumerate(("2025-04-10", "2025-06-10"), start=1)
        ]
    )
    documents.append(
        CaseTableDocument(
            doc_id="q3-2026-1",
            source_type="case_table_rag",
            source_file="defendant.xlsx",
            sheet_name="Sheet1",
            row_number=30,
            table_type="defendant_case_table",
            case_name="2026\u4e09\u5b63\u5ea6\u65b0\u589e\u6848\u4ef6",
            department="\u6cd5\u52a1\u4e8c\u90e8",
            assignee_name="(001)\u738b\u559c",
            status="\u53d7\u7406",
            text="\u6848\u4ef6\u540d\u79f0: 2026\u4e09\u5b63\u5ea6\u65b0\u589e\u6848\u4ef6",
            facts={"\u662f\u5426\u7ed3\u6848": "\u5426", "\u7cfb\u7edf\u767b\u8bb0\u65e5\u671f": "2026-07-02"},
        )
    )
    sqlite_path = tmp_path / "quarter_case_index.sqlite"
    write_case_table_index(documents, sqlite_path=sqlite_path)
    return sqlite_path


def _resolve(index_path, text, metadata=None):
    evidence = CaseTableRagAdapter(index_path).resolve(
        KnowledgeQuery(text=text, intent="internal_qa", metadata=metadata)
    )
    assert evidence
    return evidence[0]


def _requester(name="普通成员", role="member", team_name="\u6cd5\u52a1\u4e8c\u90e8", dingtalk_user_id="dt-member"):
    return {
        "requester": {
            "user_id": f"user-{dingtalk_user_id}",
            "dingtalk_user_id": dingtalk_user_id,
            "name": name,
            "role": role,
            "team_id": "team-2",
            "team_name": team_name,
            "department_name": "\u6cd5\u52a1\u5408\u7ea6\u4e2d\u5fc3",
        }
    }


def _pack(evidence, text="\u738b\u559c\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11"):
    return Agent2ContextPack(
        user=UserContextFrame(user_id="u1", dingtalk_user_id="dt1", name="\u6d4b\u8bd5"),
        message=MessageContextFrame(text=text, source="test"),
        knowledge=(evidence,),
    )


def test_case_table_rag_counts_defendant_cases_by_assignee(tmp_path):
    evidence = _resolve(_build_index(tmp_path), "\u738b\u559c\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11")

    assert evidence.facts["assignee_name"] == "\u738b\u559c"
    assert evidence.facts["department"] == ""
    assert evidence.facts["table_type"] == "defendant_case_table"
    assert evidence.facts["case_count"] == 3


def test_case_table_rag_lists_unclosed_defendant_cases_by_assignee(tmp_path):
    evidence = _resolve(_build_index(tmp_path), "\u738b\u559c\u672a\u7ed3\u6848\u7684\u88ab\u544a\u6848\u4ef6\u6709\u54ea\u4e9b")

    assert evidence.facts["case_count"] == 2
    assert evidence.facts["total_case_count"] == 3
    assert evidence.facts["status_filter"] == "unclosed"
    assert evidence.facts["query_mode"] == "list"
    assert evidence.facts["sample_case_names"] == ["\u738b\u559c\u6848\u4ef61", "\u738b\u559c\u6848\u4ef62"]


def test_case_table_rag_counts_defendant_cases_by_department_without_treating_team_as_person(tmp_path):
    evidence = _resolve(_build_index(tmp_path), "\u6cd5\u52a1\u4e8c\u90e8\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11")

    assert evidence.facts["department"] == "\u6cd5\u52a1\u4e8c\u90e8"
    assert evidence.facts["assignee_name"] == ""
    assert evidence.facts["case_count"] == 3


def test_case_table_rag_permission_allows_member_own_team(tmp_path):
    evidence = _resolve(
        _build_index(tmp_path),
        "\u6cd5\u52a1\u4e8c\u90e8\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11",
        metadata=_requester(),
    )

    assert evidence.facts["case_count"] == 3
    assert evidence.facts["permission"]["checked"] is True
    assert evidence.facts["permission"]["allowed"] is True


def test_case_table_rag_permission_blocks_member_cross_team(tmp_path):
    evidence = _resolve(
        _build_index(tmp_path),
        "\u6cd5\u52a1\u4e09\u90e8\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11",
        metadata=_requester(),
    )

    assert evidence.facts["permission_denied"] is True
    reply = build_rag_qa_reply(
        raw_text="\u6cd5\u52a1\u4e09\u90e8\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11",
        context_pack=_pack(evidence),
        reply_type="internal_qa",
    )
    assert reply is not None
    assert "\u4e0d\u80fd\u67e5\u8be2" in reply.text
    assert "\u6cd5\u52a1\u4e09\u90e8" in reply.text


def test_case_table_rag_permission_allows_team_leader_all_teams(tmp_path):
    evidence = _resolve(
        _build_index(tmp_path),
        "\u5404\u56e2\u961f\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11",
        metadata=_requester(role="team_leader"),
    )

    assert evidence.facts["group_by"] == "department"
    assert evidence.facts["permission"]["allowed"] is True


def test_case_table_rag_permission_allows_panghao_all_teams(tmp_path):
    evidence = _resolve(
        _build_index(tmp_path),
        "\u5404\u56e2\u961f\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11",
        metadata=_requester(name="\u5e9e\u6d69", role="member", dingtalk_user_id="0515246015778891"),
    )

    assert evidence.facts["group_by"] == "department"
    assert evidence.facts["permission"]["allowed"] is True


def test_case_table_rag_display_name_alone_does_not_grant_all_team_access(tmp_path):
    evidence = _resolve(
        _build_index(tmp_path),
        "各团队被告案件有多少",
        metadata=_requester(name="庞浩", role="member", dingtalk_user_id="dt-not-privileged"),
    )

    assert evidence.facts["permission_denied"] is True
    assert evidence.facts["permission"]["allowed"] is False


def test_case_table_rag_counts_defendant_inventory_by_department_using_monthly_formula(tmp_path):
    evidence = _resolve(_build_index(tmp_path), "\u6cd5\u52a1\u4e8c\u90e8\u76ee\u524d\u88ab\u544a\u5b58\u91cf\u591a\u5c11")

    assert evidence.facts["metric_mode"] == "defendant_monthly"
    assert evidence.facts["metric_kind"] == "inventory"
    assert evidence.facts["department"] == "\u6cd5\u52a1\u4e8c\u90e8"
    assert evidence.facts["as_of_date"] == "2026-06-10"
    assert evidence.facts["inventory_count"] == 2
    assert evidence.facts["new_count"] == 1
    assert evidence.facts["case_count"] == 2
    contract = evidence.facts["fact_contract"]
    assert contract["contract_version"] == "fact_contract.v1"
    assert contract["fact_type"] == "case_metric"
    assert contract["metric"]["name"] == "defendant_case_inventory_count"
    assert contract["metric"]["period_end"] == "2026-06-10"
    assert contract["definition"].startswith("\u5b58\u91cf=")
    assert contract["value"] == 2
    assert contract["unit"] == "\u4ef6"
    assert contract["permission"]["checked"] is True
    assert contract["permission"]["allowed"] is True
    assert contract["permission"]["policy"] == "case_fact_scope_v1"


def test_case_table_rag_counts_defendant_inventory_without_question_marker(tmp_path):
    evidence = _resolve(_build_index(tmp_path), "\u5f53\u524d\u6cd5\u52a1\u4e8c\u90e8\u5b58\u91cf\u88ab\u544a\u6848\u4ef6")

    assert evidence.facts["metric_mode"] == "defendant_monthly"
    assert evidence.facts["metric_kind"] == "inventory"
    assert evidence.facts["department"] == "\u6cd5\u52a1\u4e8c\u90e8"
    assert evidence.facts["inventory_count"] == 2


@pytest.mark.parametrize(
    ("text", "metric_kind", "expected_count"),
    [
        ("\u603b\u4f53\u88ab\u544a\u5b58\u91cf\u53d1\u6211", "inventory", 3),
        ("\u6574\u4f53\u88ab\u544a\u5b58\u91cf\u770b\u4e0b", "inventory", 3),
        ("\u5168\u90e8\u88ab\u544a\u65b0\u589e\u53d1\u4e00\u4e0b", "new", 2),
        ("\u88ab\u544a\u5b58\u91cf\u7edf\u8ba1\u4e0b", "inventory", 3),
    ],
)
def test_case_table_rag_counts_total_defendant_metrics_with_request_verbs(tmp_path, text, metric_kind, expected_count):
    evidence = _resolve(_build_index(tmp_path), text)

    assert evidence.facts["metric_mode"] == "defendant_monthly"
    assert evidence.facts["metric_kind"] == metric_kind
    assert evidence.facts["department"] == ""
    assert evidence.facts["assignee_name"] == ""
    assert evidence.facts["case_count"] == expected_count
    assert evidence.facts["inventory_count"] == 3
    assert evidence.facts["new_count"] == 2


def test_case_table_rag_counts_defendant_second_quarter_new_yoy_without_treating_period_as_person(tmp_path):
    evidence = _resolve(
        _build_quarter_index(tmp_path),
        "\u53d1\u6211\u88ab\u544a\u4e8c\u5b63\u5ea6\u65b0\u589e\u540c\u6bd4\u6570\u636e",
        metadata={"current_date": "2026-07-06"},
    )

    assert evidence.facts["metric_mode"] == "defendant_monthly"
    assert evidence.facts["metric_kind"] == "new"
    assert evidence.facts["period_type"] == "quarter"
    assert evidence.facts["period_label"] == "2026-Q2"
    assert evidence.facts["period_start"] == "2026-04-01"
    assert evidence.facts["period_end"] == "2026-06-30"
    assert evidence.facts["department"] == ""
    assert evidence.facts["assignee_name"] == ""
    assert evidence.facts["new_count"] == 3
    assert evidence.facts["last_year_new_count"] == 2
    assert evidence.facts["new_yoy_change"] == pytest.approx(50.0)
    assert evidence.facts["case_count"] == 3

    reply = build_rag_qa_reply(
        raw_text="\u53d1\u6211\u88ab\u544a\u4e8c\u5b63\u5ea6\u65b0\u589e\u540c\u6bd4\u6570\u636e",
        context_pack=_pack(evidence),
        reply_type="internal_qa",
    )
    assert reply is not None
    assert "2026-Q2 \u81ea\u7136\u5b63\u5ea6\u5185\u65b0\u589e 3 \u4ef6" in reply.text
    assert "\u540c\u6bd4\uff1a\u65b0\u589e\u589e\u957f 50.00%" in reply.text


def test_case_table_rag_inherits_defendant_inventory_for_team_followup(tmp_path):
    evidence = _resolve(
        _build_index(tmp_path),
        "\u6cd5\u52a1\u4e8c\u90e8\u5462",
        metadata={
            "recent_case_messages": [
                {"text": "\u6cd5\u52a1\u4e00\u90e8\u76ee\u524d\u88ab\u544a\u5b58\u91cf\u591a\u5c11"}
            ]
        },
    )

    assert evidence.facts["metric_mode"] == "defendant_monthly"
    assert evidence.facts["metric_kind"] == "inventory"
    assert evidence.facts["department"] == "\u6cd5\u52a1\u4e8c\u90e8"
    assert evidence.facts["inventory_count"] == 2


def test_case_table_rag_groups_defendant_cases_by_department(tmp_path):
    evidence = _resolve(_build_index(tmp_path), "\u5404\u56e2\u961f\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11")

    assert evidence.facts["group_by"] == "department"
    assert evidence.facts["case_count"] == 4
    assert evidence.facts["groups"] == [
        {"department": "\u6cd5\u52a1\u4e8c\u90e8", "case_count": 3},
        {"department": "\u6cd5\u52a1\u4e09\u90e8", "case_count": 1},
    ]
    contract = evidence.facts["fact_contract"]
    assert contract["metric"]["name"] == "case_count_by_department"
    assert contract["value"]["total_count"] == 4
    assert contract["value"]["group_count"] == 2
    assert contract["unit"] == "\u4ef6"


def test_rag_qa_reply_renders_case_count_without_llm(tmp_path):
    evidence = _resolve(_build_index(tmp_path), "\u738b\u559c\u672a\u7ed3\u6848\u7684\u88ab\u544a\u6848\u4ef6\u6709\u54ea\u4e9b")
    reply = build_rag_qa_reply(
        raw_text="\u738b\u559c\u672a\u7ed3\u6848\u7684\u88ab\u544a\u6848\u4ef6\u6709\u54ea\u4e9b",
        context_pack=_pack(evidence),
        reply_type="internal_qa",
    )

    assert reply is not None
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in reply.text
    assert "\u6709 2 \u4ef6" in reply.text
    assert "\u738b\u559c\u6848\u4ef61" in reply.text
    assert "\u738b\u559c\u6848\u4ef62" in reply.text


def test_rag_qa_reply_adds_fact_contract_before_structured_case_answer():
    evidence = KnowledgeEvidenceFrame(
        source_type="case_table_rag",
        source_id="legacy-evidence",
        title="\u65e7\u6848\u4ef6\u8bc1\u636e",
        summary="\u65e7\u7ed3\u6784\u5316\u8bc1\u636e",
        facts={
            "case_count": 2,
            "table_type": "defendant_case_table",
            "table_label": "\u88ab\u544a",
        },
        confidence=0.8,
        freshness="test",
    )

    reply = build_rag_qa_reply(
        raw_text="\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11",
        context_pack=_pack(evidence),
        reply_type="internal_qa",
    )

    assert reply is not None
    assert reply.source == "rag_qa"
    assert evidence.facts["fact_contract"]["contract_version"] == "fact_contract.v1"


def test_rag_qa_reply_renders_defendant_inventory_formula_without_llm(tmp_path):
    evidence = _resolve(_build_index(tmp_path), "\u6cd5\u52a1\u4e8c\u90e8\u76ee\u524d\u88ab\u544a\u5b58\u91cf\u591a\u5c11")
    reply = build_rag_qa_reply(
        raw_text="\u6cd5\u52a1\u4e8c\u90e8\u76ee\u524d\u88ab\u544a\u5b58\u91cf\u591a\u5c11",
        context_pack=_pack(evidence),
        reply_type="internal_qa",
    )

    assert reply is not None
    assert "\u4e0d\u5199\u5165\u65e5\u62a5" in reply.text
    assert "\u5b58\u91cf\uff1a\u622a\u81f3 2026-06-10\uff0c\u5171 2 \u4ef6" in reply.text
    assert "\u53e3\u5f84\uff1a\u5b58\u91cf=\u767b\u8bb0\u65e5\u2264\u622a\u6b62\u65e5" in reply.text


def test_assistant_tool_reply_prefers_rag_qa_over_llm(tmp_path):
    evidence = _resolve(_build_index(tmp_path), "\u5404\u56e2\u961f\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11")

    result = asyncio.run(
        build_tool_assisted_reply(
            raw_text="\u5404\u56e2\u961f\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11",
            assistant_reply=AssistantReply(
                reply_type="internal_qa",
                workflow="internal_qa",
                text="\u8fd9\u662f\u515c\u5e95\u56de\u590d",
            ),
            llm_client=object(),
            context_pack=_pack(evidence, text="\u5404\u56e2\u961f\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11"),
        )
    )

    assert result.source == "rag_qa"
    assert result.fallback_used is False
    assert "\u6cd5\u52a1\u4e8c\u90e8\uff1a3 \u4ef6" in result.text
    assert "\u6cd5\u52a1\u4e09\u90e8\uff1a1 \u4ef6" in result.text
