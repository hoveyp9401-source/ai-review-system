from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from uuid import uuid4

from app.agent2.daily_shadow import evaluate_daily_shadow
from app.agent2.workflow_audit import create_agent2_workflow_audit_event
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
    assert event.message_text == text
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
