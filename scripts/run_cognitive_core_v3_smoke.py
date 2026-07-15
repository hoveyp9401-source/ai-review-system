from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import delete

from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveTurn
from app.agent2.command_planner_v3 import CognitiveCommandPlanner, CommandPlanningContext
from app.agent2.conversation_state import (
    BoundPending,
    ConversationEntity,
    ConversationGoal,
    ConversationState,
    RecentContextFrame,
)
from app.agent2.conversation_state_store import (
    ConversationStateVersionConflict,
    SQLAlchemyConversationStateStore,
)
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.llm.client import LLMClient
from app.models import Agent2ConversationState


TZ = ZoneInfo("Asia/Shanghai")


async def _db_state_smoke(run_id: str) -> dict[str, Any]:
    user_key = f"__cognitive_v3_smoke_user__{run_id}"
    conversation_id = f"__cognitive_v3_smoke_conversation__{run_id}"
    try:
        async with AsyncSessionLocal() as session:
            store = SQLAlchemyConversationStateStore(session)
            initial = await store.load(user_id=user_key, conversation_id=conversation_id)
            saved = await store.save(
                replace(
                    initial,
                    version=1,
                    current_goal=ConversationGoal(intent="chat"),
                ),
                expected_version=0,
            )
            await session.commit()
        async with AsyncSessionLocal() as session:
            store = SQLAlchemyConversationStateStore(session)
            loaded = await store.load(user_id=user_key, conversation_id=conversation_id)
            assert loaded == saved
            conflict = False
            try:
                await store.save(saved, expected_version=0)
            except ConversationStateVersionConflict:
                conflict = True
            assert conflict, "stale conversation-state insert was not blocked"
            await session.rollback()
        return {"stored_version": saved.version, "version_conflict_blocked": True}
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(Agent2ConversationState).where(
                    Agent2ConversationState.user_key == user_key,
                    Agent2ConversationState.conversation_id == conversation_id,
                )
            )
            await session.commit()


def _daily_resource() -> dict[str, Any]:
    return {
        "daily_draft": {
            "report_id": "smoke-report",
            "version": 3,
            "status": "collecting",
            "items": [
                {"item_id": "item-1", "field": "today_work", "field_index": 1, "text": "完成合同审核"},
                {"item_id": "item-2", "field": "today_work", "field_index": 2, "text": "整理案件材料"},
            ],
        },
        "active_tasks": [],
        "daily_policy": {
            "current_report_date": "2026-07-10",
            "historical_mutation_cutoff": "09:00",
            "historical_mutation_allowed": False,
            "current_time": "2026-07-10T13:00:00+08:00",
        },
    }


async def _live_llm_smoke(run_id: str) -> list[dict[str, Any]]:
    settings = get_settings()
    client = LLMClient(settings)
    interpreter = LLMCognitiveSemanticInterpreter(
        client,
        model=settings.agent2_cognitive_core_v3_model or settings.llm_high_risk_model,
        thinking_enabled=settings.agent2_cognitive_core_v3_thinking,
    )
    core = CognitiveCoreV3(interpreter)
    now = datetime.now(TZ)
    results: list[dict[str, Any]] = []

    async def evaluate(
        name: str,
        text: str,
        state: ConversationState,
        *,
        required_intents: set[str],
        required_actions: set[str],
        required_daily_commands: set[str],
        required_business_commands: set[str],
        forbidden_daily_commands: set[str] | None = None,
        required_clarification_reason: str = "",
        require_bound_pending: bool = False,
    ):
        turn = CognitiveTurn(
            user_id=state.user_id,
            conversation_id=state.conversation_id,
            message_id=f"{run_id}:{name}",
            text=text,
            occurred_at=now,
            resources=_daily_resource(),
        )
        outcome = await core.process(turn, state)
        actor_user_id = uuid5(NAMESPACE_URL, state.user_id)
        command_plan = CognitiveCommandPlanner().plan(
            outcome.decision,
            CommandPlanningContext(
                message_id=turn.message_id,
                actor_user_id=actor_user_id,
                daily_snapshot=DailyReportMutationSnapshot(
                    report_id=uuid5(NAMESPACE_URL, f"smoke-report:{state.user_id}"),
                    owner_user_id=actor_user_id,
                    version=3,
                    status="collecting",
                    today_work=("完成合同审核", "整理案件材料"),
                    problems=("暂无",),
                    tomorrow_plan=("继续跟进",),
                    item_ids={"today_work": ("item-1", "item-2")},
                ),
                user_constraints=outcome.state.user_constraints,
            ),
        )
        intents = set(outcome.decision.intents)
        actions = {action.action_type for action in outcome.decision.required_actions}
        daily_commands = {command.command_type for command in command_plan.daily_commands}
        business_commands = {command.command_type for command in command_plan.business_commands}
        print(
            "COGNITIVE_V3_LLM "
            + json.dumps(
                {
                    "name": name,
                    "intents": list(outcome.decision.intents),
                    "actions": [action.action_type for action in outcome.decision.required_actions],
                    "entities": [
                        {
                            "type": entity.entity_type,
                            "value": entity.value,
                            "attributes": entity.attributes,
                            "source_context_id": entity.source_context_id,
                        }
                        for entity in outcome.decision.entities
                    ],
                    "clarification": (
                        outcome.decision.clarification_need.reason
                        if outcome.decision.clarification_need is not None
                        else ""
                    ),
                    "daily_commands": sorted(daily_commands),
                    "business_commands": sorted(business_commands),
                    "blocked_actions": [block.reason_code for block in command_plan.blocked_actions],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        assert required_intents <= intents, f"{name}: intents={sorted(intents)}"
        assert required_actions <= actions, f"{name}: actions={sorted(actions)}"
        assert required_daily_commands <= daily_commands, f"{name}: daily_commands={sorted(daily_commands)}"
        assert required_business_commands <= business_commands, (
            f"{name}: business_commands={sorted(business_commands)}"
        )
        assert not set(forbidden_daily_commands or ()).intersection(daily_commands), (
            f"{name}: forbidden daily commands={sorted(daily_commands)}"
        )
        if required_clarification_reason:
            assert outcome.decision.clarification_need is not None, f"{name}: clarification missing"
            assert outcome.decision.clarification_need.reason == required_clarification_reason, (
                f"{name}: clarification={outcome.decision.clarification_need.reason}"
            )
        if require_bound_pending:
            assert outcome.state.pending, f"{name}: bound pending missing"
        results.append(
            {
                "name": name,
                "intents": list(outcome.decision.intents),
                "actions": [action.action_type for action in outcome.decision.required_actions],
                "clarification": (
                    outcome.decision.clarification_need.reason
                    if outcome.decision.clarification_need is not None
                    else ""
                ),
                "daily_commands": sorted(daily_commands),
                "business_commands": sorted(business_commands),
                "blocked_actions": [block.reason_code for block in command_plan.blocked_actions],
                "current_goal": outcome.state.current_goal.intent if outcome.state.current_goal else "",
            }
        )
        return outcome

    try:
        chat_state = ConversationState(
            user_id="smoke-user-chat",
            conversation_id=f"smoke-chat-{run_id}",
            current_goal=ConversationGoal(intent="chat"),
        )
        chat_outcome = await evaluate(
            "chat_with_daily",
            "今天去了法院",
            chat_state,
            required_intents={"daily_append"},
            required_actions={"capture_daily_event"},
            required_daily_commands={"append_item"},
            required_business_commands=set(),
        )
        assert chat_outcome.state.current_goal and chat_outcome.state.current_goal.intent == "chat"

        await evaluate(
            "daily_and_case_query",
            "今天完成XX，另外王总那个案件风险怎么看",
            ConversationState.empty(
                user_id="smoke-user-mixed",
                conversation_id=f"smoke-mixed-{run_id}",
            ),
            required_intents={"daily_append", "case_query"},
            required_actions={"capture_daily_event", "answer_case_query"},
            required_daily_commands={"append_item"},
            required_business_commands={"query_case_risk"},
        )

        context_id = f"case-context-{run_id}"
        case_state = ConversationState(
            user_id="smoke-user-case",
            conversation_id=f"smoke-case-{run_id}",
            current_goal=ConversationGoal(intent="case_discussion", source_context_id=context_id),
            recent_context=(
                RecentContextFrame(
                    context_id=context_id,
                    message_id="case-before-smoke",
                    intents=("case_discussion",),
                    entity_ids=("case-fact-smoke",),
                    summary="法院认为证据链完整",
                    occurred_at=now - timedelta(minutes=1),
                ),
            ),
        )
        context_outcome = await evaluate(
            "case_context_to_daily",
            "刚才那个补充到今天日报",
            case_state,
            required_intents={"daily_append"},
            required_actions={"capture_daily_event"},
            required_daily_commands={"append_item"},
            required_business_commands=set(),
        )
        assert any("证据链完整" in entity.value for entity in context_outcome.decision.entities)

        await evaluate(
            "travel_daily_case",
            "明天上海开庭，帮我记一下，然后看看这个案件有没有风险",
            ConversationState.empty(
                user_id="smoke-user-travel",
                conversation_id=f"smoke-travel-{run_id}",
            ),
            required_intents={"travel_event", "daily_append", "case_query"},
            required_actions={"record_travel_event", "capture_daily_event", "answer_case_query"},
            required_daily_commands={"append_item"},
            required_business_commands={"record_travel_candidate", "query_case_risk"},
        )

        await evaluate(
            "exact_daily_delete",
            "删除第 2 条",
            ConversationState.empty(
                user_id="smoke-user-delete",
                conversation_id=f"smoke-delete-{run_id}",
            ),
            required_intents={"daily_modify"},
            required_actions={"delete_daily_item"},
            required_daily_commands={"delete_item"},
            required_business_commands=set(),
        )

        await evaluate(
            "ambiguous_daily_editorial_instruction",
            "帮我整合优化",
            ConversationState.empty(
                user_id="smoke-user-ambiguous-editorial",
                conversation_id=f"smoke-ambiguous-editorial-{run_id}",
            ),
            required_intents={"daily_modify"},
            required_actions=set(),
            required_daily_commands=set(),
            required_business_commands=set(),
            forbidden_daily_commands={
                "append_item",
                "edit_item",
                "delete_item",
                "merge_items",
                "submit_report",
            },
        )

        await evaluate(
            "lifestyle_future_chatter",
            "明天吃屎",
            ConversationState.empty(
                user_id="smoke-user-lifestyle-chatter",
                conversation_id=f"smoke-lifestyle-chatter-{run_id}",
            ),
            required_intents={"chat"},
            required_actions=set(),
            required_daily_commands=set(),
            required_business_commands=set(),
            forbidden_daily_commands={"append_item"},
        )

        await evaluate(
            "daily_meta_statement",
            "写日报了",
            ConversationState.empty(
                user_id="smoke-user-daily-meta",
                conversation_id=f"smoke-daily-meta-{run_id}",
            ),
            required_intents={"chat"},
            required_actions=set(),
            required_daily_commands=set(),
            required_business_commands=set(),
            forbidden_daily_commands={"append_item", "submit_report"},
        )

        await evaluate(
            "historical_edit_after_cutoff",
            "昨天第一条删掉",
            ConversationState.empty(
                user_id="smoke-user-historical-edit",
                conversation_id=f"smoke-historical-edit-{run_id}",
            ),
            required_intents={"daily_modify"},
            required_actions=set(),
            required_daily_commands=set(),
            required_business_commands=set(),
            forbidden_daily_commands={"delete_item", "edit_item"},
            required_clarification_reason="historical_daily_mutation_blocked_after_cutoff",
        )

        await evaluate(
            "clear_daily_requires_bound_pending",
            "清空日报",
            ConversationState.empty(
                user_id="smoke-user-clear",
                conversation_id=f"smoke-clear-{run_id}",
            ),
            required_intents={"daily_clear"},
            required_actions=set(),
            required_daily_commands=set(),
            required_business_commands=set(),
            forbidden_daily_commands={"delete_item", "edit_item", "merge_items"},
            required_clarification_reason="high_impact_confirmation_required",
            require_bound_pending=True,
        )

        await evaluate(
            "exact_daily_edit",
            "把第 2 条改成完成合同审核",
            ConversationState.empty(
                user_id="smoke-user-edit",
                conversation_id=f"smoke-edit-{run_id}",
            ),
            required_intents={"daily_modify"},
            required_actions={"edit_daily_item"},
            required_daily_commands={"edit_item"},
            required_business_commands=set(),
        )

        await evaluate(
            "exact_daily_merge",
            "合并今天工作第 1、2 条",
            ConversationState.empty(
                user_id="smoke-user-merge",
                conversation_id=f"smoke-merge-{run_id}",
            ),
            required_intents={"daily_modify"},
            required_actions={"merge_daily_items"},
            required_daily_commands={"merge_items"},
            required_business_commands=set(),
        )

        monthly_entity = ConversationEntity(
            entity_id="monthly-smoke",
            entity_type="monthly_report",
            value="7月月报",
            confidence=1.0,
        )
        monthly_state = ConversationState(
            user_id="smoke-user-pending",
            conversation_id=f"smoke-pending-{run_id}",
            current_entities=(monthly_entity,),
            pending=(
                BoundPending(
                    pending_id="monthly-pending-smoke",
                    user_id="smoke-user-pending",
                    conversation_id=f"smoke-pending-{run_id}",
                    intent="monthly_submit",
                    action="confirm_monthly_report",
                    entity_ids=(monthly_entity.entity_id,),
                    context_id="monthly-context-smoke",
                    created_at=now - timedelta(minutes=1),
                    expires_at=now + timedelta(minutes=10),
                ),
            ),
        )
        await evaluate(
            "daily_submit_with_monthly_pending",
            "提交日报",
            monthly_state,
            required_intents={"daily_submit"},
            required_actions={"submit_daily_report"},
            required_daily_commands={"submit_report"},
            required_business_commands=set(),
        )
        return results
    finally:
        await client.close()


async def run(*, live_llm: bool, output: Path) -> int:
    run_id = uuid4().hex[:12]
    payload: dict[str, Any] = {
        "run_id": run_id,
        "db_state": await _db_state_smoke(run_id),
        "live_llm": [],
    }
    if live_llm:
        payload["live_llm"] = await _live_llm_smoke(run_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "live_llm_cases": len(payload["live_llm"])}, ensure_ascii=False))
    print(f"OUTPUT {output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-llm", action="store_true")
    parser.add_argument("--output", default="outputs/cognitive_core_v3_smoke.json")
    args = parser.parse_args()
    return asyncio.run(run(live_llm=args.live_llm, output=Path(args.output)))


if __name__ == "__main__":
    raise SystemExit(main())
