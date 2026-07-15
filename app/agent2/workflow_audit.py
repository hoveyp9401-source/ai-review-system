from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.time import now_in_timezone
from app.workflows.intake import IncomingMessageEnvelope


async def create_agent2_workflow_audit_event(
    *,
    session: AsyncSession,
    user: Any,
    incoming: Any,
    settings: Any,
    envelope: IncomingMessageEnvelope,
    shadow: Any,
    mode: str,
    observe_only_log: bool,
) -> None:
    if not bool(getattr(settings, "shadow_memory_enabled", False)):
        return
    user_id = getattr(user, "id", None)
    if user_id is None:
        return

    route_observation = _workflow_route_audit_projection(
        shadow.route_observation(envelope)
    )
    gate_observation = _workflow_gate_audit_projection(
        shadow.gate_observation(envelope)
    )
    confidence = _decimal_confidence(getattr(getattr(shadow, "route", None), "confidence", None))
    backend_action = "agent2_workflow_audit_observe" if observe_only_log else "agent2_workflow_audit_gate"
    try:
        from app.models import ReportInteractionEvent

        async with session.begin_nested():
            event = ReportInteractionEvent(
                user_id=user_id,
                report_id=None,
                dingtalk_user_id=str(getattr(incoming, "dingtalk_user_id", "") or ""),
                report_date=now_in_timezone(getattr(settings, "timezone", "Asia/Shanghai")).date(),
                message_text="",
                llm_decision_json={
                    "agent2": True,
                    "audit_stage": "observe_only" if observe_only_log else "gate",
                    "mode": mode,
                    "route": route_observation,
                    "gate": gate_observation,
                    "cognitive_decision": _legacy_cognitive_audit_projection(
                        getattr(shadow, "cognitive_decision", None)
                    ),
                },
                backend_action=backend_action,
                before_snapshot_json={},
                after_snapshot_json={},
                correction_type="",
                correction_from="",
                correction_to="",
                confidence=confidence,
                asr_suspect_json={},
            )
            session.add(event)
            await session.flush()
    except Exception as exc:
        print(f"Agent2 workflow audit skipped: {exc}", flush=True)


def _workflow_route_audit_projection(observation: Any) -> dict[str, Any]:
    payload = _mapping_or_empty(observation)
    return {
        "source": str(payload.get("source") or ""),
        "message_id": str(payload.get("message_id") or ""),
        "conversation_id": str(payload.get("conversation_id") or ""),
        "raw_text_hash": str(payload.get("raw_text_hash") or ""),
        "raw_text_chars": payload.get("raw_text_chars"),
        "selected_workflow": str(payload.get("selected_workflow") or ""),
        "confidence": payload.get("confidence"),
        "reason": str(payload.get("reason") or ""),
        "task_id": str(payload.get("task_id") or ""),
        "observe_only": bool(payload.get("observe_only", False)),
        "signals": _numeric_mapping(payload.get("signals")),
        "active_tasks": [
            {
                "workflow": str(item.get("workflow") or ""),
                "task_id": str(item.get("task_id") or ""),
                "status": str(item.get("status") or ""),
                "reply_candidate": bool(item.get("reply_candidate", False)),
                "awaiting_confirmation": bool(
                    item.get("awaiting_confirmation", False)
                ),
            }
            for item in (
                _mapping_or_empty(raw)
                for raw in _sequence(payload.get("active_tasks"))
            )
        ],
    }


def _workflow_gate_audit_projection(observation: Any) -> dict[str, Any]:
    payload = _mapping_or_empty(observation)
    plan = _mapping_or_empty(payload.get("plan"))
    gate = _mapping_or_empty(payload.get("gate"))
    summary = _mapping_or_empty(payload.get("summary"))
    safety = _mapping_or_empty(plan.get("safety_decision"))
    return {
        "plan": {
            **_workflow_route_audit_projection(
                {
                    **plan,
                    "selected_workflow": plan.get("primary_workflow"),
                }
            ),
            "primary_workflow": str(plan.get("primary_workflow") or ""),
            "matched_workflows": _string_list(plan.get("matched_workflows")),
            "entities_keys": _string_list(plan.get("entities_keys")),
            "effect_refs": [
                {
                    "effect_type": str(item.get("effect_type") or ""),
                    "target_system": str(item.get("target_system") or ""),
                    "payload_keys": _string_list(item.get("payload_keys")),
                    "link_keys": _string_list(item.get("link_keys")),
                    "risk_level": str(item.get("risk_level") or ""),
                    "requires_confirmation": bool(
                        item.get("requires_confirmation", False)
                    ),
                    "reason": str(item.get("reason") or ""),
                }
                for item in (
                    _mapping_or_empty(raw)
                    for raw in _sequence(plan.get("effects"))
                )
            ],
            "safety_decision": {
                "commit_policy": str(safety.get("commit_policy") or ""),
                "flags": _string_list(safety.get("flags")),
                "safe_effects": _string_list(safety.get("safe_effects")),
                "pending_effects": _string_list(safety.get("pending_effects")),
                "blocked_effects": _string_list(safety.get("blocked_effects")),
                "reason": str(safety.get("reason") or ""),
            },
            "segment_refs": [
                {
                    "index": item.get("index"),
                    "text_hash": str(item.get("text_hash") or ""),
                    "text_chars": item.get("text_chars"),
                    "primary_workflow": str(
                        item.get("primary_workflow") or ""
                    ),
                    "matched_workflows": _string_list(
                        item.get("matched_workflows")
                    ),
                    "effect_types": _string_list(item.get("effect_types")),
                    "confidence": item.get("confidence"),
                    "intent": str(item.get("intent") or ""),
                    "reason": str(item.get("reason") or ""),
                }
                for item in (
                    _mapping_or_empty(raw)
                    for raw in _sequence(plan.get("segments"))
                )
            ],
        },
        "gate": {
            "mode": str(gate.get("mode") or ""),
            "allow_legacy_daily": bool(gate.get("allow_legacy_daily", False)),
            "block_legacy_daily": bool(gate.get("block_legacy_daily", False)),
            "reply_type": str(gate.get("reply_type") or ""),
            "safe_effects": _string_list(gate.get("safe_effects")),
            "pending_effects": _string_list(gate.get("pending_effects")),
            "blocked_effects": _string_list(gate.get("blocked_effects")),
            "need_confirmation": bool(gate.get("need_confirmation", False)),
            "need_clarification": bool(gate.get("need_clarification", False)),
            "reason": str(gate.get("reason") or ""),
            "audit_tags": _string_list(gate.get("audit_tags")),
            "has_reply_text": bool(gate.get("has_reply_text", False)),
        },
        "summary": _workflow_summary_audit_projection(summary),
    }


def _legacy_cognitive_audit_projection(decision: Any) -> dict[str, Any]:
    if decision is None:
        return {}
    payload = _as_mapping(decision)
    actions = tuple(
        _mapping_or_empty(item) for item in _sequence(payload.get("actions"))
    )
    return {
        "contract_version": str(payload.get("contract_version") or ""),
        "primary_workflow": str(payload.get("primary_workflow") or ""),
        "matched_workflows": _string_list(payload.get("matched_workflows")),
        "commit_policy": str(payload.get("commit_policy") or ""),
        "allow_write": bool(payload.get("allow_write", False)),
        "need_confirmation": bool(payload.get("need_confirmation", False)),
        "need_clarification": bool(payload.get("need_clarification", False)),
        "gate_reply_type": str(payload.get("gate_reply_type") or ""),
        "confidence": payload.get("confidence"),
        "reason": str(payload.get("reason") or ""),
        "source_text_hash": str(payload.get("source_text_hash") or ""),
        "source_text_chars": payload.get("source_text_chars"),
        "action_count": len(actions),
        "action_refs": [
            {
                "segment_index": item.get("segment_index"),
                "source_text_hash": str(item.get("source_text_hash") or ""),
                "source_text_chars": item.get("source_text_chars"),
                "workflow": str(item.get("workflow") or ""),
                "action_type": str(item.get("action_type") or ""),
                "operation": str(item.get("operation") or ""),
                "target_field": str(item.get("target_field") or ""),
                "write_policy": str(item.get("write_policy") or ""),
                "confidence": item.get("confidence"),
                "requires_confirmation": bool(
                    item.get("requires_confirmation", False)
                ),
                "safety_flags": _string_list(item.get("safety_flags")),
                "reason": str(item.get("reason") or ""),
            }
            for item in actions
        ],
        "segment_count": len(_sequence(payload.get("segments"))),
        "effect_count": len(_sequence(payload.get("effects"))),
        "daily_command_count": len(_sequence(payload.get("daily_commands"))),
        "legacy_adapter_count": len(_sequence(payload.get("legacy_adapter"))),
        "audit_tags": _string_list(payload.get("audit_tags")),
        "warning_count": len(_sequence(payload.get("warnings"))),
    }


def _workflow_summary_audit_projection(summary: Mapping[str, Any]) -> dict[str, Any]:
    scalar_fields = (
        "coordination_action_count",
        "sandbox_candidate_count",
        "sandbox_official_write_count",
        "sandbox_notification_count",
        "command_count",
        "adapter_result_count",
        "adapter_write_impact_count",
        "adapter_read_only_count",
        "adapter_confirmation_count",
        "assistant_reply_type",
    )
    counter_fields = (
        "coordination_action_type_counts",
        "sandbox_candidate_type_counts",
        "command_operation_counts",
        "adapter_status_counts",
    )
    projected = {
        key: summary[key]
        for key in scalar_fields
        if key in summary
        and isinstance(summary[key], (bool, int, float, str))
    }
    for key in counter_fields:
        value = summary.get(key)
        if isinstance(value, Mapping):
            projected[key] = {
                str(item_key): item_value
                for item_key, item_value in value.items()
                if isinstance(item_value, (int, float))
                and not isinstance(item_value, bool)
            }
    return projected


async def create_agent2_cognitive_v3_audit_event(
    *,
    session: AsyncSession,
    user: Any,
    envelope: IncomingMessageEnvelope,
    result: Any,
    report_date: Any,
) -> None:
    """Persist decision -> command-plan correlation without granting execution authority."""

    user_id = getattr(user, "id", None)
    if user_id is None:
        return
    try:
        from app.models import ReportInteractionEvent

        async with session.begin_nested():
            state = result.state
            event = ReportInteractionEvent(
                user_id=user_id,
                report_id=None,
                dingtalk_user_id=str(envelope.dingtalk_user_id or ""),
                report_date=report_date,
                message_text="",
                llm_decision_json={
                    "agent2": True,
                    "audit_stage": "cognitive_core_v3_plan",
                    "source_message_id": str(envelope.message_id or ""),
                    "cognitive_decision": _cognitive_decision_audit_projection(
                        result.decision
                    ),
                    "command_plan": _command_plan_audit_projection(
                        result.command_plan
                    ),
                    "conversation_state": _conversation_state_audit_projection(
                        state=state,
                        base_version=result.base_state.version,
                        state_persisted=result.state_persisted,
                    ),
                },
                backend_action="agent2_cognitive_v3_plan",
                before_snapshot_json={},
                after_snapshot_json={},
                correction_type="",
                correction_from="",
                correction_to="",
                confidence=_decimal_confidence(result.decision.confidence),
                asr_suspect_json={},
            )
            session.add(event)
            await session.flush()
    except Exception as exc:
        print(f"Agent2 cognitive v3 audit skipped: {exc}", flush=True)


def _cognitive_decision_audit_projection(decision: Any) -> dict[str, Any]:
    """Return the closed, non-authoritative audit view of one decision.

    This intentionally does not copy semantic text, entity values, action
    parameters, object references, authority scopes, questions, candidate
    labels, acceptable answers, or continuation payloads.
    """

    payload = _as_mapping(decision)
    clarification = _mapping_or_empty(payload.get("clarification_need"))
    trace = _mapping_or_empty(payload.get("admission_trace"))
    segments = tuple(_mapping_or_empty(item) for item in _sequence(payload.get("segments")))
    entities = tuple(_mapping_or_empty(item) for item in _sequence(payload.get("entities")))
    actions = tuple(
        _mapping_or_empty(item) for item in _sequence(payload.get("required_actions"))
    )
    tickets = tuple(
        _mapping_or_empty(item) for item in _sequence(payload.get("admission_tickets"))
    )
    information_pendings = tuple(
        _mapping_or_empty(item)
        for item in _sequence(payload.get("admission_information_pendings"))
    )
    selection_requests = tuple(
        _mapping_or_empty(item)
        for item in _sequence(payload.get("admission_selection_requests"))
    )
    return {
        "contract_version": str(payload.get("contract_version") or ""),
        "decision_id": str(payload.get("decision_id") or ""),
        "intents": _string_list(payload.get("intents")),
        "confidence": payload.get("confidence"),
        "source_text_hash": str(payload.get("source_text_hash") or ""),
        "admission_mode": str(payload.get("admission_mode") or ""),
        "segment_count": len(segments),
        "segment_refs": [
            {
                "segment_id": str(item.get("segment_id") or ""),
                "text_hash": str(item.get("text_hash") or ""),
                "intents": _string_list(item.get("intents")),
                "action_ids": _string_list(item.get("action_ids")),
            }
            for item in segments
        ],
        "entity_count": len(entities),
        "entity_refs": [
            {
                "entity_id": str(item.get("entity_id") or ""),
                "entity_type": str(item.get("entity_type") or ""),
                "confidence": item.get("confidence"),
                "source_context_id": str(item.get("source_context_id") or ""),
            }
            for item in entities
        ],
        "action_count": len(actions),
        "action_refs": [
            {
                "action_id": str(item.get("action_id") or ""),
                "action_type": str(item.get("action_type") or ""),
                "intent": str(item.get("intent") or ""),
                "entity_ids": _string_list(item.get("entity_ids")),
            }
            for item in actions
        ],
        "clarification": (
            {
                "reason": str(clarification.get("reason") or ""),
                "missing_fields": _string_list(
                    clarification.get("missing_fields")
                ),
            }
            if clarification
            else None
        ),
        "ticket_refs": [
            {
                "ticket_id": str(item.get("ticket_id") or ""),
                "trace_id": str(item.get("trace_id") or ""),
                "decision_id": str(item.get("decision_id") or ""),
                "action_id": str(item.get("action_id") or ""),
                "domain": str(item.get("domain") or ""),
                "operation": str(item.get("operation") or ""),
                "status": str(item.get("ticket_status") or ""),
                "fact_claims_sha256": str(
                    item.get("fact_claims_sha256") or ""
                ),
                "authorized_command_sha256": str(
                    item.get("authorized_command_sha256") or ""
                ),
            }
            for item in tickets
        ],
        "information_pending_refs": [
            {
                "pending_id": str(item.get("pending_id") or ""),
                "decision_id": str(item.get("decision_id") or ""),
                "domain": str(item.get("domain") or ""),
                "operation": str(item.get("operation") or ""),
                "status": str(item.get("pending_status") or ""),
                "missing_field_count": len(_sequence(item.get("missing_fields"))),
            }
            for item in information_pendings
        ],
        "selection_request_refs": [
            {
                "selection_request_id": str(
                    item.get("selection_request_id") or ""
                ),
                "decision_id": str(item.get("decision_id") or ""),
                "action_id": str(item.get("action_id") or ""),
                "domain": str(item.get("domain") or ""),
                "operation": str(item.get("operation") or ""),
                "candidate_count": len(_sequence(item.get("candidates"))),
            }
            for item in selection_requests
        ],
        "admission_trace": (
            {
                "trace_id": str(trace.get("trace_id") or ""),
                "trace_status": str(trace.get("trace_status") or ""),
                "admission_summary": str(trace.get("admission_summary") or ""),
                "failure_reason": str(trace.get("failure_reason") or ""),
                "proposal_sha256": str(trace.get("proposal_sha256") or ""),
            }
            if trace
            else None
        ),
    }


def _command_plan_audit_projection(command_plan: Any) -> dict[str, Any]:
    payload = _as_mapping(command_plan)

    def command_refs(name: str) -> list[dict[str, Any]]:
        return [
            {
                "command_id": str(item.get("command_id") or ""),
                "command_type": str(
                    item.get("command_type") or item.get("operation") or ""
                ),
                "target_system": str(item.get("target_system") or ""),
                "execution_mode": str(item.get("execution_mode") or ""),
                "admission_action_id": str(
                    item.get("admission_action_id") or ""
                ),
                "admission_operation": str(
                    item.get("admission_operation") or ""
                ),
            }
            for item in (
                _mapping_or_empty(raw) for raw in _sequence(payload.get(name))
            )
        ]

    blocked = tuple(
        _mapping_or_empty(item) for item in _sequence(payload.get("blocked_actions"))
    )
    daily_refs = command_refs("daily_commands")
    business_refs = command_refs("business_commands")
    report_refs = command_refs("report_commands")
    return {
        "decision_id": str(payload.get("decision_id") or ""),
        "daily_command_refs": daily_refs,
        "business_command_refs": business_refs,
        "report_command_refs": report_refs,
        "blocked_action_refs": [
            {
                "action_id": str(item.get("action_id") or ""),
                "reason_code": str(item.get("reason_code") or ""),
            }
            for item in blocked
        ],
        "command_count": len(daily_refs) + len(business_refs) + len(report_refs),
        "blocked_action_count": len(blocked),
    }


def _conversation_state_audit_projection(
    *, state: Any, base_version: Any, state_persisted: Any
) -> dict[str, Any]:
    payload = _as_mapping(state)
    constraints = _mapping_or_empty(payload.get("user_constraints"))
    current_goal = getattr(state, "current_goal", None)
    pending = tuple(getattr(state, "pending", ()) or ())
    selection_pending = tuple(getattr(state, "selection_pending", ()) or ())
    return {
        "stage": "persisted" if bool(state_persisted) else "proposed_pending_receipts",
        "user_id": str(getattr(state, "user_id", "") or ""),
        "conversation_id": str(getattr(state, "conversation_id", "") or ""),
        "base_version": base_version,
        "version": getattr(state, "version", None),
        "current_goal": str(getattr(current_goal, "intent", "") or ""),
        "pending_ids": [str(getattr(item, "pending_id", "") or "") for item in pending],
        "selection_pending_refs": [
            {
                "pending_id": str(getattr(item, "pending_id", "") or ""),
                "status": str(getattr(item, "status", "") or ""),
            }
            for item in selection_pending
        ],
        "user_constraints": {
            "no_daily_write": bool(constraints.get("no_daily_write", False)),
            "read_only": bool(constraints.get("read_only", False)),
            "no_history_mutation": bool(
                constraints.get("no_history_mutation", False)
            ),
            "draft_only": bool(constraints.get("draft_only", False)),
            "source_count": len(_sequence(constraints.get("sources"))),
        },
    }


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        payload = as_dict()
        if isinstance(payload, Mapping):
            return dict(payload)
    as_payload = getattr(value, "as_payload", None)
    if callable(as_payload):
        payload = as_payload()
        if isinstance(payload, Mapping):
            return dict(payload)
    return {}


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in _sequence(value)]


def _numeric_mapping(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): float(item)
        for key, item in value.items()
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    }


async def create_agent2_selection_audit_event(
    *,
    session: AsyncSession,
    user: Any,
    envelope: IncomingMessageEnvelope,
    turn_result: Any,
    report_date: Any,
) -> None:
    """Persist every selection resolution, including all zero-write outcomes."""
    user_id = getattr(user, "id", None)
    if user_id is None:
        return
    try:
        from app.models import ReportInteractionEvent

        resolution = turn_result.resolution
        outcome = turn_result.outcome
        receipt_refs = _outcome_receipt_audit_projection(outcome)
        async with session.begin_nested():
            event = ReportInteractionEvent(
                user_id=user_id,
                report_id=None,
                dingtalk_user_id=str(envelope.dingtalk_user_id or ""),
                report_date=report_date,
                message_text="",
                llm_decision_json={
                    "agent2": True,
                    "audit_stage": "selection_pending_resolution",
                    "source_message_id": str(envelope.message_id or ""),
                    "source_text_sha256": _sha256_text(envelope.raw_text),
                    "pending_id": resolution.pending_id,
                    "resolution_status": resolution.status,
                    "selected_candidate_id": resolution.selected_candidate_id,
                    "selected_candidate_version": getattr(
                        resolution, "selected_candidate_version", None
                    ),
                    "reason": resolution.reason,
                    "actual_write": bool(turn_result.actual_write),
                    "receipt_refs": receipt_refs,
                    "pending_after": _selection_pending_audit_projection(
                        resolution.pending_after
                    ),
                },
                backend_action="agent2_selection_pending_resolution",
                before_snapshot_json={},
                after_snapshot_json=(
                    {"outcome": _outcome_audit_projection(outcome)}
                    if outcome is not None
                    else {}
                ),
                correction_type="",
                correction_from="",
                correction_to="",
                confidence=None,
                asr_suspect_json={},
            )
            session.add(event)
            await session.flush()
    except Exception as exc:
        # Audit failure must not mutate the already receipt-backed business
        # outcome; the enclosing transaction can still fail closed upstream.
        print(f"Agent2 selection audit skipped: {exc}", flush=True)


def _selection_pending_audit_projection(pending: Any) -> dict[str, Any]:
    payload = _as_mapping(pending)
    candidates = getattr(pending, "candidates", None)
    candidate_count = (
        len(tuple(candidates or ()))
        if candidates is not None
        else len(_sequence(payload.get("candidates")))
    )
    return {
        "pending_id": str(
            getattr(pending, "pending_id", payload.get("pending_id")) or ""
        ),
        "status": str(getattr(pending, "status", payload.get("status")) or ""),
        "expected_conversation_state_version": getattr(
            pending,
            "expected_conversation_state_version",
            payload.get("expected_conversation_state_version"),
        ),
        "candidate_count": candidate_count,
        "consumed_receipt_id": str(
            getattr(
                pending,
                "consumed_receipt_id",
                payload.get("consumed_receipt_id"),
            )
            or ""
        ),
        "invalidation_reason": str(
            getattr(
                pending,
                "invalidation_reason",
                payload.get("invalidation_reason"),
            )
            or ""
        ),
    }


def _outcome_receipt_audit_projection(outcome: Any) -> list[dict[str, Any]]:
    if outcome is None:
        return []
    payload = _as_mapping(outcome)
    return [
        {
            "receipt_id": str(item.get("receipt_id") or ""),
            "receipt_type": str(item.get("receipt_type") or ""),
            "status": str(item.get("status") or ""),
            "actual_write": bool(item.get("actual_write", False)),
            "reliable_delivery_evidence": bool(
                item.get("reliable_delivery_evidence", False)
            ),
        }
        for item in (
            _mapping_or_empty(raw)
            for raw in _sequence(payload.get("receipt_refs"))
        )
    ]


def _outcome_audit_projection(outcome: Any) -> dict[str, Any]:
    payload = _as_mapping(outcome)
    object_ref = _mapping_or_empty(payload.get("object_ref"))
    transition = _mapping_or_empty(payload.get("state_transition"))
    return {
        "domain": str(payload.get("domain") or ""),
        "operation": str(payload.get("operation") or ""),
        "object_type": str(object_ref.get("object_type") or ""),
        "object_id": str(object_ref.get("stable_id") or ""),
        "business_status": str(payload.get("business_status") or ""),
        "message_status": str(payload.get("message_status") or ""),
        "changed_fields": _string_list(payload.get("changed_fields")),
        "blocking_reason": str(payload.get("blocking_reason") or ""),
        "actual_write": bool(payload.get("actual_write", False)),
        "would_write": bool(payload.get("would_write", False)),
        "state_transition": {
            "from": str(transition.get("from") or ""),
            "to": str(transition.get("to") or ""),
        },
        "receipt_refs": _outcome_receipt_audit_projection(outcome),
    }


async def persist_selection_settlement_audit(
    *,
    session: AsyncSession,
    audit: Any,
    outcome: Any,
    business_context: Any,
) -> str:
    """Persist receipt-linked Selection settlement in the caller transaction.

    Unlike the compatibility audit helpers, this function is deliberately not
    best-effort: an audit flush failure must prevent the Pending CAS and the
    surrounding business transaction from being committed as a completed
    Selection turn.
    """

    from app.models import ReportInteractionEvent

    receipt_refs = _outcome_receipt_audit_projection(outcome)
    event = ReportInteractionEvent(
        user_id=business_context.actor_user_id,
        report_id=None,
        dingtalk_user_id="",
        report_date=business_context.occurred_at.date(),
        message_text="",
        llm_decision_json={
            "agent2": True,
            "audit_stage": "selection_pending_settlement",
            "pending_id": audit.pending_id,
            "tenant_id": audit.tenant_id,
            "user_id": audit.user_id,
            "conversation_id": audit.conversation_id,
            "source_turn_id": audit.source_turn_id,
            "selected_candidate_id": audit.selected_candidate_id,
            "result": audit.result,
            "reason": audit.reason,
            "receipt_refs": receipt_refs,
            "occurred_at": audit.occurred_at.isoformat(),
        },
        backend_action="agent2_selection_pending_settlement",
        before_snapshot_json={},
        after_snapshot_json={
            "business_status": outcome.business_status,
            "actual_write": outcome.actual_write,
            "receipt_refs": receipt_refs,
        },
        correction_type="",
        correction_from="",
        correction_to="",
        confidence=None,
        asr_suspect_json={},
    )
    session.add(event)
    await session.flush()
    return str(event.id)


async def persist_selection_preprocess_audit(
    *,
    session: AsyncSession,
    continuation: Any,
    business_context: Any,
    source_message_id: str,
    source_text: str,
    occurred_at: Any,
) -> str:
    """Persist one non-authoritative Selection preprocessing observation."""

    from app.models import ReportInteractionEvent

    event_id = _selection_preprocess_audit_id(
        business_context=business_context,
        source_message_id=source_message_id,
    )
    existing = await session.execute(
        select(ReportInteractionEvent.id)
        .where(ReportInteractionEvent.id == event_id)
        .limit(1)
    )
    existing_id = existing.scalar_one_or_none()
    if existing_id is not None:
        return str(existing_id)
    pending = getattr(continuation, "pending", None)
    resolution = getattr(continuation, "resolution", None)
    event = ReportInteractionEvent(
        id=event_id,
        user_id=business_context.actor_user_id,
        report_id=None,
        dingtalk_user_id="",
        report_date=occurred_at.date(),
        message_text="",
        llm_decision_json={
            "agent2": True,
            "audit_stage": "selection_preprocess",
            "tenant_id": business_context.tenant_id,
            "user_id": business_context.actor_user_id,
            "conversation_id": business_context.conversation_id,
            "source_message_id": source_message_id,
            "source_text_sha256": _sha256_text(source_text),
            "selection_status": str(
                getattr(continuation, "status", "") or ""
            ),
            "reason": str(getattr(continuation, "reason", "") or ""),
            "handled": bool(getattr(continuation, "handled", False)),
            "actual_write": False,
            "pending": (
                _selection_pending_audit_projection(pending)
                if pending is not None
                else None
            ),
            "resolution": (
                {
                    "status": str(getattr(resolution, "status", "") or ""),
                    "reason": str(getattr(resolution, "reason", "") or ""),
                    "selected_candidate_id": str(
                        getattr(resolution, "selected_candidate_id", "") or ""
                    ),
                    "selected_candidate_version": getattr(
                        resolution, "selected_candidate_version", None
                    ),
                    "actual_write": False,
                }
                if resolution is not None
                else None
            ),
        },
        backend_action="agent2_selection_preprocess",
        before_snapshot_json={},
        after_snapshot_json={
            "selection_status": str(
                getattr(continuation, "status", "") or ""
            ),
            "actual_write": False,
        },
        correction_type="",
        correction_from="",
        correction_to="",
        confidence=None,
        asr_suspect_json={},
    )
    session.add(event)
    await session.flush()
    return str(getattr(event, "id", "") or "")


async def persist_pending_context_conflict_audit(
    *,
    session: AsyncSession,
    selection_continuation: Any,
    information_continuation: Any,
    business_context: Any,
    source_message_id: str,
    source_text: str,
    occurred_at: Any,
) -> str:
    """Persist one explicit zero-write conflict across Pending protocols."""

    from app.models import ReportInteractionEvent

    event_id = _selection_preprocess_audit_id(
        business_context=business_context,
        source_message_id=source_message_id,
    )
    existing = await session.execute(
        select(ReportInteractionEvent.id)
        .where(ReportInteractionEvent.id == event_id)
        .limit(1)
    )
    existing_id = existing.scalar_one_or_none()
    if existing_id is not None:
        return str(existing_id)
    selection_pending = getattr(selection_continuation, "pending", None)
    information_pending = getattr(information_continuation, "pending", None)
    event = ReportInteractionEvent(
        id=event_id,
        user_id=business_context.actor_user_id,
        report_id=None,
        dingtalk_user_id="",
        report_date=occurred_at.date(),
        message_text="",
        llm_decision_json={
            "agent2": True,
            "audit_stage": "pending_context_conflict",
            "tenant_id": business_context.tenant_id,
            "user_id": business_context.actor_user_id,
            "conversation_id": business_context.conversation_id,
            "source_message_id": source_message_id,
            "source_text_sha256": _sha256_text(source_text),
            "selection_status": "pending_context_not_unique",
            "reason": "information_and_selection_pending_matched",
            "selection_pending_id": str(
                getattr(selection_pending, "pending_id", "") or ""
            ),
            "selection_pending_status": str(
                getattr(selection_pending, "status", "") or ""
            ),
            "selection_candidate_count": len(
                tuple(getattr(selection_pending, "candidates", ()) or ())
            ),
            "information_pending_id": str(
                getattr(information_pending, "pending_id", "") or ""
            ),
            "information_pending_status": str(
                getattr(information_pending, "pending_status", "") or ""
            ),
            "matched_pending_protocol_count": 2,
            "actual_write": False,
        },
        backend_action="agent2_pending_context_conflict",
        before_snapshot_json={},
        after_snapshot_json={"actual_write": False},
        correction_type="",
        correction_from="",
        correction_to="",
        confidence=None,
        asr_suspect_json={},
    )
    session.add(event)
    await session.flush()
    return str(getattr(event, "id", "") or "")


async def persist_selection_terminal_audit(
    *,
    session: AsyncSession,
    pending: Any,
    resolution: Any,
    business_context: Any,
    source_message_id: str,
    occurred_at: Any,
) -> str:
    """Persist a receipt-free terminal invalidation before its state CAS."""

    from app.models import ReportInteractionEvent

    pending_after = resolution.pending_after
    event = ReportInteractionEvent(
        user_id=business_context.actor_user_id,
        report_id=None,
        dingtalk_user_id="",
        report_date=occurred_at.date(),
        message_text="",
        llm_decision_json={
            "agent2": True,
            "audit_stage": "selection_pending_terminal_resolution",
            "pending_id": pending.pending_id,
            "tenant_id": pending.tenant_id,
            "user_id": pending.user_id,
            "conversation_id": pending.conversation_id,
            "source_turn_id": source_message_id,
            "resolution_status": resolution.status,
            "reason": resolution.reason,
            "pending_status_after": pending_after.status,
            "invalidation_reason": pending_after.invalidation_reason,
            "actual_write": False,
            "occurred_at": occurred_at.isoformat(),
        },
        backend_action="agent2_selection_pending_terminal_resolution",
        before_snapshot_json={"pending_status": pending.status},
        after_snapshot_json={
            "pending_status": pending_after.status,
            "invalidation_reason": pending_after.invalidation_reason,
        },
        correction_type="",
        correction_from="",
        correction_to="",
        confidence=None,
        asr_suspect_json={},
    )
    session.add(event)
    await session.flush()
    return str(event.id)


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _selection_preprocess_audit_id(
    *, business_context: Any, source_message_id: str
):
    claims = json.dumps(
        {
            "tenant_id": str(business_context.tenant_id or ""),
            "user_id": str(business_context.actor_user_id or ""),
            "conversation_id": str(business_context.conversation_id or ""),
            "source_message_id": str(source_message_id or ""),
            "audit_protocol": "selection_preprocess_v1",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, claims)


def _decimal_confidence(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(round(float(value), 4)))
    except (TypeError, ValueError):
        return None
