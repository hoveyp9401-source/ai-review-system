from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.agent2.business.admission import bind_business_execution_context
from app.agent2.business.compiler import Phase2BusinessCommandCompiler
from app.agent2.business.contracts import BusinessCommandContext, BusinessCommandError
from app.agent2.cognitive_core_v3 import CognitiveTurn, SemanticInterpretation
from app.agent2.command_planner_v3 import TypedBusinessCommand
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


def _snooze_admission_fixture():
    case_id = str(uuid4())
    pending_id = str(uuid4())
    followup_id = str(uuid4())
    policy_id = str(uuid4())
    text = "Alpha case next_week"
    attributes = {
        "case_hint": "Alpha case",
        "requested_snooze": "next_week",
        "followup_notification_id": pending_id,
    }
    turn = CognitiveTurn(
        tenant_id="tenant-1",
        user_id="user-1",
        actor_user_id="user-1",
        conversation_id="conversation-1",
        message_id="message-1",
        text=text,
        occurred_at=NOW,
        resources={
            "visible_cases": [
                {
                    "case_id": case_id,
                    "case_name": "Alpha case",
                    "confirmed_aliases": [],
                    "version": 7,
                }
            ],
            "active_case_progress_followups": [
                {
                    "notification_id": pending_id,
                    "pending_id": pending_id,
                    "pending_version": 3,
                    "followup_id": followup_id,
                    "task_id": followup_id,
                    "task_version": 5,
                    "case_id": case_id,
                    "case_version": 7,
                    "case_name": "Alpha case",
                    "policy_id": policy_id,
                    "policy_version": 2,
                    "assigned_user_id": "user-1",
                    "conversation_id": "conversation-1",
                    "expected_state_version": 0,
                    "task_status": "waiting_for_reply",
                    "message_status": "accepted_by_provider",
                    "provider_message_id": "provider-1",
                    "expires_at": (NOW + timedelta(days=1)).isoformat(),
                    "source_type": "case_lifecycle_followup",
                }
            ],
        },
    )
    proposal = SemanticInterpretation.from_payload(
        {
            "intents": ["case_progress"],
            "segments": [
                {
                    "segment_id": "segment-1",
                    "text": text,
                    "start_offset": 0,
                    "end_offset": len(text),
                    "intents": ["case_progress"],
                    "entity_ids": ["entity-1"],
                    "action_ids": ["action-1"],
                }
            ],
            "entities": [
                {
                    "entity_id": "entity-1",
                    "entity_type": "case_ref",
                    "value": "Alpha case",
                    "confidence": 0.99,
                    "attributes": attributes,
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "action-1",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["entity-1"],
                }
            ],
            "clarification_need": None,
            "context_update": {"preserve_current_goal": True},
        }
    )
    state = ConversationState.empty(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
    )
    return turn, proposal, state, attributes


def test_snooze_issues_a_dedicated_exact_authority_ticket() -> None:
    turn, proposal, state, _ = _snooze_admission_fixture()

    result = DomainAdmissionEngine().admit(turn, state, proposal)

    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].domain == "case"
    assert result.decisions[0].operation == "snooze_case_followup"
    ticket = result.tickets[0]
    followup = turn.resources["active_case_progress_followups"][0]
    assert ticket.object_ref == {
        "object_type": "case_followup_pending",
        "stable_id": followup["pending_id"],
        "version": followup["pending_version"],
    }
    assert ticket.authority_scope == {
        "pending_id": followup["pending_id"],
        "pending_version": 3,
        "followup_id": followup["followup_id"],
        "task_id": followup["task_id"],
        "task_version": 5,
        "case_id": followup["case_id"],
        "case_version": 7,
        "policy_id": followup["policy_id"],
        "policy_version": 2,
        "assigned_user_id": "user-1",
        "conversation_id": "conversation-1",
        "expected_state_version": 0,
        "requested_snooze": "next_week",
        "snoozed_until": "2026-07-20T09:00:00+00:00",
        "raw_fact": turn.text,
    }
    assert ticket.allowed_changed_fields == (
        "task_status",
        "task_response_status",
        "task_next_eligible_at",
        "task_completed_at",
        "task_version",
        "pending_status",
        "pending_consumed_at",
        "pending_version",
        "policy_snoozed_until",
        "policy_next_due_at",
        "policy_version",
    )

