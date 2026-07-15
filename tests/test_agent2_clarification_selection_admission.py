from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

from app.agent2.cognitive_core_v3 import RequiredAction, SemanticInterpretation
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.cognitive_core_v3 import CognitiveTurn
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter


ROOT = Path(__file__).resolve().parents[1]
BLIND_INPUT = ROOT / "evals" / "agent2" / "semantic_admission" / "blind_input.json"
CASE_ID = "sa-blind-09-ambiguous_case_alias"


def _case09() -> dict[str, object]:
    pack = json.loads(BLIND_INPUT.read_text(encoding="utf-8"))
    return next(item for item in pack["cases"] if item["case_id"] == CASE_ID)


def _turn_and_state() -> tuple[CognitiveTurn, ConversationState]:
    item = _case09()
    scope = item["scope"]
    assert isinstance(scope, dict)
    resources = item["resources"]
    assert isinstance(resources, dict)
    turn = CognitiveTurn(
        user_id=str(scope["user_id"]),
        actor_user_id=str(scope["actor_user_id"]),
        tenant_id=str(scope["tenant_id"]),
        conversation_id=str(scope["conversation_id"]),
        message_id=str(scope["message_id"]),
        text=str(item["raw_text"]),
        occurred_at=datetime.fromisoformat(str(scope["occurred_at"])),
        resources=resources,
    )
    state = ConversationState(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
        version=0,
    )
    return turn, state


def _clarification_only_proposal() -> SemanticInterpretation:
    text = "云璟府今天补交了证据"
    return SemanticInterpretation.from_payload(
        {
            "clarification_need": {
                "missing_fields": ["case_id"],
                "question": (
                    "云璟府对应两个案件：云璟府物业服务合同执行案和"
                    "云璟府建设工程争议案，请问您指的是哪一个？"
                ),
                "reason": "ambiguous_case_alias",
            },
            "confidence": 0.85,
            "context_update": {
                "current_goal": "case_progress",
                "preserve_current_goal": False,
                "remember_entity_ids": ["case-progress-1"],
                "remember_turn": True,
            },
            "entities": [
                {
                    "attributes": {
                        "action_time_scope": "today",
                        "blocking_issues": [],
                        "completed_actions": [],
                        "current_status": "补交了证据",
                        "evidence_spans": [[0, 10]],
                        "factual_progress": ["补交了证据"],
                        "hearing_readiness": None,
                        "next_actions": [],
                        "normalized_fact": text,
                        "report_preference": "automatic",
                        "requested_snooze": None,
                        "statement_mode": "asserted",
                    },
                    "confidence": 0.85,
                    "entity_id": "case-progress-1",
                    "entity_type": "case_ref",
                    "value": "云璟府",
                }
            ],
            "intents": ["case_progress"],
            "required_actions": [],
            "segments": [
                {
                    "action_ids": [],
                    "end_offset": -1,
                    "entity_ids": ["case-progress-1"],
                    "intents": ["case_progress"],
                    "segment_id": "seg-1",
                    "start_offset": -1,
                    "text": text,
                }
            ],
        }
    )


def _explicit_action_proposal() -> SemanticInterpretation:
    proposal = _clarification_only_proposal()
    action = RequiredAction(
        action_id="explicit-record-case-progress",
        action_type="record_case_progress",
        intent="case_progress",
        entity_ids=(proposal.entities[0].entity_id,),
        parameters={},
    )
    return replace(
        proposal,
        required_actions=(action,),
        segments=(replace(proposal.segments[0], action_ids=(action.action_id,)),),
    )


class _SequenceClient:
    def __init__(self, *payloads: dict[str, object]):
        self._payloads = list(payloads)
        self.calls: list[dict[str, object]] = []

    async def complete_json(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        index = min(len(self.calls) - 1, len(self._payloads) - 1)
        return json.dumps(self._payloads[index], ensure_ascii=False)


def test_case09_clarification_only_proposal_cannot_create_selection_authority() -> None:
    turn, state = _turn_and_state()
    proposal = _clarification_only_proposal()

    result = DomainAdmissionEngine().admit(turn, state, proposal)

    assert result.decisions == ()
    assert result.tickets == ()
    assert result.information_pendings == ()
    assert result.selection_requests == ()

    admitted = result.interpretation
    assert admitted.required_actions == ()
    assert admitted.intents == ()
    assert admitted.entities == ()
    assert admitted.segments == ()
    assert admitted.clarification_need == proposal.clarification_need
    assert admitted.context_update.current_goal == ""
    assert admitted.context_update.preserve_current_goal is True
    assert admitted.context_update.remember_entity_ids == ()
    assert admitted.context_update.remember_turn is False


def _assert_zero_authority(result: object) -> None:
    assert result.decisions == ()
    assert result.tickets == ()
    assert result.information_pendings == ()
    assert result.selection_requests == ()
    assert result.interpretation.required_actions == ()


@pytest.mark.parametrize(
    ("reason", "missing_fields"),
    (
        ("case_reference_missing", ("case_id",)),
        ("ambiguous_case_alias", ("case_id", "case_version")),
    ),
)
def test_clarification_only_proposal_requires_closed_ambiguity_fields(
    reason: str,
    missing_fields: tuple[str, ...],
) -> None:
    turn, state = _turn_and_state()
    proposal = _clarification_only_proposal()
    clarification = replace(
        proposal.clarification_need,
        reason=reason,
        missing_fields=missing_fields,
    )

    result = DomainAdmissionEngine().admit(
        turn,
        state,
        replace(proposal, clarification_need=clarification),
    )

    _assert_zero_authority(result)


def test_clarification_only_proposal_cannot_borrow_a_cross_segment_fact() -> None:
    turn, state = _turn_and_state()
    proposal = _clarification_only_proposal()
    sibling_text = "补交了证据"
    sibling = replace(
        proposal.segments[0],
        segment_id="seg-2",
        text=sibling_text,
        text_hash=hashlib.sha256(sibling_text.encode("utf-8")).hexdigest(),
        entity_ids=(),
        start_offset=5,
        end_offset=len(turn.text),
    )

    result = DomainAdmissionEngine().admit(
        turn,
        state,
        replace(proposal, segments=(*proposal.segments, sibling)),
    )

    _assert_zero_authority(result)


def test_clarification_only_proposal_requires_multiple_trusted_visible_cases() -> None:
    turn, state = _turn_and_state()
    only_case = turn.resources["visible_cases"][0]
    unique_turn = replace(turn, resources={"visible_cases": [only_case]})

    result = DomainAdmissionEngine().admit(
        unique_turn,
        state,
        _clarification_only_proposal(),
    )

    _assert_zero_authority(result)


def test_clarification_only_proposal_without_trusted_candidates_stays_no_op() -> None:
    turn, state = _turn_and_state()

    result = DomainAdmissionEngine().admit(
        replace(turn, resources={}),
        state,
        _clarification_only_proposal(),
    )

    _assert_zero_authority(result)


@pytest.mark.parametrize("attribute_change", ({"statement_mode": "hypothetical"}, {"evidence_spans": []}))
def test_clarification_only_proposal_requires_asserted_grounded_case_fact(
    attribute_change: dict[str, object],
) -> None:
    turn, state = _turn_and_state()
    proposal = _clarification_only_proposal()
    entity = proposal.entities[0]
    unsafe_entity = replace(
        entity,
        attributes={**entity.attributes, **attribute_change},
    )

    result = DomainAdmissionEngine().admit(
        turn,
        state,
        replace(proposal, entities=(unsafe_entity,)),
    )

    _assert_zero_authority(result)


def test_clarification_only_proposal_rejects_an_ungrounded_case_reference() -> None:
    turn, state = _turn_and_state()
    proposal = _clarification_only_proposal()
    ungrounded_entity = replace(proposal.entities[0], value="模型猜测的其他案件")

    result = DomainAdmissionEngine().admit(
        turn,
        state,
        replace(proposal, entities=(ungrounded_entity,)),
    )

    _assert_zero_authority(result)


def test_existing_required_action_is_evaluated_once_without_derived_duplicate() -> None:
    turn, state = _turn_and_state()
    proposal = _explicit_action_proposal()
    explicit_action = proposal.required_actions[0]

    result = DomainAdmissionEngine().admit(
        turn,
        state,
        proposal,
    )

    assert len(result.decisions) == 1
    assert result.decisions[0].action_id == explicit_action.action_id
    assert result.decisions[0].status == "blocked"
    assert result.tickets == ()
    assert len(result.selection_requests) == 1
    request = result.selection_requests[0]
    assert request.business_write_allowed is False
    assert [(item.stable_id, item.version) for item in request.candidates] == [
        ("e71a619e-ab3a-56d9-81ba-77fd8d1ca971", 7),
        ("63c8c17b-f2d5-5592-ab4e-92a15c1401be", 5),
    ]
    protected_command = request.continuation_payload["typed_business_command"]
    source_segment = protected_command["payload"]["source_segments"][0]
    assert source_segment["text"] == turn.text
    assert source_segment["start_offset"] == 0
    assert source_segment["end_offset"] == len(turn.text)
    assert result.decisions[0].ticket_id == ""
    assert result.decisions[0].pending_id == ""
    assert result.decisions[0].evidence_refs[-1] == (
        f"selection_request:{request.selection_request_id}"
    )
    admitted = result.interpretation
    assert admitted.required_actions == ()
    assert admitted.intents == ()
    assert admitted.entities == ()
    assert admitted.segments == ()
    assert admitted.clarification_need == proposal.clarification_need
    assert admitted.context_update.current_goal == ""
    assert admitted.context_update.preserve_current_goal is True
    assert admitted.context_update.remember_entity_ids == ()
    assert admitted.context_update.remember_turn is False


@pytest.mark.parametrize("invalid_kind", ("missing_action", "missing_segment_binding"))
def test_semantic_interpreter_repairs_ambiguous_case_alias_contract(
    invalid_kind: str,
) -> None:
    turn, state = _turn_and_state()
    invalid = _clarification_only_proposal()
    if invalid_kind == "missing_segment_binding":
        invalid = replace(
            _explicit_action_proposal(),
            segments=(replace(invalid.segments[0], action_ids=()),),
        )
    valid = _explicit_action_proposal()
    client = _SequenceClient(asdict(invalid), asdict(valid))

    result = asyncio.run(
        LLMCognitiveSemanticInterpreter(
            client,
            legacy_semantic_enforcers_enabled=False,
        ).interpret(turn, state)
    )

    assert result == valid
    assert len(client.calls) == 2
    assert "ambiguous_case_alias" in str(client.calls[1]["user_prompt"])


def test_semantic_interpreter_fails_closed_when_ambiguity_contract_never_repairs() -> None:
    turn, state = _turn_and_state()
    client = _SequenceClient(asdict(_clarification_only_proposal()))

    with pytest.raises(ValueError, match="ambiguous_case_alias"):
        asyncio.run(
            LLMCognitiveSemanticInterpreter(
                client,
                legacy_semantic_enforcers_enabled=False,
            ).interpret(turn, state)
        )

    assert len(client.calls) == 3


def test_incompletely_bound_explicit_action_cannot_create_selection_authority() -> None:
    turn, state = _turn_and_state()
    proposal = _explicit_action_proposal()
    proposal = replace(
        proposal,
        segments=(replace(proposal.segments[0], action_ids=()),),
    )

    result = DomainAdmissionEngine().admit(turn, state, proposal)

    assert [(item.status, item.reason_code) for item in result.decisions] == [
        ("blocked", "action_segment_missing")
    ]
    assert result.tickets == ()
    assert result.information_pendings == ()
    assert result.selection_requests == ()
    assert result.interpretation.required_actions == ()
    assert result.interpretation.clarification_need == proposal.clarification_need
