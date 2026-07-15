from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.agent2.report_domain import (
    PeriodicReportSnapshot,
    TypedPeriodicReportCommand,
    execute_periodic_report_command,
    period_bounds,
)
from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveTurn, SemanticInterpretation
from app.agent2.command_planner_v3 import CognitiveCommandPlanner, CommandPlanningContext
from app.agent2.conversation_state import ConversationGoal, ConversationState
from app.agent2.business.models import REPORT_TABLES
from app.agent2.report_sql_executor import periodic_report_datetimes


class _Interpreter:
    def __init__(self, payload):
        self.payload = payload

    async def interpret(self, turn, state):
        return SemanticInterpretation.from_payload(self.payload)


def _command(command_type: str, *, version: int = 0, patch=None, targets=()):
    return TypedPeriodicReportCommand(
        command_id=uuid5(NAMESPACE_URL, f"report-command:{command_type}:{version}"),
        decision_id=uuid5(NAMESPACE_URL, "report-decision"),
        sub_decision_id=uuid5(NAMESPACE_URL, f"report-sub:{command_type}"),
        command_type=command_type,
        report_type="weekly",
        period_key="2026-W28",
        report_id=uuid5(NAMESPACE_URL, "weekly:2026-W28"),
        report_version=version,
        target_item_ids=tuple(targets),
        patch=dict(patch or {}),
        idempotency_key=f"weekly:{command_type}:{version}",
    )


def _snapshot(*, version=0, status="collecting", sections=None, item_ids=None):
    return PeriodicReportSnapshot(
        report_id=uuid5(NAMESPACE_URL, "weekly:2026-W28"),
        owner_user_id=uuid5(NAMESPACE_URL, "report-owner"),
        report_type="weekly",
        period_key="2026-W28",
        version=version,
        status=status,
        sections=dict(sections or {}),
        item_ids=dict(item_ids or {}),
    )


def test_period_bounds_use_iso_week_and_calendar_month():
    assert period_bounds("weekly", date(2026, 7, 12)) == (
        "2026-W28",
        date(2026, 7, 6),
        date(2026, 7, 12),
    )
    assert period_bounds("monthly", date(2026, 7, 12)) == (
        "2026-07",
        date(2026, 7, 1),
        date(2026, 7, 31),
    )


def test_periodic_report_append_returns_complete_snapshot_and_stable_item_id():
    command = _command(
        "append_item",
        patch={"field": "accomplishments", "value": "完成案件清单核验"},
    )
    result = execute_periodic_report_command(
        command,
        snapshot=_snapshot(),
        actor_user_id=uuid5(NAMESPACE_URL, "report-owner"),
    )

    assert result.validation_status == "authorized"
    assert result.should_write_db is True
    assert result.after.version == 1
    assert result.after.sections == {"accomplishments": ("完成案件清单核验",)}
    assert len(result.after.item_ids["accomplishments"]) == 1


def test_periodic_report_replay_is_idempotent():
    command = _command(
        "append_item",
        patch={"field": "accomplishments", "value": "完成案件清单核验"},
    )
    result = execute_periodic_report_command(
        command,
        snapshot=_snapshot(),
        actor_user_id=uuid5(NAMESPACE_URL, "report-owner"),
        executed_idempotency_keys={command.idempotency_key},
    )

    assert result.validation_status == "duplicate"
    assert result.should_write_db is False
    assert result.after == result.before


def test_periodic_report_edit_delete_and_submit_share_one_lifecycle():
    snapshot = _snapshot(
        version=3,
        sections={"risks": ("旧风险",)},
        item_ids={"risks": ("risk-1",)},
    )
    owner = uuid5(NAMESPACE_URL, "report-owner")
    edited = execute_periodic_report_command(
        _command(
            "edit_item",
            version=3,
            patch={"replacement": "暂无其他风险"},
            targets=("risk-1",),
        ),
        snapshot=snapshot,
        actor_user_id=owner,
    ).after
    assert edited.sections["risks"] == ("暂无其他风险",)
    deleted = execute_periodic_report_command(
        _command("delete_item", version=4, targets=("risk-1",)),
        snapshot=edited,
        actor_user_id=owner,
    ).after
    assert deleted.sections["risks"] == ()
    submitted = execute_periodic_report_command(
        _command("submit_report", version=5),
        snapshot=deleted,
        actor_user_id=owner,
    ).after
    assert submitted.status == "completed"
    assert submitted.version == 6


def test_periodic_report_rejects_wrong_owner_or_stale_version():
    command = _command(
        "append_item",
        version=2,
        patch={"field": "risks", "value": "风险"},
    )
    stale = execute_periodic_report_command(
        command,
        snapshot=_snapshot(version=1),
        actor_user_id=uuid5(NAMESPACE_URL, "report-owner"),
    )
    wrong_owner = execute_periodic_report_command(
        _command("query_report"),
        snapshot=_snapshot(),
        actor_user_id=uuid5(NAMESPACE_URL, "other-owner"),
    )
    assert stale.validation_status == "blocked"
    assert stale.reason_code == "version_conflict"
    assert wrong_owner.reason_code == "owner_mismatch"


def test_cognitive_planner_emits_typed_weekly_report_command():
    text = "本周完成案件清单核验"
    payload = {
        "intents": ["weekly_report"],
        "segments": [{
            "segment_id": "weekly-work",
            "text": text,
            "intents": ["weekly_report"],
            "entity_ids": ["weekly-event"],
            "action_ids": ["capture-weekly"],
        }],
        "entities": [{
            "entity_id": "weekly-event",
            "entity_type": "report_event",
            "value": "完成案件清单核验",
            "confidence": 1.0,
            "attributes": {"report_type": "weekly", "field": "accomplishments"},
        }],
        "confidence": 1.0,
        "required_actions": [{
            "action_id": "capture-weekly",
            "action_type": "capture_report_event",
            "intent": "weekly_report",
            "entity_ids": ["weekly-event"],
        }],
        "clarification_need": None,
        "context_update": {"current_goal": "weekly_report", "remember_turn": True},
    }
    owner = uuid5(NAMESPACE_URL, "report-owner")
    turn = CognitiveTurn(
        user_id=str(owner),
        conversation_id="weekly-conversation",
        message_id="weekly-message",
        text=text,
        occurred_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    decision = asyncio.run(
        CognitiveCoreV3(_Interpreter(payload)).process(
            turn,
            ConversationState.empty(user_id=str(owner), conversation_id="weekly-conversation"),
        )
    ).decision
    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=owner,
            periodic_snapshot=_snapshot(),
        ),
    )

    assert plan.blocked_actions == ()
    assert len(plan.report_commands) == 1
    command = plan.report_commands[0]
    assert command.command_type == "append_item"
    assert command.patch == {
        "field": "accomplishments",
        "value": "完成案件清单核验",
    }


def test_periodic_report_schema_is_tenant_scoped_and_receipted():
    assert set(REPORT_TABLES) == {
        "agent2_periodic_reports",
        "agent2_periodic_report_command_receipts",
    }
    for model in REPORT_TABLES.values():
        assert {"tenant_id", "created_at", "updated_at"} <= set(
            model.__table__.columns.keys()
        )
    migration = Path("scripts/create_agent2_periodic_reports.sql").read_text(
        encoding="utf-8"
    )
    assert "agent2_periodic_reports" in migration
    assert "agent2_periodic_report_command_receipts" in migration
    assert "UNIQUE (tenant_id, idempotency_key)" in migration


def test_switching_report_domain_preserves_previous_goal_on_conversation_stack():
    owner = uuid5(NAMESPACE_URL, "report-owner")
    state = ConversationState(
        user_id=str(owner),
        conversation_id="stack-conversation",
        current_goal=ConversationGoal(intent="daily_report"),
    )
    payload = {
        "intents": ["weekly_report"],
        "segments": [],
        "entities": [],
        "confidence": 1.0,
        "required_actions": [],
        "clarification_need": None,
        "context_update": {"current_goal": "weekly_report", "remember_turn": True},
    }
    turn = CognitiveTurn(
        user_id=str(owner), conversation_id="stack-conversation",
        message_id="switch-weekly", text="我想写周报",
        occurred_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )

    result = asyncio.run(CognitiveCoreV3(_Interpreter(payload)).process(turn, state))

    assert result.state.current_goal.intent == "weekly_report"
    assert [goal.intent for goal in result.state.goal_stack] == ["daily_report"]
    restored = ConversationState.from_payload(result.state.as_payload())
    assert restored.goal_stack == result.state.goal_stack


def test_planner_does_not_advance_version_for_a_no_change_periodic_append():
    owner = uuid5(NAMESPACE_URL, "report-owner")
    payload = {
        "intents": ["weekly_report"],
        "segments": [],
        "entities": [
            {"entity_id": "existing", "entity_type": "report_event", "value": "已有事项", "confidence": 1.0, "attributes": {"report_type": "weekly", "field": "accomplishments"}},
            {"entity_id": "new", "entity_type": "report_event", "value": "新增事项", "confidence": 1.0, "attributes": {"report_type": "weekly", "field": "accomplishments"}},
        ],
        "confidence": 1.0,
        "required_actions": [
            {"action_id": "append-existing", "action_type": "capture_report_event", "intent": "weekly_report", "entity_ids": ["existing"]},
            {"action_id": "append-new", "action_type": "capture_report_event", "intent": "weekly_report", "entity_ids": ["new"]},
        ],
        "clarification_need": None,
        "context_update": {"current_goal": "weekly_report"},
    }
    turn = CognitiveTurn(
        user_id=str(owner), conversation_id="no-change-version",
        message_id="two-appends", text="已有事项，新增事项",
        occurred_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    decision = asyncio.run(CognitiveCoreV3(_Interpreter(payload)).process(
        turn,
        ConversationState.empty(user_id=str(owner), conversation_id="no-change-version"),
    )).decision
    snapshot = _snapshot(sections={"accomplishments": ("已有事项",)})
    plan = CognitiveCommandPlanner().plan(
        decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=owner,
            periodic_snapshot=snapshot,
        ),
    )

    assert [command.report_version for command in plan.report_commands] == [0, 0]
    first = execute_periodic_report_command(
        plan.report_commands[0], snapshot=snapshot, actor_user_id=owner
    )
    second = execute_periodic_report_command(
        plan.report_commands[1], snapshot=first.after, actor_user_id=owner
    )
    assert second.validation_status == "authorized"
    assert second.after.sections["accomplishments"] == ("已有事项", "新增事项")


def test_conversation_goal_stack_can_resume_the_previous_goal():
    owner = uuid5(NAMESPACE_URL, "report-owner")
    state = ConversationState(
        user_id=str(owner),
        conversation_id="resume-stack",
        current_goal=ConversationGoal(intent="case_progress"),
        goal_stack=(ConversationGoal(intent="weekly_report"),),
    )
    payload = {
        "intents": ["weekly_report"], "segments": [], "entities": [],
        "confidence": 1.0, "required_actions": [], "clarification_need": None,
        "context_update": {"resume_previous_goal": True, "remember_turn": True},
    }
    turn = CognitiveTurn(
        user_id=str(owner), conversation_id="resume-stack", message_id="resume",
        text="回到刚才的周报", occurred_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )

    result = asyncio.run(CognitiveCoreV3(_Interpreter(payload)).process(turn, state))

    assert result.state.current_goal.intent == "weekly_report"
    assert result.state.goal_stack == ()


def test_unknown_periodic_command_type_fails_closed():
    command = _command("destroy_report")  # type: ignore[arg-type]
    result = execute_periodic_report_command(
        command,
        snapshot=_snapshot(),
        actor_user_id=uuid5(NAMESPACE_URL, "report-owner"),
    )
    assert result.validation_status == "blocked"
    assert result.reason_code == "unsupported_command_type"


def test_periodic_report_boundaries_respect_shanghai_timezone():
    key, start, end = periodic_report_datetimes(
        "weekly",
        datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
        "Asia/Shanghai",
    )
    assert key == "2026-W28"
    assert start == datetime(2026, 7, 5, 16, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 12, 15, 59, 59, 999999, tzinfo=timezone.utc)
