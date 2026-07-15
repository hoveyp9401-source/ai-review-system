from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.agent2.cognitive_core_v3 import CognitiveTurn
from app.agent2.conversation_state import ConversationState
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter


class _OneProposalClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    async def complete_json(self, **kwargs):
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def test_admission_mode_interpreter_does_not_inject_keyword_derived_mutations():
    client = _OneProposalClient(
        {
            "intents": ["chat"],
            "segments": [
                {
                    "segment_id": "segment-1",
                    "text": "明天去南京出差",
                    "intents": ["chat"],
                    "entity_ids": [],
                    "action_ids": [],
                }
            ],
            "entities": [],
            "confidence": 0.8,
            "required_actions": [],
            "clarification_need": None,
            "context_update": {
                "preserve_current_goal": True,
                "remember_turn": True,
            },
        }
    )
    turn = CognitiveTurn(
        tenant_id="sandbox-agent2-phase2-20260711",
        user_id="user-pang",
        actor_user_id="user-pang",
        conversation_id="conversation-no-legacy-enforcers",
        message_id="message-no-legacy-enforcers",
        text="明天去南京出差",
        occurred_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        resources={"timezone": "Asia/Shanghai"},
    )
    interpretation = asyncio.run(
        LLMCognitiveSemanticInterpreter(
            client,
            legacy_semantic_enforcers_enabled=False,
        ).interpret(
            turn,
            ConversationState.empty(
                user_id=turn.user_id,
                conversation_id=turn.conversation_id,
            ),
        )
    )

    assert client.calls == 1
    assert interpretation.intents == ("chat",)
    assert interpretation.required_actions == ()
    assert interpretation.entities == ()
    assert interpretation.segments[0].action_ids == ()
