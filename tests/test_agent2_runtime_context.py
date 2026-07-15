from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from app.agent2.conversation_state import (
    BoundPending,
    ConversationEntity,
    ConversationGoal,
    ConversationState,
    RecentContextFrame,
)
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.runtime import (
    InMemoryDailyDomainExecutor,
    MvpContextAssembler,
    RuntimeActor,
    RuntimeTurnRequest,
)
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot


def test_context_mvp_uses_one_state_snapshot_all_active_pending_and_allowlisted_metadata():
    now = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    actor_id = uuid5(NAMESPACE_URL, "runtime-context-user")
    entity = ConversationEntity(
        entity_id="case-entity-1",
        entity_type="case_ref",
        value="海花岛案",
        confidence=1.0,
    )
    state = ConversationState(
        user_id=str(actor_id),
        conversation_id="runtime-context-conversation",
        version=1,
        current_goal=ConversationGoal(
            intent="case_discussion",
            entity_ids=(entity.entity_id,),
            source_context_id="context-1",
        ),
        current_entities=(entity,),
        recent_context=(
            RecentContextFrame(
                context_id="context-1",
                message_id="previous-message",
                intents=("case_discussion",),
                entity_ids=(entity.entity_id,),
                summary="讨论案件进展",
                occurred_at=now - timedelta(minutes=5),
            ),
        ),
        pending=(
            BoundPending(
                pending_id="pending-case",
                user_id=str(actor_id),
                conversation_id="runtime-context-conversation",
                intent="case_update",
                action="confirm_case_update",
                entity_ids=(entity.entity_id,),
                context_id="context-1",
                created_at=now - timedelta(minutes=2),
                expires_at=now + timedelta(minutes=20),
            ),
            BoundPending(
                pending_id="pending-daily-clear",
                user_id=str(actor_id),
                conversation_id="runtime-context-conversation",
                intent="daily_clear",
                action="clear_daily_report",
                entity_ids=(entity.entity_id,),
                context_id="context-1",
                created_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(minutes=10),
            ),
        ),
    )
    store = InMemoryConversationStateStore()
    asyncio.run(store.save(state, expected_version=0))
    daily = InMemoryDailyDomainExecutor(
        DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, "runtime-context-report"),
            owner_user_id=actor_id,
            version=4,
            status="collecting",
        )
    )
    assembler = MvpContextAssembler(
        state_store=store,
        daily_snapshot_provider=daily,
        daily_policy={"current_report_date": "2026-07-10"},
        active_tasks=(
            {
                "workflow": "daily_report",
                "task_id": "daily-task-1",
                "status": "collecting",
            },
        ),
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id, display_name="测试律师"),
        conversation_id="runtime-context-conversation",
        message_id="runtime-context-message",
        text="继续处理",
        occurred_at=now,
        channel="test",
        request_metadata={
            "source": "runtime_context_test",
            "traceparent": "00-trace-parent",
            "mode": "live",
            "db_session": "must-not-enter-context",
        },
    )

    context = asyncio.run(assembler.assemble(request))

    assert context.conversation_state.version == 1
    assert context.current_goal is context.conversation_state.current_goal
    assert context.entities is context.conversation_state.current_entities
    assert context.recent_context is context.conversation_state.recent_context
    assert [item.pending_id for item in context.active_pending] == [
        "pending-case",
        "pending-daily-clear",
    ]
    assert context.user_identity["actor_id"] == str(actor_id)
    assert context.cognitive_turn.tenant_id == "tenant-legal"
    assert context.cognitive_turn.actor_user_id == str(actor_id)
    assert dict(context.request_metadata) == {
        "source": "runtime_context_test",
        "traceparent": "00-trace-parent",
    }
    assert set(context.cognitive_turn.resources) == {
        "active_tasks",
        "daily_draft",
        "daily_policy",
        "timezone",
    }
    assert "mode" not in context.manifest["request_metadata"]
    assert "db_session" not in context.manifest["request_metadata"]
    assert "memory" not in context.cognitive_turn.resources
    assert "rag" not in context.cognitive_turn.resources
    assert context.planning_context.daily_snapshot is daily.snapshot
    assert context.manifest["source_text_hash"]
    assert context.manifest["conversation_state_digest"]
    assert context.manifest["active_tasks_digest"]
    assert context.manifest["daily_policy_digest"]
    assert context.manifest["daily_snapshot_digest"]
    assert context.manifest["daily_report_id"] == str(daily.snapshot.report_id)
