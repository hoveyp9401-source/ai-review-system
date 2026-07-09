from datetime import date
import hashlib
import json
from types import SimpleNamespace

from app.agent.executor import (
    _low_confidence_report_fragment_plan,
    _normalize_report_fields_after_actions,
    _restore_untouched_existing_fields,
    _split_today_work_multi_action,
)
from app.agent.state_resolver import resolve_pending_interaction
from app.services.report_service import _direct_ordinal_move_plan, _direct_problem_slot_answer_plan, _direct_plan_slot_answer_plan
from app.services.report_service import _direct_clear_current_report_plan, _is_probable_noise_input
from app.services.report_service import (
    _direct_single_action_effects_plan,
    _direct_explicit_append_plan,
    _direct_last_unwritten_candidate_plan,
    _direct_numbered_work_items_plan,
    _extract_simple_report_fields,
    _resolve_direct_agent_plan,
    _direct_range_merge_plan,
    _direct_ordinal_rewrite_plan,
    _direct_set_section_empty_plan,
    _direct_text_replace_plan,
    _direct_this_is_problem_without_candidate_plan,
    _extract_numbered_work_items,
)




def test_tomorrow_expected_slot_accepts_short_content_without_reasking_target():
    report = SimpleNamespace(
        today_work=["\u5ba1\u6838\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=[],
        section_status={"problems_acknowledged_empty": True},
    )

    plan = _direct_plan_slot_answer_plan("\u5199\u62a5\u544a", report)

    assert plan is not None
    assert plan.should_write is True
    assert plan.actions[0].field == "tomorrow_plan"
    assert plan.actions[0].items == ["\u5199\u62a5\u544a"]


def test_tomorrow_expected_slot_routes_followup_marker_to_last_today_work():
    report = SimpleNamespace(
        today_work=["\u5ba1\u6838\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=[],
        section_status={"problems_acknowledged_empty": True},
    )

    plan = _direct_plan_slot_answer_plan("\u7b49\u7b49\uff0c\u6211\u8fd8\u5f00\u4e86\u4e2a\u8bc4\u5ba1\u4f1a", report)

    assert plan is not None
    assert plan.should_write is True
    assert plan.actions[0].field == "today_work"
    assert plan.actions[0].items == ["\u5f00\u4e86\u4e2a\u8bc4\u5ba1\u4f1a"]




def test_tomorrow_expected_slot_future_marker_stays_tomorrow_plan():
    report = SimpleNamespace(
        today_work=["\u5ba1\u6838\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=[],
        section_status={"problems_acknowledged_empty": True},
    )

    plan = _direct_plan_slot_answer_plan("\u53e6\u5916\u660e\u5929\u5199\u62a5\u544a", report)

    assert plan is not None
    assert plan.should_write is True
    assert plan.actions[0].field == "tomorrow_plan"
    assert plan.actions[0].items == ["\u53e6\u5916\u660e\u5929\u5199\u62a5\u544a"]


def test_tomorrow_expected_slot_handles_empty_plan_reply_as_plan_not_risk():
    report = SimpleNamespace(
        today_work=["\u5ba1\u6838\u5408\u540c"],
        problems=["\u6682\u65e0\u660e\u663e\u95ee\u9898"],
        tomorrow_plan=[],
        section_status={"problems_acknowledged_empty": True},
    )

    plan = _direct_plan_slot_answer_plan("\u6ca1\u6709", report)

    assert plan is not None
    assert plan.should_write is True
    assert plan.actions[0].field == "tomorrow_plan"
    assert plan.actions[0].items == ["\u65e0\u660e\u65e5\u8ba1\u5212"]


def test_awaiting_append_content_done_phrase_finishes_without_target_reprompt():
    resolution = resolve_pending_interaction(
        "\u4e0d\u7528\u4e86",
        {
            "type": "awaiting_append_content",
            "operation": "append",
            "target_field": "today_work",
            "context": {},
        },
    )

    assert resolution is not None
    assert resolution.branch == "cancel_append_content"
    assert resolution.plan.should_write is False
    assert resolution.plan.clear_pending_interaction is True
    assert "\u8865\u5145\u7684\u4eca\u65e5\u5de5\u4f5c\u5185\u5bb9" not in resolution.plan.reply_to_user


def test_problem_slot_no_problem_phrase_is_normalized():
    report = SimpleNamespace(
        today_work=["优化日报发送逻辑"],
        problems=[],
        tomorrow_plan=["优化脱敏工具"],
        section_status={},
    )

    plan = _direct_problem_slot_answer_plan("没啥问题了", report)

    assert plan is not None
    assert plan.actions[0].field == "problems"
    assert plan.actions[0].items == ["暂无明显问题"]


def test_pending_current_report_query_confirmation_is_supported():
    resolution = resolve_pending_interaction(
        "恩",
        {
            "type": "awaiting_action_confirmation",
            "operation": "query_current",
            "target_field": "none",
            "context": {},
        },
    )

    assert resolution is not None
    assert resolution.plan.intent == "query_current"
    assert resolution.plan.clear_pending_interaction is True


def test_ordinal_move_uses_nearest_move_clause_only():
    report = SimpleNamespace(
        today_work=[
            "优化日报发送逻辑",
            "线上签署1份增补合同",
            "沟通服务器方案",
            "下载柬埔寨民法典英文版与高棉文版",
            "线上签署云筑网福建项目5月产值申报",
            "填报日期更清楚了",
        ],
        problems=[],
        tomorrow_plan=[],
        section_status={},
    )

    plan = _direct_ordinal_move_plan("一到四项是完成事项，五到六项放到明天计划", report)

    assert plan is not None
    assert plan.actions[0].item_indices == [5, 6]
    assert plan.actions[0].target_field == "tomorrow_plan"


def test_untouched_existing_fields_are_preserved_when_appending_other_slots():
    existing = SimpleNamespace(
        today_work=["ate pancake"],
        problems=[],
        tomorrow_plan=[],
    )

    today_work, problems, tomorrow_plan = _restore_untouched_existing_fields(
        existing,
        touched_fields={"problems", "tomorrow_plan"},
        today_work=[],
        problems=["egg was missing"],
        tomorrow_plan=["try another shop"],
    )

    assert today_work == ["ate pancake"]
    assert problems == ["egg was missing"]
    assert tomorrow_plan == ["try another shop"]


def test_meaningful_low_confidence_initial_fragment_is_recorded_as_today_work():
    plan = _low_confidence_report_fragment_plan("吃了手抓饼", None)

    assert plan is not None
    assert plan.actions[0].field == "today_work"
    assert plan.actions[0].items == ["吃了手抓饼"]


def test_low_confidence_noise_or_history_query_is_not_recorded():
    assert _low_confidence_report_fragment_plan("哈哈哈", None) is None
    assert _low_confidence_report_fragment_plan("发我看下昨天日报", None) is None


def test_global_clear_request_uses_deterministic_confirmation():
    report = SimpleNamespace(
        today_work=["审核合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天继续"],
        section_status={},
    )

    plan = _direct_clear_current_report_plan("全部删掉。", report)

    assert plan is not None
    assert plan.actions[0].type == "clear_all"
    assert plan.actions[0].requires_confirmation is False


def test_today_work_multi_action_sentence_is_split_into_items():
    assert _split_today_work_multi_action("审核合同、整理材料、沟通服务器方案") == [
        "审核合同",
        "整理材料",
        "沟通服务器方案",
    ]


def test_numbered_direct_items_can_skip_second_level_today_work_splitting():
    today_work, _problems, _tomorrow_plan, _empty_ack = _normalize_report_fields_after_actions(
        ["法务四部、五部诉讼案件过堂会；部分案件沟通处理方案"],
        [],
        [],
        {},
        split_today_work=False,
    )

    assert today_work == ["法务四部、五部诉讼案件过堂会；部分案件沟通处理方案"]


def test_single_action_effects_and_merged_semicolon_items_are_not_split():
    assert _split_today_work_multi_action("优化AI发送逻辑：填报日期更清楚了，9点规则收紧了") == [
        "优化AI发送逻辑：填报日期更清楚了，9点规则收紧了"
    ]
    assert _split_today_work_multi_action("填报日期更清楚了；9点规则收紧了；影子记忆系统") == [
        "填报日期更清楚了；9点规则收紧了；影子记忆系统"
    ]


def test_top_level_numbered_items_keep_internal_punctuation_together():
    raw_input = """今天主要工作：
1、法务四部、五部诉讼案件过堂会；部分案件沟通处理方案
2、协助风控处理履约材料、印章事项
3、整理合同台账，更新归档状态
4、沟通原告案件节点，确认后续安排
5、跟进被告案件资料补充
6、配合上下游事项推进
7、处理部门日常行政支持"""

    items = _extract_numbered_work_items(raw_input)

    assert len(items) == 7
    assert items[0] == "法务四部、五部诉讼案件过堂会；部分案件沟通处理方案"
    assert "协助风控处理履约材料、印章事项" in items


def test_direct_numbered_work_plan_keeps_top_level_items_and_extracts_tail_slots():
    raw_input = (
        "今天主要工作："
        "1、法务四部、五部诉讼案件过堂会；部分案件沟通处理方案；"
        "2、协助风控处理履约材料、印章事项；"
        "3、整理合同台账，更新归档状态；"
        "4、沟通原告案件节点，确认后续安排；"
        "5、跟进被告案件资料补充；"
        "6、配合上下游事项推进；"
        "7、处理部门日常行政支持。"
        "没啥问题，明日计划继续跟进重点案件。"
    )

    plan = _direct_numbered_work_items_plan(raw_input, None)

    assert plan is not None
    today_action = next(action for action in plan.actions if action.field == "today_work")
    problem_action = next(action for action in plan.actions if action.field == "problems")
    plan_action = next(action for action in plan.actions if action.field == "tomorrow_plan")
    assert today_action.source == "direct_numbered_work_items"
    assert len(today_action.items) == 7
    assert today_action.items[0] == "法务四部、五部诉讼案件过堂会；部分案件沟通处理方案"
    assert problem_action.items == ["暂无明显问题"]
    assert plan_action.items == ["继续跟进重点案件"]


def test_direct_ordinal_rewrite_uses_last_display_context_mapping():
    today_work = ["A", "B", "C", "D", "E", "F", "借阅资料"]
    draft_hash = hashlib.sha1(
        json.dumps(
            {"today_work": today_work, "problems": ["暂无明显问题"], "tomorrow_plan": ["继续推进"]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    report = SimpleNamespace(
        id="report-id",
        report_date=date(2026, 6, 24),
        today_work=today_work,
        problems=["暂无明显问题"],
        tomorrow_plan=["继续推进"],
        section_status={
            "_last_display_context": {
                "report_date": "2026-06-24",
                "draft_hash": draft_hash,
                "sections": [
                    {
                        "section": "today_work",
                        "items": [
                            {"section": "today_work", "display_index": index, "actual_index": index, "text": text}
                            for index, text in enumerate(["A", "B", "C", "D", "E", "F", "借阅资料"], start=1)
                        ],
                    }
                ],
            }
        },
    )

    plan = _direct_ordinal_rewrite_plan("第七条改成借阅方庆双案件资料", report)

    assert plan is not None
    assert plan.actions[0].type == "replace_text"
    assert plan.actions[0].field == "today_work"
    assert plan.actions[0].target_item_index == 7
    assert plan.actions[0].new_value == "借阅方庆双案件资料"


def test_direct_567_merge_is_parsed_without_confirmation():
    report = SimpleNamespace(
        today_work=["A", "B", "C", "D", "E", "F", "G"],
        problems=[],
        tomorrow_plan=[],
        section_status={},
    )

    plan = _direct_range_merge_plan("567合并", report)

    assert plan is not None
    assert plan.should_write is True
    assert plan.actions[0].type == "merge_items"
    assert plan.actions[0].item_indices == [5, 6, 7]
    assert plan.actions[0].requires_confirmation is False


def test_direct_chinese_567_merge_is_parsed_without_confirmation():
    report = SimpleNamespace(
        today_work=["A", "B", "C", "D", "E", "F", "G"],
        problems=[],
        tomorrow_plan=[],
        section_status={},
    )

    plan = _direct_range_merge_plan("五六七合并", report)

    assert plan is not None
    assert plan.should_write is True
    assert plan.actions[0].type == "merge_items"
    assert plan.actions[0].item_indices == [5, 6, 7]
    assert plan.actions[0].requires_confirmation is False


def test_direct_text_replace_unique_match_executes_and_multi_match_clarifies():
    unique = SimpleNamespace(
        today_work=["审核合同", "借阅资料"],
        problems=[],
        tomorrow_plan=[],
        section_status={},
    )
    unique_plan = _direct_text_replace_plan("把借阅资料改成借阅方庆双案件资料", unique)

    assert unique_plan is not None
    assert unique_plan.should_write is True
    assert unique_plan.actions[0].field == "today_work"
    assert unique_plan.actions[0].target_item_index == 2

    multi = SimpleNamespace(
        today_work=["借阅资料", "整理资料"],
        problems=[],
        tomorrow_plan=[],
        section_status={},
    )
    multi_plan = _direct_text_replace_plan("资料改成方庆双案件资料", multi)

    assert multi_plan is not None
    assert multi_plan.should_write is False
    assert multi_plan.pending_interaction_to_set is not None
    assert multi_plan.pending_interaction_to_set.type == "pending_clarification"
    assert "你要修改哪一条" in multi_plan.reply_to_user
    assert "确认" not in multi_plan.reply_to_user


def test_direct_set_section_empty_handles_no_risk_and_no_plan():
    problem_plan = _direct_set_section_empty_plan("暂无风险", None)
    tomorrow_plan = _direct_set_section_empty_plan("明天没计划", None)

    assert problem_plan is not None
    assert problem_plan.actions[0].field == "problems"
    assert problem_plan.actions[0].items == ["暂无明显问题"]
    assert tomorrow_plan is not None
    assert tomorrow_plan.actions[0].field == "tomorrow_plan"
    assert tomorrow_plan.actions[0].items == ["无明日计划"]


def test_direct_explicit_append_routes_before_llm():
    report = SimpleNamespace(
        today_work=["审核合同"],
        problems=[],
        tomorrow_plan=[],
        section_status={},
    )

    cases = [
        ("今日工作加上审核合同3份", "today_work", "审核合同3份"),
        ("问题里加拟诉评估效率低", "problems", "拟诉评估效率低"),
        ("明日计划加去苏州开庭", "tomorrow_plan", "去苏州开庭"),
        ("补充到今日工作 线上签署合同", "today_work", "线上签署合同"),
        ("补充到问题/风险 材料反馈慢", "problems", "材料反馈慢"),
        ("补充到明日计划 跟进审批", "tomorrow_plan", "跟进审批"),
    ]

    for raw_input, field, expected in cases:
        branch_plan = _resolve_direct_agent_plan(raw_input, report)
        assert branch_plan is not None
        branch, plan = branch_plan
        assert branch == "direct_explicit_append"
        assert plan.should_write is True
        assert plan.clear_pending_interaction is True
        assert plan.actions[0].type == "append_items"
        assert plan.actions[0].field == field
        assert plan.actions[0].requires_confirmation is False
        assert plan.actions[0].items == [expected]


def test_direct_tomorrow_phrase_splits_leading_problem_and_future_plan():
    report = SimpleNamespace(
        today_work=["吃了手抓饼"],
        problems=[],
        tomorrow_plan=[],
        section_status={},
    )

    branch_plan = _resolve_direct_agent_plan("蛋没给我加全，明天去苏州吃更好吃的", report)

    assert branch_plan is not None
    branch, plan = branch_plan
    assert branch == "direct_tomorrow_plan_phrase"
    actions = {action.field: action for action in plan.actions}
    assert actions["problems"].items == ["蛋没给我加全"]
    assert actions["tomorrow_plan"].items == ["明天去苏州吃更好吃的"]


def test_habit_no_problem_phrase_with_future_plan_fills_two_sections():
    report = SimpleNamespace(
        today_work=["审核3份合同"],
        problems=[],
        tomorrow_plan=[],
        section_status={},
    )

    branch_plan = _resolve_direct_agent_plan("没问题，明天继续跟进合同审批", report)

    assert branch_plan is not None
    branch, plan = branch_plan
    assert branch == "direct_tomorrow_plan_phrase"
    actions = {action.field: action for action in plan.actions}
    assert actions["problems"].items == ["暂无明显问题"]
    assert actions["tomorrow_plan"].items == ["明天继续跟进合同审批"]


def test_direct_single_action_effects_plan_splits_numbered_effects():
    plan = _direct_single_action_effects_plan(
        "今天优化了AI发送逻辑：1. 填报日期更清楚了 2. 9：00 规则收紧了 3. 编辑能力稳定了一轮",
        None,
    )

    assert plan is not None
    assert plan.actions[0].field == "today_work"
    assert plan.actions[0].items == [
        "填报日期更清楚了",
        "9：00 规则收紧了",
        "编辑能力稳定了一轮",
    ]


def test_direct_clear_all_matches_high_risk_phrases():
    report = SimpleNamespace(
        today_work=["审核合同"],
        problems=["暂无明显问题"],
        tomorrow_plan=["继续推进"],
        section_status={},
    )

    branch_plan = _resolve_direct_agent_plan("清空全部日报", report)

    assert branch_plan is not None
    branch, plan = branch_plan
    assert branch == "direct_clear_current_report"
    assert plan.actions[0].type == "clear_all"
    assert plan.actions[0].requires_confirmation is False
    assert _direct_clear_current_report_plan("重置日报", report) is not None


def test_last_unwritten_candidate_can_be_placed_into_problem_section():
    report = SimpleNamespace(
        today_work=["跟进拟诉评估"],
        problems=[],
        tomorrow_plan=[],
        section_status={
            "_last_unwritten_candidate": {
                "text": "拟诉评估技能体量大，效率低，寻找提效的办法",
            }
        },
    )

    plan = _direct_last_unwritten_candidate_plan("这个是问题", report)

    assert plan is not None
    assert plan.actions[0].field == "problems"
    assert plan.actions[0].items == ["拟诉评估技能体量大，效率低，寻找提效的办法"]
    assert plan.actions[0].source == "last_unwritten_candidate"


def test_this_is_problem_without_candidate_is_not_written_literally():
    report = SimpleNamespace(
        today_work=["跟进拟诉评估"],
        problems=["拟诉评估技能体量大，效率低，寻找提效的办法"],
        tomorrow_plan=[],
        section_status={},
    )

    plan = _direct_this_is_problem_without_candidate_plan("这个是问题", report)

    assert plan is not None
    assert plan.should_write is False
    assert not plan.actions
    assert "不重复记录" in plan.reply_to_user


def test_append_operation_words_are_removed_before_storing_today_work():
    today_work, _problems, _tomorrow_plan, _empty_ack = _normalize_report_fields_after_actions(
        ["另外补一个，整理会议纪要"],
        [],
        [],
        {},
    )

    assert today_work == ["整理会议纪要"]


def test_colloquial_no_problem_and_misfiled_plan_are_normalized():
    today_work, problems, tomorrow_plan, _empty_ack = _normalize_report_fields_after_actions(
        ["呃就是吧，今天审核了两份合同，没遇到什么风险"],
        ["明儿继续跟业务确认"],
        [],
        {},
    )

    assert today_work == ["今天审核了两份合同"]
    assert problems == ["暂无明显问题"]
    assert tomorrow_plan == ["明儿继续跟业务确认"]


def test_structured_multi_field_input_routes_before_single_field_rules():
    raw_input = "\u660e\u65e5\u8ba1\u5212\uff1a\u7ee7\u7eed\u5f00\u53d1\n\u98ce\u9669\uff1a\u65e0"

    branch_plan = _resolve_direct_agent_plan(raw_input, None)

    assert branch_plan is not None
    branch, plan = branch_plan
    assert branch == "direct_structured_multi_field_report"
    assert plan.should_write is True
    assert plan.clear_pending_interaction is True
    actions = {action.field: action for action in plan.actions}
    assert "tomorrow_plan" in actions
    assert "problems" in actions
    assert actions["tomorrow_plan"].items == ["\u7ee7\u7eed\u5f00\u53d1"]
    assert actions["problems"].items == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]
    assert "today_work" not in actions


def test_structured_three_field_input_keeps_all_sections():
    raw_input = (
        "\u4eca\u65e5\u5b8c\u6210\uff1aA\n"
        "\u660e\u65e5\u8ba1\u5212\uff1aB\n"
        "\u98ce\u9669\uff1a\u65e0"
    )

    branch_plan = _resolve_direct_agent_plan(raw_input, None)

    assert branch_plan is not None
    branch, plan = branch_plan
    assert branch == "direct_structured_multi_field_report"
    actions = {action.field: action for action in plan.actions}
    assert actions["today_work"].items == ["A"]
    assert actions["tomorrow_plan"].items == ["B"]
    assert actions["problems"].items == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]


def test_probable_noise_inputs_are_blocked_before_report_write():
    assert _is_probable_noise_input("%PDF-1.4 % 1 0 obj << /Title (Sample) >> endobj") is True
    assert _is_probable_noise_input("aGVsbG8gd29ybGQgZnJvbSBiYXNlNjQ=") is True
    assert _is_probable_noise_input("asdfjkl; qwertyuiop zxcvbnm") is True
    assert _is_probable_noise_input("' OR 1=1 --") is True
    assert _is_probable_noise_input("\u4eca\u5929\u5b8c\u6210\u63a5\u53e3\u8054\u8c03\uff0c\u660e\u5929\u7ee7\u7eed\u6d4b\u8bd5\uff0c\u98ce\u9669\u65e0") is False


def test_compound_confirmation_appends_inline_content_to_pending_target():
    resolution = resolve_pending_interaction(
        "\u5bf9\uff0c\u8fd8\u5f00\u4e86\u4e2a\u8bc4\u5ba1\u4f1a",
        {
            "type": "awaiting_append_target_confirmation",
            "operation": "append",
            "target_field": "today_work",
            "context": {},
        },
    )

    assert resolution is not None
    assert resolution.branch == "confirm_append_target_inline_content"
    assert resolution.plan.should_write is True
    assert resolution.plan.clear_pending_interaction is True
    assert resolution.plan.actions[0].field == "today_work"
    assert resolution.plan.actions[0].items == ["\u8fd8\u5f00\u4e86\u4e2a\u8bc4\u5ba1\u4f1a"]


def test_pending_delete_confirmation_accepts_delete_it_short_reply():
    resolution = resolve_pending_interaction(
        "\u5220\u6389",
        {
            "type": "awaiting_action_confirmation",
            "operation": "delete_report_item",
            "target_field": "problems",
            "context": {"item_indices": [1]},
        },
    )

    assert resolution is not None
    assert resolution.branch == "confirm_delete_report_item"
    assert resolution.plan.should_write is True
    assert resolution.plan.clear_pending_interaction is True
    assert resolution.plan.actions[0].type == "delete_item"
    assert resolution.plan.actions[0].field == "problems"
    assert resolution.plan.actions[0].item_indices == [1]


def test_long_text_fallback_extracts_tomorrow_from_mingtian_zhunbei():
    fields = _extract_simple_report_fields(
        "\u4eca\u5929\u5b8c\u6210\u4e86\u767b\u5f55\u6a21\u5757\u8054\u8c03\uff0c\u63a5\u53e3\u5df2\u7a33\u5b9a\uff1b"
        "\u8fd8\u4fee\u590d\u4e86\u4e24\u4e2a\u5386\u53f2 bug\u3002"
        "\u660e\u5929\u51c6\u5907\u5f00\u59cb\u6743\u9650\u7ba1\u7406\u529f\u80fd\u7684\u8bbe\u8ba1\u6587\u6863\u7f16\u5199\u3002"
        "\u76ee\u524d\u6ca1\u6709\u9047\u5230\u4ec0\u4e48\u95ee\u9898\u3002"
    )

    assert fields["today_work"]
    assert any("\u767b\u5f55\u6a21\u5757\u8054\u8c03" in item for item in fields["today_work"])
    assert any("\u6743\u9650\u7ba1\u7406\u529f\u80fd" in item for item in fields["tomorrow_plan"])
    assert fields["problems"] == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]


def test_long_text_fallback_extracts_minger_dasuan_and_risk_detail():
    fields = _extract_simple_report_fields(
        "\u4eca\u5929\u5dee\u4e0d\u591a\u628a\u767b\u5f55\u8fd9\u5757\u5f04\u597d\u4e86\uff0c\u80fd\u6b63\u5e38\u8df3\u8f6c\u4e86\u3002"
        "\u660e\u513f\u6253\u7b97\u641e\u4e0b\u627e\u56de\u5bc6\u7801\u7684\u529f\u80fd\u3002"
        "\u6ca1\u5565\u5927\u95ee\u9898\uff0c\u5c31\u662f\u9a8c\u8bc1\u7801\u6709\u65f6\u5019\u6536\u5f97\u6162\u3002"
    )

    assert any("\u767b\u5f55" in item and "\u8df3\u8f6c" in item for item in fields["today_work"])
    assert any("\u627e\u56de\u5bc6\u7801" in item for item in fields["tomorrow_plan"])
    assert any("\u9a8c\u8bc1\u7801" in item and "\u6162" in item for item in fields["problems"])


def test_long_text_fallback_extracts_mingri_jihua_shi():
    fields = _extract_simple_report_fields(
        "\u5f00\u59cb\n"
        "\u4eca\u5929\u5b8c\u6210\u4e86 SRM \u6a21\u5757\u7684\u9700\u6c42\u68b3\u7406\uff0c\u5171\u68b3\u7406\u51fa 12 \u4e2a\u5173\u952e\u6d41\u7a0b\u70b9\u3002\n"
        "\u660e\u65e5\u8ba1\u5212\u662f\u7ed8\u5236\u4e1a\u52a1\u6d41\u7a0b\u56fe\u3002\n"
        "\u76ee\u524d\u65e0\u660e\u663e\u98ce\u9669\u3002\n"
        "\u7ed3\u675f"
    )

    assert any("SRM" in item and "12" in item for item in fields["today_work"])
    assert fields["tomorrow_plan"] == ["\u7ed8\u5236\u4e1a\u52a1\u6d41\u7a0b\u56fe"]
    assert fields["problems"] == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]


def test_long_text_fallback_extracts_mingri_jiang_and_checkbox_today():
    fields = _extract_simple_report_fields(
        "\u25cb [x] \u5b8c\u6210\u65e5\u62a5\u6a21\u677f\u4f18\u5316\u5efa\u8bae\n"
        "\u25cb [ ] \u5f85\u8865\u5145\u8bf4\u660e\n"
        "\u660e\u65e5\u5c06\u63d0\u4ea4\u7ed9\u4ea7\u54c1\u7ecf\u7406\u8bc4\u5ba1\n"
        "\u6682\u65e0\u98ce\u9669\u63d0\u793a"
    )

    assert fields["today_work"] == ["\u5b8c\u6210\u65e5\u62a5\u6a21\u677f\u4f18\u5316\u5efa\u8bae"]
    assert fields["tomorrow_plan"] == ["\u63d0\u4ea4\u7ed9\u4ea7\u54c1\u7ecf\u7406\u8bc4\u5ba1"]
    assert fields["problems"] == ["\u6682\u65e0\u660e\u663e\u95ee\u9898"]


def test_long_text_fallback_extracts_jiexialai_plan_and_timeout_risk():
    fields = _extract_simple_report_fields(
        "\u4eca\u5929\u5b8c\u6210\u8ba2\u5355\u6d41\u7a0b\u68b3\u7406\uff0c\u5e76\u548c\u6d4b\u8bd5\u540c\u4e8b\u5bf9\u9f50\u7528\u4f8b\u3002"
        "\u63a5\u4e0b\u6765\u8981\u5bf9\u63a5\u7b2c\u4e09\u65b9\u652f\u4ed8\u63a5\u53e3\uff0c"
        "\u6570\u636e\u5e93\u8fde\u63a5\u5076\u5c14\u8d85\u65f6\u3002"
    )

    assert any("\u8ba2\u5355\u6d41\u7a0b" in item for item in fields["today_work"])
    assert fields["tomorrow_plan"] == ["\u5bf9\u63a5\u7b2c\u4e09\u65b9\u652f\u4ed8\u63a5\u53e3"]
    assert any("\u6570\u636e\u5e93\u8fde\u63a5" in item and "\u8d85\u65f6" in item for item in fields["problems"])


def test_anchor_fallback_does_not_overwrite_existing_tomorrow_plan():
    fields = _extract_simple_report_fields(
        "\u4eca\u5929\u5b8c\u6210\u4e86\u5408\u540c\u5ba1\u6838\u3002\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u5ba1\u6279\u3002\u660e\u65e5\u8ba1\u5212\u662f\u4e0d\u5e94\u8986\u76d6\u3002"
    )

    assert fields["tomorrow_plan"] == ["\u660e\u5929\u7ee7\u7eed\u8ddf\u8fdb\u5ba1\u6279"]


def test_anchor_fallback_does_not_invent_tomorrow_plan_without_anchor():
    fields = _extract_simple_report_fields(
        "\u4eca\u5929\u5b8c\u6210\u4e86\u767b\u5f55\u6a21\u5757\u8054\u8c03\uff0c\u4fee\u590d\u4e86\u4e24\u4e2a bug\uff0c\u6682\u65e0\u98ce\u9669\u3002"
    )

    assert fields["today_work"]
    assert fields["tomorrow_plan"] == []
