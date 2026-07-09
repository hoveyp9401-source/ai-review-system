from datetime import date, datetime
import asyncio
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agent2.daily_commands import DailyCommand
from app.agent2.daily_execution import (
    _attach_agent2_edit_memory,
    _historical_report_date_from_raw_input,
    _no_change_message,
    _previous_report_for_copy,
    _read_only_message,
    _report_date_for_commands,
    _should_block_historical_daily_mutation_after_cutoff,
    _write_message,
    agent2_daily_enabled_for_user,
    agent2_daily_should_fallback_to_legacy,
    apply_commands_to_snapshot,
)
from app.agent2.daily_state import PENDING_DAILY_CANDIDATE_KEY


def test_agent2_daily_enabled_requires_flag_and_user_match():
    user = SimpleNamespace(id="user-1", dingtalk_user_id="ding-1", employee_no="0001")

    assert agent2_daily_enabled_for_user(SimpleNamespace(agent2_daily_enabled=False, agent2_daily_enabled_user_ids="*"), user) is False
    assert agent2_daily_enabled_for_user(SimpleNamespace(agent2_daily_enabled=True, agent2_daily_enabled_user_ids="ding-1"), user) is True
    assert agent2_daily_enabled_for_user(SimpleNamespace(agent2_daily_enabled=True, agent2_daily_enabled_user_ids="other"), user) is False


def test_apply_fill_commands_appends_to_target_fields_without_duplicates():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[
            DailyCommand(operation="fill", target_field="today_work", content=["合同审核", "函件起草"], should_write=True),
            DailyCommand(operation="fill", target_field="problems", content=["暂无明显问题"], should_write=True),
        ],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核", "函件起草"]
    assert result.problems == ["暂无明显问题"]
    assert result.status == "collecting"


def test_apply_begin_edit_is_read_only_and_has_edit_prompt():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["暂无"],
        tomorrow_plan=["继续推进"],
        status="collecting",
        commands=[DailyCommand(operation="begin_edit", target_date="yesterday", target_field="none")],
    )

    assert result.read_only is True
    assert result.changed is False
    assert result.actions == [{"operation": "begin_edit", "changed": False, "read_only": True}]

    message = _read_only_message(
        SimpleNamespace(today_work=["合同审核"], problems=["暂无"], tomorrow_plan=["继续推进"]),
        date(2026, 7, 6),
        result.actions,
    )
    assert "2026-07-06" in message
    assert "\u76f4\u63a5\u8bf4\u8981\u6539\u54ea\u4e00\u680f" in message
    assert "\u65e5\u5de5\u4f5c\u7b2c4\u6761\u5220\u6389" in message


def test_report_date_uses_active_report_date_for_contextual_edit_without_today_hint():
    assert _report_date_for_commands(
        date(2026, 7, 7),
        [
            DailyCommand(
                operation="edit",
                target_date="",
                active_report_date="2026-07-06",
                target_field="today_work",
                should_write=True,
            )
        ],
    ) == date(2026, 7, 6)


def test_report_date_today_hint_ignores_historical_active_report_date():
    assert _report_date_for_commands(
        date(2026, 7, 7),
        [
            DailyCommand(
                operation="fill",
                target_date="today",
                active_report_date="2026-07-06",
                target_field="today_work",
                should_write=True,
            )
        ],
    ) == date(2026, 7, 7)


def test_report_date_uses_explicit_historical_date_for_edit():
    assert _report_date_for_commands(
        date(2026, 7, 7),
        [
            DailyCommand(
                operation="edit",
                target_date="yesterday",
                active_report_date="2026-07-07",
                target_field="today_work",
                should_write=True,
            )
        ],
    ) == date(2026, 7, 6)


def test_report_date_uses_explicit_historical_date_for_begin_edit():
    assert _report_date_for_commands(
        date(2026, 7, 7),
        [
            DailyCommand(
                operation="begin_edit",
                target_date="yesterday",
                target_field="none",
                should_write=False,
            )
        ],
    ) == date(2026, 7, 6)


def test_report_date_does_not_use_historical_active_date_for_no_write():
    assert _report_date_for_commands(
        date(2026, 7, 7),
        [
            DailyCommand(
                operation="no_write",
                target_date="",
                active_report_date="2026-07-06",
                target_field="none",
                should_write=False,
            )
        ],
    ) == date(2026, 7, 7)


def test_report_date_copy_previous_keeps_current_report_as_target():
    assert _report_date_for_commands(
        date(2026, 7, 7),
        [DailyCommand(operation="copy_previous", target_date="yesterday", target_field="all", should_write=True)],
    ) == date(2026, 7, 7)


def test_apply_clear_command_empties_report():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["暂无明显问题"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[DailyCommand(operation="clear", target_field="all", should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == []
    assert result.problems == []
    assert result.tomorrow_plan == []


def test_no_change_message_explains_historical_after_cutoff_block():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["暂无明显问题"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[
            DailyCommand(
                operation="no_write",
                target_field="none",
                should_write=False,
                reason="historical daily reports are read-only after the 09:00 cutoff; use query or copy instead",
                safety_flags=["historical_daily_mutation_blocked_after_cutoff"],
            )
        ],
    )

    assert result.changed is False
    assert result.actions[0]["operation"] == "no_write"
    message = _no_change_message(result.actions)
    assert "9点后" in message
    assert "查看昨日日报" in message
    assert "复制昨天日报" in message


def test_execution_guard_blocks_historical_mutation_after_cutoff_even_if_commands_would_write():
    received_at = datetime(2026, 7, 9, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert _should_block_historical_daily_mutation_after_cutoff("昨天日报今日工作第2条删掉", received_at) is True
    assert _historical_report_date_from_raw_input(date(2026, 7, 9), "昨天日报今日工作第2条删掉") == date(2026, 7, 8)


def test_execution_guard_allows_historical_copy_and_before_cutoff_makeup():
    after_cutoff = datetime(2026, 7, 9, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    before_cutoff = datetime(2026, 7, 9, 8, 59, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert _should_block_historical_daily_mutation_after_cutoff("复制昨天日报到今天", after_cutoff) is False
    assert _should_block_historical_daily_mutation_after_cutoff("我要改昨天日报", before_cutoff) is False


def test_apply_copy_previous_replaces_current_sections():
    previous = SimpleNamespace(
        id="prev-1",
        today_work=["昨天工作"],
        problems=["昨天问题"],
        tomorrow_plan=["昨天计划"],
    )
    result = apply_commands_to_snapshot(
        today_work=["今天旧内容"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="copy_previous", target_field="all", should_write=True)],
        previous_report=previous,
    )

    assert result.changed is True
    assert result.today_work == ["昨天工作"]
    assert result.problems == ["昨天问题"]
    assert result.tomorrow_plan == ["昨天计划"]


def test_apply_copy_previous_today_work_merges_without_overwriting_current_items():
    previous = SimpleNamespace(
        id="prev-1",
        today_work=["\u5408\u540c\u5ba1\u6838", "\u51fd\u4ef6\u8d77\u8349"],
        problems=["\u8d44\u6599\u4e0d\u5168"],
        tomorrow_plan=["\u660e\u5929\u8054\u7cfb\u5357\u4eac\u6cd5\u9662"],
    )
    result = apply_commands_to_snapshot(
        today_work=[
            "\u5408\u540c\u5ba1\u6838",
            "\u51fd\u4ef6\u8d77\u8349",
            "\u5b8c\u6210\u8054\u7cfb\u5357\u4eac\u6cd5\u9662",
        ],
        problems=["\u8d44\u6599\u4e0d\u5168"],
        tomorrow_plan=["\u660e\u5929\u8054\u7cfb\u5357\u4eac\u6cd5\u9662"],
        status="collecting",
        commands=[DailyCommand(operation="copy_previous", target_field="today_work", should_write=True)],
        previous_report=previous,
    )

    assert result.changed is False
    assert result.today_work == [
        "\u5408\u540c\u5ba1\u6838",
        "\u51fd\u4ef6\u8d77\u8349",
        "\u5b8c\u6210\u8054\u7cfb\u5357\u4eac\u6cd5\u9662",
    ]
    assert result.problems == ["\u8d44\u6599\u4e0d\u5168"]
    assert result.tomorrow_plan == ["\u660e\u5929\u8054\u7cfb\u5357\u4eac\u6cd5\u9662"]
    assert result.actions[0]["changed"] is False


def test_apply_copy_previous_today_work_replaces_different_current_items():
    previous = SimpleNamespace(
        id="prev-1",
        today_work=["\u6628\u5929\u5b8c\u6210\u503a\u6743\u8d44\u6599\u5f52\u6863", "\u6628\u5929\u5b8c\u6210\u9879\u76ee\u8bc4\u5ba1"],
        problems=["\u8d44\u6599\u4e0d\u5168"],
        tomorrow_plan=["\u4eca\u5929\u63a8\u8fdb\u8d44\u6599\u8865\u5145"],
    )
    result = apply_commands_to_snapshot(
        today_work=["\u4eca\u5929\u5b8c\u6210\u5408\u540c\u5ba1\u6838", "\u4eca\u5929\u5b8c\u6210\u5370\u7ae0\u8d44\u6599\u6838\u5bf9"],
        problems=["\u8d44\u6599\u4e0d\u5168"],
        tomorrow_plan=["\u4eca\u5929\u63a8\u8fdb\u8d44\u6599\u8865\u5145"],
        status="collecting",
        commands=[DailyCommand(operation="copy_previous", target_field="today_work", should_write=True)],
        previous_report=previous,
    )

    assert result.changed is True
    assert result.today_work == ["\u6628\u5929\u5b8c\u6210\u503a\u6743\u8d44\u6599\u5f52\u6863", "\u6628\u5929\u5b8c\u6210\u9879\u76ee\u8bc4\u5ba1"]
    assert result.problems == ["\u8d44\u6599\u4e0d\u5168"]
    assert result.tomorrow_plan == ["\u4eca\u5929\u63a8\u8fdb\u8d44\u6599\u8865\u5145"]
    assert result.actions[0]["changed"] is True


def test_previous_report_for_copy_uses_command_relative_date(monkeypatch):
    calls = []

    async def fake_get_report(session, user_id, report_date):
        calls.append(report_date)
        return SimpleNamespace(id=f"report-{report_date.isoformat()}")

    monkeypatch.setitem(sys.modules, "app.repositories", SimpleNamespace(get_report=fake_get_report))

    result = asyncio.run(
        _previous_report_for_copy(
            object(),
            user=SimpleNamespace(id="user-1"),
            report_date=date(2026, 7, 6),
            commands=[DailyCommand(operation="copy_previous", target_date="day_before_yesterday", target_field="today_work")],
        )
    )

    assert result.id == "report-2026-07-04"
    assert calls == [date(2026, 7, 4)]


def test_apply_complete_previous_plan_expands_yesterday_tomorrow_plan():
    previous = SimpleNamespace(
        id="prev-1",
        today_work=["昨天今日工作不能复制"],
        problems=[],
        tomorrow_plan=["明天继续完善mcp并开始测试", "明天开始做周报agent的outbox"],
    )
    result = apply_commands_to_snapshot(
        today_work=[],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="complete_previous_plan", target_field="today_work", should_write=True)],
        previous_report=previous,
    )

    assert result.changed is True
    assert result.today_work == ["完成完善mcp并开始测试", "完成周报agent的outbox"]
    assert "昨天今日工作不能复制" not in result.today_work
    assert result.actions[0]["operation"] == "complete_previous_plan"


def test_apply_copy_current_today_work_to_tomorrow_plan_merges_items():
    result = apply_commands_to_snapshot(
        today_work=["完成合同审核", "整理恒大案证据清单"],
        problems=[],
        tomorrow_plan=["整理恒大案证据清单"],
        status="collecting",
        commands=[DailyCommand(operation="copy_current_to_tomorrow", target_field="tomorrow_plan", should_write=True)],
        previous_report=None,
    )

    assert result.changed is True
    assert result.tomorrow_plan == ["整理恒大案证据清单", "完成合同审核"]
    assert result.actions[0]["operation"] == "copy_current_to_tomorrow"
    assert result.actions[0]["changed"] is True


def test_apply_complete_previous_plan_deduplicates_existing_completion():
    previous = SimpleNamespace(
        id="prev-1",
        today_work=[],
        problems=[],
        tomorrow_plan=["跟进合同B", "整理案件C材料"],
    )
    result = apply_commands_to_snapshot(
        today_work=["完成跟进合同B"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="complete_previous_plan", target_field="today_work", should_write=True)],
        previous_report=previous,
    )

    assert result.changed is True
    assert result.today_work == ["完成跟进合同B", "完成整理案件C材料"]
    assert result.actions[0]["item_count"] == 2
    assert len(result.actions[0]["item_ids"]) == 1


def test_apply_complete_previous_plan_without_previous_report_no_change():
    result = apply_commands_to_snapshot(
        today_work=[],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="complete_previous_plan", target_field="today_work", should_write=True)],
        previous_report=None,
    )

    assert result.changed is False
    assert result.today_work == []
    assert result.actions[0]["reason"] == "previous_plan_missing"


def test_apply_revoke_only_changes_completed_report_to_collecting():
    completed = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["暂无明显问题"],
        tomorrow_plan=["继续跟进"],
        status="completed",
        commands=[DailyCommand(operation="revoke", target_field="all", should_write=True)],
    )
    collecting = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["暂无明显问题"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[DailyCommand(operation="revoke", target_field="all", should_write=True)],
    )

    assert completed.changed is True
    assert completed.status == "collecting"
    assert collecting.changed is False


def test_apply_fill_does_not_silently_modify_completed_report():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["暂无明显问题"],
        tomorrow_plan=["继续跟进"],
        status="completed",
        commands=[DailyCommand(operation="fill", target_field="today_work", content=["补充合同付款争议"], should_write=True)],
    )

    assert result.changed is False
    assert result.status == "completed"
    assert result.today_work == ["合同审核"]
    assert result.actions[0]["reason"] == "completed_report_locked"


def test_apply_edit_replaces_numbered_item():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["把第一条改成合同审核已完成"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核已完成", "函件起草"]
    assert result.actions[0]["edit_action"] == "replace_item"


def test_apply_edit_deletes_numbered_item_without_clearing_report():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草"],
        problems=["暂无明显问题"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["删除第一条"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["函件起草"]
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["继续跟进"]
    assert result.actions[0]["edit_action"] == "delete_item"


def test_apply_edit_merges_numbered_items():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草", "案件沟通"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["合并第一条和第二条"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核，函件起草", "案件沟通"]
    assert result.actions[0]["edit_action"] == "merge_items"


def test_apply_edit_moves_item_to_target_field():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "材料缺失"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["把第二条移到问题风险"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核"]
    assert result.problems == ["材料缺失"]
    assert result.actions[0]["edit_action"] == "move_item"


def test_apply_edit_replaces_unique_text():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["把函件改成律师函"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核", "律师函起草"]
    assert result.actions[0]["edit_action"] == "replace_text"


def test_apply_edit_deletes_text_item_and_appends_replacement():
    result = apply_commands_to_snapshot(
        today_work=["开会", "写代码", "做测试"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["把开会去掉，加上代码评审"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["写代码", "做测试", "代码评审"]
    assert result.actions[0]["edit_action"] == "delete_and_append_item"


def test_apply_edit_replaces_text_without_treating_new_value_as_field_hint():
    result = apply_commands_to_snapshot(
        today_work=["今天完成合同审核"],
        problems=["暂无明显问题"],
        tomorrow_plan=[],
        status="collecting",
        commands=[
            DailyCommand(
                operation="edit",
                target_field="problems",
                content=["把合同审核改成合同审核及风险条款复核"],
                should_write=True,
            )
        ],
    )

    assert result.changed is True
    assert result.today_work == ["今天完成合同审核及风险条款复核"]
    assert result.problems == ["暂无明显问题"]
    assert result.actions[0]["edit_action"] == "replace_text"
    assert result.actions[0]["target_field"] == "today_work"


def test_apply_edit_removes_parenthetical_text_from_unique_item():
    result = apply_commands_to_snapshot(
        today_work=["进行上海机载（机器的机载重的载）项目评审", "名苑项目拟诉评估"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[
            DailyCommand(
                operation="edit",
                target_field="today_work",
                content=["今日工作里面的上海鸡仔。是对的，括号里面的内容删掉"],
                should_write=True,
            )
        ],
    )

    assert result.changed is True
    assert result.today_work == ["进行上海机载项目评审", "名苑项目拟诉评估"]
    assert result.actions[0]["edit_action"] == "delete_parenthetical_text"


def test_apply_edit_replaces_spoken_correction_in_unique_tomorrow_plan_item():
    result = apply_commands_to_snapshot(
        today_work=[],
        problems=[],
        tomorrow_plan=["常州豫园项目起诉材料的完成及汇报", "晋城酒店飞速收款支撑"],
        status="collecting",
        commands=[
            DailyCommand(
                operation="edit",
                target_field="tomorrow_plan",
                content=["明日计划里面的飞速收款飞速写错了，是非的非诉讼的诉"],
                should_write=True,
            )
        ],
    )

    assert result.changed is True
    assert result.tomorrow_plan == ["常州豫园项目起诉材料的完成及汇报", "晋城酒店非诉收款支撑"]
    assert result.actions[0]["edit_action"] == "replace_spoken_correction"
    assert result.actions[0]["old_value"] == "飞速"
    assert result.actions[0]["new_value"] == "非诉"


def test_apply_edit_negative_replacement_updates_existing_tomorrow_plan_without_duplicate_suffix():
    result = apply_commands_to_snapshot(
        today_work=["整理庭审材料"],
        problems=["暂无明显问题"],
        tomorrow_plan=["明天去南京开庭"],
        status="pending_confirmation",
        commands=[
            DailyCommand(
                operation="edit",
                target_field="tomorrow_plan",
                content=["明天不是去南京，是去上海开庭"],
                should_write=True,
            )
        ],
    )

    assert result.changed is True
    assert result.tomorrow_plan == ["明天去上海开庭"]
    assert result.status == "pending_confirmation"
    assert result.actions[0]["edit_action"] == "replace_negative_correction"


def test_apply_clear_field_only_clears_target_section():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["暂无明显问题"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[DailyCommand(operation="clear", target_field="today_work", should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == []
    assert result.problems == ["暂无明显问题"]
    assert result.tomorrow_plan == ["继续跟进"]


def test_apply_unresolved_structural_edit_does_not_append_raw_text():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["9.10.合并"], should_write=True)],
    )

    assert result.changed is False
    assert result.today_work == ["合同审核", "函件起草"]
    assert result.actions[0]["reason"] in {"unsupported_edit", "merge_indices_unresolved"}


def test_agent2_unresolved_edit_does_not_fallback_to_legacy():
    actions = [{"operation": "edit", "reason": "delete_indices_unresolved", "changed": False}]

    assert agent2_daily_should_fallback_to_legacy(actions) is False


def test_agent2_unsupported_edit_does_not_fallback_to_legacy():
    actions = [{"operation": "edit", "reason": "unsupported_edit", "changed": False}]

    assert agent2_daily_should_fallback_to_legacy(actions) is False


def test_no_change_message_explains_unresolved_item_index():
    message = _no_change_message([{"operation": "edit", "reason": "replace_indices_unresolved"}])

    assert "没有找到对应编号" in message


def test_no_change_message_explains_unresolved_text_replacement():
    message = _no_change_message([{"operation": "edit", "reason": "replace_text_unresolved"}])

    assert "没有在当前日报草稿里找到要替换的内容" in message


def test_no_change_message_explains_empty_previous_today_work_copy():
    message = _no_change_message(
        [{"operation": "copy_previous", "target_field": "today_work", "changed": False, "item_ids": []}]
    )

    assert "昨天日报的【今日工作】是空的" in message
    assert "昨天的计划都完成了" in message


def test_write_message_explains_clear_operation_instead_of_recorded_work():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["暂无明显问题"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[DailyCommand(operation="clear", target_field="all", should_write=True)],
    )

    message = _write_message(result, date(2026, 7, 5))

    assert message.startswith("已清空 2026-07-05 的日报草稿")
    assert not message.startswith("已记录到")


def test_apply_delete_out_of_range_stays_in_agent2_no_change():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["删除第5条"], should_write=True)],
    )

    assert result.changed is False
    assert result.today_work == ["合同审核", "函件起草"]
    assert result.actions[0]["reason"] == "delete_indices_unresolved"
    assert agent2_daily_should_fallback_to_legacy(result.actions) is False


def test_apply_edit_merges_loose_delimited_local_indices():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草", "案件沟通", "资料整理", "会议纪要"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["合并今日工作的 2，3，4，5"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核", "函件起草，案件沟通，资料整理，会议纪要"]
    assert result.actions[0]["edit_action"] == "merge_items"
    assert result.actions[0]["item_indices"] == [2, 3, 4, 5]


def test_apply_edit_uses_global_index_when_field_is_not_explicit():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草", "案件沟通"],
        problems=["材料缺失", "流程卡点"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="all", content=["第5条改成流程卡点已协调"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核", "函件起草", "案件沟通"]
    assert result.problems == ["材料缺失", "流程卡点已协调"]
    assert result.actions[0]["target_field"] == "problems"
    assert result.actions[0]["item_indices"] == [2]


def test_apply_edit_explicit_field_index_overrides_global_index():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草", "案件沟通"],
        problems=["材料缺失", "流程卡点"],
        tomorrow_plan=["继续跟进", "准备会议"],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="all", content=["明日计划第1条改成安排南京出差"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核", "函件起草", "案件沟通"]
    assert result.tomorrow_plan == ["安排南京出差", "准备会议"]
    assert result.actions[0]["target_field"] == "tomorrow_plan"
    assert result.actions[0]["item_indices"] == [1]


def test_apply_edit_deletes_loose_tomorrow_plan_indices():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["暂无明显问题"],
        tomorrow_plan=["继续跟进", "准备会议", "整理资料"],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="all", content=["明日计划2、3去掉"], should_write=True)],
    )

    assert result.changed is True
    assert result.tomorrow_plan == ["继续跟进"]
    assert result.actions[0]["edit_action"] == "delete_item"
    assert result.actions[0]["target_field"] == "tomorrow_plan"
    assert result.actions[0]["item_indices"] == [2, 3]


def test_apply_edit_deletes_single_loose_dot_global_index():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草", "案件沟通"],
        problems=["材料缺失", "流程卡点"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="all", content=["把5.去掉"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核", "函件起草", "案件沟通"]
    assert result.problems == ["材料缺失"]
    assert result.actions[0]["edit_action"] == "delete_item"
    assert result.actions[0]["target_field"] == "problems"
    assert result.actions[0]["item_indices"] == [2]


def test_apply_edit_deletes_global_range_inside_one_field():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草", "案件沟通"],
        problems=["材料缺失", "流程卡点", "系统延迟", "资料待补"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="all", content=["删除6~7条"], should_write=True)],
    )

    assert result.changed is True
    assert result.problems == ["材料缺失", "流程卡点"]
    assert result.actions[0]["target_field"] == "problems"
    assert result.actions[0]["item_indices"] == [3, 4]


def test_apply_edit_merges_adjacent_digit_indices():
    result = apply_commands_to_snapshot(
        today_work=["一", "二", "三", "四", "五", "六", "七"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="all", content=["567合并"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["一", "二", "三", "四", "五，六，七"]
    assert result.actions[0]["item_indices"] == [5, 6, 7]


def test_apply_edit_merges_adjacent_chinese_indices_in_explicit_field():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=[],
        tomorrow_plan=["继续跟进", "准备会议", "整理资料"],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="all", content=["将明日计划的一二合并"], should_write=True)],
    )

    assert result.changed is True
    assert result.tomorrow_plan == ["继续跟进，准备会议", "整理资料"]
    assert result.actions[0]["target_field"] == "tomorrow_plan"
    assert result.actions[0]["item_indices"] == [1, 2]


def test_apply_edit_clears_problem_field_from_plain_field_clear():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["材料缺失", "流程卡点"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="all", content=["把问题去掉"], should_write=True)],
    )

    assert result.changed is True
    assert result.problems == []
    assert result.today_work == ["合同审核"]
    assert result.tomorrow_plan == ["继续跟进"]
    assert result.actions[0]["edit_action"] == "clear_field"
    assert result.actions[0]["target_field"] == "problems"


def test_apply_edit_replaces_problem_field_from_plain_field_replacement():
    result = apply_commands_to_snapshot(
        today_work=["合同审核"],
        problems=["材料缺失", "流程卡点"],
        tomorrow_plan=["继续跟进"],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="all", content=["问题/风险改为资料待补齐"], should_write=True)],
    )

    assert result.changed is True
    assert result.problems == ["资料待补齐"]
    assert result.actions[0]["edit_action"] == "replace_field"
    assert result.actions[0]["target_field"] == "problems"


def test_apply_edit_compound_delete_and_merge_stays_unresolved():
    result = apply_commands_to_snapshot(
        today_work=["一", "二", "三", "四", "五", "六", "七", "八"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="all", content=["第六条删掉七八两条并成一条"], should_write=True)],
    )

    assert result.changed is False
    assert result.today_work == ["一", "二", "三", "四", "五", "六", "七", "八"]
    assert result.actions[0]["reason"] == "compound_edit_unresolved"


def test_apply_edit_preserves_item_id_when_replacing_numbered_item():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        section_status={"_draft_item_ids": {"today_work": ["tw-1", "tw-2"], "problems": [], "tomorrow_plan": []}},
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["把第2条改成律师函起草完成"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核", "律师函起草完成"]
    assert result.item_ids["today_work"] == ["tw-1", "tw-2"]
    assert result.actions[0]["item_ids"] == ["tw-2"]
    assert result.actions[0]["replaced_item_ids"] == ["tw-2"]


def test_apply_edit_merge_preserves_first_item_id_and_records_merged_ids():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草", "案件沟通"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        section_status={"_draft_item_ids": {"today_work": ["tw-1", "tw-2", "tw-3"], "problems": [], "tomorrow_plan": []}},
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["合并第1条和第2条"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核，函件起草", "案件沟通"]
    assert result.item_ids["today_work"] == ["tw-1", "tw-3"]
    assert result.actions[0]["item_ids"] == ["tw-1"]
    assert result.actions[0]["merged_item_ids"] == ["tw-1", "tw-2"]


def test_apply_edit_recent_reference_uses_saved_last_modified_item():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        section_status={
            "_draft_item_ids": {"today_work": ["tw-1", "tw-2"], "problems": [], "tomorrow_plan": []},
            "_agent2_last_modified_item": {"field": "today_work", "item_index": 1, "item_id": "tw-1"},
        },
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["把刚才那条改成合同审核复核完成"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核复核完成", "函件起草"]
    assert result.item_ids["today_work"] == ["tw-1", "tw-2"]
    assert result.actions[0]["item_indices"] == [1]
    assert result.actions[0]["item_ids"] == ["tw-1"]


def test_apply_edit_recent_reference_move_uses_last_modified_as_source_not_destination():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草"],
        problems=["材料缺失"],
        tomorrow_plan=[],
        status="collecting",
        section_status={
            "_draft_item_ids": {"today_work": ["tw-1", "tw-2"], "problems": ["pb-1"], "tomorrow_plan": []},
            "_agent2_last_modified_item": {"field": "today_work", "item_index": 2, "item_id": "tw-2"},
        },
        commands=[DailyCommand(operation="edit", target_field="problems", content=["把刚才那条移到问题风险"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核"]
    assert result.problems == ["材料缺失", "函件起草"]
    assert result.item_ids["today_work"] == ["tw-1"]
    assert result.item_ids["problems"] == ["pb-1", "tw-2"]
    assert result.actions[0]["source_field"] == "today_work"
    assert result.actions[0]["target_field"] == "problems"


def test_apply_edit_recent_reference_without_saved_memory_targets_last_item_in_field():
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["删除刚才那条"], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核"]
    assert result.actions[0]["edit_action"] == "delete_item"
    assert result.actions[0]["item_indices"] == [2]


def _pending_candidate_section_status() -> dict:
    return {
        "_draft_item_ids": {"today_work": ["tw-1", "tw-2", "tw-3"], "problems": [], "tomorrow_plan": []},
        PENDING_DAILY_CANDIDATE_KEY: {
            "type": "daily_candidate_focus",
            "field": "today_work",
            "field_label": "今日工作",
            "field_index": 2,
            "global_index": 2,
            "item_id": "tw-2",
            "text": "进行上海机载（机器的机载重的载）项目评审",
            "source_text": "上海鸡仔",
        },
    }


@pytest.mark.parametrize(
    "utterance",
    [
        "括号删掉",
        "把括号里的内容删掉",
        "括号里面那些字去掉",
        "这条括号删了",
        "就这个括号去掉",
        "对，括号删掉",
        "括号内容删除",
        "把括号内的解释去除",
    ],
)
def test_pending_daily_candidate_handles_local_parenthetical_delete_variants(utterance):
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "进行上海机载（机器的机载重的载）项目评审", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        section_status=_pending_candidate_section_status(),
        commands=[DailyCommand(operation="edit", target_field="today_work", content=[utterance], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核", "进行上海机载项目评审", "函件起草"]
    assert result.item_ids["today_work"] == ["tw-1", "tw-2", "tw-3"]
    assert result.actions[0]["target_field"] == "today_work"
    assert result.actions[0]["item_indices"] == [2]
    assert result.actions[0]["item_ids"] == ["tw-2"]


@pytest.mark.parametrize(
    "utterance",
    [
        "改成进行上海机载项目评审",
        "改为进行上海机载项目评审",
        "这条改成进行上海机载项目评审",
        "就这个改为进行上海机载项目评审",
        "把这条换成进行上海机载项目评审",
        "对，改成进行上海机载项目评审",
        "就是这个替换为进行上海机载项目评审",
        "它更新为进行上海机载项目评审",
    ],
)
def test_pending_daily_candidate_handles_full_replacement_variants(utterance):
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "进行上海机载（机器的机载重的载）项目评审", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        section_status=_pending_candidate_section_status(),
        commands=[DailyCommand(operation="edit", target_field="today_work", content=[utterance], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核", "进行上海机载项目评审", "函件起草"]
    assert result.actions[0]["edit_action"] == "replace_item"
    assert result.actions[0]["source"] == "pending_daily_candidate"
    assert result.actions[0]["item_ids"] == ["tw-2"]


@pytest.mark.parametrize(
    "utterance",
    [
        "把机载改成鸡仔",
        "把里面的机载改成鸡仔",
        "把里的机载改成鸡仔",
        "把这条里的机载改成鸡仔",
        "把这条里面的机载改成鸡仔",
        "把它里面的机载改成鸡仔",
    ],
)
def test_pending_daily_candidate_handles_inner_text_replacement_variants(utterance):
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "进行上海机载（机器的机载重的载）项目评审", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        section_status=_pending_candidate_section_status(),
        commands=[DailyCommand(operation="edit", target_field="today_work", content=[utterance], should_write=True)],
    )

    assert result.changed is True
    assert result.today_work == ["合同审核", "进行上海鸡仔（机器的机载重的载）项目评审", "函件起草"]
    assert result.actions[0]["edit_action"] == "replace_text"
    assert result.actions[0]["source"] == "pending_daily_candidate"
    assert result.actions[0]["old_value"] == "机载"
    assert result.actions[0]["new_value"] == "鸡仔"


@pytest.mark.parametrize("utterance", ["对", "对的", "嗯", "好的", "确定", "就这个", "就是这个", "没错", "收到"])
def test_pending_daily_candidate_pure_focus_confirmation_does_not_change_draft(utterance):
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "进行上海机载（机器的机载重的载）项目评审", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        section_status=_pending_candidate_section_status(),
        commands=[DailyCommand(operation="edit", target_field="today_work", content=[utterance], should_write=True)],
    )

    assert result.changed is False
    assert result.today_work == ["合同审核", "进行上海机载（机器的机载重的载）项目评审", "函件起草"]


def test_pending_daily_candidate_stale_payload_does_not_edit_wrong_item():
    section_status = _pending_candidate_section_status()
    section_status[PENDING_DAILY_CANDIDATE_KEY]["item_id"] = "missing-id"

    result = apply_commands_to_snapshot(
        today_work=["合同审核", "其他事项", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        section_status=section_status,
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["改成进行上海机载项目评审"], should_write=True)],
    )

    assert result.changed is False
    assert result.today_work == ["合同审核", "其他事项", "函件起草"]


def test_agent2_edit_memory_clears_pending_candidate_after_successful_change():
    section_status = _pending_candidate_section_status()
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "进行上海机载（机器的机载重的载）项目评审", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        section_status=section_status,
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["括号删掉"], should_write=True)],
    )

    _attach_agent2_edit_memory(section_status, result)

    assert PENDING_DAILY_CANDIDATE_KEY not in section_status
    assert section_status["_agent2_last_modified_item"]["item_id"] == "tw-2"


def test_agent2_edit_memory_clears_last_modified_after_delete():
    section_status = {
        "_agent2_last_modified_item": {"field": "today_work", "item_index": 1, "item_id": "tw-1"},
        "_last_modified_item": {"field": "today_work", "item_index": 1, "item_id": "tw-1"},
        "_correction_target": {"field": "today_work", "item_index": 1, "item_id": "tw-1"},
    }
    result = apply_commands_to_snapshot(
        today_work=["合同审核", "函件起草"],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        section_status={"_draft_item_ids": {"today_work": ["tw-1", "tw-2"], "problems": [], "tomorrow_plan": []}},
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["删除第1条"], should_write=True)],
    )

    _attach_agent2_edit_memory(section_status, result)

    assert "_agent2_last_modified_item" not in section_status
    assert "_last_modified_item" not in section_status
    assert "_correction_target" not in section_status
    assert section_status["_agent2_last_deleted_item"]["item_ids"] == ["tw-1"]


def test_tomorrow_trip_plan_replaces_generic_same_destination_with_specific_item():
    generic = "\u660e\u5929\u51fa\u5dee\u4e09\u4e9a"
    specific = "\u660e\u5929\u51fa\u5dee\u4e09\u4e9a\u529e\u7406\u6d77\u82b1\u5c9b\u6848\u4ef6\u5f00\u5ead"

    result = apply_commands_to_snapshot(
        today_work=[],
        problems=[],
        tomorrow_plan=[generic],
        status="collecting",
        section_status={"_draft_item_ids": {"today_work": [], "problems": [], "tomorrow_plan": ["tp-1"]}},
        commands=[DailyCommand(operation="fill", target_field="tomorrow_plan", content=[specific], should_write=True)],
    )

    assert result.changed is True
    assert result.tomorrow_plan == [specific]
    assert result.item_ids["tomorrow_plan"] == ["tp-1"]
    assert result.actions[0]["changed"] is True


def test_tomorrow_trip_plan_ignores_generic_same_destination_after_specific_item():
    generic = "\u660e\u5929\u51fa\u5dee\u4e09\u4e9a"
    specific = "\u660e\u5929\u51fa\u5dee\u4e09\u4e9a\u529e\u7406\u6d77\u82b1\u5c9b\u6848\u4ef6\u5f00\u5ead"

    result = apply_commands_to_snapshot(
        today_work=[],
        problems=[],
        tomorrow_plan=[specific],
        status="collecting",
        section_status={"_draft_item_ids": {"today_work": [], "problems": [], "tomorrow_plan": ["tp-1"]}},
        commands=[DailyCommand(operation="fill", target_field="tomorrow_plan", content=[generic], should_write=True)],
    )

    assert result.changed is False
    assert result.tomorrow_plan == [specific]
    assert result.actions[0]["changed"] is False


def test_tomorrow_trip_plan_deduplicates_same_destination_within_one_fill_command():
    generic = "\u660e\u5929\u51fa\u5dee\u4e09\u4e9a"
    specific = "\u660e\u5929\u51fa\u5dee\u4e09\u4e9a\u6c9f\u901a\u6d77\u82b1\u5c9b\u6848\u4ef6"

    result = apply_commands_to_snapshot(
        today_work=[],
        problems=[],
        tomorrow_plan=[],
        status="collecting",
        commands=[DailyCommand(operation="fill", target_field="tomorrow_plan", content=[generic, specific], should_write=True)],
    )

    assert result.changed is True
    assert result.tomorrow_plan == [specific]
    assert len(result.item_ids["tomorrow_plan"]) == 1


def test_tomorrow_trip_plan_keeps_different_destinations():
    sanya = "\u660e\u5929\u51fa\u5dee\u4e09\u4e9a"
    nanjing = "\u660e\u5929\u51fa\u5dee\u5357\u4eac\u76d6\u7ae0"

    result = apply_commands_to_snapshot(
        today_work=[],
        problems=[],
        tomorrow_plan=[sanya],
        status="collecting",
        commands=[DailyCommand(operation="fill", target_field="tomorrow_plan", content=[nanjing], should_write=True)],
    )

    assert result.changed is True
    assert result.tomorrow_plan == [sanya, nanjing]


def test_apply_short_delete_reply_deletes_last_modified_item_only():
    result = apply_commands_to_snapshot(
        today_work=["\u5468\u4e8c\u51fa\u5dee\u4e09\u4e9a\u5904\u7406\u6d77\u82b1\u5c9b\u6848\u4ef6", "\u597d\u65e0\u804a"],
        problems=["\u6682\u65e0"],
        tomorrow_plan=["\u7ee7\u7eed\u8ddf\u8fdb\u6848\u4ef6"],
        status="collecting",
        commands=[DailyCommand(operation="edit", target_field="today_work", content=["\u5220\u6389\u5427"], should_write=True)],
        section_status={
            "_agent2_last_modified_item": {
                "field": "today_work",
                "item_index": 2,
                "text": "\u597d\u65e0\u804a",
            }
        },
    )

    assert result.changed is True
    assert result.today_work == ["\u5468\u4e8c\u51fa\u5dee\u4e09\u4e9a\u5904\u7406\u6d77\u82b1\u5c9b\u6848\u4ef6"]
    assert result.problems == ["\u6682\u65e0"]
    assert result.tomorrow_plan == ["\u7ee7\u7eed\u8ddf\u8fdb\u6848\u4ef6"]
    assert result.actions[0]["edit_action"] == "delete_item"
