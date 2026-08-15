from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from app.agent2.memory.postgres import (
    PersonalMemoryAuditRecord,
    PersonalMemoryRecord,
)
from app.agent2.tool_calling.assembly import (
    TrustedContextAssembler,
    TrustedContextRequest,
)
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE
from app.agent2.tool_calling.production_store import (
    ProductionContextStore,
    ToolCallCanaryClearPending,
    ToolCallCanaryReceipt,
)
from app.agent2.tool_calling.turn_batching import (
    canonical_turn_batch_source_id,
)
from app.models import DailyReport, ReportInteractionEvent, User, WebhookEvent

NOW = datetime(2026, 8, 16, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
TENANT_ID = "legal-daily-production-v1"
USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-4222-8222-222222222222")
TEAM_ID = UUID("33333333-3333-4333-8333-333333333333")
DINGTALK_USER_ID = "ding-test-user-1"
CONVERSATION_ID = "cid-test-direct-1"
CURRENT_SOURCE_MESSAGE_ID = "dingtalk:current-provider-message"
TURN_OBSERVATION_KEY = "_agent2_turn_observation_v1"


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


def _statement_limit(statement: Any, default: int) -> int:
    clause = getattr(statement, "_limit_clause", None)
    value = getattr(clause, "value", None)
    return int(value) if isinstance(value, int) else default


class _PostgresShapeReadSession:
    """Database-boundary adapter over real production ORM row types.

    It applies the same authenticated scope and ORDER BY/LIMIT behavior that
    PostgreSQL applies before ProductionContextStore receives scalar rows.
    """

    def __init__(
        self,
        *,
        events: tuple[WebhookEvent, ...] = (),
        receipts: tuple[ToolCallCanaryReceipt, ...] = (),
        outbound_events: tuple[ReportInteractionEvent, ...] = (),
    ) -> None:
        self._events = events
        self._receipts = receipts
        self._outbound_events = outbound_events

    async def scalar(self, statement: Any) -> Any | None:
        assert _statement_entity(statement) is DailyReport
        return None

    async def scalars(self, statement: Any) -> _Rows:
        entity = _statement_entity(statement)
        if entity is WebhookEvent:
            rows = [
                row
                for row in self._events
                if row.dingtalk_user_id == DINGTALK_USER_ID
                and row.idempotency_key != CURRENT_SOURCE_MESSAGE_ID
                and row.received_at >= NOW - timedelta(hours=16)
                and str(row.payload.get("conversationId") or "") == CONVERSATION_ID
            ]
            rows.sort(key=lambda row: row.received_at, reverse=True)
            return _Rows(rows[: _statement_limit(statement, 12)])
        if entity is ReportInteractionEvent:
            rows = [
                row
                for row in self._outbound_events
                if row.user_id == USER_ID
                and row.created_at >= NOW - timedelta(hours=16)
            ]
            rows.sort(key=lambda row: row.created_at, reverse=True)
            return _Rows(rows[: _statement_limit(statement, 3)])
        if entity is ToolCallCanaryReceipt:
            rows = [
                row
                for row in self._receipts
                if row.tenant_id == TENANT_ID
                and row.user_id == str(USER_ID)
                and row.conversation_id == CONVERSATION_ID
                and row.source_message_id != CURRENT_SOURCE_MESSAGE_ID
                and row.created_at >= NOW - timedelta(hours=16)
            ]
            rows.sort(key=lambda row: row.created_at, reverse=True)
            return _Rows(rows[: _statement_limit(statement, len(rows))])
        if entity in {
            ToolCallCanaryClearPending,
            PersonalMemoryRecord,
        }:
            return _Rows([])
        raise AssertionError(f"unexpected scalar entity: {entity}")

    async def execute(self, statement: Any) -> _Rows:
        assert _statement_entity(statement) is PersonalMemoryAuditRecord
        return _Rows([])


class _PolicyPort:
    async def permission_allowed(self, request, definition):
        del request, definition
        return True

    async def gate_allowed(self, request, definition):
        del request, definition
        return True


def _user() -> User:
    return User(
        id=USER_ID,
        dingtalk_user_id=DINGTALK_USER_ID,
        employee_no="TEST-001",
        name="测试用户",
        team_id=TEAM_ID,
        role="member",
        timezone="Asia/Shanghai",
        active=True,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=1),
    )


def _turn_observation(
    *,
    source_turn_id: str,
    receipt_count: int,
    successful_pure_read: bool,
    business_write_committed: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": "agent2.turn.observation.v1",
        "message_processing_status": "consumed",
        "business_result_status": "success",
        "business_write_committed": business_write_committed,
        "reply_status": "formed",
        "model_result_status": "success",
        "source_turn_id": source_turn_id,
        "tool_receipt_count": receipt_count,
        "successful_pure_read": successful_pure_read,
    }


def _leader_event(
    *,
    provider_source_message_id: str,
    source_turn_id: str,
    receipt_count: int,
    successful_pure_read: bool,
    business_write_committed: bool = False,
    received_at: datetime | None = None,
) -> WebhookEvent:
    occurred_at = received_at or NOW - timedelta(minutes=5)
    return WebhookEvent(
        id=uuid4(),
        idempotency_key=provider_source_message_id,
        platform="dingtalk",
        external_message_id=provider_source_message_id,
        dingtalk_user_id=DINGTALK_USER_ID,
        report_id=None,
        payload={
            "conversationId": CONVERSATION_ID,
            "text": {"content": "昨天测试甲的日报交了吗？"},
        },
        response_payload={
            "msgtype": "text",
            "text": {"content": "未填写 1 人：测试甲。"},
            TURN_OBSERVATION_KEY: _turn_observation(
                source_turn_id=source_turn_id,
                receipt_count=receipt_count,
                successful_pure_read=successful_pure_read,
                business_write_committed=business_write_committed,
            ),
        },
        status="processed",
        error_message=None,
        received_at=occurred_at,
        processed_at=occurred_at + timedelta(seconds=1),
        created_at=occurred_at,
        updated_at=occurred_at + timedelta(seconds=1),
    )


def _receipt(
    *,
    source_turn_id: str,
    tool_call_id: str,
    tool_name: str = "query_managed_daily_reports",
    status: str = "success",
    changed: bool = False,
    user_id: UUID = USER_ID,
    conversation_id: str = CONVERSATION_ID,
    created_at: datetime | None = None,
) -> ToolCallCanaryReceipt:
    row_id = uuid4()
    occurred_at = created_at or NOW - timedelta(minutes=4)
    return ToolCallCanaryReceipt(
        receipt_id=row_id,
        tenant_id=TENANT_ID,
        user_id=str(user_id),
        conversation_id=conversation_id,
        source_message_id=source_turn_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        idempotency_key=f"test-receipt:{row_id}",
        canonical_arguments_hash="1" * 64,
        request_fingerprint="2" * 64,
        operation_fingerprint="3" * 64,
        status=status,
        changed=changed,
        target_type="managed_daily_report",
        target_id="2026-08-15:test-user",
        before_version=None,
        after_version=None,
        affected_item_ids=[],
        safe_user_facts={"actual_write": changed},
        before_state_hash="4" * 64,
        after_state_hash="5" * 64,
        typed_receipt_ids=[],
        error_code=("TEST_READ_FAILED" if status == "failed" else None),
        execution_mode="canary_execute",
        created_at=occurred_at,
    )


def _proactive_briefing() -> ReportInteractionEvent:
    return ReportInteractionEvent(
        id=uuid4(),
        user_id=USER_ID,
        report_id=None,
        dingtalk_user_id=DINGTALK_USER_ID,
        report_date=date(2026, 8, 15),
        message_text="这是主动发送的晨报。",
        llm_decision_json={},
        backend_action="daily_briefing_sent",
        before_snapshot_json={},
        after_snapshot_json={},
        correction_type="",
        correction_from="",
        correction_to="",
        confidence=None,
        is_undo=False,
        is_repeated_item_edit=False,
        asr_suspect_json={},
        created_at=NOW - timedelta(minutes=3),
    )


async def _assemble(
    *,
    events: tuple[WebhookEvent, ...] = (),
    receipts: tuple[ToolCallCanaryReceipt, ...] = (),
    outbound_events: tuple[ReportInteractionEvent, ...] = (),
):
    session = _PostgresShapeReadSession(
        events=events,
        receipts=receipts,
        outbound_events=outbound_events,
    )
    store = ProductionContextStore(
        session,
        user=_user(),
        tenant_id=TENANT_ID,
    )
    return await TrustedContextAssembler(
        read_port=store,
        policy_port=_PolicyPort(),
        namespace=CANARY_STATE_NAMESPACE,
        recent_message_limit=12,
        recent_operation_limit=6,
    ).assemble(
        TrustedContextRequest(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            source_message_id=CURRENT_SOURCE_MESSAGE_ID,
            timezone="Asia/Shanghai",
            server_now=NOW,
            conversation_kind="unknown",
        )
    )


def _assistant_payloads(context) -> list[dict[str, Any]]:
    return [
        message
        for message in context.model_payload()["recent_messages"]
        if message["role"] == "assistant"
    ]


@pytest.mark.asyncio
async def test_single_message_stream_read_reply_uses_the_canonical_turn_source() -> (
    None
):
    provider_source = "dingtalk:provider-read-1"
    source_turn_id = canonical_turn_batch_source_id((provider_source,))
    context = await _assemble(
        events=(
            _leader_event(
                provider_source_message_id=provider_source,
                source_turn_id=source_turn_id,
                receipt_count=1,
                successful_pure_read=True,
            ),
        ),
        receipts=(
            _receipt(
                source_turn_id=source_turn_id,
                tool_call_id="read-current-submission",
            ),
        ),
    )

    assistant = _assistant_payloads(context)
    internal_assistant = next(
        message for message in context.recent_messages if message.role == "assistant"
    )

    assert len(assistant) == 1
    assert assistant[0]["fact_time_scope"] == "past_snapshot"
    assert internal_assistant.source_message_id == f"{provider_source}:assistant"
    assert internal_assistant.source_turn_id == source_turn_id
    assert internal_assistant.read_snapshot_verified is True
    assert "source_turn_id" not in assistant[0]
    assert "read_snapshot_verified" not in assistant[0]


@pytest.mark.asyncio
async def test_multi_message_stream_leader_keeps_the_batch_turn_source() -> None:
    provider_sources = (
        "dingtalk:provider-read-part-1",
        "dingtalk:provider-read-part-2",
    )
    source_turn_id = canonical_turn_batch_source_id(provider_sources)
    leader = _leader_event(
        provider_source_message_id=provider_sources[0],
        source_turn_id=source_turn_id,
        receipt_count=1,
        successful_pure_read=True,
    )
    follower = _leader_event(
        provider_source_message_id=provider_sources[1],
        source_turn_id=source_turn_id,
        receipt_count=1,
        successful_pure_read=True,
        received_at=NOW - timedelta(minutes=5) + timedelta(seconds=1),
    )
    follower.response_payload = {
        "_agent2_tool_call_canary": {
            "batch_id": source_turn_id,
            "delivery": "batched_follower",
            "leader_event_id": str(leader.id),
        }
    }
    context = await _assemble(
        events=(leader, follower),
        receipts=(
            _receipt(
                source_turn_id=source_turn_id,
                tool_call_id="read-batched-submission",
            ),
        ),
    )

    assistant = _assistant_payloads(context)
    internal_assistant = next(
        message for message in context.recent_messages if message.role == "assistant"
    )

    assert len(assistant) == 1
    assert assistant[0]["fact_time_scope"] == "past_snapshot"
    assert (
        internal_assistant.source_message_id
        == f"{provider_sources[0]}:assistant"
    )
    assert internal_assistant.source_turn_id == source_turn_id
    assert internal_assistant.read_snapshot_verified is True
    assert "source_turn_id" not in assistant[0]
    assert "read_snapshot_verified" not in assistant[0]


@pytest.mark.asyncio
async def test_successful_pure_read_turn_is_not_truncated_to_six_receipts() -> None:
    source_turn_id = "dingtalk:webhook-seven-pure-reads"
    receipts = tuple(
        _receipt(
            source_turn_id=source_turn_id,
            tool_call_id=f"pure-read-{index}",
            created_at=NOW - timedelta(minutes=4) + timedelta(seconds=index),
        )
        for index in range(7)
    )
    context = await _assemble(
        events=(
            _leader_event(
                provider_source_message_id=source_turn_id,
                source_turn_id=source_turn_id,
                receipt_count=7,
                successful_pure_read=True,
            ),
        ),
        receipts=receipts,
    )

    assistant = _assistant_payloads(context)

    assert len(assistant) == 1
    assert assistant[0]["fact_time_scope"] == "past_snapshot"


@pytest.mark.asyncio
async def test_successful_and_failed_reads_are_not_a_successful_pure_read_turn() -> (
    None
):
    source_turn_id = "dingtalk:webhook-mixed-read-result"
    context = await _assemble(
        events=(
            _leader_event(
                provider_source_message_id=source_turn_id,
                source_turn_id=source_turn_id,
                receipt_count=2,
                successful_pure_read=False,
            ),
        ),
        receipts=(
            _receipt(
                source_turn_id=source_turn_id,
                tool_call_id="read-success",
                status="success",
            ),
            _receipt(
                source_turn_id=source_turn_id,
                tool_call_id="read-failed",
                status="failed",
                created_at=NOW - timedelta(minutes=3),
            ),
        ),
    )

    assistant = _assistant_payloads(context)

    assert len(assistant) == 1
    assert "fact_time_scope" not in assistant[0]


@pytest.mark.asyncio
async def test_a_write_outside_the_recent_six_receipts_keeps_the_turn_ineligible() -> (
    None
):
    source_turn_id = "dingtalk:webhook-seven-receipts"
    write_receipt = _receipt(
        source_turn_id=source_turn_id,
        tool_call_id="write-first",
        tool_name="add_daily_items",
        changed=True,
        created_at=NOW - timedelta(minutes=10),
    )
    read_receipts = tuple(
        _receipt(
            source_turn_id=source_turn_id,
            tool_call_id=f"read-{index}",
            created_at=NOW - timedelta(minutes=9) + timedelta(seconds=index),
        )
        for index in range(6)
    )
    context = await _assemble(
        events=(
            _leader_event(
                provider_source_message_id=source_turn_id,
                source_turn_id=source_turn_id,
                receipt_count=7,
                successful_pure_read=False,
                business_write_committed=True,
                received_at=NOW - timedelta(minutes=11),
            ),
        ),
        receipts=(write_receipt, *read_receipts),
    )

    assistant = _assistant_payloads(context)

    assert len(assistant) == 1
    assert "fact_time_scope" not in assistant[0]


@pytest.mark.asyncio
async def test_reply_without_a_receipt_is_not_a_past_snapshot() -> None:
    source_turn_id = "dingtalk:webhook-no-receipt"
    context = await _assemble(
        events=(
            _leader_event(
                provider_source_message_id=source_turn_id,
                source_turn_id=source_turn_id,
                receipt_count=0,
                successful_pure_read=False,
            ),
        ),
    )

    assistant = _assistant_payloads(context)

    assert len(assistant) == 1
    assert "fact_time_scope" not in assistant[0]


@pytest.mark.asyncio
async def test_legacy_reply_without_turn_observation_is_not_guessed() -> None:
    source_turn_id = "dingtalk:legacy-read-reply"
    event = _leader_event(
        provider_source_message_id=source_turn_id,
        source_turn_id=source_turn_id,
        receipt_count=1,
        successful_pure_read=True,
    )
    event.response_payload.pop(TURN_OBSERVATION_KEY)
    context = await _assemble(
        events=(event,),
        receipts=(
            _receipt(
                source_turn_id=source_turn_id,
                tool_call_id="legacy-read",
            ),
        ),
    )

    assistant = _assistant_payloads(context)

    assert len(assistant) == 1
    assert "fact_time_scope" not in assistant[0]


@pytest.mark.asyncio
async def test_proactive_message_is_not_a_past_read_snapshot() -> None:
    context = await _assemble(outbound_events=(_proactive_briefing(),))

    assistant = _assistant_payloads(context)

    assert len(assistant) == 1
    assert assistant[0]["content"] == "这是主动发送的晨报。"
    assert "fact_time_scope" not in assistant[0]


@pytest.mark.parametrize(
    ("receipt_user_id", "receipt_conversation_id"),
    (
        (OTHER_USER_ID, CONVERSATION_ID),
        (USER_ID, "cid-another-conversation"),
    ),
)
@pytest.mark.asyncio
async def test_read_receipt_from_another_scope_cannot_mark_the_reply(
    receipt_user_id: UUID,
    receipt_conversation_id: str,
) -> None:
    source_turn_id = "dingtalk:webhook-other-scope"
    context = await _assemble(
        events=(
            _leader_event(
                provider_source_message_id=source_turn_id,
                source_turn_id=source_turn_id,
                receipt_count=1,
                successful_pure_read=True,
            ),
        ),
        receipts=(
            _receipt(
                source_turn_id=source_turn_id,
                tool_call_id="other-scope-read",
                user_id=receipt_user_id,
                conversation_id=receipt_conversation_id,
            ),
        ),
    )

    assistant = _assistant_payloads(context)

    assert len(assistant) == 1
    assert "fact_time_scope" not in assistant[0]
