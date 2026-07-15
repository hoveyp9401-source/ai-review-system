from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from types import SimpleNamespace
from uuid import uuid4

from app.agent2.daily_shadow import evaluate_daily_shadow
from app.agent2.workflow_audit import (
    create_agent2_cognitive_v3_audit_event,
    create_agent2_selection_audit_event,
    create_agent2_workflow_audit_event,
)
from app.workflows.intake import IncomingMessageEnvelope


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self) -> None:
        self.added = []
        self.flush_count = 0

    def begin_nested(self):
        return _NestedTransaction()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


class _FakeReportInteractionEvent:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


def _install_fake_models(monkeypatch) -> None:
    fake_models = types.ModuleType("app.models")
    fake_models.ReportInteractionEvent = _FakeReportInteractionEvent
    monkeypatch.setitem(sys.modules, "app.models", fake_models)


def _settings(*, shadow_memory_enabled: bool = True):
    return SimpleNamespace(shadow_memory_enabled=shadow_memory_enabled, timezone="Asia/Shanghai")


def _incoming(text: str):
    return SimpleNamespace(dingtalk_user_id="dt-user-1", text=text)


def _envelope(text: str) -> IncomingMessageEnvelope:
    return IncomingMessageEnvelope(
        sender_id="user-1",
        sender_name="Test User",
        dingtalk_user_id="dt-user-1",
        source="test",
        raw_text=text,
        message_id="msg-1",
        conversation_id="conv-1",
    )


def test_agent2_workflow_audit_writes_gate_observation_without_raw_text_in_decision_json(monkeypatch):
    _install_fake_models(monkeypatch)
    text = "今天完成合同审核"
    envelope = _envelope(text)
    shadow = evaluate_daily_shadow(envelope, mode="protective_gate")
    session = _FakeSession()

    asyncio.run(
        create_agent2_workflow_audit_event(
            session=session,
            user=SimpleNamespace(id=uuid4()),
            incoming=_incoming(text),
            settings=_settings(),
            envelope=envelope,
            shadow=shadow,
            mode="protective_gate",
            observe_only_log=False,
        )
    )

    assert session.flush_count == 1
    assert len(session.added) == 1
    event = session.added[0]
    assert event.backend_action == "agent2_workflow_audit_gate"
    assert event.message_text == ""
    assert event.llm_decision_json["agent2"] is True
    assert event.llm_decision_json["audit_stage"] == "gate"
    assert event.llm_decision_json["route"]["raw_text_hash"]
    assert event.llm_decision_json["gate"]["gate"]["allow_legacy_daily"] is True
    assert text not in json.dumps(event.llm_decision_json, ensure_ascii=False)


def test_agent2_workflow_audit_respects_shadow_memory_switch(monkeypatch):
    _install_fake_models(monkeypatch)
    text = "今天完成合同审核"
    envelope = _envelope(text)
    shadow = evaluate_daily_shadow(envelope, mode="protective_gate")
    session = _FakeSession()

    asyncio.run(
        create_agent2_workflow_audit_event(
            session=session,
            user=SimpleNamespace(id=uuid4()),
            incoming=_incoming(text),
            settings=_settings(shadow_memory_enabled=False),
            envelope=envelope,
            shadow=shadow,
            mode="protective_gate",
            observe_only_log=False,
        )
    )

    assert session.added == []
    assert session.flush_count == 0


def test_cognitive_audit_persists_only_the_redacted_decision_projection(monkeypatch):
    _install_fake_models(monkeypatch)
    raw_business_text = "SENSITIVE_BUSINESS_TEXT_CONTACTED_COURT"
    authority_secret = "SENSITIVE_AUTHORITY_SCOPE_FACT"
    candidate_secret = "SENSITIVE_CANDIDATE_PAYLOAD"
    decision = SimpleNamespace(
        confidence=0.91,
        as_dict=lambda: {
            "contract_version": "cognitive_core.v3",
            "decision_id": "decision-1",
            "intents": ["case_progress"],
            "confidence": 0.91,
            "source_text_hash": "source-sha256",
            "admission_mode": "enforced",
            "segments": [
                {
                    "segment_id": "segment-1",
                    "text": raw_business_text,
                    "text_hash": "segment-sha256",
                    "intents": ["case_progress"],
                    "action_ids": ["action-1"],
                }
            ],
            "entities": [
                {
                    "entity_id": "entity-1",
                    "entity_type": "case_ref",
                    "value": raw_business_text,
                    "attributes": {"normalized_fact": authority_secret},
                }
            ],
            "required_actions": [
                {
                    "action_id": "action-1",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "parameters": {"raw_fact": raw_business_text},
                }
            ],
            "clarification_need": {
                "reason": "ambiguous_case",
                "missing_fields": ["case_id"],
                "question": raw_business_text,
            },
            "admission_tickets": [
                {
                    "ticket_id": "ticket-1",
                    "trace_id": "trace-1",
                    "decision_id": "admission-decision-1",
                    "action_id": "action-1",
                    "domain": "case",
                    "operation": "record_case_progress",
                    "ticket_status": "issued",
                    "authority_scope": {"raw_fact": authority_secret},
                    "object_ref": {"label": raw_business_text},
                    "fact_claims_sha256": "fact-sha256",
                }
            ],
            "admission_information_pendings": [
                {
                    "pending_id": "information-1",
                    "domain": "case",
                    "operation": "record_case_progress",
                    "pending_status": "active",
                    "question_snapshot": {"question": raw_business_text},
                }
            ],
            "admission_selection_requests": [
                {
                    "selection_request_id": "selection-request-1",
                    "domain": "case",
                    "operation": "record_case_progress",
                    "candidates": [
                        {"stable_id": "case-1", "label": candidate_secret}
                    ],
                    "continuation_payload": {"raw_fact": candidate_secret},
                }
            ],
            "admission_trace": {
                "trace_id": "trace-1",
                "trace_status": "evaluated",
                "admission_summary": "blocked",
                "failure_reason": "selection_required",
                "proposal_sha256": "proposal-sha256",
            },
        },
    )
    command_plan = SimpleNamespace(
        as_dict=lambda: {
            "decision_id": "decision-1",
            "daily_commands": [],
            "business_commands": [
                {
                    "command_id": "command-1",
                    "command_type": "record_case_progress_candidate",
                    "target_system": "case_progress",
                    "execution_mode": "candidate",
                    "payload": {"raw_fact": raw_business_text},
                    "admission_ticket": {"authority_scope": authority_secret},
                }
            ],
            "report_commands": [],
            "blocked_actions": [
                {
                    "action_id": "action-2",
                    "reason_code": "missing_information",
                    "detail": raw_business_text,
                    "metadata": {"candidate": candidate_secret},
                }
            ],
        }
    )
    state = SimpleNamespace(
        user_id="user-1",
        conversation_id="conv-1",
        version=4,
        current_goal=SimpleNamespace(intent="case_progress"),
        pending=(SimpleNamespace(pending_id="pending-legacy-1"),),
        selection_pending=(SimpleNamespace(pending_id="selection-1", status="active"),),
        as_payload=lambda: {
            "user_constraints": {
                "read_only": False,
                "sources": [raw_business_text],
            }
        },
    )
    result = SimpleNamespace(
        decision=decision,
        command_plan=command_plan,
        state=state,
        base_state=SimpleNamespace(version=3),
        state_persisted=False,
    )
    session = _FakeSession()

    asyncio.run(
        create_agent2_cognitive_v3_audit_event(
            session=session,
            user=SimpleNamespace(id=uuid4()),
            envelope=_envelope(raw_business_text),
            result=result,
            report_date=SimpleNamespace(),
        )
    )

    assert session.flush_count == 1
    event = session.added[0]
    serialized = json.dumps(event.__dict__, ensure_ascii=False, default=str)
    assert event.message_text == ""
    assert raw_business_text not in serialized
    assert authority_secret not in serialized
    assert candidate_secret not in serialized
    assert '"authority_scope"' not in serialized
    assert '"continuation_payload"' not in serialized
    assert '"user_visible_snapshot"' not in serialized
    audit = event.llm_decision_json
    assert audit["source_message_id"] == "msg-1"
    assert audit["cognitive_decision"]["decision_id"] == "decision-1"
    assert audit["cognitive_decision"]["ticket_refs"][0]["ticket_id"] == "ticket-1"
    assert audit["cognitive_decision"]["selection_request_refs"][0]["candidate_count"] == 1
    assert audit["command_plan"]["business_command_refs"][0]["command_id"] == "command-1"
    assert audit["command_plan"]["blocked_action_refs"][0]["reason_code"] == "missing_information"
    assert audit["conversation_state"]["selection_pending_refs"] == [
        {"pending_id": "selection-1", "status": "active"}
    ]

    repeated_session = _FakeSession()
    asyncio.run(
        create_agent2_cognitive_v3_audit_event(
            session=repeated_session,
            user=SimpleNamespace(id=uuid4()),
            envelope=_envelope(raw_business_text),
            result=result,
            report_date=SimpleNamespace(),
        )
    )
    assert repeated_session.added[0].llm_decision_json == audit


def test_selection_audit_omits_pending_continuation_and_outcome_business_text(monkeypatch):
    _install_fake_models(monkeypatch)
    raw_answer = "SENSITIVE_SELECTION_ANSWER"
    candidate_label = "SENSITIVE_CANDIDATE_LABEL"
    continuation_secret = "SENSITIVE_CONTINUATION_PAYLOAD"
    outcome_business_text = "SENSITIVE_OUTCOME_BUSINESS_TEXT"
    pending_after = SimpleNamespace(
        pending_id="selection-1",
        status="active",
        expected_conversation_state_version=8,
        candidates=(
            SimpleNamespace(stable_id="case-1", label=candidate_label),
            SimpleNamespace(stable_id="case-2", label="another sensitive label"),
        ),
        consumed_receipt_id="",
        invalidation_reason="",
        continuation_payload={"raw_fact": continuation_secret},
        as_dict=lambda: {
            "pending_id": "selection-1",
            "status": "active",
            "candidates": [{"label": candidate_label}],
            "continuation_payload": {"raw_fact": continuation_secret},
        },
    )
    receipt = SimpleNamespace(
        as_dict=lambda: {
            "receipt_id": "receipt-1",
            "receipt_type": "database",
            "status": "executed",
            "actual_write": True,
            "external_message_id": "provider-secret-id",
        }
    )
    outcome = SimpleNamespace(
        receipt_refs=(receipt,),
        as_dict=lambda: {
            "domain": "case_progress",
            "operation": "create",
            "object_ref": {
                "object_type": "case_progress",
                "stable_id": "progress-1",
                "label": outcome_business_text,
            },
            "business_status": "succeeded",
            "message_status": "not_applicable",
            "changed_fields": ["content"],
            "user_visible_snapshot": {"content": outcome_business_text},
            "blocking_reason": "",
            "receipt_refs": [receipt.as_dict()],
            "state_transition": {"from": "missing", "to": "active"},
            "actual_write": True,
            "metadata": {"raw_fact": outcome_business_text},
        },
    )
    resolution = SimpleNamespace(
        pending_id="selection-1",
        status="selected",
        selected_candidate_id="case-1",
        selected_candidate_version=3,
        reason="unique_selection",
        pending_after=pending_after,
    )
    turn_result = SimpleNamespace(
        resolution=resolution,
        outcome=outcome,
        actual_write=True,
    )
    session = _FakeSession()

    asyncio.run(
        create_agent2_selection_audit_event(
            session=session,
            user=SimpleNamespace(id=uuid4()),
            envelope=_envelope(raw_answer),
            turn_result=turn_result,
            report_date=SimpleNamespace(),
        )
    )

    assert session.flush_count == 1
    event = session.added[0]
    serialized = json.dumps(event.__dict__, ensure_ascii=False, default=str)
    assert event.message_text == ""
    assert raw_answer not in serialized
    assert candidate_label not in serialized
    assert continuation_secret not in serialized
    assert outcome_business_text not in serialized
    assert "provider-secret-id" not in serialized
    assert '"continuation_payload"' not in serialized
    assert '"user_visible_snapshot"' not in serialized
    audit = event.llm_decision_json
    assert audit["source_message_id"] == "msg-1"
    assert audit["source_text_sha256"] == hashlib.sha256(
        raw_answer.encode("utf-8")
    ).hexdigest()
    assert audit["pending_after"] == {
        "pending_id": "selection-1",
        "status": "active",
        "expected_conversation_state_version": 8,
        "candidate_count": 2,
        "consumed_receipt_id": "",
        "invalidation_reason": "",
    }
    assert audit["receipt_refs"] == [
        {
            "receipt_id": "receipt-1",
            "receipt_type": "database",
            "status": "executed",
            "actual_write": True,
            "reliable_delivery_evidence": False,
        }
    ]
    assert event.after_snapshot_json["outcome"] == {
        "domain": "case_progress",
        "operation": "create",
        "object_type": "case_progress",
        "object_id": "progress-1",
        "business_status": "succeeded",
        "message_status": "not_applicable",
        "changed_fields": ["content"],
        "blocking_reason": "",
        "actual_write": True,
        "would_write": False,
        "state_transition": {"from": "missing", "to": "active"},
        "receipt_refs": audit["receipt_refs"],
    }
