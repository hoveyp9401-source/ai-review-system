from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from app.agent2.cognitive_core_v3 import SemanticInterpretation
from app.agent2.runtime.shadow_adapter import (
    InMemoryShadowLogSink,
    OfflineShadowEvaluator,
    ProductionShadowCandidateAdapter,
    ShadowAdapterConfig,
    ShadowRuntimeInput,
)
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot


class CountingInterpreter:
    def __init__(self, *, delay: float = 0.0):
        self.calls = 0
        self.delay = delay

    async def interpret(self, turn, state):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return SemanticInterpretation.from_payload(
            {
                "intents": ["daily_append"],
                "segments": [
                    {
                        "segment_id": "segment-1",
                        "text": turn.text,
                        "intents": ["daily_append"],
                        "entity_ids": ["event-1"],
                        "action_ids": ["capture-1"],
                    }
                ],
                "entities": [
                    {
                        "entity_id": "event-1",
                        "entity_type": "daily_event",
                        "value": turn.text,
                        "confidence": 1.0,
                        "attributes": {"field": "today_work"},
                    }
                ],
                "confidence": 1.0,
                "required_actions": [
                    {
                        "action_id": "capture-1",
                        "action_type": "capture_daily_event",
                        "intent": "daily_append",
                        "entity_ids": ["event-1"],
                    }
                ],
                "clarification_need": None,
                "context_update": {"current_goal": "daily_append"},
            }
        )


class RaisingInterpreter:
    def __init__(self):
        self.calls = 0

    async def interpret(self, turn, state):
        self.calls += 1
        raise RuntimeError("model unavailable")


class FailingLogSink:
    def record(self, event):
        raise RuntimeError("log unavailable")


def _input() -> ShadowRuntimeInput:
    actor_id = uuid5(NAMESPACE_URL, "offline-shadow-actor")
    return ShadowRuntimeInput(
        tenant_id="tenant-a",
        actor_id=actor_id,
        conversation_id="conversation-a",
        message_id="message-a",
        text="今天完成合同审核，手机号13800138000",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="readonly-production-copy",
        trace_id="trace-a",
        daily_snapshot=DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, "offline-shadow-report"),
            owner_user_id=actor_id,
            version=0,
            status="collecting",
            today_work=(),
            problems=(),
            tomorrow_plan=(),
            item_ids={},
        ),
    )


def test_shadow_kill_switch_prevents_runtime_evaluation():
    interpreter = CountingInterpreter()
    sink = InMemoryShadowLogSink()
    adapter = ProductionShadowCandidateAdapter(
        evaluator=OfflineShadowEvaluator(interpreter),
        config=ShadowAdapterConfig(enabled=True, kill_switch=True, pii_hash_salt="test-salt"),
        log_sink=sink,
    )

    observation = asyncio.run(adapter.observe(_input()))

    assert observation.status == "skipped"
    assert observation.reason == "kill_switch"
    assert interpreter.calls == 0


def test_shadow_observation_is_ephemeral_no_write_and_pii_minimized():
    interpreter = CountingInterpreter()
    sink = InMemoryShadowLogSink()
    request = _input()
    adapter = ProductionShadowCandidateAdapter(
        evaluator=OfflineShadowEvaluator(interpreter),
        config=ShadowAdapterConfig(
            enabled=True,
            kill_switch=False,
            pii_hash_salt="test-salt",
        ),
        log_sink=sink,
    )

    observation = asyncio.run(adapter.observe(request))
    payload = observation.as_mapping()
    event = sink.events[0]

    assert observation.status == "observed"
    assert observation.would_write is True
    assert observation.actual_write is False
    assert observation.legacy_fallback_used is False
    assert request.daily_snapshot.today_work == ()
    assert "reply" not in payload
    assert "text" not in payload
    assert "13800138000" not in str(event)
    assert "tenant-a" not in str(event)
    assert event["trace_id"] == "trace-a"
    assert event["input_text_hash"] == observation.input_text_hash


def test_shadow_timeout_opens_circuit_and_next_request_is_not_evaluated():
    interpreter = CountingInterpreter(delay=0.05)
    adapter = ProductionShadowCandidateAdapter(
        evaluator=OfflineShadowEvaluator(interpreter),
        config=ShadowAdapterConfig(
            enabled=True,
            kill_switch=False,
            timeout_seconds=0.001,
            circuit_failure_threshold=1,
            pii_hash_salt="test-salt",
        ),
        log_sink=InMemoryShadowLogSink(),
    )

    first = asyncio.run(adapter.observe(_input()))
    second = asyncio.run(adapter.observe(_input()))

    assert first.status == "failed"
    assert first.reason == "timeout"
    assert second.status == "skipped"
    assert second.reason == "circuit_open"
    assert interpreter.calls == 1


def test_shadow_failed_closed_counts_as_failure_and_opens_circuit():
    interpreter = RaisingInterpreter()
    adapter = ProductionShadowCandidateAdapter(
        evaluator=OfflineShadowEvaluator(interpreter),
        config=ShadowAdapterConfig(
            enabled=True,
            kill_switch=False,
            circuit_failure_threshold=2,
            pii_hash_salt="test-salt",
        ),
        log_sink=InMemoryShadowLogSink(),
    )

    first = asyncio.run(adapter.observe(_input()))
    second = asyncio.run(adapter.observe(_input()))
    third = asyncio.run(adapter.observe(_input()))

    assert (first.status, first.reason) == ("failed", "runtime_failed_closed")
    assert (second.status, second.reason) == ("failed", "runtime_failed_closed")
    assert (third.status, third.reason) == ("skipped", "circuit_open")
    assert interpreter.calls == 2


def test_shadow_log_failure_is_isolated_and_opens_circuit_without_escaping():
    interpreter = CountingInterpreter()
    adapter = ProductionShadowCandidateAdapter(
        evaluator=OfflineShadowEvaluator(interpreter),
        config=ShadowAdapterConfig(
            enabled=True,
            kill_switch=False,
            circuit_failure_threshold=1,
            pii_hash_salt="test-salt",
        ),
        log_sink=FailingLogSink(),
    )

    first = asyncio.run(adapter.observe(_input()))
    second = asyncio.run(adapter.observe(_input()))

    assert (first.status, first.reason) == ("failed", "log_failure")
    assert (second.status, second.reason) == ("skipped", "circuit_open")
    assert interpreter.calls == 1
