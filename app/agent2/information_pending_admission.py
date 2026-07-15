from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from app.agent2.admission_contracts import (
    ADMISSION_CONTRACT_VERSION,
    ADMISSION_POLICY_VERSION,
    AdmissionDecision,
    AdmissionTicket,
    AdmissionTrace,
)
from app.agent2.admission_hashes import compute_admission_claim_hashes
from app.agent2.cognitive_core_v3 import (
    CognitiveTurn,
    RequiredAction,
    SemanticContextUpdate,
    SemanticInterpretation,
    SemanticSegment,
)
from app.agent2.conversation_state import ConversationEntity, ConversationState
from app.agent2.domain_admission import DomainAdmissionResult
from app.agent2.information_pending_runtime import (
    InformationContinuationPreprocessResult,
)


class InformationContinuationAdmissionError(ValueError):
    pass


class InformationContinuationSemanticInterpreter:
    """Deterministic proposal builder; it never chooses an object or DB id."""

    def __init__(self, continuation: InformationContinuationPreprocessResult) -> None:
        self._continuation = continuation

    async def interpret(
        self,
        turn: CognitiveTurn,
        state: ConversationState,
    ) -> SemanticInterpretation:
        return _proposal(turn, state, self._continuation)


class InformationContinuationAdmissionEngine:
    """Fresh Domain Admission for an exact answer bound to trusted Pending.

    The current answer remains the only current-turn source segment.  The old
    destination and preallocated object id are read from the authoritative
    Pending and represented as cross-turn authority claims, never as invented
    current-turn text.
    """

    _TICKET_TTL = timedelta(minutes=5)

    def __init__(self, continuation: InformationContinuationPreprocessResult) -> None:
        self._continuation = continuation

    def admit(
        self,
        turn: CognitiveTurn,
        state: ConversationState,
        proposal: SemanticInterpretation,
    ) -> DomainAdmissionResult:
        pending, fresh, action, entity, segment = _validate(
            turn,
            state,
            proposal,
            self._continuation,
        )
        created_at = turn.occurred_at
        proposal_hash = _sha256_json(_proposal_payload(proposal))
        trace_id = _stable_uuid(
            "information-continuation-trace",
            pending.pending_id,
            turn.tenant_id,
            turn.actor_user_id,
            turn.conversation_id,
            turn.message_id,
            str(state.version),
            proposal_hash,
            ADMISSION_POLICY_VERSION,
        )
        object_ref = dict(pending.object_ref or {})
        destination = str(pending.question_snapshot.get("destination") or "").strip()
        travel_date = str(fresh.field_values.get("travel_date") or "").strip()
        purpose = str(pending.question_snapshot.get("purpose") or "").strip()
        authority_scope = {
            "destination": destination,
            "travel_date": travel_date,
            "raw_fact": segment.text,
            "purpose": purpose,
            "information_pending_id": pending.pending_id,
            "information_pending_trace_id": pending.trace_id,
            "continuation_source_message_id": turn.message_id,
            "continuation_field_values": dict(fresh.field_values),
            "continuation_raw_values": dict(fresh.raw_values),
            "continuation_evidence_spans": {
                key: list(value) for key, value in fresh.evidence_spans.items()
            },
        }
        allowed_changed_fields = (
            "destination",
            "start_at",
            "end_at",
            "purpose_summary",
        )
        fact_hash, command_hash = compute_admission_claim_hashes(
            action_id=action.action_id,
            operation=action.action_type,
            segment_text_sha256=segment.text_hash,
            domain="travel",
            object_ref=object_ref,
            authority_scope=authority_scope,
            allowed_changed_fields=allowed_changed_fields,
        )
        decision_id = _stable_uuid(
            "information-continuation-decision",
            trace_id,
            action.action_id,
            segment.segment_id,
            segment.text_hash,
            pending.pending_id,
        )
        ticket_id = _stable_uuid(
            "information-continuation-ticket",
            trace_id,
            decision_id,
            pending.pending_id,
            fact_hash,
            command_hash,
        )
        ticket = AdmissionTicket(
            ticket_id=ticket_id,
            trace_id=trace_id,
            decision_id=decision_id,
            tenant_id=turn.tenant_id,
            user_id=turn.actor_user_id,
            conversation_id=turn.conversation_id,
            source_turn_id=turn.message_id,
            source_message_id=turn.message_id,
            action_id=action.action_id,
            segment_id=segment.segment_id,
            segment_text_sha256=segment.text_hash,
            segment_start_offset=segment.start_offset,
            segment_end_offset=segment.end_offset,
            domain="travel",
            operation="record_travel_event",
            object_ref=object_ref,
            expected_conversation_state_version=state.version,
            authority_scope=authority_scope,
            allowed_changed_fields=allowed_changed_fields,
            fact_claims_sha256=fact_hash,
            authorized_command_sha256=command_hash,
            contract_version=ADMISSION_CONTRACT_VERSION,
            policy_version=ADMISSION_POLICY_VERSION,
            issued_at=created_at,
            expires_at=created_at + self._TICKET_TTL,
            executor_revalidation_required=True,
            proves_business_write=False,
            idempotency_key=_stable_digest(
                "information-continuation-ticket",
                turn.tenant_id,
                pending.pending_id,
                turn.message_id,
                command_hash,
            ),
        )
        decision = AdmissionDecision(
            decision_id=decision_id,
            trace_id=trace_id,
            tenant_id=turn.tenant_id,
            user_id=turn.actor_user_id,
            conversation_id=turn.conversation_id,
            source_turn_id=turn.message_id,
            source_message_id=turn.message_id,
            action_id=action.action_id,
            segment_id=segment.segment_id,
            segment_text_sha256=segment.text_hash,
            segment_start_offset=segment.start_offset,
            segment_end_offset=segment.end_offset,
            domain="travel",
            operation="record_travel_event",
            object_ref=object_ref,
            expected_conversation_state_version=state.version,
            status="admitted",
            reason_code="information_pending_fresh_admission_verified",
            evidence_refs=(
                f"segment_sha256:{segment.text_hash}",
                f"information_pending:{pending.pending_id}",
            ),
            ticket_id=ticket_id,
            idempotency_key=_stable_digest(
                "information-continuation-decision",
                turn.tenant_id,
                decision_id,
            ),
            created_at=created_at,
        )
        trace = AdmissionTrace(
            trace_id=trace_id,
            tenant_id=turn.tenant_id,
            user_id=turn.actor_user_id,
            conversation_id=turn.conversation_id,
            source_turn_id=turn.message_id,
            source_message_id=turn.message_id,
            expected_conversation_state_version=state.version,
            proposal_sha256=proposal_hash,
            contract_version=ADMISSION_CONTRACT_VERSION,
            policy_version=ADMISSION_POLICY_VERSION,
            decisions=(decision,),
            trace_status="evaluated",
            admission_summary="admitted",
            idempotency_key=_stable_digest(
                "information-continuation-trace",
                turn.tenant_id,
                trace_id,
            ),
            created_at=created_at,
        )
        return DomainAdmissionResult(
            interpretation=proposal,
            decisions=(decision,),
            tickets=(ticket,),
            information_pendings=(),
            trace=trace,
        )


def _proposal(
    turn: CognitiveTurn,
    state: ConversationState,
    continuation: InformationContinuationPreprocessResult,
) -> SemanticInterpretation:
    pending = continuation.pending
    fresh = continuation.fresh_admission_request
    if pending is None or fresh is None:
        raise InformationContinuationAdmissionError("ready continuation contract missing")
    destination = str(pending.question_snapshot.get("destination") or "").strip()
    travel_date = str(fresh.field_values.get("travel_date") or "").strip()
    purpose = str(pending.question_snapshot.get("purpose") or "").strip()
    action_id = _stable_uuid(
        "information-continuation-action",
        pending.pending_id,
        turn.message_id,
    )
    entity_id = _stable_uuid(
        "information-continuation-entity",
        pending.pending_id,
        turn.message_id,
    )
    segment_id = _stable_uuid(
        "information-continuation-segment",
        pending.pending_id,
        turn.message_id,
    )
    entity = ConversationEntity(
        entity_id=entity_id,
        entity_type="travel_event",
        value=destination,
        confidence=1.0,
        attributes={
            "destination": destination,
            "date_hint": travel_date,
            "purpose": purpose,
            "statement_mode": "asserted",
            "traveler_scope": "self",
            "evidence_spans": [
                list(span) for span in fresh.evidence_spans.values()
            ],
            "information_pending_id": pending.pending_id,
        },
        source_context_id=pending.decision_id,
    )
    segment = SemanticSegment(
        segment_id=segment_id,
        text=turn.text,
        text_hash=hashlib.sha256(turn.text.encode("utf-8")).hexdigest(),
        intents=("travel",),
        entity_ids=(entity_id,),
        action_ids=(action_id,),
        start_offset=0,
        end_offset=len(turn.text),
    )
    return SemanticInterpretation(
        intents=("travel",),
        segments=(segment,),
        entities=(entity,),
        confidence=1.0,
        required_actions=(
            RequiredAction(
                action_id=action_id,
                action_type="record_travel_event",
                intent="travel",
                entity_ids=(entity_id,),
                parameters={"information_pending_id": pending.pending_id},
            ),
        ),
        clarification_need=None,
        context_update=SemanticContextUpdate(
            preserve_current_goal=True,
            remember_turn=True,
            remember_entity_ids=(entity_id,),
        ),
    )


def _validate(
    turn: CognitiveTurn,
    state: ConversationState,
    proposal: SemanticInterpretation,
    continuation: InformationContinuationPreprocessResult,
):
    pending = continuation.pending
    fresh = continuation.fresh_admission_request
    if continuation.status != "ready_for_fresh_admission" or pending is None or fresh is None:
        raise InformationContinuationAdmissionError("continuation is not ready")
    if (
        turn.tenant_id != pending.tenant_id
        or turn.actor_user_id != pending.user_id
        or turn.conversation_id != pending.conversation_id
        or turn.message_id != fresh.source_message_id
        or state.version != pending.expected_conversation_state_version
        or pending.pending_status not in {"active", "awaiting_input"}
        or turn.occurred_at >= pending.expires_at
    ):
        raise InformationContinuationAdmissionError("continuation trusted scope changed")
    if (
        pending.domain != "travel"
        or pending.operation != "record_travel_event"
        or pending.missing_fields != ("travel_date",)
        or fresh.domain != pending.domain
        or fresh.operation != pending.operation
        or dict(fresh.object_ref or {}) != dict(pending.object_ref or {})
        or set(fresh.field_values) != {"travel_date"}
        or set(fresh.raw_values) != {"travel_date"}
        or set(fresh.evidence_spans) != {"travel_date"}
    ):
        raise InformationContinuationAdmissionError("continuation operation contract changed")
    start, end = fresh.evidence_spans["travel_date"]
    if (
        start < 0
        or end <= start
        or end > len(turn.text)
        or turn.text[start:end] != fresh.raw_values["travel_date"]
    ):
        raise InformationContinuationAdmissionError("continuation answer is not grounded")
    if not str(pending.question_snapshot.get("destination") or "").strip():
        raise InformationContinuationAdmissionError("pending destination snapshot missing")
    if len(proposal.required_actions) != 1 or len(proposal.entities) != 1 or len(proposal.segments) != 1:
        raise InformationContinuationAdmissionError("continuation proposal shape changed")
    action = proposal.required_actions[0]
    entity = proposal.entities[0]
    segment = proposal.segments[0]
    expected = _proposal(turn, state, continuation)
    if _proposal_payload(proposal) != _proposal_payload(expected):
        raise InformationContinuationAdmissionError("continuation proposal was modified")
    return pending, fresh, action, entity, segment


def _proposal_payload(proposal: SemanticInterpretation) -> dict[str, Any]:
    return {
        "intents": list(proposal.intents),
        "segments": [
            {
                "segment_id": item.segment_id,
                "text": item.text,
                "text_hash": item.text_hash,
                "intents": list(item.intents),
                "entity_ids": list(item.entity_ids),
                "action_ids": list(item.action_ids),
                "start_offset": item.start_offset,
                "end_offset": item.end_offset,
            }
            for item in proposal.segments
        ],
        "entities": [
            {
                "entity_id": item.entity_id,
                "entity_type": item.entity_type,
                "value": item.value,
                "confidence": item.confidence,
                "attributes": dict(item.attributes),
                "source_context_id": item.source_context_id,
            }
            for item in proposal.entities
        ],
        "actions": [
            {
                "action_id": item.action_id,
                "action_type": item.action_type,
                "intent": item.intent,
                "entity_ids": list(item.entity_ids),
                "parameters": dict(item.parameters),
            }
            for item in proposal.required_actions
        ],
    }


def _stable_uuid(namespace: str, *parts: Any) -> str:
    material = "\x1f".join((namespace, *(str(part) for part in parts)))
    return str(uuid5(NAMESPACE_URL, f"agent2-domain-admission:{material}"))


def _stable_digest(namespace: str, *parts: Any) -> str:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{namespace}-{hashlib.sha256(material).hexdigest()}"


def _sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
