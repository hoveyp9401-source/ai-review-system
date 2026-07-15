from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from uuid import NAMESPACE_URL, uuid5

from app.agent2.business.admission import bind_business_execution_context
from app.agent2.business.compiler import Phase2BusinessCommandCompiler
from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.executor import InMemoryBusinessExecutor
from app.agent2.admission_store import InMemoryAdmissionTicketStore
from app.agent2.command_planner_v3 import TypedBusinessCommand
from app.agent2.cognitive_core_v3 import CognitiveTurn, SemanticInterpretation
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


def _proposal(
    *,
    text: str,
    action_type: str,
    intent: str,
    entity_type: str,
    value: str,
    attributes: dict,
) -> SemanticInterpretation:
    return SemanticInterpretation.from_payload(
        {
            "intents": [intent],
            "segments": [
                {
                    "segment_id": "segment-1",
                    "text": text,
                    "start_offset": 0,
                    "end_offset": len(text),
                    "intents": [intent],
                    "entity_ids": ["entity-1"],
                    "action_ids": ["action-1"],
                }
            ],
            "entities": [
                {
                    "entity_id": "entity-1",
                    "entity_type": entity_type,
                    "value": value,
                    "confidence": 0.99,
                    "attributes": attributes,
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "action-1",
                    "action_type": action_type,
                    "intent": intent,
                    "entity_ids": ["entity-1"],
                }
            ],
            "clarification_need": None,
            "context_update": {"preserve_current_goal": True},
        }
    )


def _admit(
    proposal: SemanticInterpretation,
    *,
    text: str,
    resources: dict,
    user_id: str = "user-1",
    tenant_id: str = "tenant-1",
    conversation_id: str = "conversation-1",
):
    turn = CognitiveTurn(
        tenant_id=tenant_id,
        user_id=user_id,
        actor_user_id=user_id,
        conversation_id=conversation_id,
        message_id="message-1",
        text=text,
        occurred_at=NOW,
        resources=resources,
    )
    state = ConversationState.empty(
        user_id=user_id,
        conversation_id=conversation_id,
    )
    return DomainAdmissionEngine().admit(turn, state, proposal)


def _typed_candidate(
    *,
    command_type: str,
    operation: str,
    entity: dict,
    text: str,
    ticket: dict,
) -> TypedBusinessCommand:
    decision_id = uuid5(NAMESPACE_URL, f"decision:{operation}")
    return TypedBusinessCommand(
        command_id=uuid5(NAMESPACE_URL, f"command:{operation}"),
        decision_id=decision_id,
        sub_decision_id=uuid5(NAMESPACE_URL, f"subdecision:{operation}"),
        command_type=command_type,
        target_system="test",
        entity_ids=(str(entity["entity_id"]),),
        payload={
            "entities": [entity],
            "source_segments": [
                {
                    "segment_id": "segment-1",
                    "text": text,
                    "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "start_offset": 0,
                    "end_offset": len(text),
                }
            ],
        },
        execution_mode="candidate",
        idempotency_key=f"candidate:{operation}",
        admission_ticket=ticket,
        admission_required=True,
        admission_action_id="action-1",
        admission_operation=operation,
    )


def _business_context(*, tenant_id: str = "tenant-1") -> BusinessCommandContext:
    return BusinessCommandContext(
        tenant_id=tenant_id,
        company_id="company-1",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=(),
        source_message_id="message-1",
        source_channel="dingtalk",
        occurred_at=NOW,
        conversation_id="conversation-1",
        execution_started_at=NOW + timedelta(seconds=1),
        conversation_state_version=0,
    )


def test_authorized_case_progress_query_is_admitted_without_a_ticket() -> None:
    text = "Query the Alpha case progress"
    proposal = _proposal(
        text=text,
        action_type="query_case_progress",
        intent="case_progress_query",
        entity_type="case_progress_ref",
        value="Alpha case",
        attributes={"case_hint": "Alpha case"},
    )

    result = _admit(
        proposal,
        text=text,
        resources={
            "visible_cases": [
                {
                    "case_id": "case-1",
                    "case_name": "Alpha case",
                    "confirmed_aliases": [],
                    "version": 3,
                }
            ]
        },
    )

    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].domain == "case"
    assert result.decisions[0].operation == "query_case_progress"
    assert result.decisions[0].object_ref == {
        "object_type": "case",
        "stable_id": "case-1",
        "version": 3,
    }
    assert result.tickets == ()


def test_case_inventory_query_requires_a_grounded_query_and_trusted_case_scope() -> None:
    text = "Show my assigned cases"
    proposal = _proposal(
        text=text,
        action_type="answer_case_query",
        intent="case_query",
        entity_type="case_query",
        value=text,
        attributes={"matter_hint": "my assigned cases", "question": text},
    )

    admitted = _admit(
        proposal,
        text=text,
        resources={"visible_cases": []},
    )
    blocked = _admit(
        proposal,
        text=text,
        resources={},
    )

    assert admitted.decisions[0].status == "admitted"
    assert admitted.decisions[0].object_ref["object_type"] == "case_query"
    assert admitted.tickets == ()
    assert blocked.decisions[0].status == "blocked"


def test_operation_status_query_is_read_only_only_with_verified_scope() -> None:
    text = "Query case progress operation status"
    proposal = _proposal(
        text=text,
        action_type="query_operation_status",
        intent="operation_status_query",
        entity_type="operation_status_query",
        value=text,
        attributes={"domain": "case_progress"},
    )

    result = _admit(
        proposal,
        text=text,
        resources={
            "operation_status_access": {
                "tenant_id": "tenant-1",
                "user_id": "user-1",
                "allowed": True,
            }
        },
    )

    assert result.decisions[0].status == "admitted"
    assert result.tickets == ()


def test_enterprise_knowledge_query_is_read_only_only_with_verified_access() -> None:
    text = "Find the seal borrowing policy"
    proposal = _proposal(
        text=text,
        action_type="search_enterprise_knowledge",
        intent="knowledge_query",
        entity_type="knowledge_query",
        value=text,
        attributes={"query": "seal borrowing policy", "topic": "seal"},
    )

    result = _admit(
        proposal,
        text=text,
        resources={
            "enterprise_knowledge_access": {
                "tenant_id": "tenant-1",
                "user_id": "user-1",
                "allowed": True,
            }
        },
    )

    assert result.decisions[0].status == "admitted"
    assert result.tickets == ()


def test_case_progress_update_binds_unique_trusted_progress_version_and_ticket() -> None:
    text = "Change Alpha case progress to court replies Friday"
    proposal = _proposal(
        text=text,
        action_type="update_case_progress",
        intent="case_progress_update",
        entity_type="case_progress_ref",
        value="Alpha case progress",
        attributes={
            "case_hint": "Alpha case",
            "progress_id": "progress-1",
            "expected_version": 4,
            "replacement_summary": "court replies Friday",
        },
    )

    result = _admit(
        proposal,
        text=text,
        resources={
            "visible_cases": [
                {"case_id": "case-1", "case_name": "Alpha case", "version": 3}
            ],
            "recent_case_progress": [
                {
                    "progress_id": "progress-1",
                    "case_id": "case-1",
                    "version": 4,
                    "summary": "old",
                }
            ],
        },
    )

    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].object_ref == {
        "object_type": "case_progress",
        "stable_id": "progress-1",
        "version": 4,
    }
    assert len(result.tickets) == 1
    ticket = result.tickets[0]
    assert ticket.operation == "update_case_progress"
    assert ticket.allowed_changed_fields == ("summary",)
    assert ticket.authority_scope["case_id"] == "case-1"
    assert ticket.authority_scope["replacement_summary"] == "court replies Friday"


def test_case_progress_mutation_blocks_version_drift_and_ambiguous_recent_target() -> None:
    text = "Change the progress to court replies Friday"
    proposal = _proposal(
        text=text,
        action_type="update_case_progress",
        intent="case_progress_update",
        entity_type="case_progress_ref",
        value="the progress",
        attributes={
            "expected_version": 3,
            "replacement_summary": "court replies Friday",
        },
    )
    resources = {
        "visible_cases": [
            {"case_id": "case-1", "case_name": "Alpha", "version": 1}
        ],
        "recent_case_progress": [
            {"progress_id": "p-1", "case_id": "case-1", "version": 4},
            {"progress_id": "p-2", "case_id": "case-1", "version": 3},
        ],
    }

    ambiguous = _admit(proposal, text=text, resources=resources)
    drift = _admit(
        _proposal(
            text=text,
            action_type="update_case_progress",
            intent="case_progress_update",
            entity_type="case_progress_ref",
            value="the progress",
            attributes={
                "progress_id": "p-1",
                "expected_version": 3,
                "replacement_summary": "court replies Friday",
            },
        ),
        text=text,
        resources=resources,
    )

    assert ambiguous.decisions[0].status == "blocked"
    assert drift.decisions[0].status == "blocked"
    assert ambiguous.tickets == drift.tickets == ()


def test_case_progress_delete_and_link_require_grounded_or_trusted_change_fields() -> None:
    delete_text = "Delete progress p-1 because duplicate"
    base_resources = {
        "visible_cases": [
            {"case_id": "case-1", "case_name": "Alpha", "version": 1}
        ],
        "recent_case_progress": [
            {"progress_id": "p-1", "case_id": "case-1", "version": 2}
        ],
        "visible_travel_intent_ids": ["travel-1"],
    }
    delete = _admit(
        _proposal(
            text=delete_text,
            action_type="delete_case_progress",
            intent="case_progress_delete",
            entity_type="case_progress_ref",
            value="progress p-1",
            attributes={"progress_id": "p-1", "delete_reason": "duplicate"},
        ),
        text=delete_text,
        resources=base_resources,
    )
    link_text = "Link progress p-1 to this trip"
    link = _admit(
        _proposal(
            text=link_text,
            action_type="link_case_progress",
            intent="case_progress_update",
            entity_type="case_progress_ref",
            value="progress p-1",
            attributes={
                "progress_id": "p-1",
                "related_travel_intent_ids": ["travel-1"],
            },
        ),
        text=link_text,
        resources=base_resources,
    )
    forged_link = _admit(
        _proposal(
            text=link_text,
            action_type="link_case_progress",
            intent="case_progress_update",
            entity_type="case_progress_ref",
            value="progress p-1",
            attributes={
                "progress_id": "p-1",
                "related_travel_intent_ids": ["travel-forged"],
            },
        ),
        text=link_text,
        resources=base_resources,
    )
    untrusted_document_link = _admit(
        _proposal(
            text=link_text,
            action_type="link_case_progress",
            intent="case_progress_update",
            entity_type="case_progress_ref",
            value="progress p-1",
            attributes={
                "progress_id": "p-1",
                "related_document_ids": ["document-without-trusted-resource"],
            },
        ),
        text=link_text,
        resources=base_resources,
    )

    assert delete.decisions[0].status == "admitted"
    assert delete.tickets[0].allowed_changed_fields == (
        "deleted_at",
        "deleted_by",
        "delete_reason",
    )
    assert link.decisions[0].status == "admitted"
    assert link.tickets[0].allowed_changed_fields == (
        "related_travel_intent_ids",
    )
    assert forged_link.decisions[0].status == "blocked"
    assert forged_link.tickets == ()
    assert untrusted_document_link.decisions[0].status == "blocked"
    assert untrusted_document_link.decisions[0].reason_code == (
        "case_progress_link_target_not_trusted"
    )
    assert untrusted_document_link.tickets == ()


def test_travel_collaboration_response_binds_one_active_participant_candidate() -> None:
    text = "accept"
    proposal = _proposal(
        text=text,
        action_type="respond_travel_collaboration",
        intent="travel_collaboration_response",
        entity_type="travel_collaboration_ref",
        value=text,
        attributes={"candidate_id": "candidate-1", "response": "accept"},
    )

    result = _admit(
        proposal,
        text=text,
        resources={
            "active_travel_collaborations": [
                {
                    "candidate_id": "candidate-1",
                    "participant_ids": ["user-1", "user-2"],
                    "status": "notified",
                    "version": 5,
                    "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                }
            ]
        },
    )

    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].object_ref == {
        "object_type": "travel_collaboration_candidate",
        "stable_id": "candidate-1",
        "version": 5,
    }
    assert result.tickets[0].allowed_changed_fields == (
        "responses_json",
        "status",
        "version",
    )
    assert result.tickets[0].authority_scope["response"] == "accept"


def test_travel_collaboration_response_blocks_multiple_expired_or_wrong_user_context() -> None:
    text = "accept"
    proposal = _proposal(
        text=text,
        action_type="respond_travel_collaboration",
        intent="travel_collaboration_response",
        entity_type="travel_collaboration_ref",
        value=text,
        attributes={"candidate_id": "candidate-1", "response": "accept"},
    )
    active = {
        "candidate_id": "candidate-1",
        "participant_ids": ["user-1", "user-2"],
        "status": "notified",
        "version": 5,
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
    }
    second = {**active, "candidate_id": "candidate-2"}
    expired = {**active, "expires_at": (NOW - timedelta(seconds=1)).isoformat()}
    wrong_user = {**active, "participant_ids": ["user-2", "user-3"]}

    results = (
        _admit(
            proposal,
            text=text,
            resources={"active_travel_collaborations": [active, second]},
        ),
        _admit(
            proposal,
            text=text,
            resources={"active_travel_collaborations": [expired]},
        ),
        _admit(
            proposal,
            text=text,
            resources={"active_travel_collaborations": [wrong_user]},
        ),
    )

    assert all(result.decisions[0].status == "blocked" for result in results)
    assert all(result.tickets == () for result in results)


def test_followup_policy_update_binds_authorized_case_and_current_policy_version() -> None:
    text = "Set Alpha case follow-up to weekly"
    proposal = _proposal(
        text=text,
        action_type="update_case_followup_policy",
        intent="case_followup_policy",
        entity_type="case_followup_policy",
        value="Alpha case",
        attributes={
            "case_hint": "Alpha case",
            "cadence_type": "weekly",
            "enabled": True,
            "evidence_spans": [[0, len(text)]],
        },
    )

    result = _admit(
        proposal,
        text=text,
        resources={
            "visible_cases": [
                {"case_id": "case-1", "case_name": "Alpha case", "version": 7}
            ],
            "active_case_followup_policies": [
                {
                    "case_id": "case-1",
                    "assigned_user_id": "user-1",
                    "version": 4,
                }
            ],
        },
    )

    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].domain == "case"
    assert result.decisions[0].object_ref == {
        "object_type": "case_followup_policy",
        "stable_id": "case-1",
        "version": 4,
    }
    assert result.tickets[0].authority_scope["cadence_type"] == "weekly"
    assert result.tickets[0].allowed_changed_fields == (
        "cadence_type",
        "enabled",
    )


def test_followup_policy_mutation_blocks_wrong_assignee_or_policy_version_context() -> None:
    text = "Set Alpha case follow-up to weekly"
    proposal = _proposal(
        text=text,
        action_type="update_case_followup_policy",
        intent="case_followup_policy",
        entity_type="case_followup_policy",
        value="Alpha case",
        attributes={
            "case_hint": "Alpha case",
            "cadence_type": "weekly",
            "evidence_spans": [[0, len(text)]],
        },
    )
    cases = [{"case_id": "case-1", "case_name": "Alpha case", "version": 7}]

    wrong_user = _admit(
        proposal,
        text=text,
        resources={
            "visible_cases": cases,
            "active_case_followup_policies": [
                {
                    "case_id": "case-1",
                    "assigned_user_id": "user-2",
                    "version": 4,
                }
            ],
        },
    )
    duplicate_versions = _admit(
        proposal,
        text=text,
        resources={
            "visible_cases": cases,
            "active_case_followup_policies": [
                {"case_id": "case-1", "assigned_user_id": "user-1", "version": 3},
                {"case_id": "case-1", "assigned_user_id": "user-1", "version": 4},
            ],
        },
    )

    assert wrong_user.decisions[0].status == "blocked"
    assert duplicate_versions.decisions[0].status == "blocked"
    assert wrong_user.tickets == duplicate_versions.tickets == ()


def test_trigger_followup_now_requires_exact_grounded_authorized_case() -> None:
    text = "Ask me now about Alpha case"
    proposal = _proposal(
        text=text,
        action_type="trigger_case_followup_now",
        intent="case_followup_policy",
        entity_type="case_followup_policy",
        value="Alpha case",
        attributes={
            "case_hint": "Alpha case",
            "evidence_spans": [[0, len(text)]],
        },
    )

    result = _admit(
        proposal,
        text=text,
        resources={
            "visible_cases": [
                {"case_id": "case-1", "case_name": "Alpha case", "version": 7}
            ],
            "active_case_followup_policies": [
                {"case_id": "case-1", "assigned_user_id": "user-1", "version": 4}
            ],
        },
    )

    assert result.decisions[0].status == "admitted"
    assert result.decisions[0].domain == "case"
    assert result.tickets[0].operation == "trigger_case_followup_now"
    assert result.tickets[0].domain == "case"
    assert result.tickets[0].allowed_changed_fields == (
        "followup_task",
        "notification_outbox",
    )


def test_travel_response_ticket_is_single_use_and_claims_drift_is_zero_write() -> None:
    text = "accept"
    proposal = _proposal(
        text=text,
        action_type="respond_travel_collaboration",
        intent="travel_collaboration_response",
        entity_type="travel_collaboration_ref",
        value=text,
        attributes={"candidate_id": "candidate-1", "response": "accept"},
    )
    admission = _admit(
        proposal,
        text=text,
        resources={
            "active_travel_collaborations": [
                {
                    "candidate_id": "candidate-1",
                    "participant_ids": ["user-1", "user-2"],
                    "status": "notified",
                    "version": 2,
                    "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                }
            ]
        },
    )
    entity = {
        "entity_id": "entity-1",
        "entity_type": "travel_collaboration_ref",
        "value": text,
        "confidence": 0.99,
        "attributes": {"candidate_id": "candidate-1", "response": "accept"},
    }
    typed = _typed_candidate(
        command_type="respond_travel_collaboration_candidate",
        operation="respond_travel_collaboration",
        entity=entity,
        text=text,
        ticket=admission.tickets[0].as_dict(),
    )
    context = bind_business_execution_context(typed, _business_context())
    compilation = Phase2BusinessCommandCompiler().compile(
        typed,
        context,
        cases=(),
    )
    assert compilation.command is not None

    drift_store = InMemoryAdmissionTicketStore((typed.admission_ticket,))
    drift_executor = InMemoryBusinessExecutor(admission_ticket_store=drift_store)
    drift_executor.seed_travel_candidate(
        tenant_id="tenant-1",
        candidate_id="candidate-1",
        travel_intent_ids=(),
        participant_ids=("user-1", "user-2"),
        destination="Nanjing",
        overlap_start=NOW,
        overlap_end=NOW + timedelta(days=1),
    )
    drift_executor.dispatch_travel_notifications("candidate-1")
    drifted = compilation.command.__class__(
        command_id=compilation.command.command_id,
        candidate_id=compilation.command.candidate_id,
        response="decline",
        expected_version=compilation.command.expected_version,
    )
    drift_receipt = drift_executor.execute(drifted, context)
    assert drift_receipt.actual_write is False
    assert drift_receipt.error_code == "admission_ticket_claims_mismatch"
    assert drift_store.status(typed.admission_ticket["ticket_id"]) == "issued"

    store = InMemoryAdmissionTicketStore((typed.admission_ticket,))
    executor = InMemoryBusinessExecutor(admission_ticket_store=store)
    executor.seed_travel_candidate(
        tenant_id="tenant-1",
        candidate_id="candidate-1",
        travel_intent_ids=(),
        participant_ids=("user-1", "user-2"),
        destination="Nanjing",
        overlap_start=NOW,
        overlap_end=NOW + timedelta(days=1),
    )
    executor.dispatch_travel_notifications("candidate-1")
    first = executor.execute(compilation.command, context)
    duplicate = executor.execute(compilation.command, context)

    assert first.actual_write is True
    assert duplicate.status == "duplicate"
    assert duplicate.actual_write is False
    assert executor.travel_candidates["candidate-1"].version == 3
    assert store.status(typed.admission_ticket["ticket_id"]) == "consumed"
