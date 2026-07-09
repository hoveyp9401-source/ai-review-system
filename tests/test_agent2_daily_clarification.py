from datetime import date
from types import SimpleNamespace

from app.agent2.context_pack import build_agent2_context_pack
from app.agent2.daily_clarification import (
    build_daily_candidate_clarification,
    build_daily_candidate_clarification_reply,
    build_pending_daily_candidate_focus_reply,
)
from app.agent2.daily_state import PENDING_DAILY_CANDIDATE_KEY
from app.workflows.intake import ActiveWorkflowTask, IncomingMessageEnvelope, WORKFLOW_DAILY_REPORT


def _context_pack(raw_text: str):
    report = SimpleNamespace(
        id="report-1",
        report_date=date(2026, 7, 5),
        status="collecting",
        today_work=[
            "完成了",
            "进行上海机载（机器的机载重的载）项目评审",
            "名苑项目拟诉评估",
        ],
        problems=[],
        tomorrow_plan=["晋城酒店飞速收款支撑"],
        section_status={
            "_draft_item_ids": {
                "today_work": ["tw-1", "tw-2", "tw-3"],
                "problems": [],
                "tomorrow_plan": ["tp-1"],
            }
        },
    )
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="测试用户",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
        active_tasks=(
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="report-1",
                status="collecting",
                reply_candidate=True,
            ),
        ),
    )
    return build_agent2_context_pack(envelope, daily_report=report)


def test_daily_clarification_uses_draft_candidate_instead_of_open_question():
    reply = build_daily_candidate_clarification_reply(
        raw_text="上海鸡仔",
        context_pack=_context_pack("上海鸡仔"),
    )

    assert "我没直接改日报" in reply
    assert "最接近的是" in reply
    assert "【今日工作2】进行上海机载" in reply
    assert "第几条" not in reply
    assert "你是要" not in reply


def test_daily_clarification_returns_persistable_candidate_payload():
    clarification = build_daily_candidate_clarification(
        raw_text="上海鸡仔",
        context_pack=_context_pack("上海鸡仔"),
    )

    assert clarification is not None
    assert clarification.pending_payload == {
        "type": "daily_candidate_focus",
        "field": "today_work",
        "field_label": "今日工作",
        "field_index": 2,
        "global_index": 2,
        "item_id": "tw-2",
        "text": "进行上海机载（机器的机载重的载）项目评审",
        "source_text": "上海鸡仔",
        "reason": "fuzzy",
        "score": 0.55,
    }


def test_daily_clarification_maps_bare_ordinal_to_current_global_item():
    reply = build_daily_candidate_clarification_reply(
        raw_text="第一",
        context_pack=_context_pack("第一"),
    )

    assert "我没直接改日报" in reply
    assert "当前草稿第 1 项" in reply
    assert "【今日工作1】完成了" in reply
    assert "你是要" not in reply


def test_daily_clarification_does_not_guess_when_candidate_is_not_unique():
    report = SimpleNamespace(
        id="report-1",
        report_date=date(2026, 7, 5),
        status="collecting",
        today_work=["上海项目沟通", "上海法院联系"],
        problems=[],
        tomorrow_plan=[],
        section_status={},
    )
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="测试用户",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text="上海",
    )
    pack = build_agent2_context_pack(envelope, daily_report=report)

    assert build_daily_candidate_clarification_reply(raw_text="上海", context_pack=pack) == ""


def test_daily_clarification_does_not_match_non_daily_chatter_to_draft_items():
    report = SimpleNamespace(
        id="report-1",
        report_date=date(2026, 7, 5),
        status="collecting",
        today_work=[],
        problems=[],
        tomorrow_plan=["明天出差三亚沟通海花岛案件"],
        section_status={
            "_draft_item_ids": {
                "today_work": [],
                "problems": [],
                "tomorrow_plan": ["tp-1"],
            }
        },
    )
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="测试用户",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text="明天吃屎",
        active_tasks=(
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="report-1",
                status="collecting",
                reply_candidate=True,
            ),
        ),
    )
    pack = build_agent2_context_pack(envelope, daily_report=report)

    assert build_daily_candidate_clarification_reply(raw_text="明天吃屎", context_pack=pack) == ""


def test_pending_candidate_focus_confirmation_does_not_sound_like_submit_or_open_question():
    report = SimpleNamespace(
        id="report-1",
        report_date=date(2026, 7, 5),
        status="collecting",
        today_work=["进行上海机载（机器的机载重的载）项目评审"],
        problems=[],
        tomorrow_plan=[],
        section_status={
            "_draft_item_ids": {"today_work": ["tw-1"], "problems": [], "tomorrow_plan": []},
            PENDING_DAILY_CANDIDATE_KEY: {
                "field": "today_work",
                "field_label": "今日工作",
                "field_index": 1,
                "item_id": "tw-1",
                "text": "进行上海机载（机器的机载重的载）项目评审",
                "source_text": "上海鸡仔",
            },
        },
    )
    envelope = IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="测试用户",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text="对",
        active_tasks=(
            ActiveWorkflowTask(
                workflow=WORKFLOW_DAILY_REPORT,
                task_id="report-1",
                status="collecting",
                reply_candidate=True,
                metadata={"pending_keys": [PENDING_DAILY_CANDIDATE_KEY]},
            ),
        ),
    )
    reply = build_pending_daily_candidate_focus_reply(
        raw_text="对",
        context_pack=build_agent2_context_pack(envelope, daily_report=report),
    )

    assert "没有提交或修改日报" in reply
    assert "【今日工作1】进行上海机载" in reply
    assert "确认提交日报" in reply
    assert "第几条" not in reply
    assert "你是要" not in reply
