from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

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
from app.agent2.cognitive_core_v3 import CognitiveCoreV3
from app.agent2.command_planner_v3 import CognitiveCommandPlanner
from app.agent2.semantic_interpreter_v3 import LLMCognitiveSemanticInterpreter
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot
from app.agent2.evaluation.historical_daily_replay import (
    run_historical_daily_replay_sync,
)


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "evals"
    / "agent2"
    / "dialogues"
    / "historical_daily_context_failures_20260721.json"
)


class AdversarialInternalQaClient:
    """A generic competing intent used to prove context routing, not content matching."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def complete_json(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        user_prompt = str(kwargs.get("user_prompt") or "")
        runtime_payload = json.loads(user_prompt.rsplit("\n\nInput:\n\n", 1)[1])
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


class TravelOnlyClient:
    async def complete_json(self, **kwargs: object) -> str:
        user_prompt = str(kwargs.get("user_prompt") or "")
        runtime_payload = json.loads(user_prompt.rsplit("\n\nInput:\n\n", 1)[1])
        source = str(runtime_payload["turn"]["text"])
        return json.dumps(
            {
                "intents": ["travel_event"],
                "segments": [
                    {
                        "segment_id": "travel-segment",
                        "text": source,
                        "intents": ["travel_event"],
                        "entity_ids": ["travel-event"],
                        "action_ids": ["record-travel"],
                        "start_offset": 0,
                        "end_offset": len(source),
                    }
                ],
                "entities": [
                    {
                        "entity_id": "travel-event",
                        "entity_type": "travel_event",
                        "value": source,
                        "confidence": 1.0,
                        "attributes": {
                            "destination": "广州",
                            "date_hint": "明天",
                            "purpose": "出差",
                            "statement_mode": "asserted",
                            "traveler_scope": "self",
                            "evidence_spans": [[0, 8]],
                        },
                    }
                ],
                "confidence": 1.0,
                "required_actions": [
                    {
                        "action_id": "record-travel",
                        "action_type": "record_travel_event",
                        "intent": "travel_event",
                        "entity_ids": ["travel-event"],
                    }
                ],
                "clarification_need": None,
                "context_update": {"preserve_current_goal": True},
            },
            ensure_ascii=False,
        )


def _fixture_turn(case_id: str, turn_id: str) -> dict[str, object]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    case = next(item for item in payload["cases"] if item["case_id"] == case_id)
    return next(item for item in case["turns"] if item["turn_id"] == turn_id)


def _runtime(
    client: AdversarialInternalQaClient,
    *,
    active_tasks: tuple[dict[str, object], ...] = (),
):
    actor_id = uuid5(NAMESPACE_URL, "historical-daily-acceptance-actor")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "historical-daily-acceptance-report"),
        owner_user_id=actor_id,
        version=0,
        status="collecting",
    )
    daily = InMemoryDailyDomainExecutor(snapshot)
    state_store = InMemoryConversationStateStore()
    interpreter = LLMCognitiveSemanticInterpreter(client)
    audit = InMemoryRuntimeAuditSink()
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
    return actor_id, daily, interpreter, runtime


def _request(actor_id, turn_id: str, text: str) -> RuntimeTurnRequest:
    return RuntimeTurnRequest(
        tenant_id="tenant-acceptance",
        actor=RuntimeActor(actor_id=actor_id, display_name="reviewer-alias"),
        conversation_id="historical-explicit-daily-entry",
        message_id=turn_id,
        text=text,
        occurred_at=datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc),
        channel="offline_acceptance",
        request_metadata={"source": "historical_daily_replay"},
    )


def test_explicit_daily_entry_keeps_plain_work_content_out_of_internal_qa():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)
    opener = _fixture_turn("historical-explicit-daily-entry", "b-03")
    content = _fixture_turn("historical-explicit-daily-entry", "b-04")

    opener_outcome = asyncio.run(
        runtime.handle(_request(actor_id, "b-03", str(opener["text"])))
    )
    content_outcome = asyncio.run(
        runtime.handle(_request(actor_id, "b-04", str(content["text"])))
    )

    assert opener_outcome.state.current_goal is not None
    assert opener_outcome.state.current_goal.intent == "daily_report"
    assert content_outcome.decision.intents == ("daily_append",)
    assert len(content_outcome.command_plan.daily_commands) == 1
    assert daily.snapshot.today_work == (str(content["text"]),)
    assert "live_model" not in interpreter.source_for("b-04")


@pytest.mark.parametrize("text", ["我写今天日报", "进入日报", "写日报"])
def test_report_entry_variants_enter_daily_context_without_writing(text: str):
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)

    outcome = asyncio.run(runtime.handle(_request(actor_id, "daily-entry", text)))

    assert outcome.state.current_goal is not None
    assert outcome.state.current_goal.intent == "daily_report"
    assert outcome.command_plan.daily_commands == ()
    assert daily.snapshot.version == 0
    assert interpreter.source_for("daily-entry") == "deterministic_contract"


def test_explicit_section_correction_moves_the_exact_existing_item():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)
    turns = [
        _fixture_turn("historical-explicit-daily-entry", "b-03"),
        _fixture_turn("historical-explicit-daily-entry", "b-04"),
        _fixture_turn("historical-daily-collection-correction", "a-02"),
        _fixture_turn("historical-daily-collection-correction", "a-03"),
    ]

    outcomes = [
        asyncio.run(runtime.handle(_request(actor_id, f"move-{index}", str(turn["text"]))))
        for index, turn in enumerate(turns, start=1)
    ]

    correction = outcomes[-1]
    assert daily.snapshot.today_work == ("半年度绩效评估",)
    assert daily.snapshot.tomorrow_plan == ("梳理推进待办，并梳理参赛作品",)
    assert [command.command_type for command in correction.command_plan.daily_commands] == [
        "move_items"
    ]
    assert "live_model" not in interpreter.source_for("move-4")


def test_complete_three_section_daily_produces_and_applies_three_typed_commands():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)
    turn = _fixture_turn("historical-daily-collection-correction", "a-05")

    outcome = asyncio.run(runtime.handle(_request(actor_id, "complete-document", str(turn["text"]))))

    assert [command.command_type for command in outcome.command_plan.daily_commands] == [
        "replace_section",
        "replace_section",
        "replace_section",
    ]
    assert outcome.command_plan.blocked_actions == ()
    assert daily.snapshot.today_work == ("半年度绩效评估",)
    assert daily.snapshot.problems == (
        "来函界面被调整，问题没解决被压着上线，降低效率",
    )
    assert daily.snapshot.tomorrow_plan == ("梳理推进待办，并梳理参赛作品",)
    assert interpreter.source_for("complete-document") == "deterministic_contract"


def test_scheduler_established_daily_collection_context_routes_plain_work_to_daily():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        active_tasks=(
            {
                "workflow": "daily_report",
                "task_id": "reminder-alias",
                "status": "collecting",
                "reply_candidate": True,
            },
        ),
    )
    turn = _fixture_turn("historical-daily-collection-correction", "a-01")

    outcome = asyncio.run(runtime.handle(_request(actor_id, "active-task-work", str(turn["text"]))))

    assert outcome.decision.intents == ("daily_append",)
    assert daily.snapshot.today_work == (str(turn["text"]),)
    assert interpreter.source_for("active-task-work") == "deterministic_contract"


def test_compound_correction_keeps_plan_and_applies_problem_as_separate_segment():
    client = AdversarialInternalQaClient()
    actor_id, daily, _, runtime = _runtime(client)
    setup_turns = [
        "我写今天日报",
        "梳理推进待办，并梳理参赛作品",
        "梳理推进待办，并梳理参赛作品，这是明天的计划",
    ]
    for index, text in enumerate(setup_turns, start=1):
        asyncio.run(runtime.handle(_request(actor_id, f"compound-setup-{index}", text)))
    turn = _fixture_turn("historical-daily-collection-correction", "a-04")

    outcome = asyncio.run(runtime.handle(_request(actor_id, "compound-correction", str(turn["text"]))))

    assert daily.snapshot.tomorrow_plan == ("梳理推进待办，并梳理参赛作品",)
    assert daily.snapshot.problems == ("来函界面被调整，问题没解决被压着上线",)
    assert daily.snapshot.today_work == ()
    assert len(outcome.decision.segments) == 2
    assert [command.command_type for command in outcome.command_plan.daily_commands] == [
        "replace_section"
    ]
    assert outcome.status == "partial"
    assert "本次没有修改" in outcome.reply.text


def test_sentence_delimited_compound_correction_is_segmented_without_model():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)
    for index, text in enumerate(
        ("进入日报", "整理评审材料，并准备复核"),
        start=1,
    ):
        asyncio.run(runtime.handle(_request(actor_id, f"sentence-setup-{index}", text)))

    outcome = asyncio.run(
        runtime.handle(
            _request(
                actor_id,
                "sentence-compound",
                "整理评审材料，并准备复核，这是明天的计划。问题是沟通界面调整后降低了效率。",
            )
        )
    )

    assert daily.snapshot.today_work == ()
    assert daily.snapshot.tomorrow_plan == ("整理评审材料，并准备复核",)
    assert daily.snapshot.problems == ("沟通界面调整后降低了效率。",)
    assert len(outcome.decision.segments) == 2
    assert interpreter.source_for("sentence-compound") == "deterministic_contract"
    assert client.calls == []


def test_replacing_only_tomorrow_plan_preserves_today_work_and_problems():
    client = AdversarialInternalQaClient()
    actor_id, daily, _, runtime = _runtime(client)
    complete = _fixture_turn("historical-daily-collection-correction", "a-05")
    asyncio.run(runtime.handle(_request(actor_id, "preserve-setup", str(complete["text"]))))
    before_today = daily.snapshot.today_work
    before_problems = daily.snapshot.problems

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "replace-plan-only", "明日计划改成跟进新的待办"))
    )

    assert outcome.status == "completed"
    assert daily.snapshot.today_work == before_today
    assert daily.snapshot.problems == before_problems
    assert daily.snapshot.tomorrow_plan == ("跟进新的待办",)


def test_repeated_section_correction_is_no_op_and_never_claims_changed():
    client = AdversarialInternalQaClient()
    actor_id, daily, _, runtime = _runtime(client)
    complete = _fixture_turn("historical-daily-collection-correction", "a-05")
    repeat = _fixture_turn("historical-daily-collection-correction", "a-07")
    asyncio.run(runtime.handle(_request(actor_id, "no-op-setup", str(complete["text"]))))
    before = daily.snapshot

    outcome = asyncio.run(runtime.handle(_request(actor_id, "no-op-repeat", str(repeat["text"]))))

    assert daily.snapshot == before
    assert outcome.command_plan.daily_commands == ()
    assert "已经改好" not in outcome.reply.text
    assert "本次没有修改" in outcome.reply.text


def test_formal_historical_replay_has_no_failed_acceptance_check():
    replay = run_historical_daily_replay_sync(FIXTURE_PATH)

    assert replay["summary"] == {
        "case_count": 2,
        "turn_count": 17,
        "acceptance_check_count": 30,
        "passed": 29,
        "failed": 0,
        "not_scored": 1,
    }
    assert replay["external_side_effects"] == 0


def test_scheduler_reminder_is_outbound_only_and_does_not_touch_runtime_state():
    replay = run_historical_daily_replay_sync(FIXTURE_PATH)
    first_case = next(
        case
        for case in replay["cases"]
        if case["case_id"] == "historical-daily-collection-correction"
    )
    scheduler_turn = next(turn for turn in first_case["turns"] if turn["turn_id"] == "a-06")

    assert scheduler_turn["semantic_source"] == "not_invoked_for_scheduler_event"
    assert scheduler_turn["context_composer_input"] is None
    assert scheduler_turn["semantic_decision"] is None
    assert scheduler_turn["typed_command"] is None
    assert scheduler_turn["conversation_state"]["before"] == scheduler_turn[
        "conversation_state"
    ]["after"]
    assert scheduler_turn["before_snapshot"] == scheduler_turn["after_snapshot"]


@pytest.mark.parametrize(
    "text",
    [
        "绩效材料什么时候提交？",
        "帮我查一下某案件的进展",
    ],
)
def test_active_daily_goal_does_not_swallow_explicit_cross_domain_or_question(text: str):
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)
    asyncio.run(runtime.handle(_request(actor_id, "guard-open", "进入日报")))
    before = daily.snapshot

    outcome = asyncio.run(runtime.handle(_request(actor_id, "guard-turn", text)))

    assert daily.snapshot == before
    assert interpreter.source_for("guard-turn") == "live_model"
    assert len(client.calls) == 1
    assert outcome.command_plan is None or outcome.command_plan.daily_commands == ()


def test_future_self_travel_projects_to_tomorrow_plan_without_daily_goal():
    client = TravelOnlyClient()
    actor_id, daily, interpreter, runtime = _runtime(client)

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "travel-without-daily-goal", "明天去广州出差"))
    )

    assert interpreter.source_for("travel-without-daily-goal") == "live_model"
    assert "travel_event" in outcome.decision.intents
    assert "daily_append" in outcome.decision.intents
    assert daily.snapshot.today_work == ()
    assert daily.snapshot.problems == ()
    assert daily.snapshot.tomorrow_plan == ("明天去广州出差",)
    assert [command.command_type for command in outcome.command_plan.daily_commands] == [
        "append_item"
    ]
    assert outcome.command_plan.daily_commands[0].patch == {
        "field": "tomorrow_plan",
        "items": ["明天去广州出差"],
    }
