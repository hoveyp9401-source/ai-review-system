from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest

from app.agent2.tool_calling.assembly import TrustedContextRequest
from app.agent2.tool_calling.canary_config import canary_system_prompt
from app.agent2.tool_calling.deepseek_adapter import (
    _latest_verified_outbound_chat_message,
)
from app.agent2.tool_calling.outbound_context import (
    OUTBOUND_CONTEXT_BACKEND_ACTION,
    build_verified_outbound_context_event,
    record_verified_outbound_context_message,
    trusted_recent_outbound_message,
)
from app.agent2.tool_calling.production_store import (
    ProductionContextStore,
    _select_recent_messages_with_scheduled_outbound,
)
from app.agent2.tool_calling.registry import TOOL_REGISTRY
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedRecentMessage,
)


NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)
USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _user(**overrides):
    values = {
        "id": USER_ID,
        "dingtalk_user_id": "ding-user-1",
        "timezone": "Asia/Shanghai",
        "active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _receipt(**overrides):
    values = {
        "processQueryKey": "provider-verified-1",
        "deliveryVerified": True,
        "deliveryStatus": "SUCCESS",
        "deliveryRecipientUserIds": ["ding-user-1"],
    }
    values.update(overrides)
    return values


def _event(**overrides):
    values = {
        "user": _user(),
        "conversation_id": "cid-private-1",
        "message_text": "庞总，刚才的旧链路问题已经修复，可以再试试。",
        "source_message_id": "recovery-notice-v1:user-1",
        "delivery_receipt": _receipt(),
        "sent_at": NOW,
    }
    values.update(overrides)
    return build_verified_outbound_context_event(**values)


def _request(**overrides):
    values = {
        "tenant_id": "default",
        "user_id": USER_ID,
        "conversation_id": "cid-private-1",
        "source_message_id": "inbound-reply-1",
        "timezone": "Asia/Shanghai",
        "server_now": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return TrustedContextRequest(**values)


def test_verified_outbound_event_is_bound_to_user_conversation_and_delivery():
    event = _event()

    assert event.backend_action == OUTBOUND_CONTEXT_BACKEND_ACTION
    assert event.user_id == USER_ID
    assert event.dingtalk_user_id == "ding-user-1"
    assert event.llm_decision_json["conversation_id"] == "cid-private-1"
    assert event.llm_decision_json["message_status"] == "delivery_confirmed"
    assert event.llm_decision_json["business_write"] is False

    message = trusted_recent_outbound_message(
        event,
        request=_request(),
        dingtalk_user_id="ding-user-1",
    )
    assert message is not None
    assert message.role == "assistant"
    assert message.content == event.message_text
    assert message.source_message_id.startswith("outbound:")


def test_prompt_prioritizes_natural_followup_to_verified_outbound_message():
    prompt = " ".join(canary_system_prompt().split()).lower()

    assert "source starts with `outbound:`" in prompt
    assert "immediately preceding proactive message" in prompt
    assert "do not broaden it into a fresh all-history query" in prompt
    assert "read-tool boundary does not override a natural question" in prompt
    assert "do not call a historical insight tool" in prompt
    insight_description = TOOL_REGISTRY["query_report_insights"].description
    assert "not merely the contents" in insight_description
    assert "preceding delivered message is not such a read" in insight_description


def test_latest_verified_outbound_is_exposed_as_native_assistant_history():
    outbound = TrustedRecentMessage(
        role="assistant",
        content="刚发出的个人周简报",
        source_message_id="outbound:personal-weekly-brief:test",
    )

    assert _latest_verified_outbound_chat_message(
        SimpleNamespace(recent_messages=(outbound,))
    ) == {"role": "assistant", "content": outbound.content}
    assert _latest_verified_outbound_chat_message(
        SimpleNamespace(
            recent_messages=(
                outbound,
                TrustedRecentMessage(
                    role="user",
                    content="后来已经换了话题",
                    source_message_id="inbound:new-topic",
                ),
            )
        )
    ) is None


@pytest.mark.parametrize(
    "receipt",
    [
        _receipt(deliveryVerified=False),
        _receipt(deliveryStatus="FAILED"),
        _receipt(deliveryRecipientUserIds=["someone-else"]),
        _receipt(processQueryKey=""),
        _receipt(invalidStaffIdList=["ding-user-1"]),
    ],
)
def test_unverified_or_failed_delivery_cannot_be_recorded(receipt):
    with pytest.raises(ValueError):
        _event(delivery_receipt=receipt)


def test_outbound_context_fails_closed_for_wrong_scope_expiry_and_tampering():
    event = _event()

    assert trusted_recent_outbound_message(
        event,
        request=_request(conversation_id="cid-other"),
        dingtalk_user_id="ding-user-1",
    ) is None
    assert trusted_recent_outbound_message(
        event,
        request=_request(user_id=uuid.uuid4()),
        dingtalk_user_id="ding-user-1",
    ) is None
    assert trusted_recent_outbound_message(
        event,
        request=_request(),
        dingtalk_user_id="ding-user-2",
    ) is None
    assert trusted_recent_outbound_message(
        event,
        request=_request(server_now=NOW + timedelta(hours=17)),
        dingtalk_user_id="ding-user-1",
    ) is None

    event.message_text = "被改过的消息"
    assert trusted_recent_outbound_message(
        event,
        request=_request(),
        dingtalk_user_id="ding-user-1",
    ) is None


def test_outbound_event_identity_is_deterministic_for_retry_safety():
    first = _event()
    second = _event()

    assert first.id == second.id


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, existing=None):
        self.existing = existing
        self.added = []
        self.locked = False
        self.flushed = False

    async def execute(self, statement, params):
        self.locked = True

    async def scalars(self, statement):
        return _ScalarResult([] if self.existing is None else [self.existing])

    def add(self, event):
        self.added.append(event)

    async def flush(self):
        self.flushed = True


class _ReadSession:
    def __init__(self, result_sets):
        self._result_sets = list(result_sets)
        self.statements = []

    async def scalars(self, statement):
        if not self._result_sets:
            raise AssertionError("unexpected context-store query")
        self.statements.append(statement)
        return _ScalarResult(self._result_sets.pop(0))

    async def execute(self, statement):
        if not self._result_sets:
            raise AssertionError("unexpected context-store query")
        self.statements.append(statement)
        return _ScalarResult(self._result_sets.pop(0))


@pytest.mark.asyncio
async def test_record_is_idempotent_and_rejects_source_identity_collision():
    session = _Session()
    first = await record_verified_outbound_context_message(
        session,
        user=_user(),
        conversation_id="cid-private-1",
        message_text="庞总，已经修复。",
        source_message_id="recovery-notice-v1:user-1",
        delivery_receipt=_receipt(),
        sent_at=NOW,
    )
    assert first.created is True
    assert session.locked is True
    assert session.flushed is True
    assert len(session.added) == 1

    duplicate_session = _Session(existing=session.added[0])
    duplicate = await record_verified_outbound_context_message(
        duplicate_session,
        user=_user(),
        conversation_id="cid-private-1",
        message_text="庞总，已经修复。",
        source_message_id="recovery-notice-v1:user-1",
        delivery_receipt=_receipt(),
        sent_at=NOW,
    )
    assert duplicate.created is False
    assert duplicate.event.id == first.event.id
    assert duplicate_session.added == []

    collision_session = _Session(existing=session.added[0])
    with pytest.raises(RuntimeError, match="identity collision"):
        await record_verified_outbound_context_message(
            collision_session,
            user=_user(),
            conversation_id="cid-private-1",
            message_text="同一来源却换了内容",
            source_message_id="recovery-notice-v1:user-1",
            delivery_receipt=_receipt(),
            sent_at=NOW,
        )


@pytest.mark.asyncio
async def test_production_context_store_loads_verified_outbound_as_assistant_turn():
    event = _event()
    session = _ReadSession(
        [
            [],
            [event],
            [],
            [],
        ]
    )
    store = ProductionContextStore(
        session,
        user=_user(),
        tenant_id="default",
    )

    messages = await store.load_recent_messages(
        _request(),
        namespace=CANARY_STATE_NAMESPACE,
        limit=6,
    )

    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert messages[0].content == event.message_text
    assert session._result_sets == []
    outbound_query = str(session.statements[1])
    assert "llm_decision_json" in outbound_query
    assert "conversation_id" in session.statements[1].compile().params.values()


def test_latest_outbound_is_reserved_when_context_limit_is_full():
    old_outbound = TrustedRecentMessage(
        role="assistant",
        content="主动说明",
        source_message_id="outbound:notice-1",
    )
    newer_user = TrustedRecentMessage(
        role="user",
        content="第一句回复",
        source_message_id="reply-1",
    )
    newest_user = TrustedRecentMessage(
        role="user",
        content="第二句回复",
        source_message_id="reply-2",
    )

    selected = _select_recent_messages_with_scheduled_outbound(
        [
            (NOW, 2, old_outbound, True),
            (NOW + timedelta(minutes=1), 0, newer_user, False),
            (NOW + timedelta(minutes=2), 0, newest_user, False),
        ],
        limit=2,
    )

    assert selected == (old_outbound, newest_user)
