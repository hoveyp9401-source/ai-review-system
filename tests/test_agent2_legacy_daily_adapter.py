from app.agent2.daily_commands import DailyCommand
from app.agent2.legacy_daily_adapter import LegacyDailyAdapter


def _adapter_result(command: DailyCommand):
    return LegacyDailyAdapter().adapt_command(command)


def test_fill_command_maps_to_ready_legacy_append_action():
    result = _adapter_result(
        DailyCommand(
            operation="fill",
            target_field="today_work",
            content=["审核合同"],
            should_write=True,
            confidence="high",
        )
    )

    assert result.status == "ready"
    assert result.legacy_action == "append_daily_items"
    assert result.write_impact is True
    assert result.read_only is False
    assert result.dry_run is True


def test_edit_command_maps_to_ready_legacy_edit_action():
    result = _adapter_result(
        DailyCommand(
            operation="edit",
            target_field="problems",
            content=["暂无问题"],
            should_write=True,
            confidence="high",
        )
    )

    assert result.status == "ready"
    assert result.legacy_action == "apply_daily_edit"
    assert result.target_field == "problems"
    assert result.write_impact is True


def test_confirm_command_maps_to_ready_submit_action():
    result = _adapter_result(
        DailyCommand(
            operation="confirm",
            target_field="all",
            should_write=True,
            confidence="high",
        )
    )

    assert result.status == "ready"
    assert result.legacy_action == "confirm_submit"
    assert result.write_impact is True


def test_query_current_is_read_only():
    result = _adapter_result(
        DailyCommand(
            operation="query_current",
            target_field="none",
            should_write=False,
            confidence="high",
        )
    )

    assert result.status == "ready"
    assert result.legacy_action == "show_current_report"
    assert result.write_impact is False
    assert result.read_only is True


def test_clear_command_maps_to_ready_clear_action():
    result = _adapter_result(
        DailyCommand(
            operation="clear",
            target_field="all",
            should_write=True,
            requires_confirmation=False,
            safety_flags=["destructive_or_overwrite"],
        )
    )

    assert result.status == "ready"
    assert result.legacy_action == "clear_report"
    assert result.write_impact is True
    assert result.requires_confirmation is False


def test_copy_previous_command_maps_to_ready_copy_action():
    result = _adapter_result(
        DailyCommand(
            operation="copy_previous",
            target_field="all",
            should_write=True,
            requires_confirmation=False,
        )
    )

    assert result.status == "ready"
    assert result.legacy_action == "copy_previous_report"
    assert result.write_impact is True


def test_revoke_command_maps_to_ready_revoke_action():
    result = _adapter_result(
        DailyCommand(
            operation="revoke",
            target_field="all",
            should_write=True,
            requires_confirmation=False,
        )
    )

    assert result.status == "ready"
    assert result.legacy_action == "revoke_report"
    assert result.write_impact is True


def test_write_command_without_safe_write_is_blocked():
    result = _adapter_result(
        DailyCommand(
            operation="fill",
            target_field="today_work",
            content=["审核合同"],
            should_write=False,
        )
    )

    assert result.status == "blocked"
    assert result.write_impact is False
    assert "write_command_not_safe" in result.safety_flags


def test_unknown_command_is_unsupported():
    result = _adapter_result(DailyCommand(operation="unknown"))

    assert result.status == "unsupported"
    assert result.legacy_action == "unsupported"
    assert result.write_impact is False
