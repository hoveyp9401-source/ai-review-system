from __future__ import annotations

import pytest

from app.agent2.operation_outcomes import (
    OperationOutcome,
    OutcomeObjectRef,
    OutcomeReceiptRef,
    OutcomeReplyComposer,
    OutcomeStateTransition,
)


@pytest.mark.parametrize(
    "business_status",
    (
        "accepted_by_one_party",
        "accepted_by_both",
        "declined",
        "cancelled",
        "expired",
    ),
)
def test_travel_response_fact_requires_a_committed_database_receipt(
    business_status: str,
) -> None:
    with pytest.raises(ValueError, match="committed travel response receipt"):
        OperationOutcome(
            domain="travel",
            operation="respond",
            object_ref=OutcomeObjectRef(
                "travel_collaboration", "candidate-1", "Nanjing collaboration"
            ),
            business_status=business_status,
            message_status="not_applicable",
            changed_fields=("status",),
            user_visible_snapshot={"destination": "Nanjing"},
            blocking_reason="",
            receipt_refs=(),
            state_transition=OutcomeStateTransition("waiting", business_status),
            actual_write=False,
        )


def test_committed_travel_acceptance_can_be_composed_from_the_receipt() -> None:
    outcome = OperationOutcome(
        domain="travel",
        operation="respond",
        object_ref=OutcomeObjectRef(
            "travel_collaboration", "candidate-1", "Nanjing collaboration", 2
        ),
        business_status="accepted_by_both",
        message_status="not_applicable",
        changed_fields=("status",),
        user_visible_snapshot={"destination": "Nanjing", "date_label": "2026-07-15"},
        blocking_reason="",
        receipt_refs=(
            OutcomeReceiptRef(
                "receipt-1", "database", "executed", True
            ),
        ),
        state_transition=OutcomeStateTransition(
            "accepted_by_one_party", "accepted_by_both"
        ),
        actual_write=True,
    )

    reply = OutcomeReplyComposer().compose((outcome,))

    assert "Nanjing" in reply


@pytest.mark.parametrize("operation", ("query", "notify", "status"))
def test_travel_acceptance_fact_cannot_bypass_receipt_by_changing_operation(
    operation: str,
) -> None:
    with pytest.raises(ValueError, match="committed travel response receipt"):
        OperationOutcome(
            domain="travel",
            operation=operation,
            object_ref=OutcomeObjectRef(
                "travel_collaboration", "candidate-1", "Nanjing collaboration"
            ),
            business_status="accepted_by_both",
            message_status="not_applicable",
            changed_fields=(),
            user_visible_snapshot={"destination": "Nanjing"},
            blocking_reason="",
            receipt_refs=(),
            state_transition=OutcomeStateTransition(
                "accepted_by_one_party", "accepted_by_both"
            ),
            actual_write=False,
        )


def test_travel_response_fact_requires_a_stable_business_object() -> None:
    with pytest.raises(ValueError, match="stable business object"):
        OperationOutcome(
            domain="travel",
            operation="respond",
            object_ref=OutcomeObjectRef(
                "travel_collaboration", "", "Nanjing collaboration"
            ),
            business_status="accepted_by_one_party",
            message_status="not_applicable",
            changed_fields=("status",),
            user_visible_snapshot={"destination": "Nanjing"},
            blocking_reason="",
            receipt_refs=(
                OutcomeReceiptRef(
                    "receipt-1", "database", "executed", True
                ),
            ),
            state_transition=OutcomeStateTransition(
                "waiting_for_reply", "accepted_by_one_party"
            ),
            actual_write=True,
        )
