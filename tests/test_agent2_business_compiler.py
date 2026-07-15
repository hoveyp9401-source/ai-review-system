from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import uuid4

import pytest

from app.agent2.admission_contracts import ADMISSION_CONTRACT_VERSION
from app.agent2.business.compiler import (
    CaseFollowupPolicyRecord,
    Phase2BusinessCommandCompiler,
)
from app.agent2.case_followup_commands import TriggerCaseFollowupNow, UpdateCaseFollowupPolicy
from app.agent2.business.composition import Phase2BusinessComposer, business_composition_reply_text
from app.agent2.business.case_progress import CaseRecord
from app.agent2.business.contracts import (
    BusinessCommandContext,
    BusinessReceipt,
    CreateCaseProgress,
    SnoozeCaseFollowup,
    CreateTravelIntent,
    DeleteCaseProgress,
    QueryCaseProgress,
    QueryPartyCases,
    RespondTravelCollaboration,
    UpdateCaseProgress,
)
from app.agent2.business.executor import InMemoryBusinessExecutor
from app.agent2.business.party import PartyResolution
from app.agent2.business.repositories import CaseProgressTargetResolution
from app.agent2.command_planner_v3 import TypedBusinessCommand
from app.agent2.selection_continuation import bind_selected_business_command
from app.agent2.selection_pending import SelectionPendingFactory


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def _context(*, allowed_case_ids=()) -> BusinessCommandContext:
    return BusinessCommandContext(
        tenant_id="tenant-test",
        company_id="company-test",
        department_id="legal",
        team_id="litigation",
        actor_user_id="user-1",
        actor_role_ids=("lawyer",),
        allowed_case_ids=tuple(allowed_case_ids),
        source_message_id="message-1",
        source_channel="dingtalk",
        occurred_at=NOW,
    )


def _candidate(command_type: str, entity: dict, segment_text: str) -> TypedBusinessCommand:
    return TypedBusinessCommand(
        command_id=uuid4(),
        decision_id=uuid4(),
        sub_decision_id=uuid4(),
        command_type=command_type,
        target_system="travel_coordination" if "travel" in command_type else "case_progress",
        entity_ids=(entity["entity_id"],),
        payload={
            "entities": [entity],
            "parameters": {},
            "source_segments": [
                {
                    "segment_id": "segment-1",
                    "text": segment_text,
                    "text_hash": hashlib.sha256(segment_text.encode("utf-8")).hexdigest(),
                    "start_offset": 0,
                    "end_offset": len(segment_text),
                }
            ],
        },
        execution_mode="candidate",
        idempotency_key="candidate-key",
    )


def _sha256_json(value: object) -> str:
    material = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _with_shadow_case_ticket(
    candidate: TypedBusinessCommand,
    *,
    case_id: str,
    case_version: int,
) -> TypedBusinessCommand:
    entity = candidate.payload["entities"][0]
    segment = candidate.payload["source_segments"][0]
    source_text = segment["text"]
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    action_id = "action-1"
    object_ref = {
        "object_type": "case",
        "stable_id": case_id,
        "version": case_version,
    }
    authority_scope = {
        "case_id": case_id,
        "version": case_version,
        "raw_fact": source_text,
        "case_reference": entity["value"],
        "attributes": deepcopy(entity.get("attributes") or {}),
    }
    allowed_changed_fields = [
        "summary",
        "details",
        "progress_type",
        "current_status",
        "next_actions",
        "hearing_readiness",
        "blocking_issues",
    ]
    fact_claims_sha256 = _sha256_json(
        {
            "action_id": action_id,
            "operation": "record_case_progress",
            "segment_text_sha256": source_hash,
            "authority_scope": authority_scope,
            "allowed_changed_fields": allowed_changed_fields,
        }
    )
    authorized_command_sha256 = _sha256_json(
        {
            "domain": "case",
            "operation": "record_case_progress",
            "object_ref": object_ref,
            "authority_scope": authority_scope,
            "allowed_changed_fields": allowed_changed_fields,
            "fact_claims_sha256": fact_claims_sha256,
        }
    )
    ticket = {
        "ticket_id": str(uuid4()),
        "tenant_id": "tenant-test",
        "user_id": "user-1",
        "conversation_id": "",
        "source_message_id": "message-1",
        "action_id": action_id,
        "segment_id": segment["segment_id"],
        "segment_text_sha256": source_hash,
        "domain": "case",
        "operation": "record_case_progress",
        "object_ref": object_ref,
        "authority_scope": authority_scope,
        "allowed_changed_fields": allowed_changed_fields,
        "fact_claims_sha256": fact_claims_sha256,
        "authorized_command_sha256": authorized_command_sha256,
        "ticket_status": "issued",
        "contract_version": ADMISSION_CONTRACT_VERSION,
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=4)).isoformat(),
        "executor_revalidation_required": True,
        "proves_business_write": False,
    }
    return replace(candidate, admission_ticket=ticket)


def test_compiler_canonicalizes_travel_candidate_into_create_travel_intent():
    candidate = _candidate(
        "record_travel_candidate",
        {
            "entity_id": "travel-1",
            "entity_type": "travel_event",
            "value": "明天去南京开庭",
            "confidence": 0.97,
            "attributes": {"destination": "江苏南京", "date_hint": "tomorrow", "purpose": "开庭"},
        },
        "明天去南京开庭",
    )

    result = Phase2BusinessCommandCompiler().compile(candidate, _context(), cases=())

    assert result.block is None
    assert isinstance(result.command, CreateTravelIntent)
    assert result.command.destination_normalized == "南京市"
    assert result.command.city_code == "320100"
    assert result.command.start_at.date().isoformat() == "2026-07-12"
    assert result.command.end_at.date().isoformat() == "2026-07-12"


def test_compiler_accepts_model_iso_date_hint_for_explicit_chinese_travel_turn():
    candidate = _candidate(
        "record_travel_candidate",
        {
            "entity_id": "travel-production-model-shape",
            "entity_type": "travel_event",
            "value": "明日上午出差去昆明",
            "confidence": 1.0,
            "attributes": {
                "destination": "昆明",
                "date_hint": "2026-07-12 上午",
                "purpose": "出差",
            },
        },
        "明日上午出差去昆明",
    )

    result = Phase2BusinessCommandCompiler().compile(candidate, _context(), cases=())

    assert result.block is None
    assert isinstance(result.command, CreateTravelIntent)
    assert result.command.destination_normalized == "昆明市"
    assert result.command.start_at.date().isoformat() == "2026-07-12"
    assert result.command.end_at.date().isoformat() == "2026-07-12"


def test_compiler_fails_closed_when_travel_location_is_only_a_province():
    candidate = _candidate(
        "record_travel_candidate",
        {
            "entity_id": "travel-1",
            "entity_type": "travel_event",
            "value": "明天去江苏出差",
            "confidence": 0.97,
            "attributes": {"destination": "江苏", "date_hint": "tomorrow", "purpose": "出差"},
        },
        "明天去江苏出差",
    )

    result = Phase2BusinessCommandCompiler().compile(candidate, _context(), cases=())

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "travel_location_needs_clarification"


def test_compiler_recognizes_changzhou_and_only_asks_for_missing_date():
    candidate = _candidate(
        "record_travel_candidate",
        {
            "entity_id": "travel-changzhou",
            "entity_type": "travel_event",
            "value": "出差常州，进行武进2007065地块酒店项目沟通取证",
            "confidence": 1.0,
            "attributes": {
                "destination": "常州",
                "date_hint": "",
                "purpose": "进行武进2007065地块酒店项目沟通取证",
            },
        },
        "出差常州，进行武进2007065地块酒店项目沟通取证",
    )

    result = Phase2BusinessCommandCompiler().compile(candidate, _context(), cases=())

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "travel_time_needs_clarification"
    assert result.outcome_context == {
        "destination": "常州市",
        "purpose": "进行武进2007065地块酒店项目沟通取证",
    }


def test_compiler_resolves_unique_case_and_preserves_user_segment_as_human_progress():
    case_id = str(uuid4())
    candidate = _candidate(
        "record_case_progress_candidate",
        {
            "entity_id": "case-1",
            "entity_type": "case_ref",
            "value": "恒大案件",
            "confidence": 0.96,
            "attributes": {"stage": "execution"},
        },
        "恒大案件今天和法院沟通了执行进展，下周重新查控",
    )
    cases = (
        CaseRecord(
            case_id,
            "tenant-test",
            "（2026）苏01执1号",
            "恒大执行案",
            ("恒大公司",),
            confirmed_aliases=("恒大案件",),
        ),
    )

    result = Phase2BusinessCommandCompiler().compile(
        candidate,
        _context(allowed_case_ids=(case_id,)),
        cases=cases,
    )

    assert result.block is None
    assert isinstance(result.command, CreateCaseProgress)
    assert result.command.case_id == case_id
    assert result.command.progress_type == "execution"
    assert result.command.summary == "恒大案件今天和法院沟通了执行进展，下周重新查控"


def test_compiler_uses_valid_shadow_admission_case_instead_of_resolving_model_alias_again():
    trusted_case_id = str(uuid4())
    source_text = "BGGL-2512-0006，今天与分公司核对了案件材料"
    candidate = _candidate(
        "record_case_progress_candidate",
        {
            "entity_id": "case-shadow-ticket",
            "entity_type": "case_ref",
            "value": "置地·汇金商务中心项目BC座户内精装修工程案",
            "confidence": 1.0,
            "attributes": {
                "statement_mode": "asserted",
                "normalized_fact": source_text,
            },
        },
        source_text,
    )
    candidate = _with_shadow_case_ticket(
        candidate,
        case_id=trusted_case_id,
        case_version=3,
    )
    cases = (
        CaseRecord(
            trusted_case_id,
            "tenant-test",
            "BGGL-2512-0006",
            "第一纠纷案",
            ("第一公司",),
            version=3,
        ),
    )
    context = replace(
        _context(allowed_case_ids=(trusted_case_id,)),
        execution_started_at=NOW,
    )

    result = Phase2BusinessCommandCompiler().compile(candidate, context, cases=cases)

    assert result.block is None
    assert isinstance(result.command, CreateCaseProgress)
    assert result.command.case_id == trusted_case_id
    assert result.command.summary == source_text


@pytest.mark.parametrize(
    "tamper",
    (
        "object_ref",
        "authority_attributes",
        "claims_hash",
        "expired",
    ),
)
def test_compiler_fails_closed_for_invalid_shadow_case_ticket(tamper):
    trusted_case_id = str(uuid4())
    source_text = "BGGL-2512-0006，今天与分公司核对了案件材料"
    candidate = _with_shadow_case_ticket(
        _candidate(
            "record_case_progress_candidate",
            {
                "entity_id": "case-shadow-ticket-tampered",
                "entity_type": "case_ref",
                "value": "置地·汇金商务中心项目BC座户内精装修工程案",
                "confidence": 1.0,
                "attributes": {
                    "statement_mode": "asserted",
                    "normalized_fact": source_text,
                },
            },
            source_text,
        ),
        case_id=trusted_case_id,
        case_version=3,
    )
    ticket = deepcopy(candidate.admission_ticket)
    if tamper == "object_ref":
        ticket["object_ref"]["stable_id"] = str(uuid4())
    elif tamper == "authority_attributes":
        ticket["authority_scope"]["attributes"]["normalized_fact"] = "被篡改"
    elif tamper == "claims_hash":
        ticket["fact_claims_sha256"] = "0" * 64
    else:
        ticket["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    candidate = replace(candidate, admission_ticket=ticket)
    cases = (
        CaseRecord(
            trusted_case_id,
            "tenant-test",
            "BGGL-2512-0006",
            "置地·汇金商务中心项目BC座户内精装修工程案",
            ("置地公司",),
            version=3,
        ),
    )
    context = replace(
        _context(allowed_case_ids=(trusted_case_id,)),
        execution_started_at=NOW,
    )

    result = Phase2BusinessCommandCompiler().compile(candidate, context, cases=cases)

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "admission_ticket_claims_mismatch"


def test_compiler_preserves_unknown_case_reference_for_safe_reply():
    candidate = _candidate(
        "record_case_progress_candidate",
        {
            "entity_id": "unknown-case",
            "entity_type": "case_ref",
            "value": "SSGL-2605-0024",
            "confidence": 1.0,
            "attributes": {"statement_mode": "asserted"},
        },
        "SSGL-2605-0024，评估暂时不诉，暂缓诉讼",
    )

    result = Phase2BusinessCommandCompiler().compile(
        candidate,
        _context(allowed_case_ids=()),
        cases=(),
    )

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "case_target_not_found"
    assert result.outcome_context == {"case_name": "SSGL-2605-0024"}


def test_compiler_preserves_trusted_followup_notification_binding():
    case_id = str(uuid4())
    notification_id = str(uuid4())
    candidate = _candidate(
        "record_case_progress_candidate",
        {
            "entity_id": "case-followup-1",
            "entity_type": "case_ref",
            "value": "恒大案件",
            "confidence": 0.96,
            "attributes": {
                "stage": "execution",
                "followup_notification_id": notification_id,
                "normalized_fact": "法院表示下周重新查控",
                "factual_progress": ["法院表示下周重新查控"],
                "completed_actions": [],
                "next_actions": [],
                "current_status": "等待重新查控",
                "action_time_scope": "unknown",
                "report_preference": "automatic",
                "evidence_spans": [[0, 13]],
            },
        },
        "法院表示下周重新查控",
    )
    cases = (CaseRecord(case_id, "tenant-test", "1", "恒大案件", ("恒大",)),)

    result = Phase2BusinessCommandCompiler().compile(
        candidate,
        _context(allowed_case_ids=(case_id,)),
        cases=cases,
    )

    assert isinstance(result.command, CreateCaseProgress)
    assert result.command.followup_notification_id == notification_id
    assert result.command.current_status == ""
    assert result.command.next_actions == ()
    extraction = result.outcome_context["case_fact_extraction"]
    assert extraction["current_status"] == ""
    assert extraction["normalized_fact"] == "法院表示下周重新查控"
    assert extraction["action_time_scope"] == "unknown"
    assert extraction["completed_actions"] == []


def test_compiler_does_not_fabricate_evidence_when_semantic_output_omits_it():
    case_id = str(uuid4())
    raw = "明天与对方律师沟通。"
    candidate = _candidate(
        "record_case_progress_candidate",
        {
            "entity_id": "case-followup-plan",
            "entity_type": "case_ref",
            "value": "恒大案件",
            "confidence": 0.99,
            "attributes": {
                "completed_actions": [],
                "next_actions": ["与对方律师沟通"],
                "action_time_scope": "future",
                "report_preference": "automatic",
            },
        },
        raw,
    )
    cases = (CaseRecord(case_id, "tenant-test", "1", "恒大案件", ("恒大",)),)

    result = Phase2BusinessCommandCompiler().compile(
        candidate,
        _context(allowed_case_ids=(case_id,)),
        cases=cases,
    )

    assert result.block is None
    assert result.outcome_context["case_fact_extraction"]["evidence_spans"] == []


def test_followup_snooze_compiles_to_typed_snooze_without_case_progress_write():
    case_id = str(uuid4())
    pending_id = str(uuid4())
    candidate = _candidate(
        "record_case_progress_candidate",
        {
            "entity_id": "case-followup-snooze",
            "entity_type": "case_ref",
            "value": "恒大案件",
            "confidence": 0.98,
            "attributes": {
                "followup_notification_id": pending_id,
                "requested_snooze": "next_week",
                "factual_progress": [],
                "completed_actions": [],
                "next_actions": [],
            },
        },
        "下周再问我",
    )
    cases = (CaseRecord(case_id, "tenant-test", "1", "恒大案件", ("恒大",)),)

    result = Phase2BusinessCommandCompiler().compile(
        candidate,
        _context(allowed_case_ids=(case_id,)),
        cases=cases,
    )

    assert isinstance(result.command, SnoozeCaseFollowup)
    assert result.command.pending_id == pending_id
    assert result.command.case_id == case_id
    assert result.command.snoozed_until > NOW


def test_compiler_requires_clarification_instead_of_selecting_last_case():
    first_id = str(uuid4())
    second_id = str(uuid4())
    candidate = _candidate(
        "record_case_progress_candidate",
        {
            "entity_id": "case-1",
            "entity_type": "case_ref",
            "value": "恒大案件",
            "confidence": 0.96,
            "attributes": {"stage": "execution"},
        },
        "恒大案件今天有新进展",
    )
    cases = (
        CaseRecord(first_id, "tenant-test", "1", "恒大执行案一", ("恒大公司",)),
        CaseRecord(second_id, "tenant-test", "2", "恒大执行案二", ("恒大公司",)),
    )

    result = Phase2BusinessCommandCompiler().compile(
        candidate,
        _context(allowed_case_ids=(first_id, second_id)),
        cases=cases,
    )

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "case_target_needs_clarification"
    assert set(result.block.detail.split(",")) == {first_id, second_id}
    selection = result.block.metadata["selection"]
    assert selection["domain"] == "case_progress"
    assert selection["operation"] == "create"
    assert {item["stable_id"] for item in selection["candidates"]} == {first_id, second_id}
    assert {item["label"] for item in selection["candidates"]} == {"恒大执行案一", "恒大执行案二"}


@pytest.mark.parametrize(
    "source_text",
    (
        "只是举例：恒大案件今天开庭了",
        "比如恒大案件今天开庭了",
        "恒大案件今天开庭了，但不要记录",
        "假设恒大案件今天开庭了",
    ),
)
def test_case_progress_compiler_blocks_explicit_examples_hypotheticals_and_no_write_text(source_text):
    case_id = str(uuid4())
    candidate = _candidate(
        "record_case_progress_candidate",
        {
            "entity_id": "case-non-assertive",
            "entity_type": "case_ref",
            "value": "恒大案件",
            "confidence": 0.99,
            "attributes": {"stage": "hearing"},
        },
        source_text,
    )
    cases = (CaseRecord(case_id, "tenant-test", "1", "恒大案件", ("恒大",)),)

    result = Phase2BusinessCommandCompiler().compile(
        candidate,
        _context(allowed_case_ids=(case_id,)),
        cases=cases,
    )

    assert result.command is None
    assert result.block is not None
    assert result.block.reason_code == "case_progress_not_asserted"


class _CaseRepository:
    def __init__(self, cases):
        self.cases = cases

    async def list_visible(self, context):
        return self.cases


class _AsyncExecutor:
    def __init__(self, cases):
        self.delegate = InMemoryBusinessExecutor(cases=cases)

    async def execute(self, command, context):
        return self.delegate.execute(command, context)


@pytest.mark.asyncio
async def test_multi_domain_composition_executes_valid_travel_while_blocking_ambiguous_case():
    first_id = str(uuid4())
    second_id = str(uuid4())
    cases = (
        CaseRecord(first_id, "tenant-test", "1", "恒大执行案一", ("恒大公司",)),
        CaseRecord(second_id, "tenant-test", "2", "恒大执行案二", ("恒大公司",)),
    )
    travel = _candidate(
        "record_travel_candidate",
        {
            "entity_id": "travel-1",
            "entity_type": "travel_event",
            "value": "明天去南京出差",
            "confidence": 0.99,
            "attributes": {"destination": "南京", "date_hint": "tomorrow", "purpose": "出差"},
        },
        "明天去南京出差",
    )
    case_progress = _candidate(
        "record_case_progress_candidate",
        {
            "entity_id": "case-1",
            "entity_type": "case_ref",
            "value": "恒大案件",
            "confidence": 0.99,
            "attributes": {"stage": "execution"},
        },
        "恒大案件今天有新进展",
    )
    context = _context(allowed_case_ids=(first_id, second_id))
    executor = _AsyncExecutor(cases)
    composer = Phase2BusinessComposer(
        case_repository=_CaseRepository(cases),  # type: ignore[arg-type]
        executor=executor,
    )

    result = await composer.execute((travel, case_progress), context)

    assert result.source_message_id == "message-1"
    assert result.executed_count == 1
    assert result.blocked_count == 1
    assert result.actions[0].receipt is not None
    assert result.actions[0].receipt.source_message_id == "message-1"
    assert result.actions[1].block is not None
    assert result.actions[1].block.reason_code == "case_target_needs_clarification"
    pending = SelectionPendingFactory().from_block(
        result.actions[1].block,
        tenant_id="tenant-test",
        user_id="user-1",
        conversation_id="conversation-1",
        source_turn_id="message-1",
        expected_conversation_state_version=1,
        now=NOW,
        expires_in_seconds=600,
    )
    assert pending is not None
    selected = next(item for item in pending.candidates if item.stable_id == second_id)
    continuation = bind_selected_business_command(pending, selected)
    assert continuation.payload["entities"][0]["value"] == second_id
    assert continuation.admission_required is True
    assert continuation.admission_ticket == {}
    compiled = Phase2BusinessCommandCompiler().compile(
        continuation, context, cases=cases
    )
    assert compiled.command is None
    assert compiled.block is not None
    assert compiled.block.reason_code == "missing_admission_ticket"


class _PartyRepository:
    async def resolve(self, query, *, scope):
        assert query == "南京华东建设有限公司"
        assert scope.tenant_id == "tenant-test"
        return PartyResolution("resolved", "11111111-1111-5111-8111-111111111111", "exact_canonical_name")


class _PartyQueryExecutor:
    def __init__(self):
        self.commands = []

    async def execute(self, command, context):
        self.commands.append(command)
        assert isinstance(command, QueryPartyCases)
        return BusinessReceipt(
            receipt_id="receipt-party-query",
            command_id=command.command_id,
            command_type=command.command_type,
            tenant_id=context.tenant_id,
            actor_user_id=context.actor_user_id,
            source_message_id=context.source_message_id,
            idempotency_key="party-query-key",
            status="executed",
            resource_type="party_case_query",
            resource_id=command.party_id,
            before={},
            after={
                "party": {"canonical_name": "南京华东建设有限公司"},
                "match_basis": command.match_basis,
                "case_count": 1,
                "status_counts": {"open": 1},
                "cases": [
                    {
                        "case_number": "（2026）苏01民初101号",
                        "case_name": "华东建设合同纠纷案",
                        "role_type": command.role_type,
                        "status": "open",
                    }
                ],
            },
            error_code=None,
            failed_stage=None,
            actual_write=False,
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_party_case_question_resolves_exact_party_filters_role_and_returns_typed_receipt_answer():
    case_id = str(uuid4())
    candidate = _candidate(
        "query_case_risk",
        {
            "entity_id": "query-1",
            "entity_type": "case_query",
            "value": "南京华东建设有限公司作为被告有哪些案件？",
            "confidence": 0.99,
            "attributes": {
                "matter_hint": "南京华东建设有限公司",
                "question": "作为被告有哪些案件？",
            },
        },
        "南京华东建设有限公司作为被告有哪些案件？",
    )
    executor = _PartyQueryExecutor()
    composer = Phase2BusinessComposer(
        case_repository=_CaseRepository(()),  # type: ignore[arg-type]
        party_repository=_PartyRepository(),  # type: ignore[arg-type]
        executor=executor,
    )

    result = await composer.execute(
        (candidate,),
        _context(allowed_case_ids=(case_id,)),
    )

    assert result.executed_count == 1
    assert isinstance(executor.commands[0], QueryPartyCases)
    assert executor.commands[0].role_type == "defendant"
    assert executor.commands[0].match_basis == "exact_canonical_name"
    reply = business_composition_reply_text(result)
    assert "南京华东建设有限公司" in reply
    assert "匹配依据：exact_canonical_name" in reply
    assert "（2026）苏01民初101号" in reply
    assert "defendant/open" in reply


class _ProgressRepository:
    def __init__(self, resolution):
        self.resolution = resolution
        self.calls = []

    async def resolve_write_target(self, context, **kwargs):
        self.calls.append(kwargs)
        return self.resolution


class _RecordingExecutor:
    def __init__(self):
        self.commands = []

    async def execute(self, command, context):
        self.commands.append(command)
        return BusinessReceipt(
            receipt_id=f"receipt-{command.command_type}",
            command_id=command.command_id,
            command_type=command.command_type,
            tenant_id=context.tenant_id,
            actor_user_id=context.actor_user_id,
            source_message_id=context.source_message_id,
            idempotency_key=f"key-{command.command_type}",
            status="executed",
            resource_type="case_progress",
            resource_id=getattr(command, "progress_id", getattr(command, "case_id", "")),
            before={},
            after={},
            error_code=None,
            failed_stage=None,
            actual_write=not isinstance(command, QueryCaseProgress),
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_unique_recent_progress_compiles_update_and_delete_with_current_version():
    case_id = str(uuid4())
    progress_id = str(uuid4())
    cases = (CaseRecord(case_id, "tenant-test", "1", "华东建设执行案", ("华东建设",)),)
    repository = _ProgressRepository(
        CaseProgressTargetResolution("resolved", progress_id, case_id, 4)
    )
    executor = _RecordingExecutor()
    composer = Phase2BusinessComposer(
        case_repository=_CaseRepository(cases),  # type: ignore[arg-type]
        progress_repository=repository,  # type: ignore[arg-type]
        executor=executor,
    )
    update = _candidate(
        "update_case_progress_candidate",
        {
            "entity_id": "progress-ref-1",
            "entity_type": "case_progress_ref",
            "value": "刚才的案件进展",
            "confidence": 0.99,
            "attributes": {
                "case_hint": "华东建设执行案",
                "replacement_summary": "法院预计本周五反馈",
            },
        },
        "把刚才的进展改成法院预计本周五反馈",
    )
    delete = _candidate(
        "delete_case_progress_candidate",
        {
            "entity_id": "progress-ref-2",
            "entity_type": "case_progress_ref",
            "value": "刚才的案件进展",
            "confidence": 0.99,
            "attributes": {"progress_id": progress_id, "delete_reason": "用户误记"},
        },
        "删除我刚才误记的案件进展",
    )

    result = await composer.execute(
        (update, delete),
        _context(allowed_case_ids=(case_id,)),
    )

    assert result.executed_count == 2
    assert isinstance(executor.commands[0], UpdateCaseProgress)
    assert executor.commands[0].progress_id == progress_id
    assert executor.commands[0].expected_version == 4
    assert executor.commands[0].summary == "法院预计本周五反馈"
    assert isinstance(executor.commands[1], DeleteCaseProgress)
    assert executor.commands[1].expected_version == 4
    assert executor.commands[1].reason == "用户误记"
    assert result.actions[0].outcome_context["case_name"] == cases[0].case_name
    assert result.actions[1].outcome_context["case_name"] == cases[0].case_name


@pytest.mark.asyncio
async def test_ambiguous_recent_progress_blocks_update_instead_of_selecting_latest():
    case_id = str(uuid4())
    candidate_ids = (str(uuid4()), str(uuid4()))
    cases = (CaseRecord(case_id, "tenant-test", "1", "华东建设执行案", ("华东建设",)),)
    repository = _ProgressRepository(
        CaseProgressTargetResolution(
            "needs_clarification",
            candidate_progress_ids=candidate_ids,
        )
    )
    executor = _RecordingExecutor()
    composer = Phase2BusinessComposer(
        case_repository=_CaseRepository(cases),  # type: ignore[arg-type]
        progress_repository=repository,  # type: ignore[arg-type]
        executor=executor,
    )
    update = _candidate(
        "update_case_progress_candidate",
        {
            "entity_id": "progress-ref-1",
            "entity_type": "case_progress_ref",
            "value": "刚才的案件进展",
            "confidence": 0.99,
            "attributes": {"replacement_summary": "法院预计本周五反馈"},
        },
        "把刚才的进展改成法院预计本周五反馈",
    )

    result = await composer.execute((update,), _context(allowed_case_ids=(case_id,)))

    assert result.executed_count == 0
    assert result.blocked_count == 1
    assert result.actions[0].block is not None
    assert result.actions[0].block.reason_code == "case_progress_target_needs_clarification"
    assert set(result.actions[0].block.detail.split(",")) == set(candidate_ids)
    assert executor.commands == []


@pytest.mark.asyncio
async def test_case_progress_query_resolves_case_and_compiles_read_only_typed_command():
    case_id = str(uuid4())
    cases = (CaseRecord(case_id, "tenant-test", "1", "华东建设执行案", ("华东建设",)),)
    executor = _RecordingExecutor()
    composer = Phase2BusinessComposer(
        case_repository=_CaseRepository(cases),  # type: ignore[arg-type]
        executor=executor,
    )
    query = _candidate(
        "query_case_progress_candidate",
        {
            "entity_id": "progress-query-1",
            "entity_type": "case_progress_ref",
            "value": "华东建设案最近进展",
            "confidence": 0.99,
            "attributes": {"case_hint": "华东建设执行案"},
        },
        "查一下华东建设案最近的进展",
    )

    result = await composer.execute((query,), _context(allowed_case_ids=(case_id,)))

    assert result.executed_count == 1
    assert isinstance(executor.commands[0], QueryCaseProgress)
    assert executor.commands[0].case_id == case_id
    assert result.actions[0].receipt is not None
    assert result.actions[0].receipt.actual_write is False
    assert result.actions[0].outcome_context["case_name"] == cases[0].case_name


@pytest.mark.parametrize(
    ("semantic_response", "expected"),
    (("需要", "accept"), ("不需要", "decline"), ("稍后确认", "later"), ("行程变了", "changed"), ("取消出差", "cancel")),
)
def test_travel_collaboration_reply_compiles_only_with_explicit_candidate_binding(
    semantic_response,
    expected,
):
    candidate_id = str(uuid4())
    semantic = _candidate(
        "respond_travel_collaboration_candidate",
        {
            "entity_id": "collaboration-1",
            "entity_type": "travel_collaboration_ref",
            "value": semantic_response,
            "confidence": 1.0,
            "attributes": {"candidate_id": candidate_id, "response": semantic_response},
        },
        semantic_response,
    )

    result = Phase2BusinessCommandCompiler().compile(semantic, _context(), cases=())

    assert result.block is None
    assert isinstance(result.command, RespondTravelCollaboration)
    assert result.command.candidate_id == candidate_id
    assert result.command.response == expected
def test_compiler_resolves_policy_case_server_side_and_uses_current_policy_version():
    case = CaseRecord(
        case_id="case-1", tenant_id="tenant-test", case_number="(2026)苏01民初1号",
        case_name="南京工程款案", party_names=("南京公司",), version=5,
    )
    candidate = _candidate(
        "update_case_followup_policy_candidate",
        {
            "entity_id": "policy-1", "entity_type": "case_followup_policy",
            "value": "南京工程款案", "confidence": 0.98,
            "attributes": {
                "case_hint": "南京工程款案", "cadence_type": "weekly",
                "enabled": True,
            },
        },
        "这个案子一周问我一次",
    )

    result = Phase2BusinessCommandCompiler().compile(
        candidate, _context(allowed_case_ids=("case-1",)), cases=(case,),
        followup_policies=(CaseFollowupPolicyRecord("case-1", "user-1", 4),),
    )

    assert result.block is None
    assert isinstance(result.command, UpdateCaseFollowupPolicy)
    assert result.command.case_id == "case-1"
    assert result.command.expected_version == 4
    assert result.command.cadence_type == "weekly"


def test_compiler_does_not_guess_between_ambiguous_case_policy_targets():
    cases = (
        CaseRecord("case-1", "tenant-test", "A-1", "南京合同案一", ("南京公司",)),
        CaseRecord("case-2", "tenant-test", "A-2", "南京合同案二", ("南京公司",)),
    )
    candidate = _candidate(
        "update_case_followup_policy_candidate",
        {
            "entity_id": "policy-1", "entity_type": "case_followup_policy",
            "value": "南京合同案", "confidence": 0.98,
            "attributes": {"case_hint": "南京合同案", "cadence_type": "daily"},
        },
        "南京合同案每天问我一次",
    )

    result = Phase2BusinessCommandCompiler().compile(
        candidate, _context(allowed_case_ids=("case-1", "case-2")), cases=cases,
    )

    assert result.command is None
    assert result.block.reason_code == "case_target_needs_clarification"
    assert len(result.block.metadata["selection"]["candidates"]) == 2


def test_compiler_turns_manual_followup_request_into_typed_command():
    case = CaseRecord(
        "case-1", "tenant-test", "A-1", "南京工程款案", ("南京公司",)
    )
    candidate = _candidate(
        "trigger_case_followup_now_candidate",
        {
            "entity_id": "policy-1", "entity_type": "case_followup_policy",
            "value": "南京工程款案", "confidence": 0.99,
            "attributes": {"case_hint": "南京工程款案"},
        },
        "现在问我一次",
    )

    result = Phase2BusinessCommandCompiler().compile(
        candidate, _context(allowed_case_ids=("case-1",)), cases=(case,)
    )

    assert result.block is None
    assert isinstance(result.command, TriggerCaseFollowupNow)
    assert result.command.case_id == "case-1"
    assert result.command.assigned_user_id == "user-1"
