from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.agent2.context_pack import (
    AssistantReplyFrame,
    KnowledgeEvidenceFrame,
    build_agent2_context_pack,
)
from app.agent2.personal_memory import build_personal_memory_profile
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT


def _envelope(text: str = "把第5条改成流程已协调") -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="庞浩",
        dingtalk_user_id="0515246015778891",
        source="dingtalk_stream_text",
        raw_text=text,
        message_id="msg-1",
        conversation_id="conv-1",
        received_at=datetime(2026, 7, 3, 10, 0, 0),
        active_tasks=(
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="report-1",
                status="collecting",
                reply_candidate=True,
                awaiting_confirmation=False,
                reason="recent daily report has active state",
                metadata={"report_date": "2026-07-03"},
            ),
        ),
    )


def test_context_pack_builds_daily_draft_with_field_and_global_indices():
    report = SimpleNamespace(
        id="report-1",
        report_date=date(2026, 7, 3),
        status="collecting",
        today_work=["合同审核", "函件起草", "案件沟通"],
        problems=["材料缺失", "流程卡点"],
        tomorrow_plan=["南京出差"],
        section_status={
            "_draft_item_ids": {
                "today_work": ["tw-1", "tw-2", "tw-3"],
                "problems": ["pb-1", "pb-2"],
                "tomorrow_plan": ["tp-1"],
            }
        },
    )

    pack = build_agent2_context_pack(_envelope(), daily_report=report)
    payload = pack.as_payload()

    assert payload["user"]["name"] == "庞浩"
    assert payload["active_tasks"][0]["workflow"] == WORKFLOW_DAILY_REPORT
    items = payload["daily_draft"]["items"]
    assert [(item["field"], item["field_index"], item["global_index"], item["item_id"]) for item in items] == [
        ("today_work", 1, 1, "tw-1"),
        ("today_work", 2, 2, "tw-2"),
        ("today_work", 3, 3, "tw-3"),
        ("problems", 1, 4, "pb-1"),
        ("problems", 2, 5, "pb-2"),
        ("tomorrow_plan", 1, 6, "tp-1"),
    ]
    assert payload["daily_draft"]["items_by_field"]["problems"][1]["global_index"] == 5


def test_context_pack_carries_recent_edit_memory_and_pending_action():
    report = {
        "report_id": "report-1",
        "report_date": "2026-07-03",
        "status": "collecting",
        "today_work": ["合同审核"],
        "problems": ["材料缺失"],
        "tomorrow_plan": [],
        "section_status": {
            "_agent2_last_modified_item": {
                "field": "today_work",
                "item_index": 1,
                "item_id": "tw-1",
                "text": "合同审核",
                "source": "agent2",
            },
            "_agent2_last_deleted_item": {
                "field": "problems",
                "item_ids": ["pb-1"],
                "source": "agent2",
            },
            "_pending_action": "confirm_clear_current_report",
        },
    }

    pack = build_agent2_context_pack(_envelope("刚才那条删掉"), daily_report=report)
    actions = [action.as_payload() for action in pack.recent_actions]

    assert actions[0] == {
        "action_type": "last_modified_item",
        "field": "today_work",
        "item_index": 1,
        "item_id": "tw-1",
        "item_ids": [],
        "text": "合同审核",
        "source": "agent2",
    }
    assert actions[1]["action_type"] == "last_deleted_item"
    assert actions[1]["item_ids"] == ["pb-1"]
    assert actions[2]["action_type"] == "pending_action"


def test_knowledge_evidence_requires_provenance_and_confidence():
    with pytest.raises(ValueError):
        KnowledgeEvidenceFrame(source_type="", source_id="case-1", title="案件", summary="在办案件", confidence=0.9)
    with pytest.raises(ValueError):
        KnowledgeEvidenceFrame(source_type="case_registry", source_id="", title="案件", summary="在办案件", confidence=0.9)
    with pytest.raises(ValueError):
        KnowledgeEvidenceFrame(source_type="case_registry", source_id="case-1", title="案件", summary="", confidence=0.9)
    with pytest.raises(ValueError):
        KnowledgeEvidenceFrame(source_type="case_registry", source_id="case-1", title="案件", summary="在办案件", confidence=1.2)


def test_context_pack_keeps_rag_evidence_separate_from_daily_context():
    evidence = KnowledgeEvidenceFrame(
        source_type="case_registry",
        source_id="assignee:user-1:active",
        title="庞浩在办案件",
        summary="案件台账显示庞浩名下有 3 个在办案件。",
        facts={
            "assignee_user_id": "user-1",
            "active_case_count": 3,
            "case_names": ["A案", "B案", "C案"],
        },
        confidence=0.96,
        freshness="2026-07-03",
    )

    pack = build_agent2_context_pack(_envelope("我手底下有几个案子？"), knowledge=[evidence])
    payload = pack.as_payload()

    assert payload["daily_draft"] is None
    assert payload["knowledge_status"] == "available"
    assert payload["knowledge"][0]["source_type"] == "case_registry"
    assert payload["knowledge"][0]["facts"]["active_case_count"] == 3


def test_context_pack_marks_knowledge_absent_instead_of_guessing():
    pack = build_agent2_context_pack(
        _envelope("我手底下有几个案子？"),
        recent_assistant_replies=[
            AssistantReplyFrame(text="这句我按【内部问答】处理，不写入日报。", reply_type="internal_qa", source="static")
        ],
    )
    payload = pack.as_payload()

    assert payload["knowledge"] == []
    assert payload["knowledge_status"] == "not_retrieved_or_no_match"
    assert payload["recent_assistant_replies"][0]["reply_type"] == "internal_qa"


def test_context_pack_carries_user_scoped_personal_memory():
    profile = build_personal_memory_profile(
        user=SimpleNamespace(id="user-1", dingtalk_user_id="0515246015778891", name="庞浩"),
        user_habits=[
            SimpleNamespace(
                habit_type="previous_plan_rollover",
                trigger_text="昨天计划已完成",
                meaning="参考昨天明日计划转今日工作",
                confidence=0.9,
                evidence_count=4,
            )
        ],
    )

    pack = build_agent2_context_pack(_envelope("我有点懒得测"), personal_memory=profile)
    payload = pack.as_payload()

    assert payload["personal_memory"]["user_id"] == "user-1"
    assert payload["personal_memory"]["dingtalk_user_id"] == "0515246015778891"
    assert payload["personal_memory"]["active_habits"][0]["trigger_text"] == "昨天计划已完成"
    assert payload["personal_memory"]["is_user_scoped"] is True
