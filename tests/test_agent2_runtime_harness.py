from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import date, datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from app.agent2.cognitive_core_v3 import CognitiveCoreV3, CognitiveTurn, SemanticInterpretation
from app.agent2.command_planner_v3 import CognitiveCommandPlan, CognitiveCommandPlanner
from app.agent2.conversation_state import ConversationState
from app.agent2.conversation_state_store import InMemoryConversationStateStore
from app.agent2.runtime import (
    Agent2RuntimeHarness,
    DomainExecutionResult,
    DomainPack,
    DomainPackContract,
    DomainPackRegistry,
    FUTURE_DOMAIN_CONTRACTS,
    InMemoryDailyDomainExecutor,
    InMemoryRuntimeAuditSink,
    MvpContextAssembler,
    RuntimeActor,
    RuntimeFailureOutcome,
    RuntimeInvariantViolation,
    RuntimeTurnRequest,
    build_phase1_domain_registry,
)
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot, TypedDailyCommand


class QueueSemanticInterpreter:
    def __init__(self, *payloads: dict):
        self._payloads = list(payloads)

    async def interpret(self, turn: CognitiveTurn, state: ConversationState) -> SemanticInterpretation:
        assert turn.conversation_id == state.conversation_id
        return SemanticInterpretation.from_payload(self._payloads.pop(0))


class BlockedDailyDomainExecutor:
    def __init__(self, snapshot: DailyReportMutationSnapshot):
        self.snapshot = snapshot
        self.received_commands: tuple[TypedDailyCommand, ...] = ()

    async def load_daily_snapshot(self, request: RuntimeTurnRequest) -> DailyReportMutationSnapshot:
        return self.snapshot

    async def execute(self, commands, context) -> DomainExecutionResult:
        self.received_commands = tuple(commands)
        assert all(isinstance(command, TypedDailyCommand) for command in commands)
        return DomainExecutionResult(
            domain_id="daily",
            status="blocked",
            command_count=len(commands),
            actual_write=False,
            would_write=False,
            reply_text="日报版本冲突，没有执行。",
            command_results=(
                {
                    "typed_command": commands[0].as_dict(),
                    "validation_status": "blocked",
                    "reason": "version_conflict",
                    "actual_write": False,
                },
            ),
        )


class CapturingDailyDomainExecutor(InMemoryDailyDomainExecutor):
    def __init__(self, snapshot: DailyReportMutationSnapshot):
        super().__init__(snapshot)
        self.execution_context = None

    async def execute(self, commands, context) -> DomainExecutionResult:
        self.execution_context = context
        return await super().execute(commands, context)


class DroppingPlanner:
    def plan(self, decision, context) -> CognitiveCommandPlan:
        return CognitiveCommandPlan(decision_id=UUID(decision.decision_id))


class ForgedReceiptDailyExecutor(BlockedDailyDomainExecutor):
    async def execute(self, commands, context) -> DomainExecutionResult:
        command = commands[0]
        forged = command.as_dict()
        forged["patch"] = {"field": "today_work", "items": ["伪造内容"]}
        return DomainExecutionResult(
            domain_id="daily",
            status="simulated",
            command_count=1,
            actual_write=False,
            would_write=True,
            reply_text="伪造 receipt",
            command_results=(
                {
                    "typed_command": forged,
                    "validation_status": "authorized",
                    "simulated": True,
                    "would_write": True,
                },
            ),
        )


class ContradictoryReceiptDailyExecutor(BlockedDailyDomainExecutor):
    async def execute(self, commands, context) -> DomainExecutionResult:
        command = commands[0]
        return DomainExecutionResult(
            domain_id="daily",
            status="simulated",
            command_count=1,
            actual_write=False,
            would_write=True,
            reply_text="矛盾 receipt",
            command_results=(
                {
                    "typed_command": command.as_dict(),
                    "validation_status": "authorized",
                    "simulated": True,
                    "actual_write": False,
                    "would_write": False,
                },
            ),
        )


class FailingAuditSink:
    def __init__(self):
        self.calls = 0

    async def record(self, record) -> None:
        self.calls += 1
        raise RuntimeError("audit unavailable")


def _append_payload(prefix: str, value: str = "完成审核") -> dict:
    return {
        "intents": ["daily_append"],
        "entities": [
            {
                "entity_id": f"{prefix}-entity",
                "entity_type": "daily_event",
                "value": value,
                "confidence": 1.0,
                "attributes": {"field": "today_work"},
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": f"{prefix}-action",
                "action_type": "capture_daily_event",
                "intent": "daily_append",
                "entity_ids": [f"{prefix}-entity"],
            }
        ],
        "context_update": {"current_goal": "daily_append", "remember_turn": True},
    }


def test_runtime_harness_executes_daily_through_typed_domain_pack_without_legacy_fallback():
    actor_id = uuid5(NAMESPACE_URL, "runtime-phase1-user")
    report_id = uuid5(NAMESPACE_URL, "runtime-phase1-report")
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_append"],
            "entities": [
                {
                    "entity_id": "daily-event-1",
                    "entity_type": "daily_event",
                    "value": "完成合同审核",
                    "confidence": 0.99,
                    "attributes": {"field": "today_work"},
                }
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "capture-daily-1",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-event-1"],
                }
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": ["daily-event-1"],
                "remember_turn": True,
            },
        }
    )
    state_store = InMemoryConversationStateStore()
    snapshot = DailyReportMutationSnapshot(
        report_id=report_id,
        owner_user_id=actor_id,
        version=0,
        status="collecting",
    )
    daily_executor = CapturingDailyDomainExecutor(snapshot)
    audit_sink = InMemoryRuntimeAuditSink()
    harness = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(interpreter),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=state_store,
            daily_snapshot_provider=daily_executor,
            daily_policy={"current_report_date": "2026-07-10"},
        ),
        domains=build_phase1_domain_registry(daily_executor=daily_executor),
        audit_sink=audit_sink,
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(
            actor_id=actor_id,
            display_name="测试律师",
            role="member",
            timezone="Asia/Shanghai",
        ),
        conversation_id="conversation-1",
        message_id="message-1",
        text="完成合同审核",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
        request_metadata={"source": "runtime_phase1_test"},
    )

    outcome = asyncio.run(harness.handle(request))
    saved_state = asyncio.run(
        state_store.load(user_id=str(actor_id), conversation_id="conversation-1")
    )

    assert outcome.status == "completed"
    assert outcome.reply.reply_type == "ack_simulated"
    assert outcome.actual_write is False
    assert outcome.would_write is True
    assert outcome.legacy_fallback_used is False
    assert outcome.state_version == 1
    assert outcome.domain_results[0].domain_id == "daily"
    assert outcome.domain_results[0].data["today_work"] == ["完成合同审核"]
    assert [event.stage for event in outcome.trace] == [
        "context_assembled",
        "decision_produced",
        "decision_validated",
        "domains_resolved",
        "plan_created",
        "domain_executed",
        "state_saved",
        "reply_composed",
        "audit_recorded",
    ]
    assert saved_state.version == 1
    assert saved_state.current_goal is not None
    assert saved_state.current_goal.intent == "daily_append"
    assert len(audit_sink.records) == 1
    assert audit_sink.records[0].actual_write is False
    assert audit_sink.records[0].would_write is True
    assert not {
        "allow_write",
        "should_write_db",
        "effects",
        "commands",
        "database_operation",
    } & set(asdict(outcome.decision))
    assert daily_executor.execution_context is not None
    assert not hasattr(daily_executor.execution_context, "request")
    assert not hasattr(daily_executor.execution_context, "text")


def test_domain_registry_rejects_duplicate_action_owner_at_startup():
    with pytest.raises(ValueError, match="duplicate domain action owner"):
        DomainPackRegistry(
            (
                DomainPack(
                    DomainPackContract(
                        domain_id="daily-a",
                        version="0.1.0",
                        command_source="daily",
                        action_types=("capture_daily_event",),
                        command_types=("append_item",),
                    )
                ),
                DomainPack(
                    DomainPackContract(
                        domain_id="daily-b",
                        version="0.1.0",
                        command_source="daily",
                        action_types=("capture_daily_event",),
                        command_types=("submit_report",),
                    )
                ),
            )
        )


def test_domain_registry_rejects_duplicate_domain_id_at_startup():
    with pytest.raises(ValueError, match="duplicate domain_id"):
        DomainPackRegistry(
            (
                DomainPack(
                    DomainPackContract(
                        domain_id="duplicate",
                        version="0.1.0",
                        command_source="daily",
                        action_types=("capture_daily_event",),
                        command_types=("append_item",),
                    )
                ),
                DomainPack(
                    DomainPackContract(
                        domain_id="duplicate",
                        version="0.2.0",
                        command_source="business",
                        action_types=("answer_case_query",),
                        command_types=("query_case_risk",),
                    )
                ),
            )
        )


def test_future_domains_have_contracts_without_phase1_executors():
    future = {contract.domain_id: contract for contract in FUTURE_DOMAIN_CONTRACTS}

    assert set(future) == {"case", "travel", "performance", "knowledge"}
    assert all(contract.version.endswith("contract-only") for contract in future.values())
    assert future["performance"].command_types == (
        "build_monthly_performance_view",
        "submit_monthly_report",
    )
    assert future["knowledge"].command_types == ("search_enterprise_knowledge",)


def test_mixed_daily_and_case_turn_returns_partial_with_explicit_case_receipt():
    actor_id = uuid5(NAMESPACE_URL, "runtime-phase1-mixed-user")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "runtime-phase1-mixed-report"),
        owner_user_id=actor_id,
        version=0,
        status="collecting",
    )
    interpreter = QueueSemanticInterpreter(
        {
            "intents": ["daily_append", "case_query"],
            "entities": [
                {
                    "entity_id": "daily-event-1",
                    "entity_type": "daily_event",
                    "value": "完成合同审核",
                    "confidence": 0.99,
                    "attributes": {"field": "today_work"},
                },
                {
                    "entity_id": "case-query-1",
                    "entity_type": "case_query",
                    "value": "海花岛案风险如何",
                    "confidence": 0.96,
                    "attributes": {"matter_hint": "海花岛案", "question": "风险如何"},
                },
            ],
            "confidence": 0.97,
            "required_actions": [
                {
                    "action_id": "capture-daily-1",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-event-1"],
                },
                {
                    "action_id": "answer-case-1",
                    "action_type": "answer_case_query",
                    "intent": "case_query",
                    "entity_ids": ["case-query-1"],
                },
            ],
            "clarification_need": None,
            "context_update": {"current_goal": "case_query", "remember_turn": True},
        }
    )
    state_store = InMemoryConversationStateStore()
    daily_executor = InMemoryDailyDomainExecutor(snapshot)
    harness = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(interpreter),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=state_store,
            daily_snapshot_provider=daily_executor,
            daily_policy={"current_report_date": "2026-07-10"},
        ),
        domains=build_phase1_domain_registry(daily_executor=daily_executor),
        audit_sink=InMemoryRuntimeAuditSink(),
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id),
        conversation_id="conversation-mixed",
        message_id="message-mixed",
        text="完成合同审核，另外海花岛案风险如何",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
    )

    outcome = asyncio.run(harness.handle(request))

    assert outcome.status == "partial"
    assert outcome.reply.reply_type == "partial"
    assert outcome.actual_write is False
    assert outcome.would_write is True
    assert [(result.domain_id, result.status) for result in outcome.domain_results] == [
        ("daily", "simulated"),
        ("case", "unavailable"),
    ]
    assert outcome.domain_results[1].command_results[0]["status"] == "unsupported_domain_contract"
    assert "case 能力尚未接入" in outcome.reply.text
    assert [command.command_type for command in outcome.command_plan.business_commands] == ["query_case_risk"]
    assert outcome.state_version == 0
    persisted = asyncio.run(
        state_store.load(user_id=str(actor_id), conversation_id="conversation-mixed")
    )
    assert persisted.version == 0


def test_blocked_typed_receipt_keeps_proposed_conversation_state_uncommitted():
    actor_id = uuid5(NAMESPACE_URL, "runtime-phase1-blocked-user")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "runtime-phase1-blocked-report"),
        owner_user_id=actor_id,
        version=3,
        status="collecting",
    )
    executor = BlockedDailyDomainExecutor(snapshot)
    state_store = InMemoryConversationStateStore()
    harness = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(
            QueueSemanticInterpreter(
                {
                    "intents": ["daily_append"],
                    "entities": [
                        {
                            "entity_id": "daily-event-blocked",
                            "entity_type": "daily_event",
                            "value": "完成尽调",
                            "confidence": 0.99,
                            "attributes": {"field": "today_work"},
                        }
                    ],
                    "confidence": 0.99,
                    "required_actions": [
                        {
                            "action_id": "capture-daily-blocked",
                            "action_type": "capture_daily_event",
                            "intent": "daily_append",
                            "entity_ids": ["daily-event-blocked"],
                        }
                    ],
                    "clarification_need": None,
                    "context_update": {
                        "current_goal": "daily_append",
                        "remember_turn": True,
                    },
                }
            )
        ),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=state_store,
            daily_snapshot_provider=executor,
        ),
        domains=build_phase1_domain_registry(daily_executor=executor),
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id),
        conversation_id="conversation-blocked",
        message_id="message-blocked",
        text="完成尽调",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
    )

    outcome = asyncio.run(harness.handle(request))
    persisted = asyncio.run(
        state_store.load(user_id=str(actor_id), conversation_id="conversation-blocked")
    )

    assert outcome.status == "blocked"
    assert outcome.actual_write is False
    assert outcome.state_version == 0
    assert outcome.state.current_goal is None
    assert persisted.version == 0
    assert persisted.current_goal is None
    assert len(executor.received_commands) == 1
    assert executor.received_commands[0].command_type == "append_item"
    assert "state_retained" in [event.stage for event in outcome.trace]


@pytest.mark.parametrize("forbidden_key", ["database_operation", "operation_spec"])
def test_nested_execution_field_in_cognitive_payload_fails_before_planning(forbidden_key):
    actor_id = uuid5(NAMESPACE_URL, "runtime-phase1-forbidden-user")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "runtime-phase1-forbidden-report"),
        owner_user_id=actor_id,
        version=0,
        status="collecting",
    )
    executor = InMemoryDailyDomainExecutor(snapshot)
    state_store = InMemoryConversationStateStore()
    audit_sink = InMemoryRuntimeAuditSink()
    harness = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(
            QueueSemanticInterpreter(
                {
                    "intents": ["daily_append"],
                    "entities": [
                        {
                            "entity_id": "daily-event-forbidden",
                            "entity_type": "daily_event",
                            "value": "完成审核",
                            "confidence": 0.99,
                            "attributes": {
                                "field": "today_work",
                                forbidden_key: "insert daily_reports",
                            },
                        }
                    ],
                    "confidence": 0.99,
                    "required_actions": [
                        {
                            "action_id": "capture-daily-forbidden",
                            "action_type": "capture_daily_event",
                            "intent": "daily_append",
                            "entity_ids": ["daily-event-forbidden"],
                        }
                    ],
                    "clarification_need": None,
                    "context_update": {},
                }
            )
        ),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=state_store,
            daily_snapshot_provider=executor,
        ),
        domains=build_phase1_domain_registry(daily_executor=executor),
        audit_sink=audit_sink,
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id),
        conversation_id="conversation-forbidden",
        message_id="message-forbidden",
        text="完成审核",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
    )

    outcome = asyncio.run(harness.handle(request))

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.status == "failed_closed"
    assert outcome.failed_stage == "decision_validation"
    assert outcome.error_code == "runtime_invariant_violation"
    assert outcome.actual_write is False
    assert outcome.legacy_fallback_used is False
    assert outcome.trace[-1].stage == "audit_recorded"
    assert audit_sink.records[0].status == "failed_closed"
    assert executor.snapshot.version == 0


def test_shadow_runtime_never_persists_conversation_state():
    actor_id = uuid5(NAMESPACE_URL, "runtime-phase1-shadow-user")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "runtime-phase1-shadow-report"),
        owner_user_id=actor_id,
        version=0,
        status="collecting",
    )
    executor = InMemoryDailyDomainExecutor(snapshot)
    store = InMemoryConversationStateStore()
    harness = Agent2RuntimeHarness(
        mode="shadow",
        core=CognitiveCoreV3(
            QueueSemanticInterpreter(
                {
                    "intents": ["daily_append"],
                    "entities": [
                        {
                            "entity_id": "shadow-event",
                            "entity_type": "daily_event",
                            "value": "完成影子验证",
                            "confidence": 1.0,
                            "attributes": {"field": "today_work"},
                        }
                    ],
                    "confidence": 1.0,
                    "required_actions": [
                        {
                            "action_id": "shadow-action",
                            "action_type": "capture_daily_event",
                            "intent": "daily_append",
                            "entity_ids": ["shadow-event"],
                        }
                    ],
                    "context_update": {"current_goal": "daily_append", "remember_turn": True},
                }
            )
        ),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=store,
            daily_snapshot_provider=executor,
        ),
        domains=build_phase1_domain_registry(daily_executor=executor),
        audit_sink=InMemoryRuntimeAuditSink(),
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id),
        conversation_id="shadow-conversation",
        message_id="shadow-message",
        text="完成影子验证",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
    )

    outcome = asyncio.run(harness.handle(request))
    persisted = asyncio.run(
        store.load(user_id=str(actor_id), conversation_id="shadow-conversation")
    )

    assert outcome.status == "completed"
    assert outcome.state_version == 0
    assert persisted.version == 0
    assert "state_not_persisted" in [event.stage for event in outcome.trace]


def test_planner_cannot_silently_drop_a_cognitive_action():
    actor_id = uuid5(NAMESPACE_URL, "runtime-phase1-dropped-action-user")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "runtime-phase1-dropped-action-report"),
        owner_user_id=actor_id,
        version=0,
        status="collecting",
    )
    executor = InMemoryDailyDomainExecutor(snapshot)
    store = InMemoryConversationStateStore()
    harness = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(
            QueueSemanticInterpreter(
                {
                    "intents": ["daily_append"],
                    "entities": [
                        {
                            "entity_id": "dropped-event",
                            "entity_type": "daily_event",
                            "value": "完成审查",
                            "confidence": 1.0,
                            "attributes": {"field": "today_work"},
                        }
                    ],
                    "confidence": 1.0,
                    "required_actions": [
                        {
                            "action_id": "dropped-action",
                            "action_type": "capture_daily_event",
                            "intent": "daily_append",
                            "entity_ids": ["dropped-event"],
                        }
                    ],
                    "context_update": {"remember_turn": True},
                }
            )
        ),
        planner=DroppingPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=store,
            daily_snapshot_provider=executor,
        ),
        domains=build_phase1_domain_registry(daily_executor=executor),
        audit_sink=InMemoryRuntimeAuditSink(),
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id),
        conversation_id="dropped-action-conversation",
        message_id="dropped-action-message",
        text="完成审查",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
    )

    outcome = asyncio.run(harness.handle(request))

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.failed_stage == "plan_validation"
    assert outcome.error_code == "runtime_invariant_violation"
    assert executor.snapshot.version == 0


def test_unknown_cognitive_action_type_fails_closed_before_planning():
    actor_id = uuid5(NAMESPACE_URL, "runtime-phase1-unknown-action-user")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "runtime-phase1-unknown-action-report"),
        owner_user_id=actor_id,
        version=0,
        status="collecting",
    )
    executor = InMemoryDailyDomainExecutor(snapshot)
    payload = _append_payload("unknown-action")
    payload["required_actions"][0]["action_type"] = "unknown_contract_action"
    harness = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(QueueSemanticInterpreter(payload)),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=InMemoryConversationStateStore(),
            daily_snapshot_provider=executor,
        ),
        domains=build_phase1_domain_registry(daily_executor=executor),
        audit_sink=InMemoryRuntimeAuditSink(),
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id),
        conversation_id="unknown-action-conversation",
        message_id="unknown-action-message",
        text="未知动作",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
    )

    outcome = asyncio.run(harness.handle(request))

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.failed_stage == "decision_validation"
    assert outcome.error_code == "runtime_invariant_violation"
    assert executor.snapshot.version == 0


def test_closed_cognitive_schema_rejects_nested_payload_in_allowed_business_field():
    actor_id = uuid5(NAMESPACE_URL, "runtime-phase1-nested-business-user")
    executor = InMemoryDailyDomainExecutor(
        DailyReportMutationSnapshot(
            report_id=uuid5(NAMESPACE_URL, "runtime-phase1-nested-business-report"),
            owner_user_id=actor_id,
            version=0,
            status="collecting",
        )
    )
    payload = {
        "intents": ["case_query"],
        "entities": [
            {
                "entity_id": "nested-case-query",
                "entity_type": "case_query",
                "value": "案件风险",
                "confidence": 1.0,
                "attributes": {
                    "matter_hint": "海花岛案",
                    "question": {"operation_spec": "write elsewhere"},
                },
            }
        ],
        "confidence": 1.0,
        "required_actions": [
            {
                "action_id": "nested-case-action",
                "action_type": "answer_case_query",
                "intent": "case_query",
                "entity_ids": ["nested-case-query"],
            }
        ],
        "context_update": {},
    }
    harness = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(QueueSemanticInterpreter(payload)),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=InMemoryConversationStateStore(),
            daily_snapshot_provider=executor,
        ),
        domains=build_phase1_domain_registry(daily_executor=executor),
        audit_sink=InMemoryRuntimeAuditSink(),
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id),
        conversation_id="nested-business-conversation",
        message_id="nested-business-message",
        text="查询案件风险",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
    )

    outcome = asyncio.run(harness.handle(request))

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.failed_stage == "decision_validation"
    assert outcome.error_code == "runtime_invariant_violation"


@pytest.mark.parametrize(
    "executor_type",
    [ForgedReceiptDailyExecutor, ContradictoryReceiptDailyExecutor],
)
def test_domain_receipt_must_match_the_complete_planned_typed_command(executor_type):
    actor_id = uuid5(NAMESPACE_URL, "runtime-phase1-forged-receipt-user")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "runtime-phase1-forged-receipt-report"),
        owner_user_id=actor_id,
        version=0,
        status="collecting",
    )
    executor = executor_type(snapshot)
    store = InMemoryConversationStateStore()
    harness = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(QueueSemanticInterpreter(_append_payload("forged"))),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=store,
            daily_snapshot_provider=executor,
        ),
        domains=build_phase1_domain_registry(daily_executor=executor),
        audit_sink=InMemoryRuntimeAuditSink(),
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id),
        conversation_id="forged-receipt-conversation",
        message_id="forged-receipt-message",
        text="完成审核",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
    )

    outcome = asyncio.run(harness.handle(request))

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.failed_stage == "domain_execution"
    assert outcome.error_code == "invalid_runtime_contract"
    assert outcome.state_version == 0


def test_audit_failure_after_replay_checkpoint_reports_the_saved_state_version():
    actor_id = uuid5(NAMESPACE_URL, "runtime-phase1-audit-failure-user")
    snapshot = DailyReportMutationSnapshot(
        report_id=uuid5(NAMESPACE_URL, "runtime-phase1-audit-failure-report"),
        owner_user_id=actor_id,
        version=0,
        status="collecting",
    )
    executor = InMemoryDailyDomainExecutor(snapshot)
    store = InMemoryConversationStateStore()
    audit_sink = FailingAuditSink()
    harness = Agent2RuntimeHarness(
        mode="replay",
        core=CognitiveCoreV3(QueueSemanticInterpreter(_append_payload("audit-failure"))),
        planner=CognitiveCommandPlanner(),
        context_assembler=MvpContextAssembler(
            state_store=store,
            daily_snapshot_provider=executor,
        ),
        domains=build_phase1_domain_registry(daily_executor=executor),
        audit_sink=audit_sink,
    )
    request = RuntimeTurnRequest(
        tenant_id="tenant-legal",
        actor=RuntimeActor(actor_id=actor_id),
        conversation_id="audit-failure-conversation",
        message_id="audit-failure-message",
        text="完成审核",
        occurred_at=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        channel="test",
    )

    outcome = asyncio.run(harness.handle(request))
    persisted = asyncio.run(
        store.load(user_id=str(actor_id), conversation_id="audit-failure-conversation")
    )

    assert isinstance(outcome, RuntimeFailureOutcome)
    assert outcome.failed_stage == "audit"
    assert outcome.error_code == "audit_failed"
    assert outcome.state_version == 1
    assert outcome.state is not None and outcome.state.version == 1
    assert persisted.version == 1
    assert outcome.trace[-1].stage == "audit_failed"
    assert not any(event.stage == "audit_recorded" for event in outcome.trace)
    assert audit_sink.calls == 1
