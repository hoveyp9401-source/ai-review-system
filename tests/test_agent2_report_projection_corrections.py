from app.agent2.report_projection_corrections import (
    ProjectionCorrectionCommand,
    ProjectionSnapshot,
    parse_projection_correction,
    plan_projection_correction,
)


def _projection():
    return ProjectionSnapshot(
        projection_id="projection-1", tenant_id="tenant-a", user_id="user-1",
        case_id="case-1", case_progress_id="progress-1", report_id="report-1",
        report_item_id="item-1", report_type="daily", projection_type="today_work",
        status="active", version=3,
    )


def test_remove_projection_preserves_case_fact_and_targets_exact_item():
    result = plan_projection_correction(
        ProjectionCorrectionCommand(
            command_id="command-1", tenant_id="tenant-a", user_id="user-1",
            projection_id="projection-1", expected_version=3, operation="remove",
            replacement_text="", target_section="", idempotency_key="remove-1",
        ),
        _projection(),
    )

    assert result.allowed is True
    assert result.report_item_id == "item-1"
    assert result.case_progress_id == "progress-1"
    assert result.delete_case_fact is False
    assert result.projection_status_after == "removed"


def test_correction_version_or_scope_conflict_is_zero_write():
    result = plan_projection_correction(
        ProjectionCorrectionCommand(
            command_id="command-2", tenant_id="tenant-a", user_id="user-1",
            projection_id="projection-1", expected_version=2, operation="move_section",
            replacement_text="", target_section="tomorrow_plan", idempotency_key="move-1",
        ),
        _projection(),
    )

    assert result.allowed is False
    assert result.reason_code == "version_conflict"
    assert result.actual_write is False


def test_projection_correction_answer_forms_are_closed_and_preserve_replacement_text():
    assert parse_projection_correction("这条别放日报") == ("remove", "", "")
    assert parse_projection_correction("改成明天计划") == (
        "move_section", "", "tomorrow_plan"
    )
    assert parse_projection_correction("日报里换个说法：今天已向法院提交材料") == (
        "replace_text", "今天已向法院提交材料", ""
    )
    assert parse_projection_correction("随便调整一下") == ("", "", "")
