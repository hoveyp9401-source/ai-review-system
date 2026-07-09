from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent2.coordination_plan import (
    ACTION_APPEND_CASE_PROGRESS,
    ACTION_DAILY_ENTRY,
    ACTION_TRAVEL_EVENT,
    compile_coordination_plan,
)
from app.workflows.intake import IncomingMessageEnvelope


SUNDAY_NIGHT = datetime(2026, 7, 5, 21, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


def _envelope(raw_text: str, *, received_at=None) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="测试用户",
        dingtalk_user_id="ding-user-1",
        source="test",
        raw_text=raw_text,
        received_at=received_at,
    )


def _actions(plan, action_type: str):
    return [action for action in plan.actions if action.action_type == action_type]


def test_coordination_plan_splits_daily_problem_and_future_travel():
    plan = compile_coordination_plan(_envelope("合同审核流程梳理，没啥问题，明天可能出差南京"))

    daily_fields = [action.target["field"] for action in _actions(plan, ACTION_DAILY_ENTRY)]
    assert daily_fields == ["today_work", "problems", "tomorrow_plan"]

    travel = _actions(plan, ACTION_TRAVEL_EVENT)
    assert len(travel) == 1
    assert travel[0].target["destination"] == "南京"
    assert travel[0].target["date_hint"] == "tomorrow"
    assert travel[0].target["status"] == "tentative"


def test_coordination_plan_records_actual_trip_with_return_uncertain():
    plan = compile_coordination_plan(_envelope("出差去了南京开庭，同时审核合同、写函件、处理讨薪"))

    assert [action.target["field"] for action in _actions(plan, ACTION_DAILY_ENTRY)] == [
        "today_work",
        "today_work",
    ]

    travel = _actions(plan, ACTION_TRAVEL_EVENT)[0]
    assert travel.target["destination"] == "南京"
    assert travel.target["status"] == "already_traveled"
    assert travel.payload["needs_return_confirmation"] is True


def test_coordination_plan_product_work_does_not_create_travel_event():
    plan = compile_coordination_plan(
        _envelope("今天做日报系统优化，明天开始做案件进展与出差协同系统")
    )

    assert not _actions(plan, ACTION_TRAVEL_EVENT)
    assert [action.target["field"] for action in _actions(plan, ACTION_DAILY_ENTRY)] == [
        "today_work",
        "tomorrow_plan",
    ]
    assert all(action.payload.get("work_kind") == "product_work" for action in _actions(plan, ACTION_DAILY_ENTRY))


def test_coordination_plan_splits_today_work_and_tomorrow_plan_list_items():
    plan = compile_coordination_plan(
        _envelope(
            "今天优化了日报agent，参加了AI应用比赛复审会议，收集公众号被告案件信息通报，"
            "明天计划继续优化日报agent、完成公众号通报内容编辑，完成被告板块季度邮件。"
        )
    )

    daily = _actions(plan, ACTION_DAILY_ENTRY)
    assert [action.target["field"] for action in daily] == [
        "today_work",
        "today_work",
        "today_work",
        "tomorrow_plan",
        "tomorrow_plan",
        "tomorrow_plan",
    ]
    assert [action.payload["content"] for action in daily] == [
        "今天优化了日报agent",
        "参加了AI应用比赛复审会议",
        "收集公众号被告案件信息通报",
        "明天计划继续优化日报agent",
        "完成公众号通报内容编辑",
        "完成被告板块季度邮件",
    ]


def test_coordination_plan_future_trip_is_candidate_only_not_daily_plan():
    plan = compile_coordination_plan(_envelope("后天出差南通沟通保利案件调解"))

    daily = _actions(plan, ACTION_DAILY_ENTRY)
    assert daily == []

    travel = _actions(plan, ACTION_TRAVEL_EVENT)
    assert len(travel) == 1
    assert travel[0].target["destination"] == "南通"
    assert travel[0].target["status"] == "planned"


def test_coordination_plan_only_creates_case_progress_for_specific_case_mentions():
    generic = compile_coordination_plan(_envelope("项目评审、案件沟通"))
    assert not _actions(generic, ACTION_APPEND_CASE_PROGRESS)
    assert [action.target["field"] for action in _actions(generic, ACTION_DAILY_ENTRY)] == ["today_work"]

    specific = compile_coordination_plan(_envelope("恒大破产案沟通，补充诉讼材料"))
    case_actions = _actions(specific, ACTION_APPEND_CASE_PROGRESS)
    assert len(case_actions) == 1
    assert case_actions[0].target["matter_hint"] == "恒大破产案"


def test_coordination_plan_creates_case_progress_for_court_matter_without_case_suffix():
    plan = compile_coordination_plan(
        _envelope("\u82cf\u5efa\u9662\u501f\u7ae0\u4e8b\u9879\u4eca\u5929\u548c\u6cd5\u9662\u6c9f\u901a\u4e86\u6267\u884c\u8fdb\u5c55")
    )

    case_actions = _actions(plan, ACTION_APPEND_CASE_PROGRESS)
    assert len(case_actions) == 1
    assert case_actions[0].target["matter_hint"] == "\u82cf\u5efa\u9662\u501f\u7ae0\u4e8b\u9879"


def test_coordination_plan_does_not_treat_court_hearing_as_destination():
    plan = compile_coordination_plan(_envelope("\u4fdd\u5229\u6848\u4ef6\u4f30\u8ba1\u4e0b\u5468\u8981\u53bb\u5f00\u5ead"))

    assert not _actions(plan, ACTION_DAILY_ENTRY)
    assert not _actions(plan, ACTION_TRAVEL_EVENT)

    case_actions = _actions(plan, ACTION_APPEND_CASE_PROGRESS)
    assert len(case_actions) == 1
    assert case_actions[0].target["matter_hint"] == "\u4fdd\u5229\u6848\u4ef6"


def test_coordination_plan_cleans_case_hint_from_travel_and_court_words():
    plan = compile_coordination_plan(_envelope("\u4e0b\u5468\u4e94\u53bb\u5357\u4eac\u4e2d\u9662\u6c9f\u901a\u77f3\u5c71\u6848\u8fdb\u5c55"))

    travel = _actions(plan, ACTION_TRAVEL_EVENT)
    assert len(travel) == 1
    assert travel[0].target["destination"] == "\u5357\u4eac"
    assert travel[0].target["date_hint"] == "next_week"

    case_actions = _actions(plan, ACTION_APPEND_CASE_PROGRESS)
    assert len(case_actions) == 1
    assert case_actions[0].target["matter_hint"] == "\u77f3\u5c71\u6848"


def test_coordination_plan_extracts_travel_destination_before_generic_case_words():
    plan = compile_coordination_plan(_envelope("\u660e\u5929\u51fa\u5dee\u4e09\u4e9a\u6c9f\u901a\u6848\u4ef6"))

    travel = _actions(plan, ACTION_TRAVEL_EVENT)
    assert len(travel) == 1
    assert travel[0].target["destination"] == "\u4e09\u4e9a"
    assert travel[0].target["date_hint"] == "tomorrow"

    assert not _actions(plan, ACTION_APPEND_CASE_PROGRESS)


def test_coordination_plan_extracts_travel_and_specific_case_from_same_segment():
    plan = compile_coordination_plan(_envelope("\u660e\u5929\u51fa\u5dee\u4e09\u4e9a\u529e\u7406\u6d77\u82b1\u5c9b\u6848\u4ef6\u5f00\u5ead"))

    travel = _actions(plan, ACTION_TRAVEL_EVENT)
    assert len(travel) == 1
    assert travel[0].target["destination"] == "\u4e09\u4e9a"
    assert travel[0].target["date_hint"] == "tomorrow"

    case_actions = _actions(plan, ACTION_APPEND_CASE_PROGRESS)
    assert len(case_actions) == 1
    assert case_actions[0].target["matter_hint"] == "\u6d77\u82b1\u5c9b\u6848\u4ef6"


def test_coordination_plan_rejects_case_like_common_words():
    for text in [
        "今天沟通服务器方案",
        "发函原件归档至公司档案室",
        "了解法官判案思路",
        "行动方案：继续努力",
    ]:
        plan = compile_coordination_plan(_envelope(text))
        assert not _actions(plan, ACTION_APPEND_CASE_PROGRESS), text


def test_coordination_plan_rejects_travel_report_title_without_destination():
    plan = compile_coordination_plan(_envelope("近期出差工作汇报"))

    assert not _actions(plan, ACTION_TRAVEL_EVENT)


def test_coordination_plan_does_not_treat_generic_case_report_as_case_progress():
    plan = compile_coordination_plan(_envelope("用印，合同归档，月度收款计划，旬计划调整，法务小群案件汇报"))

    assert _actions(plan, ACTION_DAILY_ENTRY)
    assert not _actions(plan, ACTION_APPEND_CASE_PROGRESS)


def test_coordination_plan_does_not_treat_case_progress_system_build_as_case_progress():
    plan = compile_coordination_plan(
        _envelope(
            "3. 构建原告案件进展系统\n"
            "4. 实现日报中提及案件时自动关联并补充进展\n"
            "5. 以及在固定时间和节点询问进展\n"
            "这三个是同一条"
        )
    )

    assert _actions(plan, ACTION_DAILY_ENTRY)
    assert not _actions(plan, ACTION_APPEND_CASE_PROGRESS)


def test_coordination_plan_future_trip_is_both_tomorrow_plan_and_travel():
    plan = compile_coordination_plan(_envelope("明天计划去优化脱敏工具和出差南京盖章"))

    assert [action.target["field"] for action in _actions(plan, ACTION_DAILY_ENTRY)] == ["tomorrow_plan"]

    travel = _actions(plan, ACTION_TRAVEL_EVENT)[0]
    assert travel.target["destination"] == "南京"
    assert travel.target["date_hint"] == "tomorrow"
    assert travel.target["status"] == "planned"


def test_coordination_plan_sunday_monday_trip_is_tomorrow_plan_and_clean_destination():
    plan = compile_coordination_plan(
        _envelope("\u5468\u4e00\u9884\u8ba1\u51fa\u5dee\u53bb\u5170\u5dde", received_at=SUNDAY_NIGHT)
    )

    daily = _actions(plan, ACTION_DAILY_ENTRY)
    travel = _actions(plan, ACTION_TRAVEL_EVENT)

    assert [action.target["field"] for action in daily] == ["tomorrow_plan"]
    assert travel[0].target["destination"] == "\u5170\u5dde"
    assert travel[0].target["date_hint"] == "tomorrow"
    assert travel[0].target["status"] == "planned"


def test_coordination_plan_completed_previous_plan_goes_to_today_work():
    plan = compile_coordination_plan(_envelope("\u6628\u5929\u7684\u660e\u65e5\u8ba1\u5212\u5df2\u5b8c\u6210"))

    assert [action.target["field"] for action in _actions(plan, ACTION_DAILY_ENTRY)] == ["today_work"]


def test_coordination_plan_does_not_inherit_daily_field_for_question_segment():
    plan = compile_coordination_plan(
        _envelope("\u4eca\u5929\u641e\u5b8c\u5408\u540c\u5ba1\u6838\uff0c\u660e\u5929\u51fa\u5dee\u53bb\u5357\u4eac\u76d6\u7ae0\uff0c\u987a\u4fbf\u95ee\u4e0b\u51fa\u5dee\u62a5\u9500\u662f\u4e0d\u662f\u8981\u63d0\u524d\u7533\u8bf7\uff1f")
    )

    daily = _actions(plan, ACTION_DAILY_ENTRY)
    assert [action.target["field"] for action in daily] == ["today_work", "tomorrow_plan"]
    assert all("\u62a5\u9500" not in action.payload["content"] for action in daily)


def test_coordination_plan_splits_tomorrow_plan_and_problem_by_comma():
    plan = compile_coordination_plan(
        _envelope("\u660e\u5929\u8ba1\u5212\u53bb\u8d9f\u653f\u52a1\u5927\u5385\u529e\u8bc1\uff0c\u95ee\u9898\u5c31\u662f\u5ba2\u6237\u90a3\u8fb9\u8fd8\u72b9\u8c6b\u8981\u4e0d\u8981\u7eed")
    )

    daily = _actions(plan, ACTION_DAILY_ENTRY)
    assert [action.target["field"] for action in daily] == ["tomorrow_plan", "problems"]
    assert daily[0].payload["content"] == "\u660e\u5929\u8ba1\u5212\u53bb\u8d9f\u653f\u52a1\u5927\u5385\u529e\u8bc1"
    assert daily[1].payload["content"] == "\u95ee\u9898\u5c31\u662f\u5ba2\u6237\u90a3\u8fb9\u8fd8\u72b9\u8c6b\u8981\u4e0d\u8981\u7eed"


def test_coordination_plan_treats_asking_judge_tomorrow_as_plan_not_question():
    plan = compile_coordination_plan(_envelope("\u660e\u5929\u95ee\u4e0b\u6cd5\u5b98"))

    daily = _actions(plan, ACTION_DAILY_ENTRY)
    assert [action.target["field"] for action in daily] == ["tomorrow_plan"]


def test_coordination_plan_keeps_completed_previous_plan_sentence_together():
    plan = compile_coordination_plan(
        _envelope("\u6628\u5929\u8bf4\u7684\u90a3\u4e2a\u660e\u65e5\u8ba1\u5212\uff0c\u5c31\u662f\u62dc\u8bbf\u5ba2\u6237\u90a3\u4e2a\uff0c\u4eca\u5929\u5df2\u7ecf\u641e\u5b8c\u4e86")
    )

    daily = _actions(plan, ACTION_DAILY_ENTRY)
    assert [action.target["field"] for action in daily] == ["today_work"]
    assert "\u6628\u5929\u8bf4\u7684\u90a3\u4e2a\u660e\u65e5\u8ba1\u5212" in daily[0].payload["content"]


def test_coordination_plan_routes_business_problem_evidence_to_problem_field():
    plan = compile_coordination_plan(_envelope("\u4e1a\u52a1\u90e8\u95e8\u6750\u6599\u4e00\u76f4\u6ca1\u53cd\u9988"))

    assert [action.target["field"] for action in _actions(plan, ACTION_DAILY_ENTRY)] == ["problems"]


def test_coordination_plan_routes_future_problem_resolution_to_tomorrow_plan():
    plan = compile_coordination_plan(_envelope("\u660e\u5929\u5904\u7406\u8d44\u6599\u7f3a\u5931\u95ee\u9898"))

    assert [action.target["field"] for action in _actions(plan, ACTION_DAILY_ENTRY)] == ["tomorrow_plan"]


def test_coordination_observation_does_not_leak_raw_text():
    envelope = _envelope("明天计划去扬州出差")
    plan = compile_coordination_plan(envelope)
    observation = plan.as_observation()

    assert observation["source_text_hash"]
    assert "raw_text" not in observation
    assert all("raw_text" not in action for action in observation["actions"])
    assert all(action["source_text_hash"] for action in observation["actions"])
