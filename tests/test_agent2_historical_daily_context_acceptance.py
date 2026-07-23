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
from app.agent2.domain_admission import DomainAdmissionEngine
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


class AdversarialDailySectionReplacementClient:
    """Return a schema-valid write action for quoted Daily content."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def complete_json(self, **kwargs: object) -> str:
        self.calls.append(dict(kwargs))
        user_prompt = str(kwargs.get("user_prompt") or "")
        runtime_payload = json.loads(user_prompt.rsplit("\n\nInput:\n\n", 1)[1])
        source = str(runtime_payload["turn"]["text"])
        quoted_item = "完成合同审核"
        report_id = str(
            uuid5(NAMESPACE_URL, "historical-daily-acceptance-report")
        )
        return json.dumps(
            {
                "intents": ["daily_modify"],
                "segments": [
                    {
                        "segment_id": "adversarial-quoted-daily-replacement",
                        "text": source,
                        "intents": ["daily_modify"],
                        "entity_ids": ["adversarial-daily-report"],
                        "action_ids": ["adversarial-replace-section"],
                        "start_offset": 0,
                        "end_offset": len(source),
                    }
                ],
                "entities": [
                    {
                        "entity_id": "adversarial-daily-report",
                        "entity_type": "daily_report",
                        "value": quoted_item,
                        "confidence": 1.0,
                        "attributes": {
                            "report_id": report_id,
                            "version": 0,
                            "field": "today_work",
                            "items": [quoted_item],
                        },
                    }
                ],
                "confidence": 1.0,
                "required_actions": [
                    {
                        "action_id": "adversarial-replace-section",
                        "action_type": "replace_daily_section",
                        "intent": "daily_modify",
                        "entity_ids": ["adversarial-daily-report"],
                    }
                ],
                "clarification_need": None,
                "context_update": {
                    "current_goal": "daily_report",
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
    enforce_admission: bool = False,
    initial_snapshot: DailyReportMutationSnapshot | None = None,
):
    actor_id = uuid5(NAMESPACE_URL, "historical-daily-acceptance-actor")
    snapshot = initial_snapshot or DailyReportMutationSnapshot(
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
        core=CognitiveCoreV3(
            interpreter,
            admission_engine=(DomainAdmissionEngine() if enforce_admission else None),
            admission_enforced=enforce_admission,
        ),
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


@pytest.mark.parametrize(
    "text",
    ["我写今天日报", "进入日报", "写日报", "写日报咯", "开始写日报啦"],
)
def test_report_entry_variants_enter_daily_context_without_writing(text: str):
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)

    outcome = asyncio.run(runtime.handle(_request(actor_id, "daily-entry", text)))

    assert outcome.state.current_goal is not None
    assert outcome.state.current_goal.intent == "daily_report"
    assert outcome.command_plan.daily_commands == ()
    assert daily.snapshot.version == 0
    assert interpreter.source_for("daily-entry") == "deterministic_contract"


def test_colloquial_daily_entry_then_plain_work_survives_enforced_admission():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        enforce_admission=True,
    )

    opener = asyncio.run(runtime.handle(_request(actor_id, "colloquial-open", "写日报咯")))
    content = asyncio.run(
        runtime.handle(
            _request(actor_id, "colloquial-work", "完成了半年度绩效评估")
        )
    )

    assert opener.state.current_goal is not None
    assert opener.state.current_goal.intent == "daily_report"
    assert opener.command_plan.daily_commands == ()
    assert daily.snapshot.version == 0
    assert [ticket.operation for ticket in content.decision.admission_tickets] == [
        "capture_daily_event"
    ]
    assert content.decision.admission_trace.decisions[0].reason_code == (
        "active_daily_report_authorized"
    )
    assert [command.patch for command in content.command_plan.daily_commands] == [
        {"field": "today_work", "items": ["完成了半年度绩效评估"]}
    ]
    assert interpreter.source_for("colloquial-open") == "deterministic_contract"
    assert interpreter.source_for("colloquial-work") == "deterministic_contract"


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


def test_explicit_section_correction_survives_enforced_admission_end_to_end():
    client = AdversarialInternalQaClient()
    actor_id = uuid5(NAMESPACE_URL, "historical-daily-acceptance-actor")
    source_item = "梳理推进待办，并梳理参赛作品"
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        enforce_admission=True,
        initial_snapshot=DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, "historical-daily-acceptance-report"),
            owner_user_id=actor_id,
            version=2,
            status="collecting",
            today_work=("半年度绩效评估", source_item),
            item_ids={
                "today_work": ("work-1", "work-2"),
                "problems": (),
                "tomorrow_plan": (),
            },
        ),
    )
    correction = asyncio.run(
        runtime.handle(
            _request(
                actor_id,
                "admitted-move",
                f"{source_item}，这是明天的计划",
            )
        )
    )
    assert correction.decision.admission_mode == "enforced"
    assert correction.command_plan.blocked_actions == ()
    assert [ticket.operation for ticket in correction.decision.admission_tickets] == [
        "move_daily_items"
    ]
    assert [command.command_type for command in correction.command_plan.daily_commands] == [
        "move_items"
    ]
    assert correction.command_plan.daily_commands[0].patch == {
        "target_field": "tomorrow_plan"
    }
    assert "live_model" not in interpreter.source_for("admitted-move")


def test_explicit_today_completion_moves_exact_tomorrow_item() -> None:
    client = AdversarialInternalQaClient()
    actor_id = uuid5(NAMESPACE_URL, "historical-daily-acceptance-actor")
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        initial_snapshot=DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, "historical-daily-acceptance-report"),
            owner_user_id=actor_id,
            version=1,
            status="collecting",
            today_work=(),
            tomorrow_plan=("整理证据目录",),
            item_ids={
                "today_work": (),
                "problems": (),
                "tomorrow_plan": ("plan-1",),
            },
        ),
    )

    outcome = asyncio.run(
        runtime.handle(
            _request(
                actor_id,
                "complete-tomorrow-item",
                "整理证据目录今天已经完成了",
            )
        )
    )

    assert daily.snapshot.today_work == ("整理证据目录",)
    assert daily.snapshot.tomorrow_plan == ()
    assert [command.command_type for command in outcome.command_plan.daily_commands] == [
        "move_items"
    ]
    assert interpreter.source_for("complete-tomorrow-item") == "deterministic_contract"
    assert client.calls == []


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


def test_complete_three_section_daily_survives_enforced_admission_planning():
    client = AdversarialInternalQaClient()
    actor_id, _daily, interpreter, runtime = _runtime(
        client,
        enforce_admission=True,
    )
    turn = _fixture_turn("historical-daily-collection-correction", "a-05")

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "admitted-complete-document", str(turn["text"])))
    )

    assert [ticket.operation for ticket in outcome.decision.admission_tickets] == [
        "replace_daily_section",
        "replace_daily_section",
        "replace_daily_section",
    ]
    assert [command.command_type for command in outcome.command_plan.daily_commands] == [
        "replace_section",
        "replace_section",
        "replace_section",
    ]
    assert [command.patch["field"] for command in outcome.command_plan.daily_commands] == [
        "today_work",
        "problems",
        "tomorrow_plan",
    ]
    assert outcome.command_plan.blocked_actions == ()
    assert interpreter.source_for("admitted-complete-document") == "deterministic_contract"


def test_complete_revision_updates_pending_confirmation_draft_without_confirming_it():
    client = AdversarialInternalQaClient()
    actor_id = uuid5(NAMESPACE_URL, "historical-daily-acceptance-actor")
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        enforce_admission=True,
        initial_snapshot=DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, "historical-daily-acceptance-report"),
            owner_user_id=actor_id,
            version=3,
            status="pending_confirmation",
            today_work=("旧的今日工作",),
            problems=("旧的问题",),
            tomorrow_plan=("旧的明日计划",),
            item_ids={
                "today_work": ("old-work",),
                "problems": ("old-problem",),
                "tomorrow_plan": ("old-plan",),
            },
        ),
    )
    turn = _fixture_turn("historical-daily-collection-correction", "a-05")

    outcome = asyncio.run(
        runtime.handle(
            _request(actor_id, "pending-confirmation-revision", str(turn["text"]))
        )
    )

    assert [ticket.operation for ticket in outcome.decision.admission_tickets] == [
        "replace_daily_section",
        "replace_daily_section",
        "replace_daily_section",
    ]
    assert [command.command_type for command in outcome.command_plan.daily_commands] == [
        "replace_section",
        "replace_section",
        "replace_section",
    ]
    assert outcome.command_plan.blocked_actions == ()
    assert interpreter.source_for("pending-confirmation-revision") == (
        "deterministic_contract"
    )

    actor_id, daily, interpreter, runtime = _runtime(
        AdversarialInternalQaClient(),
        initial_snapshot=DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, "historical-daily-acceptance-report"),
            owner_user_id=actor_id,
            version=3,
            status="pending_confirmation",
            today_work=("旧的今日工作",),
            problems=("旧的问题",),
            tomorrow_plan=("旧的明日计划",),
            item_ids={
                "today_work": ("old-work",),
                "problems": ("old-problem",),
                "tomorrow_plan": ("old-plan",),
            },
        ),
    )
    executed = asyncio.run(
        runtime.handle(
            _request(
                actor_id,
                "pending-confirmation-revision-execution",
                str(turn["text"]),
            )
        )
    )

    assert executed.command_plan.blocked_actions == ()
    assert daily.snapshot.today_work == ("半年度绩效评估",)
    assert daily.snapshot.problems == (
        "来函界面被调整，问题没解决被压着上线，降低效率",
    )
    assert daily.snapshot.tomorrow_plan == ("梳理推进待办，并梳理参赛作品",)
    assert daily.snapshot.status == "pending_confirmation"
    assert interpreter.source_for("pending-confirmation-revision-execution") == (
        "deterministic_contract"
    )


def test_plain_revision_can_extend_pending_confirmation_daily_context():
    client = AdversarialInternalQaClient()
    actor_id = uuid5(NAMESPACE_URL, "historical-daily-acceptance-actor")
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        active_tasks=(
            {
                "workflow": "daily_report",
                "task_id": "pending-daily",
                "status": "pending_confirmation",
                "reply_candidate": True,
                "awaiting_confirmation": True,
            },
        ),
        initial_snapshot=DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, "historical-daily-acceptance-report"),
            owner_user_id=actor_id,
            version=2,
            status="pending_confirmation",
            today_work=("完成合同初审",),
            item_ids={
                "today_work": ("old-work",),
                "problems": (),
                "tomorrow_plan": (),
            },
        ),
    )

    outcome = asyncio.run(
        runtime.handle(
            _request(actor_id, "pending-plain-revision", "补充完成合同复核")
        )
    )

    assert [command.command_type for command in outcome.command_plan.daily_commands] == [
        "append_item"
    ]
    assert daily.snapshot.today_work == ("完成合同初审", "补充完成合同复核")
    assert daily.snapshot.status == "pending_confirmation"
    assert interpreter.source_for("pending-plain-revision") == "deterministic_contract"
    assert client.calls == []


def test_natural_three_sentence_daily_document_writes_all_sections_deterministically():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(
        client,
    )

    outcome = asyncio.run(
        runtime.handle(
            _request(
                actor_id,
                "natural-three-section-document",
                "今天完成合同审核。问题是客户资料不全。明天计划跟进补充材料。",
            )
        )
    )

    assert outcome.command_plan.blocked_actions == ()
    assert daily.snapshot.today_work == ("合同审核",)
    assert daily.snapshot.problems == ("客户资料不全",)
    assert daily.snapshot.tomorrow_plan == ("跟进补充材料",)
    assert [command.command_type for command in outcome.command_plan.daily_commands] == [
        "replace_section",
        "replace_section",
        "replace_section",
    ]
    assert interpreter.source_for("natural-three-section-document") == (
        "deterministic_contract"
    )
    assert client.calls == []


def test_structured_headings_do_not_authorize_nonassertive_or_cancelled_items():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        enforce_admission=True,
    )
    text = (
        "今日工作\n1. 同事说已经完成合同审核\n"
        "问题与风险\n1. 客户资料不全\n"
        "明日计划\n1. 南京出差取消"
    )

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "nonassertive-headed-document", text))
    )

    assert outcome.command_plan.daily_commands == ()
    assert daily.snapshot.version == 0
    assert daily.snapshot.today_work == ()
    assert daily.snapshot.problems == ()
    assert daily.snapshot.tomorrow_plan == ()
    assert interpreter.source_for("nonassertive-headed-document") == "live_model"
    assert len(client.calls) == 1


def test_structured_document_preserves_nominal_termination_work_items():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)
    tomorrow_items = (
        "跟进商标撤销",
        "研究合同撤销",
        "处理公司决议撤销",
        "跟进案件撤回",
        "评估项目暂停",
        "研究已签合同撤销",
        "处理已登记商标撤销",
        "继续跟进商标撤销",
        "持续研究合同撤销",
        "明天处理公司决议撤销",
        "重点跟进案件撤回",
        "进一步评估项目暂停",
        "研究正式合同撤销",
        "跟进确认书撤销",
        "分析主动撤回",
        "评估实际控制人资格取消",
        "研究全面履行后的合同撤销",
        "处理明确授权的撤销",
        "研究行政许可依法撤销",
        "研究被许可人资格取消",
        "评估被投资企业项目暂停",
        "核查被审计单位资格撤销",
        "研究被监护人授权撤回",
        "处理被特许经营资格取消",
        "研究已经生效的合同撤销",
        "分析曾经签订的协议撤销",
        "评估已经履行的项目终止",
        "核查已经授权的资格取消",
        "办理客户的申请撤回",
        "跟进客户的许可撤销",
        "处理公司的决议撤销",
        "处理董事会的决议撤销",
        "跟进供应商的商标撤销",
        "办理许可方的授权撤销",
        "处理法院的决定撤销",
        "跟进客户的委托撤销",
        "办理供应商的申请撤回",
        "跟进申请方的许可撤销",
        "处理合作方的合同终止",
        "办理被许可人的资格取消",
        "处理已经生效的合同撤销",
        "研究当前合同撤销",
        "研究临时合同撤销",
    )
    text = (
        "今日工作：1. 整理评审材料\n"
        "问题与风险：1. 来函界面被调整\n"
        "明日计划："
        + "\n".join(
            f"{index}. {item}" for index, item in enumerate(tomorrow_items, start=1)
        )
    )

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "nominal-termination-document", text))
    )

    assert outcome.command_plan.blocked_actions == ()
    assert daily.snapshot.tomorrow_plan == tomorrow_items
    assert interpreter.source_for("nominal-termination-document") == (
        "deterministic_contract"
    )
    assert client.calls == []


def test_structured_document_preserves_embedded_work_query_items():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)
    today_items = (
        "研究合同撤销与否",
        "评估项目继续与否",
        "核查授权有效与否",
        "核查材料发没发",
        "研究项目做不做",
    )
    text = (
        "今日工作："
        + "\n".join(
            f"{index}. {item}" for index, item in enumerate(today_items, start=1)
        )
        + "\n问题与风险：1. 暂无明显问题\n"
        "明日计划：1. 整理评审材料"
    )

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "embedded-work-query-document", text))
    )

    assert outcome.command_plan.blocked_actions == ()
    assert daily.snapshot.today_work == today_items
    assert interpreter.source_for("embedded-work-query-document") == (
        "deterministic_contract"
    )
    assert client.calls == []


def test_parenthesized_numbering_does_not_authorize_reported_daily_content():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        enforce_admission=True,
    )
    text = (
        "今日工作：（1） 刘聪说完成合同审核\n"
        "问题与风险：（1） 来函界面被调整\n"
        "明日计划：（1） 准备评审材料"
    )

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "parenthesized-reported-document", text))
    )

    assert outcome.decision.admission_tickets == ()
    assert outcome.command_plan is not None
    assert outcome.command_plan.daily_commands == ()
    assert daily.snapshot.version == 0
    assert interpreter.source_for("parenthesized-reported-document") == "live_model"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "terminal_item",
    [
        "客户拜访已经撤销\u200b",
        "客户拜访已经撤销⚠️",
        "客户拜访暂时终止啦",
        "客户拜访已经撤\u200b销",
        "客户拜访已经撤 销",
        "客户拜访已经撤-销",
        "客户拜访已经撤⚠️销",
        "客户拜访已经撤销了吧",
        "客户拜访已经撤销掉了吧",
        "客户拜访暂时终止了呢",
        "客户拜访已经撤销了没",
        "客户拜访已经撤销了没有",
        "客户拜访已经撤销了吗",
        "客户拜访已经取消哈",
        "客户拜访已经取消呐",
        "客户拜访已经取消诶",
        "客户拜访已经取消耶",
        "跟进客户拜访已经正式取消",
        "处理公司会议已确认取消",
        "评估项目目前已经全面暂停",
        "跟进案件被法院裁定终止",
        "研究合同现已依法撤销",
        "跟进客户拜访曾经取消",
        "跟进的客户拜访最终取消",
        "跟进的客户拜访取消",
        "跟进的客户拜访后来取消",
        "跟进的客户拜访突然取消",
        "跟进中的客户拜访取消",
        "处理中的案件撤回",
        "审核过的合同撤销",
        "跟进客户拜访已经因为天气和场地安排发生重大变化而取消",
        "处理公司会议已由业务部门经过全面评估后决定取消",
        "跟进客户拜访目前由业务部门经过评估后决定取消",
        "跟进客户拜访后来因为天气和场地变化而取消",
        "跟进着的客户拜访取消",
        "审核完的合同撤销",
        "办理了的许可撤销",
        "跟进多日的客户拜访取消",
        "跟进了一段时间的客户拜访取消",
        "审核多次的合同撤销",
        "处理许久的案件撤回",
        "办理到一半的许可撤销",
        "审核完毕的那个合同撤销",
        "跟进已久的客户拜访取消",
        "跟进了三个月的客户拜访取消",
        "处理过程中的案件撤回",
        "办理期间的该项许可撤销",
        "跟进超过一年的客户拜访取消",
        "审核完成后的合同撤销",
        "跟进客户拜访被告知取消",
        "处理公司会议被告知取消",
        "评估项目被告知暂停",
        "研究合同被告知撤销",
        "评估阶段的项目暂停",
        "研究阶段的项目暂停",
        "分析阶段的项目终止",
        "审查期内的许可撤销",
        "复核期间内的决定撤销",
        "研究状态下的项目暂停",
        "核查范围内的事项取消",
        "评估阶段里的那个项目暂停",
        "研究初期的那个项目暂停",
        "复核环节里的那个决定撤销",
        "审查流程上的该项许可撤销",
        "跟进客户的项目已经取消",
        "办理合作方的许可被撤销",
        "处理供应商的申请最终撤回",
    ],
)
def test_structured_document_does_not_authorize_decorated_termination_state(
    terminal_item: str,
):
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        enforce_admission=True,
    )
    text = (
        "今日工作：1. 整理评审材料\n"
        "问题与风险：1. 来函界面被调整\n"
        f"明日计划：1. {terminal_item}"
    )

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "decorated-termination-document", text))
    )

    assert outcome.decision.admission_tickets == ()
    assert outcome.command_plan is not None
    assert outcome.command_plan.daily_commands == ()
    assert daily.snapshot.version == 0
    assert interpreter.source_for("decorated-termination-document") == "live_model"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "question_item",
    [
        "合同审核完成了没",
        "合同审核完成了没有",
        "合同审核完成了吗",
        "合同审核完成了-没",
        "合同审核完成了\u200b没",
        "合同审核完成了⚠️没",
        "合同审核完成了…没有",
        "合同复核完成没有",
        "合同复核完成没",
        "合同审核做完没有",
        "材料整理好没有",
        "合同复核完成否",
        "合同复核完成了对吧",
        "合同复核完成了对不对",
        "合同复核完成了吧",
        "合同复核完成没呢",
        "合同复核完成不",
        "合同看了没",
        "材料发了没",
        "会开完没有",
        "绩效评估弄完没",
        "文档写了没",
        "案件结了没",
        "材料发没发",
        "合同审没审",
        "会议开没开",
        "日报写没写",
        "材料发不发",
        "合同审不审",
        "材料发没",
        "合同审不",
        "材料发对吧",
        "材料发吧",
        "合同审没审完",
        "材料发没发完",
        "会议开没开完",
        "日报写没写完",
        "合同审不审完",
        "材料发还是不发",
        "材料发了还是没发完",
        "合同审没审完全部条款",
        "材料发没发给业务部门",
        "会议开没开出明确结论",
        "日报写没写完全部内容",
        "材料发还是不发给业务部门",
        "材料发没发给HR",
        "合同审没审完NDA",
        "会议开没开出Q3结论",
        "日报写没写完2026Q3内容",
        "材料发还是不发给A组",
        "NDA review了没",
        "合同review了没有",
        "材料check了没",
        "PR merge了没",
        "NDA review没review完",
        "文档check没check",
        "NDA review过没",
        "合同check过没有",
        "PR merge过没",
        "材料review过没",
        "NDA reviewed没",
        "PR merged没有",
        "合同checked没有",
        "你是agent1还是agent2",
        "合同是不是看完了",
        "材料有没有发出",
    ],
)
def test_structured_document_does_not_authorize_unpunctuated_question(
    question_item: str,
):
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        enforce_admission=True,
    )
    text = (
        f"今日工作：1. {question_item}\n"
        "问题与风险：1. 来函界面被调整\n"
        "明日计划：1. 整理评审材料"
    )

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "unpunctuated-question-document", text))
    )

    assert outcome.decision.admission_tickets == ()
    assert outcome.command_plan is not None
    assert outcome.command_plan.daily_commands == ()
    assert daily.snapshot.version == 0
    assert interpreter.source_for("unpunctuated-question-document") == "live_model"
    assert len(client.calls) == 1


def test_admission_rejects_model_authored_replacement_for_quoted_daily_item():
    client = AdversarialDailySectionReplacementClient()
    actor_id, daily, interpreter, runtime = _runtime(
        client,
        enforce_admission=True,
    )
    text = (
        "今日工作：1. 刘聪说完成合同审核\n"
        "问题与风险：来函界面被调整\n"
        "明日计划：明天南京出差取消"
    )

    outcome = asyncio.run(
        runtime.handle(
            _request(actor_id, "adversarial-quoted-section-replacement", text)
        )
    )

    assert outcome.decision.admission_tickets == ()
    assert outcome.command_plan is not None
    assert outcome.command_plan.daily_commands == ()
    assert daily.snapshot.version == 0
    assert daily.snapshot.today_work == ()
    assert daily.snapshot.problems == ()
    assert daily.snapshot.tomorrow_plan == ()
    assert interpreter.source_for("adversarial-quoted-section-replacement") == (
        "live_model"
    )
    assert len(client.calls) == 1


def test_explicit_today_and_tomorrow_clauses_write_both_facets_deterministically():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)

    outcome = asyncio.run(
        runtime.handle(
            _request(
                actor_id,
                "temporal-two-facet-document",
                "今天完成两份劳动合同复核，并整理证据目录；明天跟进法院回函",
            )
        )
    )

    assert outcome.command_plan.blocked_actions == ()
    assert daily.snapshot.today_work == ("完成两份劳动合同复核，并整理证据目录",)
    assert daily.snapshot.tomorrow_plan == ("跟进法院回函",)
    assert [command.command_type for command in outcome.command_plan.daily_commands] == [
        "append_item",
        "append_item",
    ]
    assert interpreter.source_for("temporal-two-facet-document") == (
        "deterministic_contract"
    )
    assert client.calls == []


@pytest.mark.parametrize(
    "text",
    [
        "今天完成合同审核；明天是否去南京出差？",
        "今天没有完成合同审核；明天跟进客户",
        "今天完成合同审核；明天南京出差取消",
        "今天完成合同审核；明天跟进客户；云璟府案件开庭了",
    ],
)
def test_temporal_daily_parser_defers_questions_negations_cancellations_and_mixed_domains(
    text: str,
):
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "unsafe-temporal-document", text))
    )

    assert interpreter.source_for("unsafe-temporal-document") == "live_model"
    assert len(client.calls) == 1
    assert daily.snapshot.version == 0
    assert outcome.command_plan is None or outcome.command_plan.daily_commands == ()


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


def test_colon_labeled_tomorrow_plan_replaces_only_that_section_without_model():
    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)
    complete = _fixture_turn("historical-daily-collection-correction", "a-05")
    asyncio.run(runtime.handle(_request(actor_id, "colon-plan-setup", str(complete["text"]))))
    before_today = daily.snapshot.today_work
    before_problems = daily.snapshot.problems
    client.calls.clear()

    outcome = asyncio.run(
        runtime.handle(
            _request(actor_id, "colon-plan-only", "明日计划：继续推进项目验收")
        )
    )

    assert outcome.status == "completed"
    assert daily.snapshot.today_work == before_today
    assert daily.snapshot.problems == before_problems
    assert daily.snapshot.tomorrow_plan == ("继续推进项目验收",)
    assert [command.command_type for command in outcome.command_plan.daily_commands] == [
        "replace_section"
    ]
    assert interpreter.source_for("colon-plan-only") == "deterministic_contract"
    assert client.calls == []


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


@pytest.mark.parametrize(
    "text",
    [
        "今天没有完成合同审核",
        "明天不去南京出差了",
        "不是明天，是后天",
        "如果明天去南京，再提前准备材料",
        "如果今天完成合同审核，再提交给业务部门",
        "某同事说今天完成了合同审核",
        "会议纪要写着今天完成了合同审核",
        "会议纪要写着明天去南京出差",
        "你是agent1还是agent2",
        "撤回",
        "清空",
        "清空日报",
    ],
)
def test_active_daily_plain_work_fallback_defers_nonaffirmative_language_to_model(
    text: str,
):
    """The deterministic Daily fallback must not decide correction/cancellation.

    The adversarial client returns a read-only action here solely to prove the
    fallback deferred to semantics; real-model acceptance separately scores the
    appropriate business action for each language act.
    """

    client = AdversarialInternalQaClient()
    actor_id, daily, interpreter, runtime = _runtime(client)
    asyncio.run(runtime.handle(_request(actor_id, "negative-open", "进入日报")))
    before = daily.snapshot

    outcome = asyncio.run(runtime.handle(_request(actor_id, "negative-turn", text)))

    assert daily.snapshot == before
    assert interpreter.source_for("negative-turn") == "live_model"
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


def test_future_self_travel_without_daily_goal_survives_enforced_admission():
    client = TravelOnlyClient()
    actor_id, daily, _interpreter, runtime = _runtime(
        client,
        enforce_admission=True,
    )

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "admitted-travel-plan", "明天去广州出差"))
    )

    decisions = {
        decision.operation: decision
        for decision in outcome.decision.admission_trace.decisions
    }
    assert decisions["record_travel_event"].status == "admitted"
    assert decisions["record_travel_event"].reason_code == (
        "personal_travel_grounded_and_parseable"
    )
    assert decisions["capture_daily_event"].status == "admitted"
    assert decisions["capture_daily_event"].reason_code == "explicit_daily_fact_authorized"
    assert [command.command_type for command in outcome.command_plan.business_commands] == [
        "record_travel_candidate"
    ]
    assert [command.patch for command in outcome.command_plan.daily_commands] == [
        {"field": "tomorrow_plan", "items": ["明天去广州出差"]}
    ]
    assert outcome.command_plan.blocked_actions == ()


def test_cancelled_travel_never_projects_as_a_positive_tomorrow_plan():
    client = TravelOnlyClient()
    actor_id, daily, _interpreter, runtime = _runtime(client)

    outcome = asyncio.run(
        runtime.handle(_request(actor_id, "cancelled-travel", "明天不去广州出差了"))
    )

    assert {
        action.action_type for action in outcome.decision.required_actions
    } == {"record_travel_event"}
    assert daily.snapshot.today_work == ()
    assert daily.snapshot.problems == ()
    assert daily.snapshot.tomorrow_plan == ()
    assert outcome.command_plan is None or outcome.command_plan.daily_commands == ()
