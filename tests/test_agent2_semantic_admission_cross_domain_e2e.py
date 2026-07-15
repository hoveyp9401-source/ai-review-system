from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.agent2.business.case_progress import CaseRecord
from app.agent2.business.composition import Phase2BusinessComposer
from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.executor import InMemoryBusinessExecutor
from app.agent2.admission_store import InMemoryAdmissionTicketStore
from app.agent2.cognitive_core_v3 import (
    CognitiveCoreV3,
    CognitiveTurn,
    SemanticInterpretation,
)
from app.agent2.command_planner_v3 import (
    CognitiveCommandPlanner,
    CommandPlanningContext,
)
from app.agent2.conversation_state import ConversationState
from app.agent2.domain_admission import DomainAdmissionEngine
from app.agent2.typed_daily_commands import DailyReportMutationSnapshot
from app.agent2.typed_daily_executor import (
    TypedDailyExecutionContext,
    execute_typed_agent2_daily_commands,
)


NOW = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
TENANT_ID = "sandbox-agent2-phase2-20260711"
ACTOR_UUID = uuid5(NAMESPACE_URL, "semantic-admission-cross-domain-actor")
ACTOR_ID = str(ACTOR_UUID)
CASE_ID = str(uuid5(NAMESPACE_URL, "semantic-admission-cross-domain-case"))
REPORT_ID = uuid5(NAMESPACE_URL, "semantic-admission-cross-domain-report")


class _Interpreter:
    def __init__(self, proposal: SemanticInterpretation) -> None:
        self.proposal = proposal

    async def interpret(self, turn, state):
        return self.proposal


class _CaseRepository:
    def __init__(self, cases: tuple[CaseRecord, ...]) -> None:
        self.cases = cases

    async def list_visible(self, context):
        return self.cases


class _AsyncExecutor:
    def __init__(self, delegate: InMemoryBusinessExecutor) -> None:
        self.delegate = delegate

    async def execute(self, command, context):
        return self.delegate.execute(command, context)


class _DailyReceiptSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(rowcount=1)

    async def scalars(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [])

    async def flush(self) -> None:
        return None


class _DailyAdmissionTicketStore:
    def __init__(
        self,
        authoritative_ticket: dict[str, object],
        authority_store: InMemoryAdmissionTicketStore,
    ) -> None:
        self.authoritative_ticket = authoritative_ticket
        self.authority_store = authority_store
        self.requests: list[object] = []
        self.receipts: list[object] = []
        self._lease_context = None

    async def acquire(self, request):
        assert self.authority_store.status(
            str(self.authoritative_ticket["ticket_id"])
        ) == "issued"
        assert dict(request.admission_ticket) == self.authoritative_ticket
        assert request.tenant_id == TENANT_ID
        assert request.user_id == ACTOR_ID
        assert request.conversation_id == "semantic-admission-cross-domain"
        assert request.source_message_id == "semantic-admission-cross-domain-message"
        assert request.conversation_state_version == 0
        assert request.receipt_kind == "daily_report"
        self.requests.append(request)
        self._lease_context = self.authority_store.acquire(
            self.authoritative_ticket
        )
        return self._lease_context.__enter__()

    async def consume(self, lease, *, receipt, consumed_at) -> None:
        assert lease.ticket_id == self.authoritative_ticket["ticket_id"]
        assert receipt.status == "executed"
        assert receipt.actual_write is True
        assert consumed_at == NOW
        self.receipts.append(receipt)
        lease.consume(f"{receipt.receipt_kind}:{receipt.receipt_id}")
        assert self._lease_context is not None
        self._lease_context.__exit__(None, None, None)
        self._lease_context = None

    async def validate_consumed_execution_replay(self, request, *, receipt_id):
        raise AssertionError("first execution must not enter replay validation")


def _proposal(texts: tuple[str, str, str, str]) -> SemanticInterpretation:
    daily, case, travel, ambiguous = texts
    case_fact_start = case.index("今天")
    return SemanticInterpretation.from_payload(
        {
            "intents": [
                "daily_append",
                "case_progress",
                "travel_event",
            ],
            "segments": [
                {
                    "segment_id": "daily-segment",
                    "text": daily,
                    "intents": ["daily_append"],
                    "entity_ids": ["daily-event"],
                    "action_ids": ["append-daily"],
                },
                {
                    "segment_id": "case-segment",
                    "text": case,
                    "intents": ["case_progress"],
                    "entity_ids": ["case-fact"],
                    "action_ids": ["record-case"],
                },
                {
                    "segment_id": "travel-segment",
                    "text": travel,
                    "intents": ["travel_event"],
                    "entity_ids": ["travel-event"],
                    "action_ids": ["record-travel"],
                },
                {
                    "segment_id": "ambiguous-case-segment",
                    "text": ambiguous,
                    "intents": ["case_progress"],
                    "entity_ids": ["ambiguous-case"],
                    "action_ids": ["record-ambiguous-case"],
                },
            ],
            "entities": [
                {
                    "entity_id": "daily-event",
                    "entity_type": "daily_event",
                    "value": "今天完成合同审核",
                    "confidence": 1.0,
                    "attributes": {"field": "today_work"},
                },
                {
                    "entity_id": "case-fact",
                    "entity_type": "case_ref",
                    "value": "云璟府案",
                    "confidence": 1.0,
                    "attributes": {
                        "normalized_fact": "今天联系法院推进执行",
                        "factual_progress": ["今天联系法院推进执行"],
                        "completed_actions": ["联系法院"],
                        "next_actions": [],
                        "blocking_issues": [],
                        "statement_mode": "asserted",
                        "evidence_spans": [[case_fact_start, len(case)]],
                    },
                },
                {
                    "entity_id": "travel-event",
                    "entity_type": "travel_event",
                    "value": travel,
                    "confidence": 1.0,
                    "attributes": {
                        "destination": "南京",
                        "date_hint": "明天",
                        "purpose": "出差",
                        "statement_mode": "asserted",
                        "traveler_scope": "self",
                        "evidence_spans": [[0, len(travel)]],
                    },
                },
                {
                    "entity_id": "ambiguous-case",
                    "entity_type": "case_ref",
                    "value": "案件材料",
                    "confidence": 0.99,
                    "attributes": {
                        "normalized_fact": "案件材料已整理",
                        "statement_mode": "asserted",
                        "evidence_spans": [[0, len(ambiguous)]],
                    },
                },
            ],
            "confidence": 0.99,
            "required_actions": [
                {
                    "action_id": "append-daily",
                    "action_type": "capture_daily_event",
                    "intent": "daily_append",
                    "entity_ids": ["daily-event"],
                },
                {
                    "action_id": "record-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["case-fact"],
                },
                {
                    "action_id": "record-travel",
                    "action_type": "record_travel_event",
                    "intent": "travel_event",
                    "entity_ids": ["travel-event"],
                },
                {
                    "action_id": "record-ambiguous-case",
                    "action_type": "record_case_progress",
                    "intent": "case_progress",
                    "entity_ids": ["ambiguous-case"],
                },
            ],
            "clarification_need": None,
            "context_update": {
                "current_goal": "daily_append",
                "remember_entity_ids": [
                    "daily-event",
                    "case-fact",
                    "travel-event",
                ],
                "remember_turn": True,
            },
        }
    )


@pytest.mark.asyncio
async def test_three_domain_enforce_preserves_valid_siblings_and_executes_each_ticket_once(
    monkeypatch,
):
    segments = (
        "日报记：今天完成合同审核",
        "云璟府案今天联系法院推进执行",
        "明天去南京出差",
        "案件材料已整理",
    )
    text = "；".join(segments)
    turn = CognitiveTurn(
        tenant_id=TENANT_ID,
        actor_user_id=ACTOR_ID,
        user_id=f"{TENANT_ID}:{ACTOR_ID}",
        conversation_id="semantic-admission-cross-domain",
        message_id="semantic-admission-cross-domain-message",
        text=text,
        occurred_at=NOW,
        resources={
            "daily_draft": {
                "report_id": str(REPORT_ID),
                "version": 4,
                "status": "collecting",
                "items": [],
            },
            "daily_reports": [
                {
                    "report_id": str(REPORT_ID),
                    "report_date": "2026-07-14",
                    "version": 4,
                    "status": "collecting",
                    "items": [],
                }
            ],
            "daily_policy": {"current_report_date": "2026-07-14"},
            "active_tasks": [
                {
                    "workflow": "daily_report",
                    "task_id": str(REPORT_ID),
                    "status": "collecting",
                    "metadata": {"report_date": "2026-07-14"},
                }
            ],
            "visible_cases": [
                {
                    "case_id": CASE_ID,
                    "case_number": "（2026）苏01执100号",
                    "case_name": "云璟府物业服务合同执行案",
                    "external_case_id": "BGGL-2026-0100",
                    "confirmed_aliases": ["云璟府案"],
                    "version": 7,
                }
            ],
            "timezone": "Asia/Shanghai",
        },
    )
    state = ConversationState.empty(
        user_id=turn.user_id,
        conversation_id=turn.conversation_id,
    )
    core = await CognitiveCoreV3(
        _Interpreter(_proposal(segments)),
        admission_engine=DomainAdmissionEngine(),
        admission_enforced=True,
    ).process(turn, state)

    decisions = {item.action_id: item for item in core.decision.admission_trace.decisions}
    assert core.decision.admission_mode == "enforced"
    assert decisions["record-ambiguous-case"].status == "blocked"
    assert decisions["record-ambiguous-case"].reason_code == (
        "case_reference_not_uniquely_authorized"
    )
    assert {item.action_id for item in core.decision.required_actions} == {
        "append-daily",
        "record-case",
        "record-travel",
    }
    assert {(item.domain, item.operation) for item in core.decision.admission_tickets} == {
        ("report", "capture_daily_event"),
        ("case", "record_case_progress"),
        ("travel", "record_travel_event"),
    }
    assert len({item.ticket_id for item in core.decision.admission_tickets}) == 3

    daily_snapshot = DailyReportMutationSnapshot(
        report_id=REPORT_ID,
        owner_user_id=ACTOR_UUID,
        version=4,
        status="collecting",
    )
    plan = CognitiveCommandPlanner().plan(
        core.decision,
        CommandPlanningContext(
            message_id=turn.message_id,
            actor_user_id=ACTOR_UUID,
            daily_snapshot=daily_snapshot,
        ),
    )
    assert len(plan.daily_commands) == 1
    assert plan.daily_commands[0].admission_required is True
    assert [item.command_type for item in plan.business_commands] == [
        "record_case_progress_candidate",
        "record_travel_candidate",
    ]
    assert all(item.admission_required for item in plan.business_commands)
    assert [item.action_id for item in core.decision.admission_trace.decisions if item.status == "blocked"] == [
        "record-ambiguous-case"
    ]

    ticket_store = InMemoryAdmissionTicketStore(
        tuple(item.as_dict() for item in core.decision.admission_tickets)
    )
    cases = (
        CaseRecord(
            CASE_ID,
            TENANT_ID,
            "（2026）苏01执100号",
            "云璟府物业服务合同执行案",
            ("云璟府物业服务合同执行案",),
            confirmed_aliases=("云璟府案",),
            version=7,
        ),
    )
    executor = InMemoryBusinessExecutor(
        cases=cases,
        admission_ticket_store=ticket_store,
    )
    composer = Phase2BusinessComposer(
        case_repository=_CaseRepository(cases),  # type: ignore[arg-type]
        executor=_AsyncExecutor(executor),  # type: ignore[arg-type]
    )
    context = BusinessCommandContext(
        tenant_id=TENANT_ID,
        company_id="company-1",
        department_id="legal",
        team_id="litigation",
        actor_user_id=ACTOR_ID,
        actor_role_ids=("lawyer",),
        allowed_case_ids=(CASE_ID,),
        source_message_id=turn.message_id,
        source_channel="admission_e2e",
        occurred_at=NOW,
        conversation_id=turn.conversation_id,
        execution_started_at=NOW,
        conversation_state_version=0,
    )
    result = await composer.execute(plan.business_commands, context)

    assert [item.status for item in result.actions] == ["executed", "executed"], [
        (
            getattr(item.block, "reason_code", "")
            or getattr(item.receipt, "error_code", "")
        )
        for item in result.actions
    ]
    assert len(executor.case_progress) == 1
    assert len(executor.travel_intents) == 1
    business_ticket_ids = {
        item.admission_ticket["ticket_id"] for item in plan.business_commands
    }
    assert {ticket_store.status(ticket_id) for ticket_id in business_ticket_ids} == {
        "consumed"
    }
    report_ticket = dict(plan.daily_commands[0].admission_ticket)
    report_ticket_id = report_ticket["ticket_id"]
    assert ticket_store.status(report_ticket_id) == "issued"

    existing_report = SimpleNamespace(
        id=REPORT_ID,
        user_id=ACTOR_UUID,
        report_date=date(2026, 7, 14),
        status="collecting",
        today_work=[],
        problems=[],
        tomorrow_plan=[],
        section_status={"_agent2_report_version": 4},
    )
    persisted: dict[str, object] = {}

    async def fake_lock(*args, **kwargs):
        return None

    async def fake_get_report(session, user_id, report_date):
        assert user_id == ACTOR_UUID
        assert report_date == date(2026, 7, 14)
        return existing_report

    async def fake_upsert(session, **kwargs):
        persisted.update(kwargs)
        return SimpleNamespace(
            id=existing_report.id,
            today_work=kwargs["today_work"],
            problems=kwargs["problems"],
            tomorrow_plan=kwargs["tomorrow_plan"],
        )

    async def fake_sync(*args, **kwargs):
        return SimpleNamespace(status="completed", reason_code="")

    monkeypatch.setattr("app.repositories.acquire_daily_report_advisory_lock", fake_lock)
    monkeypatch.setattr("app.repositories.get_report", fake_get_report)
    monkeypatch.setattr("app.repositories.upsert_daily_report", fake_upsert)
    monkeypatch.setattr(
        "app.agent2.typed_daily_executor.sync_focused_report_task",
        fake_sync,
    )
    daily_store = _DailyAdmissionTicketStore(report_ticket, ticket_store)
    daily_result = await execute_typed_agent2_daily_commands(
        _DailyReceiptSession(),
        user=SimpleNamespace(
            id=ACTOR_UUID,
            team_id=uuid5(NAMESPACE_URL, "semantic-admission-cross-domain-team"),
            timezone="Asia/Shanghai",
        ),
        commands=plan.daily_commands,
        execution_context=TypedDailyExecutionContext(
            report_date=date(2026, 7, 14),
            source="agent2_v3_admission_e2e",
            source_text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            tenant_id=TENANT_ID,
            conversation_id=turn.conversation_id,
            source_turn_id=turn.message_id,
            occurred_at=NOW,
            execution_started_at=NOW,
            conversation_state_version=0,
        ),
        settings=SimpleNamespace(timezone="Asia/Shanghai"),
        admission_ticket_store=daily_store,
        execution_authority="semantic_ticket",
    )

    assert daily_result.report_saved is True
    assert persisted["report_id_override"] is None
    assert len(daily_store.requests) == 1
    assert len(daily_store.receipts) == 1
    all_ticket_ids = {*business_ticket_ids, report_ticket_id}
    assert {ticket_store.status(ticket_id) for ticket_id in all_ticket_ids} == {
        "consumed"
    }
    assert len(
        {
            *(action.receipt.receipt_id for action in result.actions),
            *(receipt.receipt_id for receipt in daily_store.receipts),
        }
    ) == 3

    duplicate = await composer.execute(plan.business_commands, context)
    assert [item.status for item in duplicate.actions] == ["duplicate", "duplicate"]
    assert len(executor.case_progress) == 1
    assert len(executor.travel_intents) == 1
