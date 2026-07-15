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
from app.agent2.command_planner_v3 import TypedBusinessCommand
from app.agent2.conversation_state import ConversationEntity, ConversationState
from app.agent2.domain_admission import DomainAdmissionResult
from app.agent2.selection_continuation import (
    bind_selected_business_command,
    selected_business_command_snapshot,
)
from app.agent2.selection_pending_runtime import (
    SelectionContinuationPreprocessResult,
)


class SelectionContinuationAdmissionError(ValueError):
    pass


class SelectionContinuationSemanticInterpreter:
    """Build one deterministic proposal from a validated Selection Pending."""

    def __init__(self, continuation: SelectionContinuationPreprocessResult) -> None:
        self._continuation = continuation

    async def interpret(
        self,
        turn: CognitiveTurn,
        state: ConversationState,
    ) -> SemanticInterpretation:
        return _proposal(turn, state, self._continuation)


class SelectionContinuationAdmissionEngine:
    """Mint fresh authority for the current exact selection answer.

    The current ordinal/label is evidence of object selection only.  The case
    fact remains the protected source segment captured by the original turn.
    """

    _TICKET_TTL = timedelta(minutes=5)

    def __init__(self, continuation: SelectionContinuationPreprocessResult) -> None:
        self._continuation = continuation

    def admit(
        self,
        turn: CognitiveTurn,
        state: ConversationState,
        proposal: SemanticInterpretation,
    ) -> DomainAdmissionResult:
        pending, candidate, fresh, snapshot, action, segment = _validate(
            turn,
            state,
            proposal,
            self._continuation,
        )
        contract = _command_contract(fresh.command_type)
        if contract is None:
            raise SelectionContinuationAdmissionError(
                "unsupported selection continuation contract"
            )
        domain, operation, object_type, allowed_changed_fields = contract
        created_at = turn.occurred_at
        proposal_hash = _sha256_json(_proposal_payload(proposal))
        trace_id = _stable_uuid(
            "selection-continuation-trace",
            pending.pending_id,
            turn.tenant_id,
            turn.actor_user_id,
            turn.conversation_id,
            turn.message_id,
            str(state.version),
            proposal_hash,
            ADMISSION_POLICY_VERSION,
        )
        decision_id = _stable_uuid(
            "selection-continuation-decision",
            trace_id,
            action.action_id,
            segment.segment_id,
            pending.pending_id,
            candidate.stable_id,
            str(candidate.version),
        )
        evidence = fresh.evidence.as_dict(segment_id=segment.segment_id)
        final_claims = {
            "command_type": fresh.command_type,
            "target_system": str(snapshot.raw_command.get("target_system") or ""),
            "entity_type": str(snapshot.entity.get("entity_type") or ""),
            "bound_payload_sha256": snapshot.bound_payload_sha256,
            "original_continuation_sha256": snapshot.original_continuation_sha256,
        }
        authority_scope = {
            "selection_pending_id": pending.pending_id,
            "selection_pending_source_turn_id": pending.source_turn_id,
            "original_source_digest": snapshot.original_source_digest,
            "original_continuation_sha256": snapshot.original_continuation_sha256,
            "candidate_stable_id": candidate.stable_id,
            "candidate_version": candidate.version,
            "selection_evidence": evidence,
            "final_command_claims": final_claims,
            # Domain claims consumed by the existing case compiler.  They are
            # derived from the protected command, not from the ordinal answer.
            "raw_fact": str(snapshot.source_segment.get("text") or ""),
            "case_reference": candidate.stable_id,
            "attributes": dict(snapshot.entity.get("attributes") or {}),
        }
        object_ref = {
            "object_type": object_type,
            "stable_id": candidate.stable_id,
            "version": candidate.version,
        }
        fact_hash, command_hash = compute_admission_claim_hashes(
            action_id=action.action_id,
            operation=operation,
            segment_text_sha256=segment.text_hash,
            domain=domain,
            object_ref=object_ref,
            authority_scope=authority_scope,
            allowed_changed_fields=allowed_changed_fields,
        )
        ticket_id = _stable_uuid(
            "selection-continuation-ticket",
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
            domain=domain,
            operation=operation,
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
                "selection-continuation-ticket",
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
            domain=domain,
            operation=operation,
            object_ref=object_ref,
            expected_conversation_state_version=state.version,
            status="admitted",
            reason_code="selection_pending_fresh_admission_verified",
            evidence_refs=(
                f"segment_sha256:{segment.text_hash}",
                f"selection_pending:{pending.pending_id}",
                f"original_source_sha256:{snapshot.original_source_digest}",
            ),
            ticket_id=ticket_id,
            idempotency_key=_stable_digest(
                "selection-continuation-decision",
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
                "selection-continuation-trace",
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


def bind_fresh_selected_business_command(
    continuation: SelectionContinuationPreprocessResult,
    ticket: AdmissionTicket,
) -> TypedBusinessCommand:
    if (
        continuation.status != "ready_for_fresh_admission"
        or continuation.pending is None
        or continuation.candidate is None
        or continuation.fresh_admission_request is None
    ):
        raise SelectionContinuationAdmissionError("selection continuation is not ready")
    evidence = continuation.fresh_admission_request.evidence.as_dict(
        segment_id=ticket.segment_id
    )
    try:
        return bind_selected_business_command(
            continuation.pending,
            continuation.candidate,
            admission_ticket=ticket.as_dict(),
            selection_evidence=evidence,
        )
    except (TypeError, ValueError) as exc:
        raise SelectionContinuationAdmissionError(
            "fresh selection command binding failed"
        ) from exc


def _proposal(
    turn: CognitiveTurn,
    state: ConversationState,
    continuation: SelectionContinuationPreprocessResult,
) -> SemanticInterpretation:
    del state
    pending = continuation.pending
    candidate = continuation.candidate
    fresh = continuation.fresh_admission_request
    if (
        continuation.status != "ready_for_fresh_admission"
        or pending is None
        or candidate is None
        or fresh is None
    ):
        raise SelectionContinuationAdmissionError("selection continuation is not ready")
    contract = _command_contract(fresh.command_type)
    if contract is None:
        raise SelectionContinuationAdmissionError(
            "unsupported selection continuation contract"
        )
    _, operation, _, _ = contract
    action_id = _stable_uuid(
        "selection-continuation-action",
        pending.pending_id,
        turn.message_id,
        candidate.stable_id,
        str(candidate.version),
    )
    entity_id = _stable_uuid(
        "selection-continuation-entity",
        pending.pending_id,
        turn.message_id,
        candidate.stable_id,
    )
    segment_id = _stable_uuid(
        "selection-continuation-segment",
        pending.pending_id,
        turn.message_id,
        fresh.evidence.text_sha256,
    )
    entity = ConversationEntity(
        entity_id=entity_id,
        entity_type="selection_ref",
        value=candidate.stable_id,
        confidence=1.0,
        attributes={
            "selection_pending_id": pending.pending_id,
            "candidate_version": candidate.version,
            "original_source_digest": fresh.original_source_digest,
        },
        source_context_id=pending.source_turn_id,
    )
    segment = SemanticSegment(
        segment_id=segment_id,
        text=fresh.evidence.text,
        text_hash=fresh.evidence.text_sha256,
        intents=("case_progress",),
        entity_ids=(entity_id,),
        action_ids=(action_id,),
        start_offset=fresh.evidence.start_offset,
        end_offset=fresh.evidence.end_offset,
    )
    return SemanticInterpretation(
        intents=("case_progress",),
        segments=(segment,),
        entities=(entity,),
        confidence=1.0,
        required_actions=(
            RequiredAction(
                action_id=action_id,
                action_type=operation,
                intent="case_progress",
                entity_ids=(entity_id,),
                parameters={
                    "selection_pending_id": pending.pending_id,
                    "candidate_version": candidate.version,
                },
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
    continuation: SelectionContinuationPreprocessResult,
):
    pending = continuation.pending
    candidate = continuation.candidate
    fresh = continuation.fresh_admission_request
    if (
        continuation.status != "ready_for_fresh_admission"
        or pending is None
        or candidate is None
        or fresh is None
    ):
        raise SelectionContinuationAdmissionError("selection continuation is not ready")
    if (
        turn.tenant_id != pending.tenant_id
        or turn.actor_user_id != pending.user_id
        or turn.conversation_id != pending.conversation_id
        or turn.message_id != fresh.source_message_id
        or state.version != pending.expected_conversation_state_version
        or pending.status != "active"
        or turn.occurred_at >= pending.expires_at
    ):
        raise SelectionContinuationAdmissionError("selection trusted scope changed")
    evidence = fresh.evidence
    if (
        evidence.start_offset < 0
        or evidence.end_offset <= evidence.start_offset
        or evidence.end_offset > len(turn.text)
        or turn.text[evidence.start_offset : evidence.end_offset] != evidence.text
        or hashlib.sha256(evidence.text.encode("utf-8")).hexdigest()
        != evidence.text_sha256
    ):
        raise SelectionContinuationAdmissionError("selection evidence is not grounded")
    current_candidate = next(
        (
            item
            for item in pending.candidates
            if item.stable_id == fresh.candidate_stable_id
            and item.version == fresh.candidate_version
        ),
        None,
    )
    if current_candidate != candidate:
        raise SelectionContinuationAdmissionError("selection candidate snapshot changed")
    try:
        snapshot = selected_business_command_snapshot(pending, candidate)
    except (TypeError, ValueError) as exc:
        raise SelectionContinuationAdmissionError(
            "selection protected continuation changed"
        ) from exc
    if (
        snapshot.original_source_digest != fresh.original_source_digest
        or snapshot.original_continuation_sha256
        != fresh.original_continuation_sha256
        or snapshot.bound_payload_sha256 != fresh.bound_payload_sha256
        or str(snapshot.raw_command.get("command_type") or "") != fresh.command_type
    ):
        raise SelectionContinuationAdmissionError(
            "selection protected continuation changed"
        )
    expected = _proposal(turn, state, continuation)
    if _proposal_payload(proposal) != _proposal_payload(expected):
        raise SelectionContinuationAdmissionError("selection proposal was modified")
    return (
        pending,
        candidate,
        fresh,
        snapshot,
        proposal.required_actions[0],
        proposal.segments[0],
    )


def _command_contract(
    command_type: str,
) -> tuple[str, str, str, tuple[str, ...]] | None:
    # Phase 1 intentionally enables only the proven case-progress selection
    # slice.  Follow-up candidates currently lack policy-version authority and
    # remain fail-closed until their creation contract is corrected.
    if command_type == "record_case_progress_candidate":
        return (
            "case",
            "record_case_progress",
            "case",
            (
                "summary",
                "details",
                "progress_type",
                "current_status",
                "next_actions",
                "hearing_readiness",
                "blocking_issues",
            ),
        )
    return None


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


def _sha256_json(value: Mapping[str, Any] | dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

