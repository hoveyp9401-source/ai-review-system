from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import dingtalk_stream
import pytest

from app.agent2.memory.postgres import PersonalMemoryRecord
from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.tool_calling.canary_service import (
    CanaryIngressOutcome,
    canary_provider_response_payload,
)
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE
from app.agent2.tool_calling.production_store import ProductionContextStore
from app.config import Settings
from app.models import ReportInteractionEvent, User, WebhookEvent
from app.services.dingtalk import (
    DingTalkDeliveryError,
    DingTalkOutboundContentError,
)
from app.stream_runner import (
    DailyReviewStreamHandler,
    PersistedStreamIngress,
    StreamJob,
    _handle_job,
    _reply_with_observability,
)


NOW = datetime(2026, 8, 16, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
USER_ID = UUID("11111111-1111-4111-8111-111111111111")
TEAM_ID = UUID("22222222-2222-4222-8222-222222222222")
DINGTALK_USER_ID = "ding-delivery-user"
CONVERSATION_ID = "conversation-delivery"
DELIVERY_KEY = "_agent2_reply_delivery_v1"


def _delivery(
    *,
    channel: str,
    status: str,
    provider_reference: str | None,
    provider_accepted: bool,
    delivery_verified: bool,
    error: str | None = None,
    retry_safe_preacceptance_failure: bool = False,
) -> dict[str, Any]:
    record = {
        "schema_version": "agent2.reply.delivery.v1",
        "channel": channel,
        "provider_reference": provider_reference,
        "provider_accepted": provider_accepted,
        "delivery_verified": delivery_verified,
        "status": status,
        "error": error,
        "checked_at": NOW.isoformat(),
    }
    if retry_safe_preacceptance_failure:
        record["retry_safe_preacceptance_failure"] = True
    return record


def _event(
    *,
    suffix: str,
    reply: str,
    delivery: dict[str, Any] | None,
    received_at: datetime,
) -> WebhookEvent:
    response_payload: dict[str, Any] = {
        "msgtype": "text",
        "text": {"content": reply},
    }
    if delivery is not None:
        response_payload[DELIVERY_KEY] = delivery
    return WebhookEvent(
        id=uuid4(),
        idempotency_key=f"provider-message-{suffix}",
        platform="dingtalk",
        external_message_id=f"provider-message-{suffix}",
        dingtalk_user_id=DINGTALK_USER_ID,
        report_id=None,
        payload={
            "conversationId": CONVERSATION_ID,
            "text": {"content": f"user-{suffix}"},
        },
        response_payload=response_payload,
        status="processed",
        received_at=received_at,
        processed_at=received_at + timedelta(seconds=1),
        created_at=received_at,
        updated_at=received_at + timedelta(seconds=1),
    )


class _Rows:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return list(self._values)


def _statement_entity(statement: Any) -> type[Any] | None:
    descriptions = getattr(statement, "column_descriptions", ())
    if not descriptions:
        return None
    return descriptions[0].get("entity")


class _ReadSession:
    def __init__(self, events: tuple[WebhookEvent, ...]) -> None:
        self.events = events

    async def scalars(self, statement: Any) -> _Rows:
        entity = _statement_entity(statement)
        if entity is WebhookEvent:
            return _Rows(
                sorted(
                    self.events,
                    key=lambda row: row.received_at,
                    reverse=True,
                )
            )
        if entity in {ReportInteractionEvent, PersonalMemoryRecord}:
            return _Rows([])
        return _Rows([])

    async def execute(self, _statement: Any) -> _Rows:
        return _Rows([])


def _user() -> User:
    return User(
        id=USER_ID,
        dingtalk_user_id=DINGTALK_USER_ID,
        employee_no="DELIVERY-TEST",
        name="Delivery Test User",
        team_id=TEAM_ID,
        role="member",
        timezone="Asia/Shanghai",
        active=True,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=1),
    )


@pytest.mark.asyncio
async def test_production_context_excludes_known_failed_reply_but_keeps_unknown_history() -> None:
    """A known failed assistant send is not conversation evidence.

    Old rows without future delivery evidence remain explicitly unknown; this
    test does not backfill or pretend that they were delivered.
    """

    verified = _event(
        suffix="verified",
        reply="verified-assistant-reply",
        delivery=_delivery(
            channel="direct_robot",
            status="verified",
            provider_reference="direct-verified-1",
            provider_accepted=True,
            delivery_verified=True,
        ),
        received_at=NOW - timedelta(minutes=4),
    )
    accepted = _event(
        suffix="accepted",
        reply="accepted-unverified-assistant-reply",
        delivery=_delivery(
            channel="session_webhook",
            status="accepted_unverified",
            provider_reference=None,
            provider_accepted=True,
            delivery_verified=False,
        ),
        received_at=NOW - timedelta(minutes=3),
    )
    failed = _event(
        suffix="failed",
        reply="failed-assistant-reply",
        delivery=_delivery(
            channel="direct_robot",
            status="failed",
            provider_reference=None,
            provider_accepted=False,
            delivery_verified=False,
            error="TimeoutError",
        ),
        received_at=NOW - timedelta(minutes=2),
    )
    delivery_failed = _event(
        suffix="delivery-failed",
        reply="terminally-failed-assistant-reply",
        delivery=_delivery(
            channel="direct_robot",
            status="delivery_failed",
            provider_reference="direct-terminal-failure-1",
            provider_accepted=True,
            delivery_verified=False,
            error="DingTalkDeliveryError",
        ),
        received_at=NOW - timedelta(minutes=1, seconds=30),
    )
    historical = _event(
        suffix="historical",
        reply="historical-assistant-reply",
        delivery=None,
        received_at=NOW - timedelta(minutes=1),
    )
    store = ProductionContextStore(
        _ReadSession(
            (verified, accepted, failed, delivery_failed, historical)
        ),
        user=_user(),
        tenant_id="legal-daily-production-v1",
    )

    messages = await store.load_recent_messages(
        TrustedContextRequest(
            tenant_id="legal-daily-production-v1",
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            source_message_id="current-provider-message",
            timezone="Asia/Shanghai",
            server_now=NOW,
            conversation_kind="direct",
        ),
        namespace=CANARY_STATE_NAMESPACE,
        limit=12,
    )

    assistants = {
        item.content: item
        for item in messages
        if item.role == "assistant"
    }
    assert "verified-assistant-reply" in assistants
    assert "accepted-unverified-assistant-reply" in assistants
    assert "failed-assistant-reply" not in assistants
    assert "terminally-failed-assistant-reply" not in assistants
    assert "historical-assistant-reply" in assistants
    assert assistants["verified-assistant-reply"].delivery_status == "verified"
    assert (
        assistants["accepted-unverified-assistant-reply"].delivery_status
        == "accepted_unverified"
    )
    assert assistants["historical-assistant-reply"].delivery_status == "unknown"
    assert "delivery_status" not in assistants[
        "verified-assistant-reply"
    ].model_dump(mode="json")


class _HandleSession:
    def __init__(self, event: WebhookEvent) -> None:
        self.event = event
        self.commit_count = 0
        self.response_payload_snapshots: list[dict[str, Any]] = []

    async def __aenter__(self) -> _HandleSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(
        self,
        model: type[Any],
        identity: Any,
        **_kwargs: Any,
    ) -> Any:
        if model is WebhookEvent and identity == self.event.id:
            return self.event
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commit_count += 1
        self.response_payload_snapshots.append(
            deepcopy(self.event.response_payload)
        )

    async def rollback(self) -> None:
        return None


class _HandleSessionFactory:
    def __init__(self, event: WebhookEvent) -> None:
        self.session = _HandleSession(event)

    def __call__(self) -> _HandleSession:
        return self.session


class _PerformanceService:
    async def submit_text(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _stream_message(*, conversation_type: str, session_webhook: str | None) -> Any:
    return SimpleNamespace(
        message_id="provider-stream-message",
        sender_staff_id=DINGTALK_USER_ID,
        sender_id="",
        conversation_id=CONVERSATION_ID,
        conversation_type=conversation_type,
        message_type="text",
        session_webhook=session_webhook,
    )


async def _run_agent2_stream_turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    robot: Any,
    conversation_type: str,
    session_webhook: str | None,
) -> tuple[WebhookEvent, _HandleSession]:
    event = _event(
        suffix=str(uuid4()),
        reply="",
        delivery=None,
        received_at=NOW,
    )
    event.idempotency_key = "stream-idempotency-key"
    event.status = "processing"
    event.response_payload = {}
    event.processed_at = None
    factory = _HandleSessionFactory(event)
    monkeypatch.setattr("app.stream_runner.AsyncSessionLocal", factory)
    monkeypatch.setattr(
        "app.stream_runner.get_active_user_by_dingtalk_id",
        lambda *_args, **_kwargs: _async_value(_user()),
    )

    outcome = CanaryIngressOutcome(
        owner="tool_call_core",
        reason="enabled",
        message="agent2-reply",
        handled=True,
        actual_write=False,
        messages_enabled=True,
        model_call_count=1,
        model_request_attempt_count=1,
        model_result_status="success",
        user_visible_result="success",
        reply_formed=True,
    )

    async def process_agent2(*_args: Any, **_kwargs: Any) -> CanaryIngressOutcome:
        return outcome

    monkeypatch.setattr(
        "app.stream_runner.process_tool_call_canary_ingress",
        process_agent2,
    )
    message = _stream_message(
        conversation_type=conversation_type,
        session_webhook=session_webhook,
    )
    job = StreamJob(
        message=message,
        text="current-user-message",
        payload={
            "conversationId": CONVERSATION_ID,
            "text": {"content": "current-user-message"},
        },
        event_id=event.id,
        idempotency_key=event.idempotency_key,
        persisted_received_at=NOW,
        message_type="text",
    )
    settings = Settings()
    handler = DailyReviewStreamHandler(asyncio.Queue(), robot, settings)
    await _handle_job(
        job=job,
        handler=handler,
        settings=settings,
        performance_service=_PerformanceService(),
        report_service=SimpleNamespace(
            extractor=SimpleNamespace(client=object())
        ),
        robot=robot,
    )
    return event, factory.session


async def _async_value(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_direct_agent2_reply_persists_verified_provider_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"raw_send": 0, "verified_send": 0, "wait": 0}

    class Robot:
        def has_enterprise_app(self) -> bool:
            return True

        async def send_session_webhook_text(self, **_kwargs: Any) -> None:
            raise AssertionError(
                "a direct conversation must not be downgraded "
                "to session-only evidence"
            )

        async def send_robot_direct_text(self, **_kwargs: Any) -> dict[str, Any]:
            calls["raw_send"] += 1
            return {"processQueryKey": "direct-provider-1"}

        async def send_robot_direct_text_verified(self, **_kwargs: Any) -> dict[str, Any]:
            calls["verified_send"] += 1
            return {
                "processQueryKey": "direct-provider-1",
                "deliveryVerified": True,
                "deliveryStatus": "SUCCESS",
                "deliveryRecipientUserIds": [DINGTALK_USER_ID],
            }

        async def wait_for_robot_direct_delivery(self, **_kwargs: Any) -> dict[str, Any]:
            calls["wait"] += 1
            return {
                "sendStatus": "SUCCESS",
                "messageReadInfoList": [
                    {"userId": DINGTALK_USER_ID, "readStatus": "UNREAD"}
                ],
            }

    event, session = await _run_agent2_stream_turn(
        monkeypatch,
        robot=Robot(),
        conversation_type="1",
        session_webhook="https://example.invalid/private-session",
    )

    record = event.response_payload[DELIVERY_KEY]
    assert calls == {"raw_send": 1, "verified_send": 0, "wait": 1}
    assert record["channel"] == "direct_robot"
    assert record["provider_reference"] == "direct-provider-1"
    assert record["provider_accepted"] is True
    assert record["delivery_verified"] is True
    assert record["status"] == "verified"
    assert record["error"] is None
    assert datetime.fromisoformat(record["checked_at"]).tzinfo is not None
    accepted_positions = [
        index
        for index, snapshot in enumerate(session.response_payload_snapshots)
        if (snapshot.get(DELIVERY_KEY) or {}).get("status")
        == "accepted_unverified"
    ]
    verified_positions = [
        index
        for index, snapshot in enumerate(session.response_payload_snapshots)
        if (snapshot.get(DELIVERY_KEY) or {}).get("status") == "verified"
    ]
    assert accepted_positions
    assert verified_positions
    assert accepted_positions[0] < verified_positions[-1]


@pytest.mark.asyncio
async def test_group_session_reply_is_accepted_but_never_claimed_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Robot:
        async def send_session_webhook_text(self, **_kwargs: Any) -> None:
            return None

        async def send_robot_direct_text(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("a group reply must stay on its session webhook")

        async def send_robot_direct_text_verified(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("a group reply has no direct delivery receipt")

    event, _session = await _run_agent2_stream_turn(
        monkeypatch,
        robot=Robot(),
        conversation_type="2",
        session_webhook="https://example.invalid/group-session",
    )

    record = event.response_payload[DELIVERY_KEY]
    assert record == {
        "schema_version": "agent2.reply.delivery.v1",
        "channel": "session_webhook",
        "provider_reference": None,
        "provider_accepted": True,
        "delivery_verified": False,
        "status": "accepted_unverified",
        "error": None,
        "checked_at": record["checked_at"],
    }
    assert datetime.fromisoformat(record["checked_at"]).tzinfo is not None
    assert "https://example.invalid/group-session" not in str(record)
    assert "https://example.invalid/group-session" not in str(
        event.response_payload
    )


@pytest.mark.asyncio
async def test_proven_local_preacceptance_failure_keeps_business_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Robot:
        def has_enterprise_app(self) -> bool:
            return True

        async def send_robot_direct_text(self, **_kwargs: Any) -> dict[str, Any]:
            raise DingTalkOutboundContentError("local validation blocked send")

    event, _session = await _run_agent2_stream_turn(
        monkeypatch,
        robot=Robot(),
        conversation_type="1",
        session_webhook=None,
    )

    assert event.status == "processed"
    assert event.response_payload["text"]["content"] == "agent2-reply"
    record = event.response_payload[DELIVERY_KEY]
    assert record["channel"] == "direct_robot"
    assert record["provider_reference"] is None
    assert record["provider_accepted"] is False
    assert record["delivery_verified"] is False
    assert record["status"] == "failed"
    assert record["error"] == "DingTalkOutboundContentError"
    assert record["retry_safe_preacceptance_failure"] is True
    assert datetime.fromisoformat(record["checked_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_direct_delivery_verification_timeout_keeps_acceptance_and_never_becomes_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Robot:
        async def send_robot_direct_text(self, **_kwargs: Any) -> dict[str, Any]:
            return {"processQueryKey": "direct-provider-timeout"}

        async def wait_for_robot_direct_delivery(self, **_kwargs: Any) -> dict[str, Any]:
            raise TimeoutError("delivery check timed out")

    event, _session = await _run_agent2_stream_turn(
        monkeypatch,
        robot=Robot(),
        conversation_type="1",
        session_webhook=None,
    )

    record = event.response_payload[DELIVERY_KEY]
    assert record["channel"] == "direct_robot"
    assert record["provider_reference"] == "direct-provider-timeout"
    assert record["provider_accepted"] is True
    assert record["delivery_verified"] is False
    assert record["status"] == "accepted_unverified"
    assert record["error"] == "TimeoutError"
    assert "retry_safe_preacceptance_failure" not in record


@pytest.mark.asyncio
async def test_terminal_direct_delivery_failure_is_known_failed_and_never_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"send": 0, "wait": 0}

    class Robot:
        async def send_robot_direct_text(
            self,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls["send"] += 1
            return {"processQueryKey": "direct-provider-terminal-failure"}

        async def wait_for_robot_direct_delivery(
            self,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls["wait"] += 1
            raise DingTalkDeliveryError(
                "DingTalk reported terminal delivery failure",
                terminal_failure=True,
            )

    event, session = await _run_agent2_stream_turn(
        monkeypatch,
        robot=Robot(),
        conversation_type="1",
        session_webhook=None,
    )

    assert calls == {"send": 1, "wait": 1}
    assert event.status == "processed"
    record = event.response_payload[DELIVERY_KEY]
    assert record == {
        "schema_version": "agent2.reply.delivery.v1",
        "channel": "direct_robot",
        "provider_reference": "direct-provider-terminal-failure",
        "provider_accepted": True,
        "delivery_verified": False,
        "status": "delivery_failed",
        "error": "DingTalkDeliveryError",
        "checked_at": record["checked_at"],
    }
    assert "retry_safe_preacceptance_failure" not in record
    assert any(
        (snapshot.get(DELIVERY_KEY) or {}).get("status")
        == "accepted_unverified"
        for snapshot in session.response_payload_snapshots
    )
    assert not any(
        (snapshot.get(DELIVERY_KEY) or {}).get(
            "retry_safe_preacceptance_failure"
        )
        for snapshot in session.response_payload_snapshots
    )


@pytest.mark.asyncio
async def test_direct_missing_provider_key_is_unknown_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Robot:
        async def send_robot_direct_text(self, **_kwargs: Any) -> dict[str, Any]:
            return {}

    event, _session = await _run_agent2_stream_turn(
        monkeypatch,
        robot=Robot(),
        conversation_type="1",
        session_webhook=None,
    )

    record = event.response_payload[DELIVERY_KEY]
    assert record["channel"] == "direct_robot"
    assert record["provider_reference"] is None
    assert record["provider_accepted"] is False
    assert record["delivery_verified"] is False
    assert record["status"] == "unknown"
    assert "retry_safe_preacceptance_failure" not in record


@pytest.mark.asyncio
async def test_direct_malformed_acceptance_payload_is_unknown_and_never_sent_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"send": 0, "wait": 0}

    class Robot:
        async def send_robot_direct_text(self, **_kwargs: Any) -> Any:
            calls["send"] += 1
            return ["unexpected-provider-shape"]

        async def wait_for_robot_direct_delivery(
            self,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls["wait"] += 1
            raise AssertionError("a malformed acceptance has no safe query key")

    event, _session = await _run_agent2_stream_turn(
        monkeypatch,
        robot=Robot(),
        conversation_type="1",
        session_webhook=None,
    )

    assert calls == {"send": 1, "wait": 0}
    assert event.status == "processed"
    record = event.response_payload[DELIVERY_KEY]
    assert record["status"] == "unknown"
    assert record["provider_accepted"] is False
    assert record["delivery_verified"] is False
    assert record["error"] == "InvalidDirectAcceptancePayload"
    assert "retry_safe_preacceptance_failure" not in record


@pytest.mark.asyncio
async def test_acceptance_persistence_failure_never_triggers_a_second_send() -> None:
    calls = {"send": 0, "wait": 0}

    class Robot:
        async def send_robot_direct_text(
            self,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls["send"] += 1
            return {"processQueryKey": "accepted-before-db-failure"}

        async def wait_for_robot_direct_delivery(
            self,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls["wait"] += 1
            raise AssertionError(
                "delivery verification must wait for durable acceptance"
            )

    class FailingSession:
        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            raise ConnectionError("database unavailable after acceptance")

    event = _event(
        suffix="acceptance-persistence-failure",
        reply="agent2-reply",
        delivery=None,
        received_at=NOW,
    )
    message = _stream_message(
        conversation_type="1",
        session_webhook=None,
    )
    robot = Robot()
    observation = await _reply_with_observability(
        DailyReviewStreamHandler(asyncio.Queue(), robot, Settings()),
        robot,
        StreamJob(message=message, text="current-user-message", payload={}),
        "agent2-reply",
        session=FailingSession(),
        event=event,
    )

    assert calls == {"send": 1, "wait": 0}
    assert observation.transport_status == "provider_accepted"
    assert observation.provider_accepted is True
    assert observation.delivery_verified is False
    assert observation.error_type == "DeliveryEvidencePersistenceError"


def _callback(payload: dict[str, Any]) -> dingtalk_stream.CallbackMessage:
    callback = dingtalk_stream.CallbackMessage()
    callback.data = payload
    return callback


async def _drain_reply_task() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "status",
        "retry_safe_preacceptance_failure",
        "failure_error",
        "expected_calls",
        "persisted_reply",
    ),
    [
        ("verified", False, None, [], "cached-agent2-reply"),
        (
            "accepted_unverified",
            False,
            None,
            [],
            "cached-agent2-reply",
        ),
        ("unknown", False, None, [], "cached-agent2-reply"),
        ("unknown_record", False, None, [], "cached-agent2-reply"),
        ("malformed", False, None, [], "cached-agent2-reply"),
        # A normal timeout is ambiguous: DingTalk might already have accepted
        # the request, so an old failed-looking record is not retryable.
        (
            "failed",
            False,
            "TimeoutError",
            [],
            "cached-agent2-reply",
        ),
        (
            "failed",
            False,
            "ConnectionError",
            [],
            "cached-agent2-reply",
        ),
        (
            "delivery_failed",
            False,
            "DingTalkDeliveryError",
            [],
            "cached-agent2-reply",
        ),
        # Only a server-owned proof that the request never left this process
        # may recover an otherwise cached reply.
        (
            "failed",
            True,
            "DingTalkOutboundContentError",
            ["cached-agent2-reply"],
            "cached-agent2-reply",
        ),
        # A stale ingress snapshot may not send a different reply than the
        # current row protected by the retry claim lock.
        (
            "failed",
            True,
            "DingTalkOutboundContentError",
            [],
            "different-current-row-reply",
        ),
    ],
)
async def test_duplicate_callback_recovers_only_a_known_pre_acceptance_failure(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    retry_safe_preacceptance_failure: bool,
    failure_error: str | None,
    expected_calls: list[str],
    persisted_reply: str,
) -> None:
    calls: list[str] = []
    delivery = None
    if status == "malformed":
        delivery = {
            "schema_version": "agent2.reply.delivery.v1",
            "channel": "direct_robot",
            "provider_reference": None,
            "provider_accepted": "false",
            "delivery_verified": False,
            "status": "failed",
            "error": "untrusted legacy shape",
            "checked_at": NOW.isoformat(),
            "retry_safe_preacceptance_failure": True,
        }
    elif status != "unknown":
        accepted = status in {
            "verified",
            "accepted_unverified",
            "delivery_failed",
        }
        normalized_status = (
            "unknown" if status == "unknown_record" else status
        )
        delivery = _delivery(
            channel=(
                "session_webhook"
                if normalized_status == "accepted_unverified"
                else "direct_robot"
            ),
            status=normalized_status,
            provider_reference=(
                "direct-provider-1"
                if normalized_status in {"verified", "delivery_failed"}
                else None
            ),
            provider_accepted=accepted,
            delivery_verified=normalized_status == "verified",
            error=(
                "DingTalkOutboundContentError"
                if retry_safe_preacceptance_failure
                else failure_error
            ),
            retry_safe_preacceptance_failure=(
                retry_safe_preacceptance_failure
            ),
        )
    payload = {
        "msgtype": "text",
        "text": {"content": "cached-agent2-reply"},
    }
    if delivery is not None:
        payload[DELIVERY_KEY] = delivery
    duplicate_event = _event(
        suffix=str(uuid4()),
        reply=persisted_reply,
        delivery=delivery,
        received_at=NOW,
    )
    duplicate_event.response_payload = deepcopy(payload)
    duplicate_event.response_payload["text"]["content"] = persisted_reply
    duplicate_event.external_message_id = "duplicate-provider-message"

    async def duplicate_ingress(_job: StreamJob) -> PersistedStreamIngress:
        return PersistedStreamIngress(
            event_id=duplicate_event.id,
            idempotency_key="duplicate-idempotency",
            inserted=False,
            status="processed",
            response_payload=payload,
            payload={},
            received_at=NOW,
        )

    monkeypatch.setattr("app.stream_runner._persist_stream_ingress", duplicate_ingress)
    duplicate_factory = _HandleSessionFactory(duplicate_event)
    monkeypatch.setattr("app.stream_runner.AsyncSessionLocal", duplicate_factory)

    class Robot:
        async def send_session_webhook_text(self, *, text: str, **_kwargs: Any) -> None:
            calls.append(text)

        async def send_robot_direct_text(self, *, text: str, **_kwargs: Any) -> dict[str, Any]:
            calls.append(text)
            return {"processQueryKey": "unexpected-resend"}

    handler = DailyReviewStreamHandler(asyncio.Queue(), Robot(), Settings())
    result = await handler.process(
        _callback(
            {
                "msgtype": "text",
                "msgId": "duplicate-provider-message",
                "senderStaffId": DINGTALK_USER_ID,
                "conversationId": CONVERSATION_ID,
                "conversationType": "1",
                "sessionWebhook": "https://example.invalid/private-session",
                "createAt": 1786887552000,
                "text": {"content": "current-user-message"},
            }
        )
    )
    await _drain_reply_task()

    assert result == (dingtalk_stream.AckMessage.STATUS_OK, "duplicate")
    assert calls == expected_calls
    if retry_safe_preacceptance_failure and expected_calls:
        recovered = duplicate_event.response_payload[DELIVERY_KEY]
        assert recovered["status"] == "accepted_unverified"
        assert recovered["provider_reference"] == "unexpected-resend"
        assert "retry_safe_preacceptance_failure" not in recovered


@pytest.mark.asyncio
async def test_batched_follower_is_never_a_standalone_cached_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the canonical batch leader may own one transport attempt."""

    calls: list[str] = []
    payload = {
        "msgtype": "text",
        "text": {"content": "follower-must-not-send"},
        "_agent2_tool_call_canary": {
            "batch_id": "batch-delivery-test",
            "delivery": "batched_follower",
            "leader_event_id": str(uuid4()),
        },
        DELIVERY_KEY: _delivery(
            channel="direct_robot",
            status="failed",
            provider_reference=None,
            provider_accepted=False,
            delivery_verified=False,
            error="DingTalkOutboundContentError",
            retry_safe_preacceptance_failure=True,
        ),
    }
    duplicate_event = _event(
        suffix=str(uuid4()),
        reply="follower-must-not-send",
        delivery=payload[DELIVERY_KEY],
        received_at=NOW,
    )
    duplicate_event.response_payload = payload

    async def duplicate_ingress(_job: StreamJob) -> PersistedStreamIngress:
        return PersistedStreamIngress(
            event_id=duplicate_event.id,
            idempotency_key="batched-follower-duplicate",
            inserted=False,
            status="processed",
            response_payload=payload,
            payload={},
            received_at=NOW,
        )

    monkeypatch.setattr("app.stream_runner._persist_stream_ingress", duplicate_ingress)
    monkeypatch.setattr(
        "app.stream_runner.AsyncSessionLocal",
        _HandleSessionFactory(duplicate_event),
    )

    class Robot:
        async def send_session_webhook_text(self, *, text: str, **_kwargs: Any) -> None:
            calls.append(text)

        async def send_robot_direct_text(self, *, text: str, **_kwargs: Any) -> dict[str, Any]:
            calls.append(text)
            return {"processQueryKey": "must-not-send"}

    handler = DailyReviewStreamHandler(asyncio.Queue(), Robot(), Settings())
    result = await handler.process(
        _callback(
            {
                "msgtype": "text",
                "msgId": "batched-follower-provider-message",
                "senderStaffId": DINGTALK_USER_ID,
                "conversationId": CONVERSATION_ID,
                "conversationType": "1",
                "sessionWebhook": "https://example.invalid/private-session",
                "createAt": 1786887552000,
                "text": {"content": "current-user-message"},
            }
        )
    )
    await _drain_reply_task()

    assert result == (dingtalk_stream.AckMessage.STATUS_OK, "duplicate")
    assert calls == []


def test_internal_delivery_record_is_removed_from_provider_payload() -> None:
    persisted = {
        "msgtype": "text",
        "text": {"content": "agent2-reply"},
        "_agent2_turn_observation_v1": {"transport_status": "verified"},
        DELIVERY_KEY: _delivery(
            channel="direct_robot",
            status="verified",
            provider_reference="direct-provider-1",
            provider_accepted=True,
            delivery_verified=True,
        ),
    }

    assert canary_provider_response_payload(persisted) == {
        "msgtype": "text",
        "text": {"content": "agent2-reply"},
    }
