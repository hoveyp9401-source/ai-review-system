from datetime import UTC, datetime
from uuid import uuid4

from app.agent2.business.models import BusinessCommandReceipt
from app.agent2.case_followup_scheduler import (
    hearing_triggers_from_case_source,
    lifecycle_triggers_from_receipt,
)


def test_hearing_source_builds_versioned_7_3_1_and_post_hearing_triggers():
    triggers = hearing_triggers_from_case_source(
        {
            "hearing_event_id": "hearing-1",
            "hearing_at": "2026-07-20T09:30:00+08:00",
        },
        case_version=4,
    )

    assert len(triggers) == 4
    assert all("hearing-1:v4:" in item.trigger_event_id for item in triggers)
    assert {item.question_type for item in triggers} == {
        "hearing_readiness", "hearing_result"
    }


def test_invalid_or_date_only_hearing_source_fails_closed_without_guessing_time():
    assert hearing_triggers_from_case_source(
        {"hearing_date": "2026-07-20"}, case_version=1
    ) == ()
    assert hearing_triggers_from_case_source(
        {"hearing_at": "not-a-date"}, case_version=1
    ) == ()


def _lifecycle_receipt(*, status="executed", actual_write=True):
    case_id = uuid4()
    return BusinessCommandReceipt(
        receipt_id=uuid4(), tenant_id="tenant-test", command_id="command-1",
        command_type="create_case_progress", actor_user_id="user-1",
        source_message_id="message-1", idempotency_key="receipt-key",
        status=status, resource_type="case_progress", resource_id=str(uuid4()),
        before_json={},
        after_json={
            "case_id": str(case_id),
            "lifecycle_change": {
                "case_type": "plaintiff", "from_stage": "拟诉",
                "to_stage": "诉讼中", "node": "已立案", "case_version": 2,
                "occurred_at": "2026-07-13T10:00:00+08:00",
            },
        },
        error_code="", failed_stage="", actual_write=actual_write,
        created_at=datetime(2026, 7, 13, 2, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 13, 2, 0, tzinfo=UTC),
    )


def test_committed_lifecycle_receipt_builds_allowlisted_stage_and_node_triggers():
    receipt = _lifecycle_receipt()

    triggers = lifecycle_triggers_from_receipt(receipt)

    assert [item.trigger_type for item in triggers] == [
        "stage_transition", "node_transition"
    ]
    assert all(str(receipt.receipt_id) in item.trigger_event_id for item in triggers)


def test_uncommitted_or_malformed_lifecycle_receipt_never_builds_triggers():
    assert lifecycle_triggers_from_receipt(
        _lifecycle_receipt(status="blocked", actual_write=False)
    ) == ()
    malformed = _lifecycle_receipt()
    malformed.after_json["lifecycle_change"]["occurred_at"] = "not-a-time"
    assert lifecycle_triggers_from_receipt(malformed) == ()
