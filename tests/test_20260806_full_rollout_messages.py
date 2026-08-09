from app.services.full_rollout_messages import (
    employee_welcome_message,
    management_query_notice,
)
from app.services.dingtalk import validate_dingtalk_outbound_text


def test_employee_welcome_introduces_daily_report_query_and_memory() -> None:
    message = employee_welcome_message()

    assert "确认后提交" in message
    assert "查询个人、团队和部门" in message
    assert "以后怎么称呼你" in message
    assert "个人记忆" in message
    assert "修改或忘记" in message


def test_management_notice_explains_queries_unclosed_rule_and_briefing() -> None:
    message = management_query_notice()

    assert "查询功能面向全员开放" in message
    assert "同一工作跨日重复计划合并为一项" in message
    assert "已有跟进" in message
    assert "暂无法判断" in message
    assert "每天 9:00" in message
    assert "法务合约中心七个部门" in message


def test_rollout_notices_pass_outbound_encoding_guard() -> None:
    employee_preview = "【全员首句预览】\n\n" + employee_welcome_message()
    management_preview = "【管理团队通知预览】\n\n" + management_query_notice()

    assert validate_dingtalk_outbound_text(employee_preview) == employee_preview
    assert validate_dingtalk_outbound_text(management_preview) == management_preview
