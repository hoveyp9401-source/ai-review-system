from app.legal_ops.write_service import daily_write_result_from_command_result


def test_daily_write_result_uses_committed_receipt_status_not_validation_reason():
    result = daily_write_result_from_command_result(
        {
            "status": "executed",
            "validation_status": "authorized",
            "reason": "exact_target",
            "actual_write": True,
        },
        report_ref="report-1",
    )

    assert result.status == "executed"
    assert result.actual_write is True
    assert result.succeeded is True


def test_daily_write_result_preserves_duplicate_and_blocked_receipts():
    duplicate = daily_write_result_from_command_result(
        {"status": "duplicate", "reason": "duplicate", "actual_write": False},
        report_ref="report-1",
    )
    blocked = daily_write_result_from_command_result(
        {"status": "blocked", "reason": "version_conflict", "actual_write": False},
        report_ref="report-1",
    )

    assert duplicate.status == "duplicate" and duplicate.succeeded
    assert blocked.status == "blocked" and not blocked.succeeded
