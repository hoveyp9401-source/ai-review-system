from app.agent2.daily_state import (
    PENDING_ACTION_KEY,
    PENDING_DAILY_CANDIDATE_KEY,
    PENDING_DRAFT_EDIT_KEY,
    clear_pending_daily_candidate,
    focus_item,
    has_confirmation_pending,
    has_pending_daily_candidate,
    item_reference_from_payload,
    pending_daily_candidate,
    pending_keys,
    set_pending_daily_candidate,
)


def test_pending_daily_candidate_is_focus_not_confirmation():
    status = {
        PENDING_DAILY_CANDIDATE_KEY: {
            "field": "today_work",
            "field_index": 2,
            "item_id": "tw-2",
            "text": "进行上海机载项目评审",
            "source_text": "上海鸡仔",
        }
    }

    assert pending_keys(status) == [PENDING_DAILY_CANDIDATE_KEY]
    assert has_pending_daily_candidate(status) is True
    assert has_confirmation_pending(status) is False
    assert pending_daily_candidate(status).target_tuple() == ("today_work", 2)


def test_confirmation_pending_is_separate_from_focus_state():
    assert has_confirmation_pending({PENDING_ACTION_KEY: "confirm_clear_current_report"}) is True
    assert has_confirmation_pending({PENDING_DRAFT_EDIT_KEY: {"requires_confirmation": True}}) is True
    assert has_confirmation_pending({PENDING_DRAFT_EDIT_KEY: {"requires_confirmation": False}}) is False


def test_focus_item_prefers_pending_candidate_before_last_modified_memory():
    status = {
        "_agent2_last_modified_item": {"field": "today_work", "item_index": 1, "item_id": "tw-1", "text": "合同审核"},
        PENDING_DAILY_CANDIDATE_KEY: {
            "field": "tomorrow_plan",
            "field_index": 2,
            "item_id": "tp-2",
            "text": "明天南京开庭",
        },
    }

    reference = focus_item(status)

    assert reference.field == "tomorrow_plan"
    assert reference.item_index == 2
    assert reference.item_id == "tp-2"


def test_set_and_clear_pending_daily_candidate_copies_payload():
    payload = {"field": "today_work", "field_index": 1, "item_id": "tw-1", "text": "合同审核"}
    status = set_pending_daily_candidate({}, payload, created_at="2026-07-05T14:00:00+08:00")
    payload["text"] = "外部被改"

    assert status[PENDING_DAILY_CANDIDATE_KEY]["text"] == "合同审核"
    assert status[PENDING_DAILY_CANDIDATE_KEY]["created_at"] == "2026-07-05T14:00:00+08:00"

    clear_pending_daily_candidate(status)

    assert PENDING_DAILY_CANDIDATE_KEY not in status


def test_item_reference_rejects_invalid_payloads():
    assert item_reference_from_payload(None) is None
    assert item_reference_from_payload({"field": "unknown", "item_index": 1}) is None
    assert item_reference_from_payload({"field": "today_work"}) is None
