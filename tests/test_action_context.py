from datetime import datetime
from zoneinfo import ZoneInfo

from app.workflows.action_context import (
    OP_ASK,
    OP_BEGIN_EDIT,
    OP_CHAT,
    OP_STATUS_QUERY,
    OP_WRITE,
    WORK_OBJECT_CASE_DATA,
    WORK_OBJECT_CASE_PROGRESS,
    WORK_OBJECT_CHAT,
    WORK_OBJECT_DAILY_REPORT,
    WORK_OBJECT_MONTHLY_REPORT,
    WORK_OBJECT_TRAVEL_PLAN,
    WRITE_INTENT_NO_WRITE,
    WRITE_INTENT_READ_ONLY,
    WRITE_INTENT_SANDBOX,
    resolve_action_context,
)
from app.workflows.action_intake import plan_user_actions
from app.workflows.intake import IncomingMessageEnvelope


MORNING = datetime(2026, 7, 7, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _envelope(raw_text: str, *, received_at=None) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
        received_at=received_at,
    )


def test_action_context_observes_dated_daily_begin_edit():
    context = resolve_action_context("\u6211\u8981\u6539\u6628\u5929\u65e5\u62a5", received_at=MORNING)

    assert context.work_object == WORK_OBJECT_DAILY_REPORT
    assert context.operation == OP_BEGIN_EDIT
    assert context.temporal_hint == "yesterday"
    assert context.report_date_policy == "explicit_history"
    assert context.write_intent == WRITE_INTENT_READ_ONLY


def test_action_context_observes_before_nine_daily_default_policy():
    context = resolve_action_context("\u5199\u65e5\u62a5", received_at=MORNING)

    assert context.work_object == WORK_OBJECT_DAILY_REPORT
    assert context.operation == OP_WRITE
    assert context.report_date_policy == "before_nine_defaults_yesterday"


def test_action_context_observes_monthly_status_query():
    context = resolve_action_context("\u5927\u5bb6\u6708\u62a5\u586b\u7684\u600e\u6837\u4e86")

    assert context.work_object == WORK_OBJECT_MONTHLY_REPORT
    assert context.operation == OP_STATUS_QUERY
    assert context.write_intent == WRITE_INTENT_READ_ONLY


def test_action_context_observes_case_data_question():
    context = resolve_action_context("\u6cd5\u52a1\u4e8c\u90e8\u76ee\u524d\u88ab\u544a\u5b58\u91cf\u591a\u5c11")

    assert context.work_object == WORK_OBJECT_CASE_DATA
    assert context.operation == OP_ASK
    assert context.write_intent == WRITE_INTENT_READ_ONLY


def test_action_context_separates_future_case_progress_from_tomorrow_daily_plan():
    context = resolve_action_context("\u4fdd\u5229\u6848\u4ef6\u4f30\u8ba1\u4e0b\u5468\u8981\u53bb\u5f00\u5ead")

    assert context.work_object == WORK_OBJECT_CASE_PROGRESS
    assert context.operation == OP_WRITE
    assert context.temporal_hint == "next_week"
    assert context.write_intent == WRITE_INTENT_SANDBOX


def test_action_context_observes_travel_candidate():
    context = resolve_action_context("\u660e\u5929\u53bb\u5357\u4eac\u51fa\u5dee")

    assert context.work_object == WORK_OBJECT_DAILY_REPORT
    assert WORK_OBJECT_TRAVEL_PLAN in context.secondary_work_objects
    assert context.operation == OP_WRITE
    assert context.temporal_hint == "tomorrow"
    assert context.write_intent == "write"


def test_action_context_treats_tomorrow_ask_judge_as_daily_plan():
    context = resolve_action_context("\u660e\u5929\u95ee\u4e0b\u6cd5\u5b98")

    assert context.work_object == WORK_OBJECT_DAILY_REPORT
    assert context.operation == OP_WRITE
    assert context.temporal_hint == "tomorrow"
    assert context.write_intent == "write"


def test_action_context_blocks_meta_test_as_chat():
    context = resolve_action_context("\u8ba9\u6211\u6d4b\u8bd5\u4e0b")

    assert context.work_object == WORK_OBJECT_CHAT
    assert context.operation == OP_CHAT
    assert context.write_intent == WRITE_INTENT_NO_WRITE


def test_action_context_treats_non_business_short_chatter_as_chat():
    for text in ["\u54ce", "\u54c8\u54e6", "\u660e\u5929\u5403\u5c4e"]:
        context = resolve_action_context(text)

        assert context.work_object == WORK_OBJECT_CHAT
        assert context.operation == OP_CHAT
        assert context.write_intent == WRITE_INTENT_NO_WRITE


def test_action_context_treats_daily_meta_status_as_chat():
    context = resolve_action_context("\u5199\u65e5\u62a5\u4e86")

    assert context.work_object == WORK_OBJECT_CHAT
    assert context.operation == OP_CHAT
    assert context.write_intent == WRITE_INTENT_NO_WRITE


def test_action_plan_observation_includes_action_context():
    plan = plan_user_actions(_envelope("\u6211\u8981\u6539\u6628\u5929\u65e5\u62a5", received_at=MORNING))
    observation = plan.as_observation()

    assert observation["action_context"]["work_object"] == WORK_OBJECT_DAILY_REPORT
    assert observation["action_context"]["operation"] == OP_BEGIN_EDIT
    assert observation["action_context"]["report_date_policy"] == "explicit_history"
