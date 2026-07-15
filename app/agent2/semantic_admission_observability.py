from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


READ_ONLY_OPERATIONS = frozenset(
    {
        "query_daily_report",
        "query_periodic_report",
        "answer_case_query",
        "query_case_progress",
        "query_operation_status",
        "search_enterprise_knowledge",
    }
)

_TRACE_MODES = frozenset({"shadow", "enforced"})
_TRACE_STATUSES = frozenset({"evaluated", "failed"})
_DECISION_VERDICTS = frozenset(
    {
        "admitted",
        "blocked",
        "no_op",
        "information_required",
        "review_only",
        "deferred_audit_only",
    }
)
_TICKET_STATUSES = frozenset(
    {
        "issued",
        "consumed",
        "expired",
        "cancelled",
        "conflicted",
        "permission_revoked",
    }
)
_PENDING_STATUSES = frozenset(
    {
        "active",
        "awaiting_input",
        "consumed",
        "expired",
        "cancelled",
        "conflicted",
        "permission_revoked",
    }
)
_REVIEW_STATUSES = frozenset(
    {"pending_human_review", "resolved", "dismissed", "expired"}
)
_DEFERRED_EVENT_STATUSES = frozenset(
    {"recorded", "superseded", "reviewed", "cancelled", "expired"}
)

_VIOLATION_KEYS = (
    "unknown_trace_mode",
    "unknown_trace_status",
    "unknown_decision_verdict",
    "unknown_ticket_status",
    "unknown_pending_status",
    "orphan_decision",
    "decision_scope_mismatch",
    "admitted_mutation_without_ticket",
    "admitted_mutation_ticket_missing",
    "read_only_decision_has_ticket",
    "information_required_without_pending",
    "non_authorizing_decision_has_artifact",
    "orphan_ticket",
    "ticket_scope_mismatch",
    "ticket_operation_mismatch",
    "ticket_link_mismatch",
    "shadow_ticket_not_cancelled",
    "consumed_ticket_without_receipt",
    "ticket_proves_business_write",
    "expired_ticket_still_issued",
    "orphan_pending",
    "pending_scope_mismatch",
    "pending_link_mismatch",
    "pending_can_authorize_write",
    "expired_pending_still_active",
    "consumed_pending_without_trace",
    "unknown_review_status",
    "orphan_review_item",
    "review_scope_mismatch",
    "review_link_mismatch",
    "review_not_audit_only",
    "review_can_authorize_write",
    "unknown_deferred_status",
    "orphan_deferred_event",
    "deferred_scope_mismatch",
    "deferred_link_mismatch",
    "deferred_not_audit_only",
    "deferred_can_authorize_write",
    "deferred_without_fresh_admission",
)


@dataclass(frozen=True)
class SemanticAdmissionSafetyResult:
    metrics: Mapping[str, int]
    violation_counts: Mapping[str, int]
    p0_violation_count: int
    enforce_recommended: bool
    kill_switch_recommended: bool
    evidence_sufficient: bool

    @property
    def verdict(self) -> str:
        if self.kill_switch_recommended:
            return "AUTO_DISABLE_ENFORCE"
        if not self.evidence_sufficient:
            return "INSUFFICIENT_OBSERVATION"
        return "STRUCTURAL_GATE_PASSED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "evidence_sufficient": self.evidence_sufficient,
            "enforce_recommended": self.enforce_recommended,
            "kill_switch_recommended": self.kill_switch_recommended,
            "p0_violation_count": self.p0_violation_count,
            "metrics": dict(self.metrics),
            "violation_counts": dict(self.violation_counts),
        }


def evaluate_semantic_admission_safety(
    *,
    traces: Iterable[Mapping[str, object]],
    decisions: Iterable[Mapping[str, object]],
    tickets: Iterable[Mapping[str, object]],
    information_pendings: Iterable[Mapping[str, object]],
    now: datetime,
    semantic_review_items: Iterable[Mapping[str, object]] = (),
    deferred_semantic_events: Iterable[Mapping[str, object]] = (),
) -> SemanticAdmissionSafetyResult:
    """Evaluate digest-only Admission artifacts without reading business text.

    This is a structural P0 circuit breaker, not a semantic quality score and
    not a production-readiness verdict.  A non-zero violation count means an
    Enforce deployment must be disabled; zero violations still requires the
    replay, usability, PostgreSQL and real-user gates defined by the release.
    """

    checked_at = _aware(now)
    trace_rows = tuple(dict(row) for row in traces)
    decision_rows = tuple(dict(row) for row in decisions)
    ticket_rows = tuple(dict(row) for row in tickets)
    pending_rows = tuple(dict(row) for row in information_pendings)
    review_rows = tuple(dict(row) for row in semantic_review_items)
    deferred_rows = tuple(dict(row) for row in deferred_semantic_events)

    violations = {name: 0 for name in _VIOLATION_KEYS}
    metrics: dict[str, int] = {
        "traces_total": len(trace_rows),
        "decisions_total": len(decision_rows),
        "tickets_total": len(ticket_rows),
        "information_pendings_total": len(pending_rows),
        "semantic_review_items_total": len(review_rows),
        "deferred_semantic_events_total": len(deferred_rows),
        "admitted_mutations": 0,
        "admitted_read_only": 0,
        "tickets_consumed": 0,
        "tickets_issued": 0,
        "tickets_cancelled": 0,
        "pendings_active": 0,
        "pendings_consumed": 0,
    }

    traces_by_id = {_text(row, "trace_id"): row for row in trace_rows}
    decisions_by_id = {
        _text(row, "decision_id"): row for row in decision_rows
    }
    tickets_by_id = {_text(row, "ticket_id"): row for row in ticket_rows}
    pendings_by_id = {_text(row, "pending_id"): row for row in pending_rows}

    for trace in trace_rows:
        mode = _text(trace, "admission_mode")
        status = _text(trace, "trace_status")
        _increment(metrics, f"traces_mode_{mode or 'missing'}")
        _increment(metrics, f"traces_status_{status or 'missing'}")
        if mode not in _TRACE_MODES:
            violations["unknown_trace_mode"] += 1
        if status not in _TRACE_STATUSES:
            violations["unknown_trace_status"] += 1

    for decision in decision_rows:
        verdict = _text(decision, "verdict")
        operation = _text(decision, "operation")
        ticket_id = _optional_text(decision, "ticket_id")
        pending_id = _optional_text(decision, "pending_id")
        trace = traces_by_id.get(_text(decision, "trace_id"))
        _increment(metrics, f"decisions_verdict_{verdict or 'missing'}")
        _increment(metrics, f"decisions_domain_{_text(decision, 'domain') or 'missing'}")
        if verdict not in _DECISION_VERDICTS:
            violations["unknown_decision_verdict"] += 1
        if trace is None:
            violations["orphan_decision"] += 1
        elif not _same_scope(decision, trace):
            violations["decision_scope_mismatch"] += 1

        if verdict == "admitted" and operation in READ_ONLY_OPERATIONS:
            metrics["admitted_read_only"] += 1
            if ticket_id:
                violations["read_only_decision_has_ticket"] += 1
        elif verdict == "admitted":
            metrics["admitted_mutations"] += 1
            if not ticket_id:
                violations["admitted_mutation_without_ticket"] += 1
            elif ticket_id not in tickets_by_id:
                violations["admitted_mutation_ticket_missing"] += 1
        elif verdict == "information_required":
            if not pending_id or pending_id not in pendings_by_id:
                violations["information_required_without_pending"] += 1
            if ticket_id:
                violations["non_authorizing_decision_has_artifact"] += 1
        elif ticket_id or pending_id:
            violations["non_authorizing_decision_has_artifact"] += 1

    for ticket in ticket_rows:
        status = _text(ticket, "ticket_status")
        trace = traces_by_id.get(_text(ticket, "trace_id"))
        decision = decisions_by_id.get(_text(ticket, "decision_id"))
        _increment(metrics, f"tickets_status_{status or 'missing'}")
        if status == "consumed":
            metrics["tickets_consumed"] += 1
        elif status == "issued":
            metrics["tickets_issued"] += 1
        elif status == "cancelled":
            metrics["tickets_cancelled"] += 1
        if status not in _TICKET_STATUSES:
            violations["unknown_ticket_status"] += 1
        if trace is None or decision is None:
            violations["orphan_ticket"] += 1
        else:
            if not _same_scope(ticket, trace) or not _same_scope(ticket, decision):
                violations["ticket_scope_mismatch"] += 1
            if _text(ticket, "operation") != _text(decision, "operation"):
                violations["ticket_operation_mismatch"] += 1
            if _optional_text(decision, "ticket_id") != _text(ticket, "ticket_id"):
                violations["ticket_link_mismatch"] += 1
            if _text(trace, "admission_mode") == "shadow" and status != "cancelled":
                violations["shadow_ticket_not_cancelled"] += 1
        if status == "consumed" and not _optional_text(
            ticket, "consumed_receipt_ref"
        ):
            violations["consumed_ticket_without_receipt"] += 1
        if bool(ticket.get("proves_business_write")):
            violations["ticket_proves_business_write"] += 1
        expires_at = _datetime(ticket.get("expires_at"))
        if status == "issued" and expires_at is not None and expires_at <= checked_at:
            violations["expired_ticket_still_issued"] += 1

    for pending in pending_rows:
        status = _text(pending, "pending_status")
        trace = traces_by_id.get(_text(pending, "trace_id"))
        decision = decisions_by_id.get(_text(pending, "decision_id"))
        _increment(metrics, f"pendings_status_{status or 'missing'}")
        if status in {"active", "awaiting_input"}:
            metrics["pendings_active"] += 1
        elif status == "consumed":
            metrics["pendings_consumed"] += 1
        if status not in _PENDING_STATUSES:
            violations["unknown_pending_status"] += 1
        if trace is None or decision is None:
            violations["orphan_pending"] += 1
        else:
            if not _same_scope(pending, trace) or not _same_scope(pending, decision):
                violations["pending_scope_mismatch"] += 1
            if _optional_text(decision, "pending_id") != _text(
                pending, "pending_id"
            ):
                violations["pending_link_mismatch"] += 1
        if bool(pending.get("business_write_allowed")):
            violations["pending_can_authorize_write"] += 1
        expires_at = _datetime(pending.get("expires_at"))
        if (
            status in {"active", "awaiting_input"}
            and expires_at is not None
            and expires_at <= checked_at
        ):
            violations["expired_pending_still_active"] += 1
        if status == "consumed" and (
            _datetime(pending.get("consumed_at")) is None
            or not _optional_text(pending, "consumed_by_trace_id")
        ):
            violations["consumed_pending_without_trace"] += 1

    for review in review_rows:
        status = _text(review, "review_status")
        trace = traces_by_id.get(_text(review, "trace_id"))
        decision_id = _optional_text(review, "decision_id")
        decision = decisions_by_id.get(decision_id or "") if decision_id else None
        _increment(metrics, f"review_items_status_{status or 'missing'}")
        if status not in _REVIEW_STATUSES:
            violations["unknown_review_status"] += 1
        if trace is None or decision is None:
            violations["orphan_review_item"] += 1
        else:
            if not _same_scope(review, trace) or not _same_scope(review, decision):
                violations["review_scope_mismatch"] += 1
            if not _same_decision_binding(
                review, decision
            ) or not _same_candidate_binding(
                review,
                decision,
                persisted_key="candidate_snapshot_json",
                contract_key="candidate_snapshot",
            ):
                violations["review_link_mismatch"] += 1
        if review.get("audit_only") is not True:
            violations["review_not_audit_only"] += 1
        if review.get("business_write_allowed") is not False:
            violations["review_can_authorize_write"] += 1

    for deferred in deferred_rows:
        status = _text(deferred, "event_status")
        trace = traces_by_id.get(_text(deferred, "trace_id"))
        decision_id = _optional_text(deferred, "decision_id")
        decision = decisions_by_id.get(decision_id or "") if decision_id else None
        _increment(metrics, f"deferred_events_status_{status or 'missing'}")
        if status not in _DEFERRED_EVENT_STATUSES:
            violations["unknown_deferred_status"] += 1
        if trace is None or decision is None:
            violations["orphan_deferred_event"] += 1
        else:
            if not _same_scope(deferred, trace) or not _same_scope(
                deferred, decision
            ):
                violations["deferred_scope_mismatch"] += 1
            if not _same_decision_binding(
                deferred, decision
            ) or not _same_candidate_binding(
                deferred,
                decision,
                persisted_key="payload_json",
                contract_key="payload",
            ):
                violations["deferred_link_mismatch"] += 1
        if deferred.get("audit_only") is not True:
            violations["deferred_not_audit_only"] += 1
        if deferred.get("business_write_allowed") is not False:
            violations["deferred_can_authorize_write"] += 1
        if deferred.get("requires_fresh_admission") is not True:
            violations["deferred_without_fresh_admission"] += 1

    p0_count = sum(violations.values())
    evidence_sufficient = bool(trace_rows)
    return SemanticAdmissionSafetyResult(
        metrics=metrics,
        violation_counts=violations,
        p0_violation_count=p0_count,
        enforce_recommended=evidence_sufficient and p0_count == 0,
        kill_switch_recommended=p0_count > 0,
        evidence_sufficient=evidence_sufficient,
    )


def _same_scope(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    return all(
        _text(left, key) == _text(right, key)
        for key in (
            "tenant_id",
            "user_id",
            "conversation_id",
            "source_turn_id",
            "source_message_id",
        )
    )


def _same_decision_binding(
    artifact: Mapping[str, object],
    decision: Mapping[str, object],
) -> bool:
    return all(
        artifact.get(key) == decision.get(key)
        for key in (
            "trace_id",
            "segment_id",
            "segment_text_sha256",
            "segment_start_offset",
            "segment_end_offset",
            "domain",
            "operation",
        )
    ) and all(
        _empty_normalized(artifact.get(key)) == _empty_normalized(decision.get(key))
        for key in (
            "object_type",
            "object_stable_id",
            "object_version",
            "object_label",
        )
    )


def _same_candidate_binding(
    artifact: Mapping[str, object],
    decision: Mapping[str, object],
    *,
    persisted_key: str,
    contract_key: str,
) -> bool:
    raw_candidate = (
        artifact.get(persisted_key)
        if persisted_key in artifact
        else artifact.get(contract_key)
    )
    if not isinstance(raw_candidate, Mapping) or set(raw_candidate) != {
        "action_id",
        "decision_verdict",
        "evidence_refs",
    }:
        return False
    raw_evidence_refs = (
        decision.get("evidence_refs_json")
        if "evidence_refs_json" in decision
        else decision.get("evidence_refs")
    )
    candidate_evidence_refs = raw_candidate.get("evidence_refs")
    if not isinstance(raw_evidence_refs, (list, tuple)) or not isinstance(
        candidate_evidence_refs, (list, tuple)
    ):
        return False
    decision_verdict = (
        decision.get("verdict")
        if "verdict" in decision
        else decision.get("status")
    )
    return (
        raw_candidate.get("action_id") == decision.get("action_id")
        and raw_candidate.get("decision_verdict") == decision_verdict
        and tuple(candidate_evidence_refs) == tuple(raw_evidence_refs)
    )


def _empty_normalized(value: object) -> object:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


def _text(row: Mapping[str, object], key: str) -> str:
    return str(row.get(key) or "").strip()


def _optional_text(row: Mapping[str, object], key: str) -> str | None:
    value = _text(row, key)
    return value or None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("semantic admission observation time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def _increment(metrics: dict[str, int], key: str) -> None:
    metrics[key] = metrics.get(key, 0) + 1
