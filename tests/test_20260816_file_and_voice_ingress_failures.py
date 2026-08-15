from __future__ import annotations

import asyncio
from typing import Any, Self

import dingtalk_stream
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent2.tool_calling.turn_batching import is_recoverable_ingress_payload
from app.api.webhook import router as webhook_router
from app.config import Settings, get_settings
from app.db import get_session
from app.models import WebhookEvent
from app.stream_runner import TEXT_PROCESS_FAILED, DailyReviewStreamHandler


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _MemoryDatabase:
    """External database adapter used to observe durable ingress facts."""

    def __init__(self) -> None:
        self.events: dict[Any, WebhookEvent] = {}

    def __call__(self) -> _MemorySession:
        return _MemorySession(self)


class _MemorySession:
    def __init__(self, database: _MemoryDatabase) -> None:
        self.database = database

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, statement: Any) -> _ScalarResult:
        table_name = statement.table.name
        values = statement.compile().params
        if table_name == "message_ingress_claims":
            return _ScalarResult(values["idempotency_key"])
        if table_name == "webhook_events":
            event = WebhookEvent(**values)
            self.database.events[event.id] = event
            return _ScalarResult(event.id)
        raise AssertionError(f"unexpected database write: {table_name}")

    async def get(self, model: type[Any], identity: Any) -> Any:
        assert model is WebhookEvent
        return self.database.events.get(identity)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


class _FailingDatabase:
    def __call__(self) -> _FailingDatabase:
        return self

    async def __aenter__(self) -> Self:
        raise RuntimeError("database unavailable")

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Robot:
    def __init__(
        self,
        *,
        asr_error: Exception | None = None,
        recognized_text: str = "",
    ) -> None:
        self.asr_error = asr_error
        self.recognized_text = recognized_text
        self.replies: list[str] = []

    async def recognize_audio(self, _download_code: str) -> str:
        if self.asr_error is not None:
            raise self.asr_error
        return self.recognized_text

    async def send_session_webhook_text(
        self,
        *,
        session_webhook: str,
        text: str,
    ) -> None:
        assert session_webhook == "https://example.invalid/session"
        self.replies.append(text)


def _callback(payload: dict[str, Any]) -> dingtalk_stream.CallbackMessage:
    callback = dingtalk_stream.CallbackMessage()
    callback.data = payload
    return callback


async def _allow_immediate_reply_to_finish() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


def _http_client(database: _MemoryDatabase, robot: _Robot) -> TestClient:
    app = FastAPI()
    app.include_router(webhook_router)

    async def _session_override():
        async with database() as session:
            yield session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: Settings()
    app.state.dingtalk_robot = robot
    return TestClient(app)


@pytest.mark.asyncio
async def test_word_attachment_is_durably_received_and_explicitly_reports_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _MemoryDatabase()
    robot = _Robot()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    monkeypatch.setattr("app.stream_runner.AsyncSessionLocal", database)
    handler = DailyReviewStreamHandler(queue, robot, Settings())

    status = await handler.process(
        _callback(
            {
                "msgtype": "file",
                "msgId": "word-message-1",
                "senderStaffId": "ding-user-1",
                "conversationId": "conversation-1",
                "conversationType": "1",
                "sessionWebhook": "https://example.invalid/session",
                "createAt": 1786875362000,
                "content": {
                    "downloadCode": "word-download-code-1",
                    "fileName": "daily-report.docx",
                    "fileSize": 4096,
                },
            }
        )
    )
    await _allow_immediate_reply_to_finish()

    assert status == (dingtalk_stream.AckMessage.STATUS_OK, "ok")
    assert queue.empty()
    assert len(database.events) == 1
    event = next(iter(database.events.values()))
    assert event.external_message_id == "word-message-1"
    assert event.payload["msgtype"] == "file"
    assert event.payload["content"] == {
        "downloadCode": "word-download-code-1",
        "fileName": "daily-report.docx",
        "fileSize": 4096,
    }
    assert is_recoverable_ingress_payload(event.payload)
    reply = str(event.response_payload["text"]["content"])
    assert robot.replies == [reply]
    assert "Word" in reply
    assert "暂不支持" in reply
    assert "语音" not in reply


@pytest.mark.asyncio
async def test_word_attachment_does_not_claim_receipt_when_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    robot = _Robot()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    monkeypatch.setattr(
        "app.stream_runner.AsyncSessionLocal",
        _FailingDatabase(),
    )
    handler = DailyReviewStreamHandler(queue, robot, Settings())

    status = await handler.process(
        _callback(
            {
                "msgtype": "file",
                "msgId": "word-message-db-failure",
                "senderStaffId": "ding-user-1",
                "conversationId": "conversation-1",
                "conversationType": "1",
                "sessionWebhook": "https://example.invalid/session",
                "createAt": 1786875362000,
                "content": {
                    "downloadCode": "word-download-code-db-failure",
                    "fileName": "daily-report.docx",
                    "fileSize": 4096,
                },
            }
        )
    )
    await _allow_immediate_reply_to_finish()

    assert status == (
        dingtalk_stream.AckMessage.STATUS_OK,
        "persistence failed",
    )
    assert queue.empty()
    assert robot.replies == [TEXT_PROCESS_FAILED]
    assert "已收到" not in robot.replies[0]


def test_http_word_attachment_uses_the_same_durable_non_agent2_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _MemoryDatabase()
    robot = _Robot()
    agent2_or_user_lookup_called = False

    async def _unexpected_user_lookup(*_args: Any, **_kwargs: Any) -> None:
        nonlocal agent2_or_user_lookup_called
        agent2_or_user_lookup_called = True
        raise AssertionError("unsupported file must stop before Agent2 user lookup")

    monkeypatch.setattr(
        "app.api.webhook.get_active_user_by_dingtalk_id",
        _unexpected_user_lookup,
    )
    payload = {
        "msgtype": "file",
        "msgId": "http-word-message-1",
        "senderStaffId": "ding-user-1",
        "conversationId": "conversation-1",
        "conversationType": "1",
        "content": {
            "downloadCode": "http-word-download-code-1",
            "fileName": "daily-report.docx",
            "fileSize": 4096,
        },
    }

    with _http_client(database, robot) as client:
        response = client.post("/webhooks/dingtalk", json=payload)

    assert response.status_code == 200
    reply = str(response.json()["text"]["content"])
    assert "Word" in reply
    assert "暂不支持" in reply
    assert agent2_or_user_lookup_called is False
    assert len(database.events) == 1
    event = next(iter(database.events.values()))
    assert event.status == "failed"
    assert event.payload["msgtype"] == "file"
    assert event.payload["content"] == payload["content"]
    assert event.response_payload == response.json()


def test_http_asr_failure_uses_the_same_durable_non_agent2_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request(
        "POST",
        "https://api.dingtalk.com/v1.0/robot/audio/asr",
    )
    asr_response = httpx.Response(404, request=request)
    database = _MemoryDatabase()
    robot = _Robot(
        asr_error=httpx.HTTPStatusError(
            "404 Not Found",
            request=request,
            response=asr_response,
        )
    )
    agent2_or_user_lookup_called = False

    async def _unexpected_user_lookup(*_args: Any, **_kwargs: Any) -> None:
        nonlocal agent2_or_user_lookup_called
        agent2_or_user_lookup_called = True
        raise AssertionError("failed ASR must stop before Agent2 user lookup")

    monkeypatch.setattr(
        "app.api.webhook.get_active_user_by_dingtalk_id",
        _unexpected_user_lookup,
    )
    payload = {
        "msgtype": "audio",
        "msgId": "http-voice-message-404",
        "senderStaffId": "ding-user-1",
        "conversationId": "conversation-1",
        "conversationType": "1",
        "content": {
            "downloadCode": "http-voice-download-code-404",
            "recognition": "",
            "duration": 8,
        },
    }

    with _http_client(database, robot) as client:
        response = client.post("/webhooks/dingtalk", json=payload)

    assert response.status_code == 200
    reply = str(response.json()["text"]["content"])
    assert "语音转写服务" in reply
    assert "暂时不可用" in reply
    assert "没有识别到语音文字" not in reply
    assert agent2_or_user_lookup_called is False
    assert len(database.events) == 1
    event = next(iter(database.events.values()))
    assert event.status == "failed"
    assert event.payload["msgtype"] == "audio"
    assert event.payload["content"] == payload["content"]
    assert str(event.error_message).startswith("voice_transcribe_failed:")
    assert event.response_payload == response.json()


@pytest.mark.parametrize(
    ("message_id", "content"),
    (
        ("http-voice-without-code", {"recognition": "", "duration": 8}),
        (
            "http-voice-empty-transcript",
            {
                "downloadCode": "http-empty-transcript-code",
                "recognition": "",
                "duration": 8,
            },
        ),
    ),
)
def test_http_unusable_voice_is_persisted_and_stops_before_agent2(
    monkeypatch: pytest.MonkeyPatch,
    message_id: str,
    content: dict[str, Any],
) -> None:
    database = _MemoryDatabase()
    robot = _Robot(recognized_text="")
    agent2_or_user_lookup_called = False

    async def _unexpected_user_lookup(*_args: Any, **_kwargs: Any) -> None:
        nonlocal agent2_or_user_lookup_called
        agent2_or_user_lookup_called = True
        raise AssertionError("unusable voice must stop before Agent2")

    monkeypatch.setattr(
        "app.api.webhook.get_active_user_by_dingtalk_id",
        _unexpected_user_lookup,
    )
    payload = {
        "msgtype": "audio",
        "msgId": message_id,
        "senderStaffId": "ding-user-1",
        "conversationId": "conversation-1",
        "conversationType": "1",
        "content": content,
    }

    with _http_client(database, robot) as client:
        response = client.post("/webhooks/dingtalk", json=payload)

    assert response.status_code == 200
    reply = str(response.json()["text"]["content"])
    assert "未进入日报处理" in reply
    assert agent2_or_user_lookup_called is False
    assert len(database.events) == 1
    event = next(iter(database.events.values()))
    assert event.status == "failed"
    assert event.payload["content"] == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_id", "content"),
    (
        ("voice-without-code", {"recognition": "", "duration": 8}),
        (
            "voice-empty-transcript",
            {
                "downloadCode": "empty-transcript-code",
                "recognition": "",
                "duration": 8,
            },
        ),
    ),
)
async def test_stream_unusable_voice_is_persisted_and_not_queued(
    monkeypatch: pytest.MonkeyPatch,
    message_id: str,
    content: dict[str, Any],
) -> None:
    database = _MemoryDatabase()
    robot = _Robot(recognized_text="")
    queue: asyncio.Queue[Any] = asyncio.Queue()
    monkeypatch.setattr("app.stream_runner.AsyncSessionLocal", database)
    handler = DailyReviewStreamHandler(queue, robot, Settings())

    status = await handler.process(
        _callback(
            {
                "msgtype": "audio",
                "msgId": message_id,
                "senderStaffId": "ding-user-1",
                "conversationId": "conversation-1",
                "conversationType": "1",
                "sessionWebhook": "https://example.invalid/session",
                "createAt": 1786887552000,
                "content": content,
            }
        )
    )
    await _allow_immediate_reply_to_finish()

    assert status == (
        dingtalk_stream.AckMessage.STATUS_OK,
        "voice unavailable",
    )
    assert queue.empty()
    assert len(database.events) == 1
    event = next(iter(database.events.values()))
    assert event.status == "failed"
    assert event.payload["content"] == content
    assert len(robot.replies) == 1
    assert "未进入日报处理" in robot.replies[0]


@pytest.mark.asyncio
async def test_asr_404_preserves_voice_metadata_and_reports_service_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = httpx.Request(
        "POST",
        "https://api.dingtalk.com/v1.0/robot/audio/asr",
    )
    response = httpx.Response(404, request=request)
    asr_error = httpx.HTTPStatusError(
        "404 Not Found",
        request=request,
        response=response,
    )
    database = _MemoryDatabase()
    robot = _Robot(asr_error=asr_error)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    monkeypatch.setattr("app.stream_runner.AsyncSessionLocal", database)
    handler = DailyReviewStreamHandler(queue, robot, Settings())

    status = await handler.process(
        _callback(
            {
                "msgtype": "audio",
                "msgId": "voice-message-404",
                "senderStaffId": "ding-user-1",
                "conversationId": "conversation-1",
                "conversationType": "1",
                "sessionWebhook": "https://example.invalid/session",
                "createAt": 1786887552000,
                "content": {
                    "downloadCode": "secret-voice-download-code-404",
                    "recognition": "",
                    "duration": 8,
                },
            }
        )
    )
    await _allow_immediate_reply_to_finish()

    assert status == (dingtalk_stream.AckMessage.STATUS_OK, "asr failed")
    assert queue.empty()
    assert len(database.events) == 1
    event = next(iter(database.events.values()))
    assert event.status == "failed"
    assert event.external_message_id == "voice-message-404"
    assert event.payload["msgtype"] == "audio"
    assert event.payload["content"] == {
        "downloadCode": "secret-voice-download-code-404",
        "recognition": "",
        "duration": 8,
    }
    assert is_recoverable_ingress_payload(event.payload)
    assert str(event.error_message).startswith("voice_transcribe_failed:")
    reply = str(event.response_payload["text"]["content"])
    assert robot.replies == [reply]
    assert "语音转写服务" in reply
    assert "暂时不可用" in reply
    assert "没有识别到语音文字" not in reply
    assert "secret-voice-download-code-404" not in caplog.text


@pytest.mark.asyncio
async def test_successful_voice_transcription_still_enters_the_agent2_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _MemoryDatabase()
    robot = _Robot(recognized_text="今天完成合同审核")
    queue: asyncio.Queue[Any] = asyncio.Queue()
    monkeypatch.setattr("app.stream_runner.AsyncSessionLocal", database)
    handler = DailyReviewStreamHandler(queue, robot, Settings())

    status = await handler.process(
        _callback(
            {
                "msgtype": "audio",
                "msgId": "voice-message-success",
                "senderStaffId": "ding-user-1",
                "conversationId": "conversation-1",
                "conversationType": "1",
                "sessionWebhook": "https://example.invalid/session",
                "createAt": 1786887552000,
                "content": {
                    "downloadCode": "voice-download-code-success",
                    "recognition": "",
                    "duration": 8,
                },
            }
        )
    )

    assert status == (dingtalk_stream.AckMessage.STATUS_OK, "ok")
    job = queue.get_nowait()
    assert job.text == "今天完成合同审核"
    assert robot.replies == []
    assert len(database.events) == 1
    event = next(iter(database.events.values()))
    assert event.status == "processing"
    assert is_recoverable_ingress_payload(event.payload)
