from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from app.agent2.cognitive_core_v3 import CognitiveCoreV3
from app.agent2.command_planner_v3 import CognitiveCommandPlanner
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.runtime import (
    Agent2RuntimeHarness,
    InMemoryDailyDomainExecutor,
    InMemoryRuntimeAuditSink,
    MvpContextAssembler,
    RuntimeActor,
    RuntimeTurnRequest,
    build_phase1_domain_registry,
)
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot
from app.scheduler.jobs import build_report_reminder_text


class CompetingInternalQaClient:
    """Offline control that always proposes the competing internal-QA route."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def complete_json(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        user_prompt = str(kwargs.get("user_prompt") or "")
        runtime_payload = json.loads(user_prompt.split("\n\n", 1)[1])
        source = str(runtime_payload["turn"]["text"])
        return json.dumps(
            {
                "intents": ["knowledge_query"],
                "segments": [
                    {
                        "segment_id": "generic-internal-question",
                        "text": source,
                        "intents": ["knowledge_query"],
                        "entity_ids": ["generic-query"],
                        "action_ids": ["generic-search"],
                        "start_offset": 0,
                        "end_offset": len(source),
                    }
                ],
                "entities": [
                    {
                        "entity_id": "generic-query",
                        "entity_type": "knowledge_query",
                        "value": source,
                        "confidence": 1.0,
                        "attributes": {"query": source, "topic": "generic"},
                    }
                ],
                "confidence": 1.0,
                "required_actions": [
                    {
                        "action_id": "generic-search",
                        "action_type": "search_enterprise_knowledge",
                        "intent": "knowledge_query",
                        "entity_ids": ["generic-query"],
                    }
                ],
                "clarification_need": None,
                "context_update": {
                    "current_goal": "knowledge_query",
                    "remember_turn": True,
                },
            },
            ensure_ascii=False,
        )


async def run_historical_daily_replay(fixture_path: Path) -> dict[str, Any]:
    fixture_bytes = fixture_path.read_bytes()
    fixture = json.loads(fixture_bytes.decode("utf-8"))
    case_results = []
    for raw_case in fixture["cases"]:
        case_results.append(await _run_case(raw_case))
    turns = [turn for case in case_results for turn in case["turns"]]
    checks = [check for turn in turns for check in turn["acceptance_results"]]
    return {
        "schema_version": "agent2-historical-replay-result-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture_path": str(fixture_path),
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "execution_mode": "offline_in_memory_current_runtime",
        "model_control": "generic_competing_internal_qa_fake",
        "external_side_effects": 0,
        "summary": {
            "case_count": len(case_results),
            "turn_count": len(turns),
            "acceptance_check_count": len(checks),
            "passed": sum(check["status"] == "PASS" for check in checks),
            "failed": sum(check["status"] == "FAIL" for check in checks),
            "not_scored": sum(check["status"] == "NOT_SCORED" for check in checks),
        },
        "cases": case_results,
    }


async def _run_case(raw_case: Mapping[str, Any]) -> dict[str, Any]:
    case_id = str(raw_case["case_id"])
    actor_alias = str(raw_case["actor_alias"])
    actor_id = uuid5(NAMESPACE_URL, f"historical-daily-replay:{actor_alias}")
    conversation_id = f"historical-replay:{case_id}"
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, f"historical-daily-replay-report:{actor_alias}"),
        owner_user_id=actor_id,
        version=0,
        status="collecting",
    )
    daily = InMemoryDailyDomainExecutor(snapshot)
    state_store = InMemoryConversationStateStore()
    client = CompetingInternalQaClient()
    interpreter = LLMCognitiveSemanticInterpreter(client)
    audit = InMemoryRuntimeAuditSink()
    collection_active = bool(
        (raw_case.get("initial_context") or {}).get("daily_collection_active")
    )
    active_tasks = (
        (
            {
                "workflow": "daily_report",
                "task_id": f"daily-collection:{case_id}",
                "status": "collecting",
                "reply_candidate": True,
            },
        )
        if collection_active
        else ()
    )
    runtime = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(interpreter),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=state_store,
            daily_snapshot_provider=daily,
            daily_policy={"current_report_date": "2026-07-21"},
            active_tasks=active_tasks,
        ),
        domains=build_phase1_domain_registry(daily_executor=daily),
        audit_sink=audit,
    )
    base_time = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)
    results = []
    for index, raw_turn in enumerate(raw_case["turns"]):
        before_state = await state_store.load(
            user_id=str(actor_id),
            conversation_id=conversation_id,
        )
        before_snapshot = daily.snapshot
        if raw_turn["speaker"] == "system_event":
            reminder_text = build_report_reminder_text(
                date(2026, 7, 21),
                type("ReplayUser", (), {"name": actor_alias})(),
                None,
            )
            after_state = await state_store.load(
                user_id=str(actor_id),
                conversation_id=conversation_id,
            )
            result = {
                "turn_id": raw_turn["turn_id"],
                "speaker": "system_event",
                "input": raw_turn["text"],
                "conversation_state": {
                    "before": before_state.as_payload(),
                    "after": after_state.as_payload(),
                },
                "context_composer_input": None,
                "context_manifest": None,
                "semantic_source": "not_invoked_for_scheduler_event",
                "semantic_decision": None,
                "typed_command": None,
                "before_snapshot": _snapshot_payload(before_snapshot),
                "after_snapshot": _snapshot_payload(daily.snapshot),
                "user_reply": {
                    "reply_type": "scheduler_outbound_message",
                    "text": reminder_text,
                },
            }
        else:
            request = RuntimeTurnRequest(
                tenant_id="tenant-offline-acceptance",
                actor=RuntimeActor(actor_id=actor_id, display_name=actor_alias),
                conversation_id=conversation_id,
                message_id=str(raw_turn["turn_id"]),
                text=str(raw_turn["text"]),
                occurred_at=base_time + timedelta(minutes=index),
                channel="offline_acceptance",
                request_metadata={"source": "historical_daily_replay"},
            )
            outcome = await runtime.handle(request)
            after_state = await state_store.load(
                user_id=str(actor_id),
                conversation_id=conversation_id,
            )
            audit_record = audit.records[-1]
            result = {
                "turn_id": raw_turn["turn_id"],
                "speaker": "user",
                "input": raw_turn["text"],
                "conversation_state": {
                    "before": before_state.as_payload(),
                    "after": after_state.as_payload(),
                },
                "context_composer_input": {
                    "conversation_state": before_state.as_payload(),
                    "active_tasks": list(active_tasks),
                    "daily_draft": _snapshot_payload(before_snapshot),
                    "turn_text": raw_turn["text"],
                },
                "context_manifest": dict(audit_record.context_manifest),
                "semantic_source": interpreter.source_for(str(raw_turn["turn_id"])),
                "semantic_decision": (
                    outcome.decision.as_dict() if outcome.decision is not None else None
                ),
                "typed_command": (
                    outcome.command_plan.as_dict()
                    if outcome.command_plan is not None
                    else None
                ),
                "before_snapshot": _snapshot_payload(before_snapshot),
                "after_snapshot": _snapshot_payload(daily.snapshot),
                "user_reply": asdict(outcome.reply),
                "runtime_status": outcome.status,
            }
        result["acceptance_results"] = _evaluate_acceptance(
            raw_turn.get("acceptance") or [],
            result,
        )
        results.append(result)
    return {
        "case_id": case_id,
        "actor_alias": actor_alias,
        "model_call_count": len(client.calls),
        "turns": results,
    }


def _evaluate_acceptance(tags: list[str], turn: Mapping[str, Any]) -> list[dict[str, str]]:
    before = turn["before_snapshot"]
    after = turn["after_snapshot"]
    decision = turn.get("semantic_decision") or {}
    plan = turn.get("typed_command") or {}
    command_types = [item["command_type"] for item in plan.get("daily_commands") or []]
    reply = str((turn.get("user_reply") or {}).get("text") or "")
    input_text = str(turn.get("input") or "")
    correction_value = input_text.split("，这是", 1)[0].strip()
    checks: dict[str, bool | None] = {
        "append_today_work": input_text in after["today_work"],
        "enter_daily_context": (
            (turn["conversation_state"]["after"].get("current_goal") or {}).get("intent")
            == "daily_report"
        ),
        "no_daily_mutation": before == after,
        "no_internal_qa_takeover": "knowledge_query" not in decision.get("intents", []),
        "stable_typed_command": bool(command_types),
        "idempotent_daily_content": len(after["today_work"]) == len(set(after["today_work"])),
        "move_exact_item_to_tomorrow_plan": (
            correction_value not in after["today_work"]
            and correction_value in after["tomorrow_plan"]
        ),
        "tomorrow_plan_not_duplicated": len(after["tomorrow_plan"])
        == len(set(after["tomorrow_plan"])),
        "append_problem": bool(after["problems"]),
        "segment_level_outcome": len(decision.get("segments") or []) >= 2,
        "replace_all_three_sections": all(
            after[field] for field in ("today_work", "problems", "tomorrow_plan")
        ),
        "three_typed_commands": command_types
        == ["replace_section", "replace_section", "replace_section"],
        "conversation_state_unchanged": (
            turn["conversation_state"]["before"] == turn["conversation_state"]["after"]
        ),
        "daily_snapshot_unchanged": before == after,
        "no_false_success": not (before == after and "已经改好" in reply),
        "preserve_other_sections": not (
            before["today_work"] and not after["today_work"]
        ),
        "preserve_existing_daily_content": all(
            set(before[field]).issubset(set(after[field]))
            for field in ("today_work", "problems", "tomorrow_plan")
        ),
        "not_scored_before_daily_entry": None,
    }
    return [
        {
            "check": tag,
            "status": "NOT_SCORED" if checks.get(tag) is None else "PASS" if checks.get(tag) else "FAIL",
        }
        for tag in tags
    ]


def _snapshot_payload(snapshot: DailyReportMutationSnapshot) -> dict[str, Any]:
    submitted_at = getattr(snapshot, "submitted_at", None)
    return {
        "report_id": str(snapshot.report_id),
        "owner_user_id": str(snapshot.owner_user_id),
        "version": snapshot.version,
        "status": snapshot.status,
        "today_work": list(snapshot.today_work),
        "problems": list(snapshot.problems),
        "tomorrow_plan": list(snapshot.tomorrow_plan),
        "item_ids": {key: list(value) for key, value in snapshot.item_ids.items()},
        "submitted_at": submitted_at.isoformat() if submitted_at else None,
    }


def run_historical_daily_replay_sync(fixture_path: Path) -> dict[str, Any]:
    return asyncio.run(run_historical_daily_replay(fixture_path))
