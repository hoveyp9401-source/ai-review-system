from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.cognitive_contract_v3 import validate_semantic_interpretation_contract
from app.agent2.cognitive_core_v3 import CognitiveDecisionV3, SemanticInterpretation
from app.agent2.command_planner_v3 import CognitiveCommandPlanner, CommandPlanningContext


def _interpretation(cadence_type="weekly"):
    return SemanticInterpretation.from_payload(
        {
            "intents": ["case_followup_policy"],
            "segments": [{
                "segment_id": "segment-1", "text": "这个案子一周问一次",
                "intents": ["case_followup_policy"], "entity_ids": ["policy-entity"],
                "action_ids": ["update-policy"],
            }],
            "entities": [{
                "entity_id": "policy-entity", "entity_type": "case_followup_policy",
                "value": "南京工程款案", "confidence": 0.98,
                "attributes": {
                    "case_hint": "南京工程款案", "cadence_type": cadence_type,
                    "enabled": True,
                },
            }],
            "confidence": 0.98,
            "required_actions": [{
                "action_id": "update-policy", "action_type": "update_case_followup_policy",
                "intent": "case_followup_policy", "entity_ids": ["policy-entity"],
                "parameters": {},
            }],
            "clarification_need": None,
            "context_update": {"current_goal": "case_followup_policy"},
        }
    )


def test_semantic_contract_plans_typed_policy_candidate_without_database_id():
    interpretation = _interpretation()
    validate_semantic_interpretation_contract(interpretation)
    decision = CognitiveDecisionV3(
        decision_id=str(uuid5(NAMESPACE_URL, "policy-decision")),
        intents=interpretation.intents, segments=interpretation.segments,
        entities=interpretation.entities, confidence=interpretation.confidence,
        required_actions=interpretation.required_actions,
        clarification_need=None, context_update=interpretation.context_update,
        source_text_hash="hash",
    )

    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id="message-1", actor_user_id=uuid5(NAMESPACE_URL, "user-1")
        ),
    )

    assert len(plan.business_commands) == 1
    command = plan.business_commands[0]
    assert command.command_type == "update_case_followup_policy_candidate"
    assert command.target_system == "case_followup_policy"
    assert command.payload["entities"][0]["attributes"]["case_hint"] == "南京工程款案"
    assert "case_id" not in command.payload["entities"][0]["attributes"]


def test_unknown_cadence_fails_closed_in_semantic_contract():
    with pytest.raises(ValueError, match="cadence_type.value"):
        validate_semantic_interpretation_contract(_interpretation("biweekly"))
