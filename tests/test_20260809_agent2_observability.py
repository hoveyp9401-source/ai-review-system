from __future__ import annotations

import json
import inspect
import logging
from datetime import datetime
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import httpx

from app.agent2.tool_calling import canary_service
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekTimeoutError,
    DeepSeekToolCallingAdapter,
    _with_canary_turn_state,
)
from app import stream_runner
from app.agent2.tool_calling.canary_service import (
    CanaryIngressOutcome,
    build_canary_response_payload,
    build_canary_persisted_response_payload,
    canary_provider_response_payload,
)
from app.stream_runner import (
    _apply_canary_observability,
    _apply_reply_observability,
    _canary_stream_status,
    _log_stream_timing,
    _reply_with_observability,
    StreamReplyObservation,
)


@pytest.mark.asyncio
async def test_canary_delivery_reports_whether_sender_was_called() -> None:
    calls: list[str] = []

    async def sender() -> None:
        calls.append("sent")

    enabled = SimpleNamespace(messages_enabled=True)
    suppressed = SimpleNamespace(messages_enabled=False)

    assert await canary_service.deliver_canary_message_if_enabled(
        enabled, sender
    ) is True
    assert calls == ["sent"]
    assert await canary_service.deliver_canary_message_if_enabled(
        suppressed, sender
    ) is False
    assert calls == ["sent"]


def test_stream_timing_uses_agent2_model_attempts_as_authoritative_evidence(
    caplog,
) -> None:
    job = SimpleNamespace(
        message=SimpleNamespace(message_id="message-1", message_type="text"),
        message_type="text",
        text="查看我的日报",
    )
    timings = {
        "agent2_model_call_count": 1,
        "agent2_model_request_attempt_count": 1,
        "agent2_model_transport_retry_count": 0,
    }

    with caplog.at_level(logging.INFO, logger="ai_review_stream"):
        _log_stream_timing(
            job=job,
            dingtalk_user_id="user-1",
            user_name="测试用户",
            timings=timings,
            status="tool_call_canary_processed",
        )

    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("stream timing ")
    )
    payload = json.loads(message.removeprefix("stream timing "))

    assert payload["entered_llm"] is True
    assert payload["fast_path"] is False
    assert payload["agent2_model_call_count"] == 1
    assert payload["agent2_model_request_attempt_count"] == 1
    assert payload["agent2_model_transport_retry_count"] == 0


def test_current_agent2_timing_never_uses_legacy_latency_as_model_evidence(
    caplog,
) -> None:
    job = SimpleNamespace(
        message=SimpleNamespace(message_id="message-legacy", message_type="text"),
        message_type="text",
        text="查看我的日报",
    )
    timings = {
        "agent2_message_processing_status": "consumed",
        "agent2_model_call_count": 0,
        "agent2_model_request_attempt_count": 0,
        "llm_intent_seconds": 9.9,
    }

    with caplog.at_level(logging.INFO, logger="ai_review_stream"):
        _log_stream_timing(
            job=job,
            dingtalk_user_id="user-legacy",
            user_name="测试用户",
            timings=timings,
            status="tool_call_canary_failed",
        )

    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("stream timing ")
    )
    payload = json.loads(message.removeprefix("stream timing "))

    assert payload["entered_llm"] is False
    assert payload["fast_path"] is True


def test_canary_outcome_model_evidence_is_copied_to_stream_timings() -> None:
    timings: dict[str, object] = {}
    outcome = CanaryIngressOutcome(
        owner="tool_call_core",
        reason="enabled",
        handled=True,
        actual_write=True,
        model_call_count=2,
        model_request_attempt_count=3,
        model_transport_retry_count=1,
        model_elapsed_seconds=0.5,
        model_result_status="success",
        tool_success_count=1,
        tool_no_op_count=1,
        tool_clarification_count=0,
        tool_blocked_count=0,
        tool_failure_count=0,
        user_visible_result="success",
        reply_formed=True,
    )

    _apply_canary_observability(timings, outcome)

    assert timings == {
        "agent2_model_call_count": 2,
        "agent2_model_request_attempt_count": 3,
        "agent2_model_transport_retry_count": 1,
        "agent2_model_elapsed_seconds": 0.5,
        "agent2_model_result_status": "success",
        "agent2_tool_success_count": 1,
        "agent2_tool_no_op_count": 1,
        "agent2_tool_clarification_count": 0,
        "agent2_tool_blocked_count": 0,
        "agent2_tool_failure_count": 0,
        "agent2_user_visible_result": "success",
        "agent2_reply_formed": True,
        "agent2_message_processing_status": "consumed",
        "agent2_business_result_status": "success",
        "agent2_business_transaction_status": "pending",
        "agent2_business_changed": True,
        "agent2_reply_status": "formed",
    }


@pytest.mark.asyncio
async def test_successful_canary_turn_exposes_actual_model_call_evidence(
    monkeypatch,
) -> None:
    resolution = SimpleNamespace(
        decision=SimpleNamespace(owner="tool_call_core", reason="enabled"),
        control=SimpleNamespace(messages_enabled=True),
        binding=SimpleNamespace(tenant_id="tenant-1"),
        capability=object(),
    )

    async def fake_resolve(*args, **kwargs):
        return resolution

    class FakeContextStore:
        def __init__(self, *args, **kwargs):
            pass

        async def load_recent_messages(self, *args, **kwargs):
            return []

    class FakeAssembler:
        def __init__(self, *args, **kwargs):
            pass

        async def assemble(self, request):
            return SimpleNamespace(allowed_tool_names=frozenset())

    class FakeRuntime:
        def open_session(self, **kwargs):
            return object()

    receipts = (
        SimpleNamespace(
            tool_name="query_today_report",
            status=SimpleNamespace(value="success"),
            changed=False,
            target_type="daily_report",
            target_id="report-1",
        ),
        SimpleNamespace(
            tool_name="query_today_report",
            status=SimpleNamespace(value="no_op"),
            changed=False,
            target_type="daily_report",
            target_id="report-1",
        ),
    )
    result = SimpleNamespace(
        final_content="已查询。",
        receipts=receipts,
        runtime_results=(),
        model_turns=(
            SimpleNamespace(response_metadata={"elapsed_seconds": 0.3}),
            SimpleNamespace(response_metadata={"elapsed_seconds": 0.2}),
        ),
        request_attempt_count=3,
        transport_retry_count=1,
    )

    class FakeAdapter:
        def __init__(self, *args, **kwargs):
            pass

        async def run_canary_turn(self, **kwargs):
            return result

    class FakeMetricsRecorder:
        def record(self, event):
            raise OSError("metrics sink unavailable")

    monkeypatch.setattr(
        canary_service,
        "resolve_tool_call_canary_route",
        fake_resolve,
    )
    monkeypatch.setattr(canary_service, "ProductionContextStore", FakeContextStore)
    monkeypatch.setattr(canary_service, "TrustedContextAssembler", FakeAssembler)
    monkeypatch.setattr(canary_service, "ProductionRuntime", FakeRuntime)
    monkeypatch.setattr(canary_service, "DeepSeekToolCallingAdapter", FakeAdapter)
    monkeypatch.setattr(canary_service, "CanaryMetricsRecorder", FakeMetricsRecorder)
    monkeypatch.setattr(
        canary_service,
        "_attach_performance_glossary",
        lambda context, **kwargs: context,
    )
    monkeypatch.setattr(
        canary_service,
        "_should_apply_personal_salutation",
        lambda receipts: False,
    )

    class Savepoint:
        is_active = True

        async def commit(self):
            self.is_active = False

        async def rollback(self):
            self.is_active = False

    class Session:
        async def begin_nested(self):
            return Savepoint()

    outcome = await canary_service.process_tool_call_canary_ingress(
        Session(),
        user=SimpleNamespace(
            id="user-1",
            name="测试用户",
            timezone="Asia/Shanghai",
        ),
        dingtalk_user_id="ding-user-1",
        user_text="查看我的日报",
        source_channel="test",
        conversation_id="conversation-1",
        source_message_id="message-1",
        settings=SimpleNamespace(
            timezone="Asia/Shanghai",
            llm_base_url="https://example.invalid",
        ),
        llm_client=SimpleNamespace(native_http_client=object()),
        now=datetime(2026, 8, 9, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert outcome.model_call_count == 2
    assert outcome.model_request_attempt_count == 3
    assert outcome.model_transport_retry_count == 1
    assert outcome.model_elapsed_seconds == 0.5
    assert outcome.model_result_status == "success"
    assert outcome.tool_success_count == 1
    assert outcome.tool_no_op_count == 1
    assert outcome.tool_clarification_count == 0
    assert outcome.tool_blocked_count == 0
    assert outcome.tool_failure_count == 0
    assert outcome.user_visible_result == "success"
    assert outcome.reply_formed is True


def test_stream_wires_success_and_fail_closed_model_evidence() -> None:
    source = inspect.getsource(stream_runner._handle_job)

    assert (
        "_apply_canary_observability(timings, tool_call_canary)"
        in source
    )
    assert (
        "_apply_canary_observability(timings, failure_outcome)"
        in source
    )
    assert source.count(
        "send_observation = await _reply_with_observability("
    ) >= 2
    assert source.count(
        "_apply_reply_observability(timings, send_observation)"
    ) >= 2
    assert source.count("_canary_stream_status(") >= 2


def test_fail_closed_outcome_preserves_model_attempt_evidence(
    monkeypatch,
) -> None:
    class FakeMetricsRecorder:
        def record(self, event):
            raise OSError("metrics sink unavailable")

    error = RuntimeError("provider timeout")
    error.model_call_count = 1
    error.request_attempt_count = 2
    error.transport_retry_count = 1
    error.model_elapsed_seconds = 0.75
    error.model_turns = ()
    monkeypatch.setattr(canary_service, "CanaryMetricsRecorder", FakeMetricsRecorder)
    monkeypatch.setattr(
        canary_service._model_audit_logger,
        "info",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("audit sink unavailable")
        ),
    )

    failure = canary_service._record_canary_execution_failure(
        error=error,
        messages_enabled=True,
        started=perf_counter(),
        source_message_id="message-2",
    )
    outcome = failure.outcome()

    assert outcome.model_call_count == 1
    assert outcome.model_request_attempt_count == 2
    assert outcome.model_transport_retry_count == 1
    assert outcome.model_elapsed_seconds == 0.75
    assert outcome.model_result_status == "failed"


@pytest.mark.asyncio
async def test_reply_observation_distinguishes_provider_acceptance_from_delivery(
    monkeypatch,
) -> None:
    async def fake_send(*args, **kwargs):
        return None

    monkeypatch.setattr(stream_runner, "_send_stream_reply", fake_send)
    handler = SimpleNamespace(
        settings=SimpleNamespace(stream_reply_timeout_seconds=3.0)
    )
    job = SimpleNamespace(
        message=SimpleNamespace(message_id="message-3")
    )

    observation = await _reply_with_observability(
        handler,
        object(),
        job,
        "回复正文",
    )

    assert observation.transport_status == "provider_accepted"
    assert observation.provider_accepted is True
    assert observation.delivery_verified is False
    assert observation.error_type == ""


@pytest.mark.asyncio
async def test_reply_observation_records_transport_failure_without_claiming_delivery(
    monkeypatch,
) -> None:
    async def fake_send(*args, **kwargs):
        raise TimeoutError("provider did not accept the request")

    monkeypatch.setattr(stream_runner, "_send_stream_reply", fake_send)
    handler = SimpleNamespace(
        settings=SimpleNamespace(stream_reply_timeout_seconds=3.0)
    )
    job = SimpleNamespace(
        message=SimpleNamespace(message_id="message-4")
    )

    observation = await _reply_with_observability(
        handler,
        object(),
        job,
        "回复正文",
    )

    assert observation.transport_status == "failed"
    assert observation.provider_accepted is False
    assert observation.delivery_verified is False
    assert observation.error_type == "TimeoutError"


def test_reply_observation_is_copied_to_separate_transport_fields() -> None:
    timings: dict[str, object] = {}
    observation = StreamReplyObservation(
        elapsed_seconds=0.25,
        transport_status="provider_accepted",
        provider_accepted=True,
        delivery_verified=False,
    )

    _apply_reply_observability(timings, observation)

    assert timings == {
        "dingtalk_send_seconds": 0.25,
        "agent2_transport_status": "provider_accepted",
        "agent2_provider_accepted": True,
        "agent2_delivery_verified": False,
        "agent2_transport_error_type": "",
        "agent2_delivery_status": "unverified",
    }


def test_persisted_canary_payload_keeps_processing_stages_separate() -> None:
    outcome = CanaryIngressOutcome(
        owner="tool_call_core",
        reason="enabled",
        message="已更新日报。",
        handled=True,
        actual_write=True,
        messages_enabled=True,
        model_call_count=2,
        model_request_attempt_count=2,
        tool_success_count=1,
        user_visible_result="success",
        reply_formed=True,
    )

    provider_payload = build_canary_response_payload(outcome)
    payload = build_canary_persisted_response_payload(outcome)
    observation = payload["_agent2_turn_observation_v1"]

    assert "_agent2_turn_observation_v1" not in provider_payload
    assert canary_provider_response_payload(payload) == provider_payload
    assert observation["message_processing_status"] == "consumed"
    assert observation["business_result_status"] == "success"
    assert observation["business_write_committed"] is True
    assert observation["reply_status"] == "formed"
    assert observation["transport_status"] == "pending"
    assert observation["delivery_status"] == "unverified"
    assert observation["model_call_count"] == 2
    assert observation["tool_success_count"] == 1
    assert "已更新日报" not in json.dumps(observation, ensure_ascii=False)


def test_stream_monitoring_failure_never_escapes_into_user_processing(
    monkeypatch,
) -> None:
    def fail_to_write(*args, **kwargs):
        raise OSError("monitor unavailable")

    monkeypatch.setattr(stream_runner.logger, "info", fail_to_write)
    job = SimpleNamespace(
        message=SimpleNamespace(message_id="message-5", message_type="text"),
        message_type="text",
        text="测试",
    )

    _log_stream_timing(
        job=job,
        dingtalk_user_id="user-5",
        user_name="测试用户",
        timings={"agent2_model_request_attempt_count": 1},
        status="tool_call_canary_processed",
    )


def test_stream_monitoring_p95_overhead_is_below_five_milliseconds(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        stream_runner.logger,
        "info",
        lambda *args, **kwargs: None,
    )
    job = SimpleNamespace(
        message=SimpleNamespace(message_id="message-6", message_type="text"),
        message_type="text",
        text="测试",
    )
    timings = {
        "agent2_model_call_count": 1,
        "agent2_model_request_attempt_count": 1,
        "agent2_tool_success_count": 1,
        "agent2_user_visible_result": "success",
        "agent2_reply_formed": True,
        "agent2_transport_status": "provider_accepted",
    }
    durations: list[float] = []

    for _ in range(300):
        started = perf_counter()
        _log_stream_timing(
            job=job,
            dingtalk_user_id="user-6",
            user_name="测试用户",
            timings=timings,
            status="tool_call_canary_processed",
        )
        durations.append(perf_counter() - started)

    p95 = sorted(durations)[int(len(durations) * 0.95) - 1]
    assert p95 < 0.005


@pytest.mark.asyncio
async def test_model_completion_records_its_actual_elapsed_time() -> None:
    class FakeHttpClient:
        async def post(self, url, **kwargs):
            return httpx.Response(
                200,
                json={
                    "id": "response-1",
                    "model": "model-1",
                    "created": 1,
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "好"},
                        }
                    ],
                    "usage": {},
                },
                request=httpx.Request("POST", url),
            )

    adapter = DeepSeekToolCallingAdapter(
        http_client=FakeHttpClient(),
        model="model-1",
        timeout_seconds=3.0,
        max_tool_loops=2,
        endpoint="https://example.invalid/chat/completions",
    )

    completion = await adapter._complete(
        messages=[{"role": "user", "content": "你好"}],
        tool_schemas=[],
        thinking_enabled=False,
    )

    assert isinstance(completion.metadata["elapsed_seconds"], float)
    assert completion.metadata["elapsed_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_model_timeout_preserves_elapsed_time_evidence() -> None:
    class FailingHttpClient:
        async def post(self, url, **kwargs):
            raise httpx.ReadTimeout(
                "timeout",
                request=httpx.Request("POST", url),
            )

    adapter = DeepSeekToolCallingAdapter(
        http_client=FailingHttpClient(),
        model="model-1",
        timeout_seconds=3.0,
        max_tool_loops=2,
        max_request_attempts=1,
        endpoint="https://example.invalid/chat/completions",
    )

    with pytest.raises(DeepSeekTimeoutError) as captured:
        await adapter._complete(
            messages=[{"role": "user", "content": "你好"}],
            tool_schemas=[],
            thinking_enabled=False,
        )

    assert captured.value.model_elapsed_seconds >= 0.0


def test_failed_model_completion_is_counted_as_a_model_call() -> None:
    error = DeepSeekTimeoutError(
        "timeout",
        request_attempt_count=2,
        transport_retry_count=1,
        model_elapsed_seconds=0.5,
    )

    enriched = _with_canary_turn_state(
        error,
        audits=[],
        model_turns=[],
    )

    assert enriched.model_call_count == 1
    assert enriched.request_attempt_count == 2


def test_canary_stream_status_does_not_call_failed_transport_processed() -> None:
    outcome = CanaryIngressOutcome(
        owner="tool_call_core",
        reason="enabled",
        handled=True,
        messages_enabled=True,
        user_visible_result="success",
        reply_formed=True,
    )
    failed_transport = StreamReplyObservation(
        elapsed_seconds=0.2,
        transport_status="failed",
        provider_accepted=False,
        delivery_verified=False,
        error_type="TimeoutError",
    )
    accepted_transport = StreamReplyObservation(
        elapsed_seconds=0.2,
        transport_status="provider_accepted",
        provider_accepted=True,
        delivery_verified=False,
    )

    assert (
        _canary_stream_status(outcome, failed_transport)
        == "tool_call_canary_reply_failed"
    )
    assert (
        _canary_stream_status(outcome, accepted_transport)
        == "tool_call_canary_processed"
    )


def test_webhook_persists_observation_but_never_returns_it_to_dingtalk() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "webhook.py"
    ).read_text(encoding="utf-8")

    assert "build_canary_persisted_response_payload(" in source
    assert "response_payload=persisted_response_payload" in source
    assert "resp = canary_provider_response_payload(" in source
