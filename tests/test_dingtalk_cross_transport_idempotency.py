from __future__ import annotations

from types import SimpleNamespace

from app.services.dingtalk import DingTalkIncomingMessage, build_idempotency_key
from app.stream_runner import _stream_idempotency_key, _stream_source_message_id


def _webhook_message(*, message_id: str | None) -> DingTalkIncomingMessage:
    return DingTalkIncomingMessage(
        dingtalk_user_id="ding-user-1",
        text="曲江国际明天去法院沟通",
        message_id=message_id,
        conversation_id="conversation-1",
        source="dingtalk_chatbot",
        session_webhook=None,
    )


def _stream_message(*, message_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        sender_staff_id="ding-user-1",
        sender_id="",
        conversation_id="conversation-1",
        create_at=1784073600000,
    )


def test_same_provider_message_uses_one_key_across_webhook_and_stream() -> None:
    webhook_key = build_idempotency_key(
        {"createAt": 1784073600000},
        _webhook_message(message_id="provider-message-1"),
    )
    stream_key = _stream_idempotency_key(
        _stream_message(message_id="provider-message-1"),  # type: ignore[arg-type]
        "曲江国际明天去法院沟通",
    )

    assert webhook_key == "dingtalk:provider-message-1"
    assert stream_key == webhook_key


def test_missing_provider_message_id_uses_one_logical_fingerprint() -> None:
    webhook_key = build_idempotency_key(
        {"createAt": 1784073600000},
        _webhook_message(message_id=None),
    )
    stream_key = _stream_idempotency_key(
        _stream_message(message_id=None),  # type: ignore[arg-type]
        "曲江国际明天去法院沟通",
    )

    assert webhook_key.startswith("dingtalk:sha256:")
    assert stream_key == webhook_key


def test_stream_runtime_uses_persisted_canonical_key_as_source_turn_id() -> None:
    message = _stream_message(message_id="provider-message-1")
    event = SimpleNamespace(
        idempotency_key="dingtalk:provider-message-1",
        external_message_id="provider-message-1",
        id="event-1",
    )

    assert (
        _stream_source_message_id(event, message, "曲江国际明天去法院沟通")
        == "dingtalk:provider-message-1"
    )
