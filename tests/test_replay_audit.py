from app.workflows.replay_audit import classify_legacy_impact


def test_current_report_query_with_display_snapshot_is_non_write():
    impact = classify_legacy_impact(
        source_kind="interaction",
        legacy_processed=True,
        legacy_action="current_report_query",
        before_snapshot={},
        after_snapshot={
            "report_id": "r1",
            "status": "collecting",
            "today_work": ["A"],
            "problems": [],
            "tomorrow_plan": [],
        },
    )

    assert impact.kind == "non_write"
    assert impact.write_impact is False


def test_report_update_with_new_snapshot_is_write_impact():
    impact = classify_legacy_impact(
        source_kind="interaction",
        legacy_processed=True,
        legacy_action="report_update",
        before_snapshot={},
        after_snapshot={
            "report_id": "r1",
            "status": "collecting",
            "today_work": ["A"],
            "problems": [],
            "tomorrow_plan": [],
        },
    )

    assert impact.kind == "daily_write"
    assert impact.write_impact is True
    assert "report" in impact.changed_fields


def test_agent_edit_draft_detects_changed_report_items():
    impact = classify_legacy_impact(
        source_kind="interaction",
        legacy_processed=True,
        legacy_action="agent_edit_draft",
        before_snapshot={
            "report_id": "r1",
            "status": "collecting",
            "today_work": ["A", "B"],
            "problems": [],
            "tomorrow_plan": [],
        },
        after_snapshot={
            "report_id": "r1",
            "status": "collecting",
            "today_work": ["A"],
            "problems": [],
            "tomorrow_plan": [],
        },
    )

    assert impact.write_impact is True
    assert impact.changed_fields == ["today_work"]


def test_agent_no_change_with_equal_snapshot_is_non_write():
    snapshot = {
        "report_id": "r1",
        "status": "collecting",
        "today_work": ["A"],
        "problems": [],
        "tomorrow_plan": [],
    }

    impact = classify_legacy_impact(
        source_kind="interaction",
        legacy_processed=True,
        legacy_action="agent_no_change",
        before_snapshot=snapshot,
        after_snapshot=dict(snapshot),
    )

    assert impact.kind == "non_write"
    assert impact.write_impact is False


def test_complete_report_status_change_is_write_impact():
    impact = classify_legacy_impact(
        source_kind="interaction",
        legacy_processed=True,
        legacy_action="complete_report",
        before_snapshot={"report_id": "r1", "status": "pending_confirmation"},
        after_snapshot={"report_id": "r1", "status": "completed"},
    )

    assert impact.kind == "daily_write"
    assert impact.write_impact is True
    assert "status" in impact.changed_fields


def test_webhook_processed_without_snapshot_is_unknown_not_write_metric():
    impact = classify_legacy_impact(
        source_kind="webhook",
        legacy_processed=True,
        legacy_report_id="r1",
    )

    assert impact.kind == "unknown"
    assert impact.write_impact is False
