from app.agent2.case_table_rag import (
    CaseTableDocument,
    CaseTableRagAdapter,
    find_case_location_hint,
    search_case_table_index,
    write_case_table_index,
)
from app.agent2.context_pack import KnowledgeEvidenceFrame, build_agent2_context_pack
from app.agent2.knowledge_resolver import KnowledgeQuery, resolve_knowledge
from app.workflows.intake import IncomingMessageEnvelope


class StaticAdapter:
    def __init__(self, source_type, evidence):
        self.source_type = source_type
        self._evidence = tuple(evidence)

    def resolve(self, query):
        return self._evidence


def _write_index(tmp_path):
    index_path = tmp_path / "case_index.sqlite"
    jsonl_path = tmp_path / "case_documents.jsonl"
    write_case_table_index(
        [
            CaseTableDocument(
                doc_id="doc-1",
                source_type="case_table_rag",
                source_file="原告案件底表.xlsx",
                sheet_name="原告案件底表",
                row_number=12,
                table_type="plaintiff_case_table",
                case_name="海花岛项目工程款纠纷案",
                department="法务二部",
                assignee_name="庞浩",
                status="执行中",
                updated_at="2026-07-06",
                text="案件名称: 海花岛项目工程款纠纷案；法务部门: 法务二部；负责人: 庞浩；执行案号: (2026)苏01执123号",
                facts={"案件名称": "海花岛项目工程款纠纷案", "执行案号": "(2026)苏01执123号"},
            ),
            CaseTableDocument(
                doc_id="doc-2",
                source_type="case_table_rag",
                source_file="被告案件底表.xlsx",
                sheet_name="被告案件明细",
                row_number=20,
                table_type="defendant_case_table",
                case_name="供应商合同纠纷被告案",
                department="法务三部",
                assignee_name="刘波",
                status="一审",
                updated_at="2026-07-06",
                text="案件名称: 供应商合同纠纷被告案；法务部门: 法务三部；负责人: 刘波",
                facts={"案件名称": "供应商合同纠纷被告案"},
            ),
        ],
        sqlite_path=index_path,
        jsonl_path=jsonl_path,
    )
    return index_path


def test_case_table_index_searches_chinese_case_keywords(tmp_path):
    index_path = _write_index(tmp_path)

    results = search_case_table_index(index_path, "海花岛执行案号", limit=3)

    assert results
    assert results[0].case_name == "海花岛项目工程款纠纷案"
    assert results[0].facts["执行案号"] == "(2026)苏01执123号"


def test_case_table_location_hint_uses_case_keyword_and_court_field(tmp_path):
    index_path = tmp_path / "case_location_index.sqlite"
    write_case_table_index(
        [
            CaseTableDocument(
                doc_id="location-1",
                source_type="case_table_rag",
                source_file="原告案件底表.xlsx",
                sheet_name="原告案件底表",
                row_number=8,
                table_type="plaintiff_case_table",
                case_name="保利东湖会所施工合同纠纷案",
                department="法务三部",
                assignee_name="刘波",
                text="案件名称: 保利东湖会所施工合同纠纷案；承办法院: 福州市长乐区人民法院；法院是否反馈保全财产清单: 是",
                facts={
                    "案件名称": "保利东湖会所施工合同纠纷案",
                    "承办法院": "福州市长乐区人民法院",
                    "法院是否反馈保全财产清单": "是",
                },
            )
        ],
        sqlite_path=index_path,
    )

    hint = find_case_location_hint(
        index_path,
        matter_hint="保利案件",
        raw_text="后天去保利案件开庭",
    )

    assert hint is not None
    assert hint.case_name == "保利东湖会所施工合同纠纷案"
    assert hint.court_or_location == "福州市长乐区人民法院"
    assert hint.assignee_name == "刘波"


def test_case_table_location_hint_does_not_treat_boolean_court_feedback_as_location(tmp_path):
    index_path = tmp_path / "case_location_no_court_index.sqlite"
    write_case_table_index(
        [
            CaseTableDocument(
                doc_id="location-2",
                source_type="case_table_rag",
                source_file="原告案件底表.xlsx",
                sheet_name="原告案件底表",
                row_number=9,
                table_type="plaintiff_case_table",
                case_name="保利项目保全纠纷案",
                department="法务三部",
                assignee_name="刘波",
                text="案件名称: 保利项目保全纠纷案；法院是否反馈保全财产清单: 是",
                facts={
                    "案件名称": "保利项目保全纠纷案",
                    "法院是否反馈保全财产清单": "是",
                },
            )
        ],
        sqlite_path=index_path,
    )

    hint = find_case_location_hint(
        index_path,
        matter_hint="保利案件",
        raw_text="后天去保利案件开庭",
    )

    assert hint is not None
    assert hint.case_name == "保利项目保全纠纷案"
    assert hint.court_or_location == ""


def test_case_table_rag_adapter_returns_context_pack_evidence(tmp_path):
    index_path = _write_index(tmp_path)
    resolution = resolve_knowledge(
        KnowledgeQuery(text="查一下海花岛案进展", user_id="u-1", dingtalk_user_id="dt-1"),
        [CaseTableRagAdapter(index_path)],
    )

    assert resolution.status == "available"
    evidence = resolution.evidence[0]
    assert evidence.source_type == "case_table_rag"
    assert evidence.facts["case_name"] == "海花岛项目工程款纠纷案"
    assert evidence.facts["row_number"] == 12

    pack = build_agent2_context_pack(
        IncomingMessageEnvelope(sender_id="u-1", sender_name="庞浩", dingtalk_user_id="dt-1", source="test", raw_text="查一下海花岛案进展"),
        knowledge=resolution.evidence,
    )
    payload = pack.as_payload()
    assert payload["knowledge_status"] == "available"
    assert payload["knowledge"][0]["facts"]["source_file"] == "原告案件底表.xlsx"


def test_case_table_rag_adapter_answers_assignee_case_count(tmp_path):
    index_path = tmp_path / "case_count_index.sqlite"
    write_case_table_index(
        [
            CaseTableDocument(
                doc_id="count-1",
                source_type="case_table_rag",
                source_file="\u88ab\u544a\u6848\u4ef6\u5e95\u8868.xlsx",
                sheet_name="\u88ab\u544a\u6848\u4ef6\u660e\u7ec6",
                row_number=1,
                table_type="defendant_case_table",
                case_name="\u4f9b\u5e94\u5546\u5408\u540c\u7ea0\u7eb7\u88ab\u544a\u6848",
                department="\u6cd5\u52a1\u4e09\u90e8",
                assignee_name="\u5218\u6ce2",
                text="\u6848\u4ef6\u540d\u79f0: \u4f9b\u5e94\u5546\u5408\u540c\u7ea0\u7eb7\u88ab\u544a\u6848\uff1b\u8d1f\u8d23\u4eba: \u5218\u6ce2",
            ),
            CaseTableDocument(
                doc_id="count-2",
                source_type="case_table_rag",
                source_file="\u88ab\u544a\u6848\u4ef6\u5e95\u8868.xlsx",
                sheet_name="\u88ab\u544a\u6848\u4ef6\u660e\u7ec6",
                row_number=2,
                table_type="defendant_case_table",
                case_name="\u6750\u6599\u4f9b\u5e94\u5408\u540c\u7ea0\u7eb7\u88ab\u544a\u6848",
                department="\u6cd5\u52a1\u4e09\u90e8",
                assignee_name="\u5218\u6ce2",
                text="\u6848\u4ef6\u540d\u79f0: \u6750\u6599\u4f9b\u5e94\u5408\u540c\u7ea0\u7eb7\u88ab\u544a\u6848\uff1b\u8d1f\u8d23\u4eba: \u5218\u6ce2",
            ),
            CaseTableDocument(
                doc_id="count-3",
                source_type="case_table_rag",
                source_file="\u539f\u544a\u6848\u4ef6\u5e95\u8868.xlsx",
                sheet_name="\u539f\u544a\u6848\u4ef6\u5e95\u8868",
                row_number=3,
                table_type="plaintiff_case_table",
                case_name="\u8ffd\u507f\u7ea0\u7eb7\u539f\u544a\u6848",
                department="\u6cd5\u52a1\u4e09\u90e8",
                assignee_name="\u5218\u6ce2",
                text="\u6848\u4ef6\u540d\u79f0: \u8ffd\u507f\u7ea0\u7eb7\u539f\u544a\u6848\uff1b\u8d1f\u8d23\u4eba: \u5218\u6ce2",
            ),
        ],
        sqlite_path=index_path,
    )

    resolution = resolve_knowledge(
        KnowledgeQuery(text="\u5218\u6ce2\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11"),
        [CaseTableRagAdapter(index_path)],
    )

    assert resolution.status == "available"
    evidence = resolution.evidence[0]
    assert evidence.source_type == "case_table_rag"
    assert evidence.source_id.startswith("count:")
    assert evidence.facts["assignee_name"] == "\u5218\u6ce2"
    assert evidence.facts["table_type"] == "defendant_case_table"
    assert evidence.facts["case_count"] == 2


def test_case_table_rag_adapter_counts_unclosed_by_assignee_and_context(tmp_path):
    index_path = tmp_path / "case_unclosed_index.sqlite"
    write_case_table_index(
        [
            CaseTableDocument(
                doc_id="open-1",
                source_type="case_table_rag",
                source_file="\u88ab\u544a\u6848\u4ef6\u5e95\u8868.xlsx",
                sheet_name="\u88ab\u544a\u6848\u4ef6\u660e\u7ec6",
                row_number=1,
                table_type="defendant_case_table",
                case_name="\u672a\u7ed3\u6848\u88ab\u544a\u6848",
                assignee_name="\u5218\u6ce2",
                status="\u53d7\u7406",
                text="\u6848\u4ef6\u540d\u79f0: \u672a\u7ed3\u6848\u88ab\u544a\u6848\uff1b\u8d1f\u8d23\u4eba: \u5218\u6ce2\uff1b\u662f\u5426\u7ed3\u6848: \u5426",
                facts={"\u662f\u5426\u7ed3\u6848": "\u5426"},
            ),
            CaseTableDocument(
                doc_id="closed-1",
                source_type="case_table_rag",
                source_file="\u88ab\u544a\u6848\u4ef6\u5e95\u8868.xlsx",
                sheet_name="\u88ab\u544a\u6848\u4ef6\u660e\u7ec6",
                row_number=2,
                table_type="defendant_case_table",
                case_name="\u5df2\u7ed3\u6848\u88ab\u544a\u6848",
                assignee_name="\u5218\u6ce2",
                status="\u5df2\u7ed3\u6848",
                text="\u6848\u4ef6\u540d\u79f0: \u5df2\u7ed3\u6848\u88ab\u544a\u6848\uff1b\u8d1f\u8d23\u4eba: \u5218\u6ce2\uff1b\u662f\u5426\u7ed3\u6848: \u662f",
                facts={"\u662f\u5426\u7ed3\u6848": "\u662f"},
            ),
            CaseTableDocument(
                doc_id="open-plaintiff",
                source_type="case_table_rag",
                source_file="\u539f\u544a\u6848\u4ef6\u5e95\u8868.xlsx",
                sheet_name="\u539f\u544a\u6848\u4ef6\u5e95\u8868",
                row_number=3,
                table_type="plaintiff_case_table",
                case_name="\u672a\u7ed3\u6848\u539f\u544a\u6848",
                assignee_name="\u5218\u6ce2",
                status="\u53d7\u7406",
                text="\u6848\u4ef6\u540d\u79f0: \u672a\u7ed3\u6848\u539f\u544a\u6848\uff1b\u8d1f\u8d23\u4eba: \u5218\u6ce2\uff1b\u662f\u5426\u7ed3\u6848: \u5426",
                facts={"\u662f\u5426\u7ed3\u6848": "\u5426"},
            ),
        ],
        sqlite_path=index_path,
    )

    explicit = resolve_knowledge(
        KnowledgeQuery(text="\u5218\u6ce2\u88ab\u544a\u6848\u4ef6\u672a\u7ed3\u6848\u6709\u51e0\u4ef6"),
        [CaseTableRagAdapter(index_path)],
    )
    assert explicit.evidence[0].facts["case_count"] == 1
    assert explicit.evidence[0].facts["total_case_count"] == 2
    assert explicit.evidence[0].facts["unclosed_only"] is True

    followup = resolve_knowledge(
        KnowledgeQuery(
            text="\u76ee\u524d\u672a\u7ed3\u6848\u7684\u6709\u51e0\u4ef6",
            metadata={"recent_case_messages": [{"text": "\u5218\u6ce2\u88ab\u544a\u6848\u4ef6\u6709\u591a\u5c11"}]},
        ),
        [CaseTableRagAdapter(index_path)],
    )
    assert followup.evidence[0].facts["assignee_name"] == "\u5218\u6ce2"
    assert followup.evidence[0].facts["table_type"] == "defendant_case_table"
    assert followup.evidence[0].facts["case_count"] == 1

    all_unclosed = resolve_knowledge(
        KnowledgeQuery(text="\u76ee\u524d\u5168\u90e8\u6848\u4ef6\u4e2d\u672a\u7ed3\u6848\u7684\u6709\u51e0\u4ef6"),
        [CaseTableRagAdapter(index_path)],
    )
    assert all_unclosed.evidence[0].facts["assignee_name"] == ""
    assert all_unclosed.evidence[0].facts["table_type"] == ""
    assert all_unclosed.evidence[0].facts["case_count"] == 2
    assert all_unclosed.evidence[0].facts["total_case_count"] == 3


def test_case_table_rag_ignores_non_case_questions(tmp_path):
    index_path = _write_index(tmp_path)
    resolution = resolve_knowledge(
        KnowledgeQuery(text="明天穿啥出门"),
        [CaseTableRagAdapter(index_path)],
    )

    assert resolution.status == "no_reliable_evidence"
    assert resolution.evidence == ()


def test_case_table_rag_priority_sits_between_registry_and_vector(tmp_path):
    index_path = _write_index(tmp_path)
    registry = KnowledgeEvidenceFrame(
        source_type="case_registry",
        source_id="registry-1",
        title="实时案件台账",
        summary="实时案件台账更优先。",
        facts={"active_case_count": 1},
        confidence=0.7,
    )
    vector = KnowledgeEvidenceFrame(
        source_type="vector_rag",
        source_id="vector-1",
        title="普通文档",
        summary="普通文档命中。",
        confidence=0.99,
    )

    resolution = resolve_knowledge(
        KnowledgeQuery(text="海花岛案进展"),
        [
            StaticAdapter("vector_rag", [vector]),
            CaseTableRagAdapter(index_path),
            StaticAdapter("case_registry", [registry]),
        ],
    )

    assert [item.source_type for item in resolution.evidence[:3]] == [
        "case_registry",
        "case_table_rag",
        "vector_rag",
    ]
