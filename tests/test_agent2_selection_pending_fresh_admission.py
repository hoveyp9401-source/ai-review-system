from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
from uuid import uuid4

import pytest

from app.agent2.admission_hashes import admission_claim_hashes_match
from app.agent2.business.admission import bind_business_execution_context
from app.agent2.business.case_progress import CaseRecord
from app.agent2.business.compiler import Phase2BusinessCommandCompiler
from app.agent2.business.contracts import BusinessCommandContext, CreateCaseProgress
from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveTurn
from app.agent2.cognitive_core_v3 import SemanticInterpretation
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.selection_pending import (
    SelectionCandidate,
    SelectionPending,
    SelectionPendingFactory,
    SelectionValidation,
    protect_selection_continuation_payload,
)
from app.agent2.selection_pending_admission import (
    SelectionContinuationAdmissionEngine,
    SelectionContinuationAdmissionError,
    SelectionContinuationSemanticInterpreter,
    bind_fresh_selected_business_command,
)
from app.agent2.selection_pending_runtime import (
    SelectionContinuationCoordinator,
    SelectionContinuationPreprocessRequest,
)


NOW = datetime(2026, 7, 14, 2, 0, tzinfo=UTC)
TENANT = "sandbox-agent2-phase2-20260711"
ORIGINAL_FACT = "恒大案件今天联系法院，法院表示下周重新查控。"


class _SourceLedger:
    def __init__(self, duplicate: bool = False) -> None:
        self.duplicate = duplicate

    async def source_message_processed(self, context) -> bool:
        return self.duplicate


class _Validator:
    def __init__(self, status: str = "valid") -> None:
        self.status = status
        self.calls = []

    async def validate(self, pending, candidate, context):
        self.calls.append((pending.pending_id, candidate.stable_id, candidate.version))
        if self.status == "valid":
            return SelectionValidation.valid()
        return SelectionValidation(self.status, self.status)


def _pending(**overrides) -> SelectionPending:
    command_id, decision_id, sub_decision_id = uuid4(), uuid4(), uuid4()
    source_hash = hashlib.sha256(ORIGINAL_FACT.encode("utf-8")).hexdigest()
    values = {
        "pending_id": "selection-1",
        "tenant_id": TENANT,
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "domain": "case_progress",
        "operation": "create",
        "source_turn_id": "message-original",
        "candidates": (
            SelectionCandidate("case-1", 3, "恒大执行案一"),
            SelectionCandidate("case-2", 5, "恒大执行案二"),
        ),
        "acceptable_answer_forms": {
            "第一个": "case-1",
            "第二个": "case-2",
            "把第一条删了": "case-1",
        },
        "expected_conversation_state_version": 7,
        "created_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=9),
        "status": "active",
        "continuation_payload": {
            "typed_business_command": {
                "command_id": str(command_id),
                "decision_id": str(decision_id),
                "sub_decision_id": str(sub_decision_id),
                "command_type": "record_case_progress_candidate",
                "target_system": "case_progress",
                "entity_ids": ["case-ref"],
                "payload": {
                    "entities": [
                        {
                            "entity_id": "case-ref",
                            "entity_type": "case_ref",
                            "value": "恒大案件",
                            "confidence": 0.99,
                            "attributes": {"stage": "execution"},
                        }
                    ],
                    "parameters": {},
                    "source_segments": [
                        {
                            "segment_id": "segment-original",
                            "text": ORIGINAL_FACT,
                            "text_hash": source_hash,
                            "start_offset": 0,
                            "end_offset": len(ORIGINAL_FACT),
                        }
                    ],
                },
                "execution_mode": "candidate",
                "idempotency_key": "original-command-key",
            },
            "bind": {"entity_type": "case_ref", "attribute": "case_id"},
        },
    }
    values["continuation_payload"] = protect_selection_continuation_payload(
        values["continuation_payload"]
    )
    values.update(overrides)
    return SelectionPending(**values)


def _request(*, pendings=None, text="第二个", **overrides):
    values = {
        "tenant_id": TENANT,
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "conversation_state_version": 7,
        "source_message_id": "message-answer",
        "source_text": text,
        "occurred_at": NOW,
        "pendings": tuple(pendings if pendings is not None else (_pending(),)),
    }
    values.update(overrides)
    return SelectionContinuationPreprocessRequest(**values)


async def _ready(*, pending=None, text="第二个"):
    return await SelectionContinuationCoordinator().preprocess(
        _request(pendings=(pending or _pending(),), text=text),
        validator=_Validator(),
        source_ledger=_SourceLedger(),
    )


@pytest.mark.asyncio
async def test_selection_preprocess_requires_one_scoped_pending_and_exact_current_evidence():
    validator = _Validator()
    result = await SelectionContinuationCoordinator().preprocess(
        _request(text="  第二个。 "),
        validator=validator,
        source_ledger=_SourceLedger(),
    )

    assert result.status == "ready_for_fresh_admission"
    assert result.actual_write is False
    assert result.candidate.stable_id == "case-2"
    assert result.candidate.version == 5
    assert result.fresh_admission_request.candidate_stable_id == "case-2"
    assert result.fresh_admission_request.candidate_version == 5
    assert result.fresh_admission_request.evidence.text == "第二个"
    assert result.fresh_admission_request.evidence.start_offset == 2
    assert result.fresh_admission_request.evidence.end_offset == 5
    assert result.fresh_admission_request.original_source_digest == hashlib.sha256(
        ORIGINAL_FACT.encode("utf-8")
    ).hexdigest()
    assert validator.calls == [("selection-1", "case-2", 5)]


@pytest.mark.asyncio
async def test_selection_preprocess_rejects_non_exact_ordinal_sentence_instead_of_regex_guessing():
    validator = _Validator()
    result = await SelectionContinuationCoordinator().preprocess(
        _request(text="第二个，然后把第一个删了"),
        validator=validator,
        source_ledger=_SourceLedger(),
    )

    assert result.status == "clarification_required"
    assert result.fresh_admission_request is None
    assert result.candidate is None
    assert validator.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_request", "validator", "ledger", "expected_status"),
    (
        (
            _request(pendings=(_pending(), _pending(pending_id="selection-2"))),
            _Validator(),
            _SourceLedger(),
            "clarification_required",
        ),
        (
            _request(pendings=(_pending(expires_at=NOW - timedelta(seconds=1)),)),
            _Validator(),
            _SourceLedger(),
            "expired",
        ),
        (
            _request(pendings=(_pending(status="consumed", consumed_receipt_id="receipt-1"),)),
            _Validator(),
            _SourceLedger(),
            "already_consumed",
        ),
        (
            _request(conversation_state_version=8),
            _Validator(),
            _SourceLedger(),
            "invalidated",
        ),
        (
            _request(pendings=(_pending(tenant_id="other-tenant"),)),
            _Validator(),
            _SourceLedger(),
            "no_pending",
        ),
        (
            _request(pendings=(_pending(user_id="other-user"),)),
            _Validator(),
            _SourceLedger(),
            "no_pending",
        ),
        (
            _request(pendings=(_pending(conversation_id="other-conversation"),)),
            _Validator(),
            _SourceLedger(),
            "no_pending",
        ),
        (
            _request(),
            _Validator("not_found"),
            _SourceLedger(),
            "invalidated",
        ),
        (
            _request(),
            _Validator("version_conflict"),
            _SourceLedger(),
            "invalidated",
        ),
        (
            _request(),
            _Validator("forbidden"),
            _SourceLedger(),
            "permission_revoked",
        ),
        (
            _request(),
            _Validator(),
            _SourceLedger(duplicate=True),
            "duplicate_source_message",
        ),
    ),
)
async def test_selection_preprocess_failures_expose_no_fresh_admission_request(
    case_request, validator, ledger, expected_status
):
    result = await SelectionContinuationCoordinator().preprocess(
        case_request,
        validator=validator,
        source_ledger=ledger,
    )

    assert result.status == expected_status
    assert result.actual_write is False
    assert result.fresh_admission_request is None
    assert result.candidate is None


def _turn(text="第二个"):
    return CognitiveTurn(
        user_id=f"{TENANT}:user-1",
        actor_user_id="user-1",
        tenant_id=TENANT,
        conversation_id="conversation-1",
        message_id="message-answer",
        text=text,
        occurred_at=NOW,
        resources={},
    )


def _state(version=7):
    return ConversationState(
        user_id=f"{TENANT}:user-1",
        conversation_id="conversation-1",
        version=version,
    )


@pytest.mark.asyncio
async def test_fresh_selection_admission_binds_current_evidence_and_protected_original_facts():
    ready = await _ready()
    result = await CognitiveCoreV3(
        SelectionContinuationSemanticInterpreter(ready),
        admission_engine=SelectionContinuationAdmissionEngine(ready),
        admission_enforced=True,
    ).process(_turn(), _state())

    ticket = result.decision.admission_tickets[0]
    authority = ticket.authority_scope
    assert ticket.source_message_id == "message-answer"
    assert ticket.segment_text_sha256 == hashlib.sha256("第二个".encode("utf-8")).hexdigest()
    assert ticket.segment_start_offset == 0
    assert ticket.segment_end_offset == 3
    assert ticket.object_ref == {
        "object_type": "case",
        "stable_id": "case-2",
        "version": 5,
    }
    assert authority["selection_pending_id"] == "selection-1"
    assert authority["selection_pending_source_turn_id"] == "message-original"
    assert authority["original_source_digest"] == hashlib.sha256(
        ORIGINAL_FACT.encode("utf-8")
    ).hexdigest()
    assert authority["candidate_stable_id"] == "case-2"
    assert authority["candidate_version"] == 5
    assert authority["selection_evidence"]["text"] == "第二个"
    assert authority["selection_evidence"]["start_offset"] == 0
    assert authority["selection_evidence"]["end_offset"] == 3
    assert authority["raw_fact"] == ORIGINAL_FACT
    assert authority["case_reference"] == "case-2"
    assert authority["final_command_claims"]["command_type"] == "record_case_progress_candidate"
    assert admission_claim_hashes_match(ticket.as_dict())


@pytest.mark.asyncio
async def test_fresh_ticket_binds_command_without_replacing_original_case_fact():
    pending = _pending()
    ready = await _ready(pending=pending)
    result = await CognitiveCoreV3(
        SelectionContinuationSemanticInterpreter(ready),
        admission_engine=SelectionContinuationAdmissionEngine(ready),
        admission_enforced=True,
    ).process(_turn(), _state())
    command = bind_fresh_selected_business_command(ready, result.decision.admission_tickets[0])

    assert command.admission_required is True
    assert command.admission_ticket
    assert command.payload["entities"][0]["value"] == "case-2"
    assert command.payload["source_segments"][0]["text"] == ORIGINAL_FACT
    assert command.payload["selection_evidence"]["text"] == "第二个"
    context = BusinessCommandContext(
        tenant_id=TENANT,
        company_id="company-1",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=("case-2",),
        source_message_id="message-answer",
        source_channel="manual_text",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        conversation_state_version=7,
    )
    compiled = Phase2BusinessCommandCompiler().compile(
        command,
        bind_business_execution_context(command, context),
        cases=(
            CaseRecord(
                "case-2",
                TENANT,
                "CASE-002",
                "恒大执行案二",
                ("恒大案件",),
                version=5,
            ),
        ),
    )

    assert compiled.block is None
    assert isinstance(compiled.command, CreateCaseProgress)
    assert compiled.command.case_id == "case-2"
    assert compiled.command.summary == ORIGINAL_FACT


@pytest.mark.asyncio
async def test_selection_admission_rejects_state_or_protected_continuation_drift_before_ticket():
    ready = await _ready()
    with pytest.raises(SelectionContinuationAdmissionError, match="scope changed"):
        await CognitiveCoreV3(
            SelectionContinuationSemanticInterpreter(ready),
            admission_engine=SelectionContinuationAdmissionEngine(ready),
            admission_enforced=True,
        ).process(_turn(), _state(version=8))

    changed_pending = replace(
        ready.pending,
        continuation_payload={
            **ready.pending.continuation_payload,
            "typed_business_command": {
                **ready.pending.continuation_payload["typed_business_command"],
                "payload": {
                    **ready.pending.continuation_payload["typed_business_command"]["payload"],
                    "source_segments": [
                        {
                            **ready.pending.continuation_payload["typed_business_command"]["payload"]["source_segments"][0],
                            "text": "被篡改的案件事实",
                        }
                    ],
                },
            },
        },
    )
    tampered = replace(ready, pending=changed_pending)
    with pytest.raises(SelectionContinuationAdmissionError, match="protected continuation changed"):
        await CognitiveCoreV3(
            SelectionContinuationSemanticInterpreter(tampered),
            admission_engine=SelectionContinuationAdmissionEngine(tampered),
            admission_enforced=True,
        ).process(_turn(), _state())


def _ambiguous_initial_turn():
    text = ORIGINAL_FACT
    turn = CognitiveTurn(
        user_id=f"{TENANT}:user-1",
        actor_user_id="user-1",
        tenant_id=TENANT,
        conversation_id="conversation-1",
        message_id="message-original",
        text=text,
        occurred_at=NOW,
        resources={
            "visible_cases": [
                {
                    "case_id": "case-1",
                    "case_number": "CASE-001",
                    "case_name": "恒大执行案一",
                    "confirmed_aliases": ["恒大案件"],
                    "version": 3,
                },
                {
                    "case_id": "case-2",
                    "case_number": "CASE-002",
                    "case_name": "恒大执行案二",
                    "confirmed_aliases": ["恒大案件"],
                    "version": 5,
                },
            ]
        },
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "segment-original",
                    "text": text,
                    "intents": ["case_progress"],
                    "entity_ids": ["case-ref"],
                    "action_ids": ["record-case"],
                    "start_offset": 0,
                    "end_offset": len(text),
                }
            ],
            "entities": [
                {
                    "entity_id": "case-ref",
                    "entity_type": "case_ref",
                    "value": "恒大案件",
                    "confidence": 0.99,
                    "attributes": {
                        "statement_mode": "asserted",
                        "normalized_fact": text,
                        "evidence_spans": [[0, len(text)]],
                    },
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "record-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-ref"],
                    "parameters": {},
                }
            ],
            "context_update": {
                "preserve_current_goal": True,
                "remember_turn": True,
                "remember_entity_ids": ["case-ref"],
            },
        }
    )
    return turn, proposal


def test_ambiguous_case_admission_creates_trusted_selection_request_with_zero_ticket():
    turn, proposal = _ambiguous_initial_turn()
    result = DomainAdmissionEngine().admit(turn, _state(), proposal)

    assert result.decisions[0].status == "blocked"
    assert result.decisions[0].reason_code == "case_reference_ambiguous"
    assert result.decisions[0].pending_id == ""
    assert len(result.decisions[0].evidence_refs) == 2
    assert result.decisions[0].evidence_refs[1].startswith("selection_request:")
    assert result.tickets == ()
    assert result.information_pendings == ()
    assert result.interpretation.required_actions == ()
    assert len(result.selection_requests) == 1
    request = result.selection_requests[0]
    assert request.business_write_allowed is False
    assert request.domain == "case"
    assert request.operation == "record_case_progress"
    assert request.expected_conversation_state_version == 8
    assert [(item.stable_id, item.version) for item in request.candidates] == [
        ("case-1", 3),
        ("case-2", 5),
    ]
    assert request.acceptable_answer_forms["第二个"] == "case-2"
    protected = request.continuation_payload["protected_snapshot"]
    assert protected["original_source_digest"] == hashlib.sha256(
        ORIGINAL_FACT.encode("utf-8")
    ).hexdigest()
    pending = SelectionPendingFactory().from_trusted_request(request)
    assert pending.pending_id == request.selection_request_id
    assert pending.expected_conversation_state_version == 8
    assert pending.status == "active"
    assert pending.continuation_payload["protected_snapshot"] == dict(protected)


@pytest.mark.asyncio
async def test_cognitive_decision_transports_trusted_selection_request_without_state_write():
    turn, proposal = _ambiguous_initial_turn()

    class _Interpreter:
        async def interpret(self, incoming_turn, state):
            assert incoming_turn == turn
            return proposal

    result = await CognitiveCoreV3(
        _Interpreter(),
        admission_engine=DomainAdmissionEngine(),
        admission_enforced=True,
    ).process(turn, _state())

    assert result.decision.required_actions == ()
    assert result.decision.admission_tickets == ()
    assert len(result.decision.admission_selection_requests) == 1
    serialized = result.decision.as_dict()
    assert serialized["admission_selection_requests"][0]["business_write_allowed"] is False
    # Core advances the normal turn version but does not install a Pending;
    # runtime must perform the later ConversationState CAS explicitly.
    assert result.state.version == 8
    assert result.state.selection_pending == ()


def test_ambiguous_case_without_version_or_asserted_fact_stays_blocked_without_request():
    turn, proposal = _ambiguous_initial_turn()
    broken_resources = dict(turn.resources)
    broken_cases = [dict(item) for item in broken_resources["visible_cases"]]
    broken_cases[1].pop("version")
    broken_turn = replace(turn, resources={"visible_cases": broken_cases})
    missing_version = DomainAdmissionEngine().admit(broken_turn, _state(), proposal)

    weak_entity = replace(
        proposal.entities[0],
        attributes={**proposal.entities[0].attributes, "statement_mode": "hypothetical"},
    )
    weak_proposal = replace(proposal, entities=(weak_entity,))
    weak_fact = DomainAdmissionEngine().admit(turn, _state(), weak_proposal)

    assert missing_version.tickets == ()
    assert missing_version.selection_requests == ()
    assert weak_fact.tickets == ()
    assert weak_fact.selection_requests == ()
