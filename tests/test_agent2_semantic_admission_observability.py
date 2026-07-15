from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agent2.semantic_admission_observability import (
    evaluate_semantic_admission_safety,
)


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


def _trace(*, mode: str = "enforced") -> dict[str, object]:
    return {
        "trace_id": "trace-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_message_id": "message-1",
        "admission_mode": mode,
        "trace_status": "evaluated",
        "admission_summary": "admitted",
    }


def _decision(
    *,
    operation: str = "record_case_progress",
    verdict: str = "admitted",
    ticket_id: str | None = "ticket-1",
) -> dict[str, object]:
    return {
        "decision_id": "decision-1",
        "trace_id": "trace-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_message_id": "message-1",
        "domain": "case",
        "operation": operation,
        "action_id": "action-1",
        "verdict": verdict,
        "evidence_refs_json": ["segment_sha256:" + "a" * 64],
        "ticket_id": ticket_id,
        "pending_id": None,
        "segment_id": "segment-1",
        "segment_text_sha256": "a" * 64,
        "segment_start_offset": 0,
        "segment_end_offset": 8,
        "object_type": "case_progress",
        "object_stable_id": "progress-1",
        "object_version": 3,
        "object_label": "南京工程款案进展",
    }


def _ticket(*, status: str = "consumed") -> dict[str, object]:
    return {
        "ticket_id": "ticket-1",
        "decision_id": "decision-1",
        "trace_id": "trace-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_message_id": "message-1",
        "operation": "record_case_progress",
        "ticket_status": status,
        "expires_at": NOW + timedelta(minutes=5),
        "consumed_at": NOW if status == "consumed" else None,
        "consumed_receipt_ref": "business:receipt-1" if status == "consumed" else None,
        "proves_business_write": False,
    }


def test_consistent_enforced_artifacts_keep_p0_gate_open():
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[_decision()],
        tickets=[_ticket()],
        information_pendings=[],
        now=NOW,
    )

    assert result.p0_violation_count == 0
    assert result.enforce_recommended is True
    assert result.kill_switch_recommended is False
    assert result.metrics["tickets_consumed"] == 1
    assert result.metrics["admitted_mutations"] == 1


def test_admitted_mutation_without_ticket_is_a_p0_violation():
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[_decision(ticket_id=None)],
        tickets=[],
        information_pendings=[],
        now=NOW,
    )

    assert result.violation_counts["admitted_mutation_without_ticket"] == 1
    assert result.enforce_recommended is False
    assert result.kill_switch_recommended is True


def test_read_only_decision_must_not_receive_a_ticket():
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[_decision(operation="query_case_progress")],
        tickets=[_ticket()],
        information_pendings=[],
        now=NOW,
    )

    assert result.violation_counts["read_only_decision_has_ticket"] == 1


def test_shadow_ticket_must_be_cancelled_and_never_consumable():
    result = evaluate_semantic_admission_safety(
        traces=[_trace(mode="shadow")],
        decisions=[_decision()],
        tickets=[_ticket(status="issued")],
        information_pendings=[],
        now=NOW,
    )

    assert result.violation_counts["shadow_ticket_not_cancelled"] == 1
    assert result.kill_switch_recommended is True


def test_ticket_scope_drift_and_missing_receipt_are_counted_independently():
    ticket = _ticket()
    ticket.update(
        {
            "user_id": "user-2",
            "consumed_receipt_ref": "",
        }
    )
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[_decision()],
        tickets=[ticket],
        information_pendings=[],
        now=NOW,
    )

    assert result.violation_counts["ticket_scope_mismatch"] == 1
    assert result.violation_counts["consumed_ticket_without_receipt"] == 1


def test_expired_active_pending_is_fail_closed_and_never_authorizes_write():
    pending = {
        "pending_id": "pending-1",
        "decision_id": "decision-1",
        "trace_id": "trace-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_message_id": "message-1",
        "pending_status": "active",
        "expires_at": NOW - timedelta(seconds=1),
        "business_write_allowed": True,
        "consumed_at": None,
        "consumed_by_trace_id": None,
    }
    decision = _decision(verdict="information_required", ticket_id=None)
    decision["pending_id"] = "pending-1"
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[decision],
        tickets=[],
        information_pendings=[pending],
        now=NOW,
    )

    assert result.violation_counts["expired_pending_still_active"] == 1
    assert result.violation_counts["pending_can_authorize_write"] == 1


def test_cross_tenant_decision_is_detected_without_reading_business_text():
    decision = _decision()
    decision["tenant_id"] = "other-tenant"
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[decision],
        tickets=[_ticket()],
        information_pendings=[],
        now=NOW,
    )

    assert result.violation_counts["decision_scope_mismatch"] == 1
    payload = result.as_dict()
    assert "raw_text" not in str(payload)
    assert payload["verdict"] == "AUTO_DISABLE_ENFORCE"


def _review_item(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "review_id": "review-1",
        "decision_id": "decision-1",
        "trace_id": "trace-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_message_id": "message-1",
        "segment_id": "segment-1",
        "segment_text_sha256": "a" * 64,
        "segment_start_offset": 0,
        "segment_end_offset": 8,
        "domain": "case",
        "operation": "record_case_progress",
        "object_type": "case_progress",
        "object_stable_id": "progress-1",
        "object_version": 3,
        "object_label": "南京工程款案进展",
        "review_status": "pending_human_review",
        "candidate_snapshot_json": {
            "action_id": "action-1",
            "decision_verdict": "review_only",
            "evidence_refs": ["segment_sha256:" + "a" * 64],
        },
        "audit_only": True,
        "business_write_allowed": False,
    }
    row.update(overrides)
    return row


def _deferred_event(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "deferred_event_id": "deferred-1",
        "decision_id": "decision-1",
        "trace_id": "trace-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "source_message_id": "message-1",
        "segment_id": "segment-1",
        "segment_text_sha256": "a" * 64,
        "segment_start_offset": 0,
        "segment_end_offset": 8,
        "domain": "case",
        "operation": "record_case_progress",
        "object_type": "case_progress",
        "object_stable_id": "progress-1",
        "object_version": 3,
        "object_label": "南京工程款案进展",
        "event_status": "recorded",
        "payload_json": {
            "action_id": "action-1",
            "decision_verdict": "deferred_audit_only",
            "evidence_refs": ["segment_sha256:" + "a" * 64],
        },
        "audit_only": True,
        "business_write_allowed": False,
        "requires_fresh_admission": True,
    }
    row.update(overrides)
    return row


def test_review_and_deferred_audit_artifacts_keep_gate_open_when_safely_linked():
    review_decision = _decision(verdict="review_only", ticket_id=None)
    deferred_decision = {
        **_decision(verdict="deferred_audit_only", ticket_id=None),
        "decision_id": "decision-2",
        "segment_id": "segment-2",
        "segment_text_sha256": "b" * 64,
    }
    deferred = _deferred_event(
        decision_id="decision-2",
        segment_id="segment-2",
        segment_text_sha256="b" * 64,
    )

    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[review_decision, deferred_decision],
        tickets=[],
        information_pendings=[],
        semantic_review_items=[_review_item()],
        deferred_semantic_events=[deferred],
        now=NOW,
    )

    assert result.p0_violation_count == 0
    assert result.metrics["semantic_review_items_total"] == 1
    assert result.metrics["deferred_semantic_events_total"] == 1


def test_review_artifact_scope_and_safety_constant_drift_are_p0_violations():
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[_decision(verdict="review_only", ticket_id=None)],
        tickets=[],
        information_pendings=[],
        semantic_review_items=[
            _review_item(
                user_id="user-2",
                audit_only=False,
                business_write_allowed=True,
            )
        ],
        deferred_semantic_events=[],
        now=NOW,
    )

    assert result.violation_counts["review_scope_mismatch"] == 1
    assert result.violation_counts["review_not_audit_only"] == 1
    assert result.violation_counts["review_can_authorize_write"] == 1
    assert result.kill_switch_recommended is True


def test_deferred_artifact_orphan_and_safety_constant_drift_are_p0_violations():
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[],
        tickets=[],
        information_pendings=[],
        semantic_review_items=[],
        deferred_semantic_events=[
            _deferred_event(
                decision_id="missing-decision",
                audit_only=False,
                business_write_allowed=True,
                requires_fresh_admission=False,
            )
        ],
        now=NOW,
    )

    assert result.violation_counts["orphan_deferred_event"] == 1
    assert result.violation_counts["deferred_not_audit_only"] == 1
    assert result.violation_counts["deferred_can_authorize_write"] == 1
    assert result.violation_counts["deferred_without_fresh_admission"] == 1
    assert result.kill_switch_recommended is True


def test_review_and_deferred_binding_drift_is_detected_without_business_text():
    decisions = [
        _decision(verdict="review_only", ticket_id=None),
        {
            **_decision(verdict="deferred_audit_only", ticket_id=None),
            "decision_id": "decision-2",
        },
    ]
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=decisions,
        tickets=[],
        information_pendings=[],
        semantic_review_items=[_review_item(operation="record_travel_event")],
        deferred_semantic_events=[
            _deferred_event(decision_id="decision-2", segment_end_offset=9)
        ],
        now=NOW,
    )

    assert result.violation_counts["review_link_mismatch"] == 1
    assert result.violation_counts["deferred_link_mismatch"] == 1


def _with_candidate_drift(
    row: dict[str, object],
    *,
    mapping_key: str,
    candidate_field: str,
    drifted_value: object,
) -> dict[str, object]:
    return {
        **row,
        mapping_key: {
            **dict(row[mapping_key]),
            candidate_field: drifted_value,
        },
    }


@pytest.mark.parametrize("artifact_kind", ("review", "deferred"))
@pytest.mark.parametrize(
    ("candidate_field", "drifted_value"),
    (
        ("action_id", "action-different"),
        ("decision_verdict", "blocked"),
        ("evidence_refs", ["segment_sha256:" + "c" * 64]),
    ),
)
def test_review_and_deferred_candidate_drift_is_a_p0_link_mismatch(
    artifact_kind,
    candidate_field,
    drifted_value,
):
    if artifact_kind == "review":
        decision = _decision(verdict="review_only", ticket_id=None)
        reviews = [
            _with_candidate_drift(
                _review_item(),
                mapping_key="candidate_snapshot_json",
                candidate_field=candidate_field,
                drifted_value=drifted_value,
            )
        ]
        deferred_events = []
        violation_key = "review_link_mismatch"
    else:
        decision = _decision(verdict="deferred_audit_only", ticket_id=None)
        deferred_events = [
            _with_candidate_drift(
                _deferred_event(),
                mapping_key="payload_json",
                candidate_field=candidate_field,
                drifted_value=drifted_value,
            )
        ]
        reviews = []
        violation_key = "deferred_link_mismatch"

    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[decision],
        tickets=[],
        information_pendings=[],
        semantic_review_items=reviews,
        deferred_semantic_events=deferred_events,
        now=NOW,
    )

    assert result.violation_counts[violation_key] == 1
    assert result.p0_violation_count == 1


def test_empty_decision_ids_are_orphans_for_review_and_deferred_artifacts():
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[],
        tickets=[],
        information_pendings=[],
        semantic_review_items=[_review_item(decision_id="")],
        deferred_semantic_events=[_deferred_event(decision_id="")],
        now=NOW,
    )

    assert result.violation_counts["orphan_review_item"] == 1
    assert result.violation_counts["orphan_deferred_event"] == 1
    assert result.p0_violation_count == 2


def test_review_object_type_drift_is_a_link_mismatch():
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[_decision(verdict="review_only", ticket_id=None)],
        tickets=[],
        information_pendings=[],
        semantic_review_items=[_review_item(object_type="travel_intent")],
        now=NOW,
    )

    assert result.violation_counts["review_link_mismatch"] == 1
    assert result.p0_violation_count == 1


def test_review_object_stable_id_drift_is_a_link_mismatch():
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[_decision(verdict="review_only", ticket_id=None)],
        tickets=[],
        information_pendings=[],
        semantic_review_items=[_review_item(object_stable_id="progress-2")],
        now=NOW,
    )

    assert result.violation_counts["review_link_mismatch"] == 1
    assert result.p0_violation_count == 1


def test_review_object_version_drift_is_a_link_mismatch():
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[_decision(verdict="review_only", ticket_id=None)],
        tickets=[],
        information_pendings=[],
        semantic_review_items=[_review_item(object_version=4)],
        now=NOW,
    )

    assert result.violation_counts["review_link_mismatch"] == 1
    assert result.p0_violation_count == 1


def test_review_object_label_drift_is_a_link_mismatch():
    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[_decision(verdict="review_only", ticket_id=None)],
        tickets=[],
        information_pendings=[],
        semantic_review_items=[_review_item(object_label="上海出差")],
        now=NOW,
    )

    assert result.violation_counts["review_link_mismatch"] == 1
    assert result.p0_violation_count == 1


def test_empty_object_binding_values_are_normalized_for_audit_artifacts():
    review_decision = _decision(verdict="review_only", ticket_id=None)
    review_decision.update(
        {
            "object_type": None,
            "object_stable_id": "",
            "object_version": None,
            "object_label": "   ",
        }
    )
    deferred_decision = {
        **review_decision,
        "decision_id": "decision-2",
        "verdict": "deferred_audit_only",
    }

    result = evaluate_semantic_admission_safety(
        traces=[_trace()],
        decisions=[review_decision, deferred_decision],
        tickets=[],
        information_pendings=[],
        semantic_review_items=[
            _review_item(
                object_type="",
                object_stable_id=None,
                object_version=" ",
                object_label=None,
            )
        ],
        deferred_semantic_events=[
            _deferred_event(
                decision_id="decision-2",
                object_type=" ",
                object_stable_id="",
                object_version=None,
                object_label="",
            )
        ],
        now=NOW,
    )

    assert result.violation_counts["review_link_mismatch"] == 0
    assert result.violation_counts["deferred_link_mismatch"] == 0
    assert result.p0_violation_count == 0


def test_any_deferred_object_binding_drift_is_a_p0_link_mismatch():
    deferred_decision = {
        **_decision(verdict="deferred_audit_only", ticket_id=None),
        "decision_id": "decision-2",
    }
    drifted_events = (
        _deferred_event(decision_id="decision-2", object_type="travel_intent"),
        _deferred_event(decision_id="decision-2", object_stable_id="progress-2"),
        _deferred_event(decision_id="decision-2", object_version=4),
        _deferred_event(decision_id="decision-2", object_label="上海出差"),
    )

    for deferred in drifted_events:
        result = evaluate_semantic_admission_safety(
            traces=[_trace()],
            decisions=[deferred_decision],
            tickets=[],
            information_pendings=[],
            deferred_semantic_events=[deferred],
            now=NOW,
        )

        assert result.violation_counts["deferred_link_mismatch"] == 1
        assert result.p0_violation_count == 1
