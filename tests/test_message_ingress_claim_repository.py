from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models import MessageIngressClaim, WebhookEvent
from app.repositories import create_webhook_event_once


class _ScalarResult:
    def __init__(self, value=None, rows=()):
        self.value = value
        self.rows = tuple(rows)

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


@pytest.mark.asyncio
async def test_new_provider_claim_and_webhook_event_share_one_generated_id() -> None:
    class Session:
        def __init__(self):
            self.candidate_id: UUID | None = None
            self.execute_count = 0

        async def execute(self, statement):
            self.execute_count += 1
            params = statement.compile().params
            if self.execute_count == 1:
                self.candidate_id = params["webhook_event_id"]
                return _ScalarResult("dingtalk:provider-1")
            assert params["id"] == self.candidate_id
            return _ScalarResult(self.candidate_id)

        async def get(self, model, identity):
            assert model is WebhookEvent
            assert identity == self.candidate_id
            return SimpleNamespace(id=identity, idempotency_key="dingtalk:provider-1")

    session = Session()

    event, inserted = await create_webhook_event_once(
        session,  # type: ignore[arg-type]
        idempotency_key="dingtalk:provider-1",
        external_message_id="provider-1",
        dingtalk_user_id="ding-user-1",
        payload={"text": "hello"},
    )

    assert inserted is True
    assert event.id == session.candidate_id
    assert session.execute_count == 2


@pytest.mark.asyncio
async def test_existing_provider_claim_returns_original_event_without_insert() -> None:
    event_id = uuid4()
    claim = MessageIngressClaim(
        idempotency_key="dingtalk-stream:provider-legacy",
        platform="dingtalk",
        external_message_id="provider-legacy",
        webhook_event_id=event_id,
    )
    original = SimpleNamespace(
        id=event_id,
        idempotency_key="dingtalk-stream:provider-legacy",
    )

    class Session:
        execute_count = 0

        async def execute(self, _statement):
            self.execute_count += 1
            if self.execute_count == 1:
                return _ScalarResult(None)
            return _ScalarResult(rows=(claim,))

        async def get(self, model, identity):
            assert model is WebhookEvent
            assert identity == event_id
            return original

    session = Session()

    event, inserted = await create_webhook_event_once(
        session,  # type: ignore[arg-type]
        idempotency_key="dingtalk:provider-legacy",
        external_message_id="provider-legacy",
        dingtalk_user_id="ding-user-1",
        payload={"text": "replay"},
    )

    assert inserted is False
    assert event is original
    assert session.execute_count == 2


@pytest.mark.asyncio
async def test_orphaned_provider_claim_fails_closed() -> None:
    claim = MessageIngressClaim(
        idempotency_key="dingtalk:provider-orphan",
        platform="dingtalk",
        external_message_id="provider-orphan",
        webhook_event_id=uuid4(),
    )

    class Session:
        execute_count = 0

        async def execute(self, _statement):
            self.execute_count += 1
            return _ScalarResult(None) if self.execute_count == 1 else _ScalarResult(rows=(claim,))

        async def get(self, _model, _identity):
            return None

    with pytest.raises(RuntimeError, match="incomplete or ambiguous"):
        await create_webhook_event_once(
            Session(),  # type: ignore[arg-type]
            idempotency_key="dingtalk:provider-orphan",
            external_message_id="provider-orphan",
            dingtalk_user_id="ding-user-1",
            payload={},
        )
