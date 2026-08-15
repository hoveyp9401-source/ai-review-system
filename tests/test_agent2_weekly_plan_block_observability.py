import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from app.agent2.tool_calling import canary_service
from app.agent2.tool_calling.canary_service import (
    CanaryIngressOutcome,
    build_canary_persisted_response_payload,
    build_canary_response_payload,
    canary_provider_response_payload,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.deepseek_adapter import (
    _canary_tool_result_messages,
)
from app.agent2.tool_calling.production_runtime import ProductionRuntimeSession
from app.agent2.tool_calling.validation import (
    NativeToolCall,
    ShadowCallBinder,
    UnavailableDateResolver,
)
from app.agent2.weekly_plan_context import (
    TrustedWeeklyPlanContext,
    TrustedWeeklyPlanDay,
)

NOW = datetime(2026, 8, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
USER_ID = UUID("10000000-0000-4000-8000-000000000001")
PLAN_ID = UUID("30000000-0000-4000-8000-000000000001")
REPORT_ID = UUID("20000000-0000-4000-8000-000000000001")
TARGET_WEEK = date(2026, 8, 17)
SENSITIVE_MATTER = "日常用印审核-不应进入观察记录"
SOURCE_MESSAGE = f"下周每天都做{SENSITIVE_MATTER}"
DAILY_SOURCE_MESSAGE = "review alpha contract and prepare beta note"
DAILY_MODEL_ADDED_CONTENT = f"{DAILY_SOURCE_MESSAGE} zzz"


class _NoTransactionSession:
    def __init__(self) -> None:
        self.begin_nested_calls = 0
        self.weekly_business_tables = {
            name: []
            for name in (
                "agent2_weekly_plan_batches",
                "agent2_weekly_plan_roster_members",
                "agent2_weekly_plans",
                "agent2_weekly_plan_days",
                "agent2_weekly_plan_items",
                "agent2_weekly_plan_suggestions",
                "agent2_weekly_plan_command_receipts",
                "agent2_weekly_plan_audit_events",
                "agent2_weekly_plan_monday_snapshots",
                "agent2_weekly_plan_reminder_outbox",
            )
        }

    async def begin_nested(self):
        self.begin_nested_calls += 1
        raise AssertionError("binder failure must happen before a transaction")


def _context() -> TrustedContext:
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=NOW,
        principal=TrustedPrincipal(
            tenant_id="tenant-private",
            user_id=USER_ID,
            conversation_id="conversation-private",
            source_message_id="message-private",
            timezone="Asia/Shanghai",
            display_name="测试姓名不应进入观察记录",
            conversation_kind="direct",
        ),
        weekly_plan=TrustedWeeklyPlanContext(
            plan_id=str(PLAN_ID),
            batch_id="40000000-0000-4000-8000-000000000001",
            tenant_id="tenant-private",
            owner_user_id=str(USER_ID),
            target_week_start=TARGET_WEEK,
            version=0,
            status="collecting",
            days=tuple(
                TrustedWeeklyPlanDay(
                    day_id=f"day-{offset}",
                    plan_date=TARGET_WEEK + timedelta(days=offset),
                    state="unfilled",
                )
                for offset in range(6)
            ),
        ),
        allowed_tool_names=frozenset(
            {"apply_next_weekly_plan", "query_next_weekly_plan"}
        ),
        gate_decisions={"apply_next_weekly_plan": True},
    )


def _blocked_call() -> NativeToolCall:
    return NativeToolCall(
        tool_call_id="weekly-blocked-1",
        tool_name="apply_next_weekly_plan",
        arguments={
            "plan_id": str(PLAN_ID),
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "operation-1",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": SENSITIVE_MATTER,
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": SOURCE_MESSAGE,
                    },
                }
            ],
        },
    )


def _daily_context() -> TrustedContext:
    return TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=NOW,
        principal=TrustedPrincipal(
            tenant_id="tenant-private",
            user_id=USER_ID,
            conversation_id="conversation-private",
            source_message_id="message-private",
            timezone="Asia/Shanghai",
            display_name="private daily display name",
            conversation_kind="direct",
        ),
        today_report=TrustedReportSnapshot(
            report_id=REPORT_ID,
            tenant_id="tenant-private",
            owner_user_id=USER_ID,
            report_date=NOW.date(),
            version=7,
            status="collecting",
        ),
        allowed_tool_names=frozenset({"add_daily_items"}),
        gate_decisions={"add_daily_items": True},
    )


def _blocked_daily_call() -> NativeToolCall:
    return NativeToolCall(
        tool_call_id="daily-blocked-1",
        tool_name="add_daily_items",
        arguments={
            "date_selection": "trusted_report",
            "report_id": str(REPORT_ID),
            "expected_version": 7,
            "items": [
                {
                    "field": "today_work",
                    "content": DAILY_MODEL_ADDED_CONTENT,
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": DAILY_MODEL_ADDED_CONTENT,
                    },
                },
                {
                    "field": "tomorrow_plan",
                    "content": "prepare beta note",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_quote": "prepare beta note",
                    },
                },
            ],
        },
    )


@pytest.mark.asyncio
async def test_weekly_binder_block_has_safe_structured_observation_before_transaction() -> None:
    context = _context()
    source = CurrentTurnSource((SOURCE_MESSAGE,), occurred_at=(NOW,))
    session = _NoTransactionSession()
    runtime = ProductionRuntimeSession(
        session=session,
        user=SimpleNamespace(id=USER_ID, active=True),
        settings=SimpleNamespace(),
        context=context,
        capability=SimpleNamespace(),
        source_channel="dingtalk_private",
        source_text_hash=source.sha256,
        current_turn_source=source,
        binder=ShadowCallBinder(
            context,
            UnavailableDateResolver(),
            report_read_port=None,
            execution_mode=ExecutionMode.CANARY_EXECUTE,
            current_turn_source=source,
        ),
        date_resolver=UnavailableDateResolver(),
    )

    result = await runtime.execute((_blocked_call(),))

    assert result.status == "blocked"
    assert result.error_code == "WEEKLY_PLAN_DAY_NOT_EXPLICIT"
    assert session.begin_nested_calls == 0
    assert all(
        rows == [] for rows in session.weekly_business_tables.values()
    )
    assert len(result.receipts) == 1
    observation = result.receipts[0].safe_user_facts[
        "pre_execution_block_observation"
    ]
    assert observation == {
        "schema_version": "agent2.pre_execution_block.observation.v1",
        "tool_name": "apply_next_weekly_plan",
        "arguments_sha256": hashlib.sha256(
            json.dumps(
                _blocked_call().arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
        "target_type": "weekly_plan",
        "target_plan_ref_sha256": hashlib.sha256(
            str(PLAN_ID).encode("utf-8")
        ).hexdigest(),
        "target_week_start": "2026-08-17",
        "target_version": 0,
        "operation_type_counts": {"add": 1},
        "operation_count": 1,
        "error_code": "WEEKLY_PLAN_DAY_NOT_EXPLICIT",
        "actual_write": False,
    }
    serialized = json.dumps(observation, ensure_ascii=False, sort_keys=True)
    for sensitive_value in (
        SOURCE_MESSAGE,
        SENSITIVE_MATTER,
        str(PLAN_ID),
        str(USER_ID),
        "tenant-private",
        "conversation-private",
        "测试姓名不应进入观察记录",
    ):
        assert sensitive_value not in serialized


@pytest.mark.asyncio
async def test_daily_binder_block_has_safe_counts_without_content_or_identity() -> None:
    context = _daily_context()
    source = CurrentTurnSource(
        (DAILY_SOURCE_MESSAGE,),
        occurred_at=(NOW,),
    )
    session = _NoTransactionSession()
    runtime = ProductionRuntimeSession(
        session=session,
        user=SimpleNamespace(id=USER_ID, active=True),
        settings=SimpleNamespace(),
        context=context,
        capability=SimpleNamespace(),
        source_channel="dingtalk_private",
        source_text_hash=source.sha256,
        current_turn_source=source,
        binder=ShadowCallBinder(
            context,
            UnavailableDateResolver(),
            report_read_port=None,
            execution_mode=ExecutionMode.CANARY_EXECUTE,
            current_turn_source=source,
        ),
        date_resolver=UnavailableDateResolver(),
    )
    call = _blocked_daily_call()

    result = await runtime.execute((call,))

    assert result.status == "blocked"
    assert result.error_code == "DAILY_ITEM_CONTENT_NOT_GROUNDED"
    assert session.begin_nested_calls == 0
    observation = result.receipts[0].safe_user_facts[
        "pre_execution_block_observation"
    ]
    assert {
        key: value
        for key, value in observation.items()
        if key != "retry_candidate"
    } == {
        "schema_version": "agent2.pre_execution_block.observation.v1",
        "tool_name": "add_daily_items",
        "arguments_sha256": hashlib.sha256(
            json.dumps(
                call.arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
        "target_type": "daily_report",
        "target_report_date": "2026-08-14",
        "target_version": 7,
        "field_item_counts": {
            "today_work": 1,
            "problems": 0,
            "tomorrow_plan": 1,
        },
        "item_count": 2,
        "error_code": "DAILY_ITEM_CONTENT_NOT_GROUNDED",
        "actual_write": False,
    }
    retry_candidate = observation["retry_candidate"]
    assert retry_candidate == {
        "schema_version": "agent2.daily_write_retry_candidate.v1",
        "candidate_id": retry_candidate["candidate_id"],
        "block_stage": "source_binding",
        "retry_class": "source_binding_recoverable",
        "source_bundle_sha256": source.sha256,
        "source_message_count": 1,
        "target_report_date": "2026-08-14",
        "target_was_absent": False,
        "target_version": 7,
        "target_state_sha256": retry_candidate["target_state_sha256"],
        "failed_local_date": "2026-08-14",
        "retry_chain_depth": 0,
        "retry_of_candidate_id": "",
    }
    for digest_field in ("candidate_id", "target_state_sha256"):
        digest = retry_candidate[digest_field]
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")
    serialized = json.dumps(observation, ensure_ascii=False, sort_keys=True)
    for sensitive_value in (
        DAILY_SOURCE_MESSAGE,
        DAILY_MODEL_ADDED_CONTENT,
        str(REPORT_ID),
        str(USER_ID),
        "tenant-private",
        "conversation-private",
        "private daily display name",
    ):
        assert sensitive_value not in serialized


@pytest.mark.asyncio
async def test_stale_trusted_report_block_does_not_create_a_retry_candidate() -> None:
    context = _daily_context()
    source = CurrentTurnSource(
        (DAILY_SOURCE_MESSAGE,),
        occurred_at=(NOW,),
    )
    session = _NoTransactionSession()
    runtime = ProductionRuntimeSession(
        session=session,
        user=SimpleNamespace(id=USER_ID, active=True),
        settings=SimpleNamespace(),
        context=context,
        capability=SimpleNamespace(),
        source_channel="dingtalk_private",
        source_text_hash=source.sha256,
        current_turn_source=source,
        binder=ShadowCallBinder(
            context,
            UnavailableDateResolver(),
            report_read_port=None,
            execution_mode=ExecutionMode.CANARY_EXECUTE,
            current_turn_source=source,
        ),
        date_resolver=UnavailableDateResolver(),
    )
    original = _blocked_daily_call()
    call = NativeToolCall(
        tool_call_id="daily-stale-source-block",
        tool_name=original.tool_name,
        arguments={**original.arguments, "expected_version": 6},
    )

    result = await runtime.execute((call,))

    assert result.status == "blocked"
    assert result.error_code == "DAILY_ITEM_CONTENT_NOT_GROUNDED"
    observation = result.receipts[0].safe_user_facts[
        "pre_execution_block_observation"
    ]
    assert "retry_candidate" not in observation
    assert session.begin_nested_calls == 0


@pytest.mark.asyncio
async def test_read_block_does_not_carry_internal_write_observation() -> None:
    context = _context()
    source = CurrentTurnSource(("查看另一个计划",), occurred_at=(NOW,))
    session = _NoTransactionSession()
    runtime = ProductionRuntimeSession(
        session=session,
        user=SimpleNamespace(id=USER_ID, active=True),
        settings=SimpleNamespace(),
        context=context,
        capability=SimpleNamespace(),
        source_channel="dingtalk_private",
        source_text_hash=source.sha256,
        current_turn_source=source,
        binder=ShadowCallBinder(
            context,
            UnavailableDateResolver(),
            report_read_port=None,
            execution_mode=ExecutionMode.CANARY_EXECUTE,
            current_turn_source=source,
        ),
        date_resolver=UnavailableDateResolver(),
    )
    call = NativeToolCall(
        tool_call_id="weekly-read-blocked-1",
        tool_name="query_next_weekly_plan",
        arguments={
            "plan_id": "50000000-0000-4000-8000-000000000001",
        },
    )

    result = await runtime.execute((call,))

    assert result.status == "blocked"
    assert result.receipts[0].error_code == "UNTRUSTED_WEEKLY_PLAN_ID"
    assert (
        "pre_execution_block_observation"
        not in result.receipts[0].safe_user_facts
    )
    assert session.begin_nested_calls == 0


@pytest.mark.asyncio
async def test_untrusted_plan_block_keeps_hash_and_code_without_claiming_a_week() -> None:
    context = _context()
    source = CurrentTurnSource(("下周一做材料整理",), occurred_at=(NOW,))
    session = _NoTransactionSession()
    runtime = ProductionRuntimeSession(
        session=session,
        user=SimpleNamespace(id=USER_ID, active=True),
        settings=SimpleNamespace(),
        context=context,
        capability=SimpleNamespace(),
        source_channel="dingtalk_private",
        source_text_hash=source.sha256,
        current_turn_source=source,
        binder=ShadowCallBinder(
            context,
            UnavailableDateResolver(),
            report_read_port=None,
            execution_mode=ExecutionMode.CANARY_EXECUTE,
            current_turn_source=source,
        ),
        date_resolver=UnavailableDateResolver(),
    )
    untrusted_plan_id = "50000000-0000-4000-8000-000000000001"
    call = NativeToolCall(
        tool_call_id="weekly-write-untrusted-plan",
        tool_name="apply_next_weekly_plan",
        arguments={
            "plan_id": untrusted_plan_id,
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "operation-1",
                    "operation": "add",
                    "plan_date": "2026-08-17",
                    "content": "材料整理",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": "下周一做材料整理",
                    },
                }
            ],
        },
    )

    result = await runtime.execute((call,))
    observations = canary_service._pre_execution_block_observations(
        result.receipts
    )

    assert result.status == "blocked"
    assert result.receipts[0].error_code == "UNTRUSTED_WEEKLY_PLAN_ID"
    assert observations == (
        {
            "schema_version": "agent2.pre_execution_block.observation.v1",
            "tool_name": "apply_next_weekly_plan",
            "arguments_sha256": hashlib.sha256(
                json.dumps(
                    call.arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
            "target_type": "weekly_plan",
            "target_plan_ref_sha256": hashlib.sha256(
                untrusted_plan_id.encode("utf-8")
            ).hexdigest(),
            "target_week_start": "",
            "target_version": 0,
            "operation_type_counts": {"add": 1},
            "operation_count": 1,
            "error_code": "UNTRUSTED_WEEKLY_PLAN_ID",
            "actual_write": False,
        },
    )
    assert untrusted_plan_id not in json.dumps(
        observations,
        ensure_ascii=False,
    )
    assert session.begin_nested_calls == 0


def test_daily_block_observation_is_whitelisted_and_never_exposed() -> None:
    call = _blocked_daily_call()
    safe_block = {
        "schema_version": "agent2.pre_execution_block.observation.v1",
        "tool_name": "add_daily_items",
        "arguments_sha256": hashlib.sha256(
            json.dumps(
                call.arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
        "target_type": "daily_report",
        "target_report_date": "2026-08-14",
        "target_version": 7,
        "field_item_counts": {
            "today_work": 1,
            "problems": 0,
            "tomorrow_plan": 1,
        },
        "item_count": 2,
        "error_code": "DAILY_ITEM_CONTENT_NOT_GROUNDED",
        "actual_write": False,
    }
    receipt = ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name="add_daily_items",
        changed=False,
        error_code="DAILY_ITEM_CONTENT_NOT_GROUNDED",
        safe_user_facts={
            "actual_write": False,
            "pre_execution_block_observation": {
                **safe_block,
                "raw_content": DAILY_MODEL_ADDED_CONTENT,
                "report_id": str(REPORT_ID),
                "user_id": str(USER_ID),
                "user_name": "private daily display name",
            },
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    observations = canary_service._pre_execution_block_observations(
        (receipt,)
    )

    assert observations == (safe_block,)
    outcome = CanaryIngressOutcome(
        owner="tool_call_core",
        reason="enabled",
        message="The write was blocked.",
        handled=True,
        actual_write=False,
        messages_enabled=True,
        tool_blocked_count=1,
        user_visible_result="blocked",
        reply_formed=True,
        pre_execution_block_observations=observations,
    )
    persisted = build_canary_persisted_response_payload(outcome)
    provider = build_canary_response_payload(outcome)
    assert persisted["_agent2_turn_observation_v1"][
        "pre_execution_blocks"
    ] == [safe_block]
    assert canary_provider_response_payload(persisted) == provider

    model_messages = _canary_tool_result_messages(
        (call,),
        (receipt,),
        write_batch_closed=True,
    )
    externally_visible = json.dumps(
        {"provider": provider, "model": model_messages},
        ensure_ascii=False,
        sort_keys=True,
    )
    for internal_or_sensitive_value in (
        "pre_execution_block_observation",
        "pre_execution_blocks",
        safe_block["arguments_sha256"],
        DAILY_SOURCE_MESSAGE,
        DAILY_MODEL_ADDED_CONTENT,
        str(REPORT_ID),
        str(USER_ID),
        "private daily display name",
    ):
        assert internal_or_sensitive_value not in externally_visible


def test_retry_continuation_is_whitelisted_and_never_exposed() -> None:
    safe_continuation = {
        "schema_version": "agent2.daily_write_retry_candidate.v1",
        "candidate_id": "a" * 64,
        "block_stage": "source_binding",
        "retry_class": "source_binding_recoverable",
        "source_bundle_sha256": "b" * 64,
        "source_message_count": 1,
        "target_report_date": "2026-08-14",
        "target_was_absent": False,
        "target_version": 7,
        "target_state_sha256": "c" * 64,
        "failed_local_date": "2026-08-14",
        "retry_chain_depth": 1,
        "retry_of_candidate_id": "a" * 64,
    }
    outcome = CanaryIngressOutcome(
        owner="blocked",
        reason="tool_call_canary_execution_failed",
        message="The write did not complete.",
        handled=True,
        actual_write=False,
        messages_enabled=True,
        model_result_status="failed",
        daily_write_retry_continuation={
            **safe_continuation,
            "raw_content": DAILY_SOURCE_MESSAGE,
            "report_id": str(REPORT_ID),
            "user_id": str(USER_ID),
        },
        user_visible_result="failed",
        reply_formed=True,
    )

    persisted = build_canary_persisted_response_payload(outcome)
    provider = canary_provider_response_payload(persisted)
    observation = persisted["_agent2_turn_observation_v1"]

    assert observation["daily_write_retry_continuation"] == (
        safe_continuation
    )
    externally_visible = json.dumps(provider, ensure_ascii=False)
    for internal_or_sensitive_value in (
        "daily_write_retry_continuation",
        DAILY_SOURCE_MESSAGE,
        str(REPORT_ID),
        str(USER_ID),
    ):
        assert internal_or_sensitive_value not in externally_visible


def test_block_observation_is_persisted_internally_but_never_sent_to_dingtalk() -> None:
    block = {
        "schema_version": "agent2.pre_execution_block.observation.v1",
        "tool_name": "apply_next_weekly_plan",
        "arguments_sha256": "a" * 64,
        "target_type": "weekly_plan",
        "target_plan_ref_sha256": "b" * 64,
        "target_week_start": "2026-08-17",
        "target_version": 0,
        "operation_type_counts": {"add": 1},
        "operation_count": 1,
        "error_code": "WEEKLY_PLAN_DAY_NOT_EXPLICIT",
        "actual_write": False,
    }
    outcome = CanaryIngressOutcome(
        owner="tool_call_core",
        reason="enabled",
        message="这次没有写入，请补充具体日期。",
        handled=True,
        actual_write=False,
        messages_enabled=True,
        tool_blocked_count=1,
        user_visible_result="blocked",
        reply_formed=True,
        pre_execution_block_observations=(block,),
    )

    persisted = build_canary_persisted_response_payload(outcome)
    provider = build_canary_response_payload(outcome)

    assert persisted["_agent2_turn_observation_v1"][
        "pre_execution_blocks"
    ] == [block]
    assert canary_provider_response_payload(persisted) == provider
    assert "pre_execution_blocks" not in json.dumps(
        provider,
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_ingress_persists_only_whitelisted_block_fields(
    monkeypatch,
    caplog,
) -> None:
    safe_block = {
        "schema_version": "agent2.pre_execution_block.observation.v1",
        "tool_name": "apply_next_weekly_plan",
        "arguments_sha256": "a" * 64,
        "target_type": "weekly_plan",
        "target_plan_ref_sha256": "b" * 64,
        "target_week_start": "2026-08-17",
        "target_version": 0,
        "operation_type_counts": {"add": 1},
        "operation_count": 1,
        "error_code": "WEEKLY_PLAN_DAY_NOT_EXPLICIT",
        "actual_write": False,
    }
    poisoned_block = {
        **safe_block,
        "raw_arguments": SOURCE_MESSAGE,
        "user_name": "测试姓名不应进入观察记录",
        "user_id": str(USER_ID),
        "plan_id": str(PLAN_ID),
    }
    receipt = ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name="apply_next_weekly_plan",
        changed=False,
        error_code="WEEKLY_PLAN_DAY_NOT_EXPLICIT",
        safe_user_facts={
            "actual_write": False,
            "pre_execution_block_observation": poisoned_block,
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )
    atomic_sibling = ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name="add_daily_items",
        changed=False,
        error_code="ATOMIC_GROUP_PREVALIDATION_FAILED",
        safe_user_facts={"actual_write": False},
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )
    result = SimpleNamespace(
        final_content="这次没有写入，请补充具体日期。",
        receipts=(atomic_sibling, receipt),
        runtime_results=(),
        model_turns=(),
        request_attempt_count=1,
        transport_retry_count=0,
    )

    async def fake_resolve(*_args, **_kwargs):
        return SimpleNamespace(
            decision=SimpleNamespace(owner="tool_call_core", reason="enabled"),
            control=SimpleNamespace(messages_enabled=True),
            binding=SimpleNamespace(tenant_id="tenant-private"),
            capability=object(),
        )

    class _ContextStore:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    class _Assembler:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def assemble(self, _request):
            return SimpleNamespace(
                allowed_tool_names=frozenset({"apply_next_weekly_plan"}),
            )

    class _Runtime:
        def open_session(self, **_kwargs):
            return object()

    class _Adapter:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run_canary_turn(self, **_kwargs):
            return result

    class _Savepoint:
        is_active = True

        async def commit(self) -> None:
            self.is_active = False

        async def rollback(self) -> None:
            self.is_active = False

    class _Session:
        async def begin_nested(self):
            return _Savepoint()

    monkeypatch.setattr(
        canary_service,
        "resolve_tool_call_canary_route",
        fake_resolve,
    )
    monkeypatch.setattr(canary_service, "ProductionContextStore", _ContextStore)
    monkeypatch.setattr(canary_service, "TrustedContextAssembler", _Assembler)
    monkeypatch.setattr(canary_service, "ProductionRuntime", _Runtime)
    monkeypatch.setattr(canary_service, "DeepSeekToolCallingAdapter", _Adapter)
    monkeypatch.setattr(
        canary_service,
        "_attach_performance_glossary",
        lambda context, **_kwargs: context,
    )
    monkeypatch.setattr(
        canary_service,
        "_should_apply_personal_salutation",
        lambda _receipts: False,
    )
    monkeypatch.setattr(
        canary_service,
        "_record_canary_metric_safely",
        lambda **_kwargs: None,
    )

    with caplog.at_level(logging.DEBUG):
        outcome = await canary_service.process_tool_call_canary_ingress(
            _Session(),
            user=SimpleNamespace(
                id=USER_ID,
                name="测试姓名不应进入观察记录",
                timezone="Asia/Shanghai",
            ),
            dingtalk_user_id="dingtalk-private-id",
            user_text=SOURCE_MESSAGE,
            source_channel="test",
            conversation_id="conversation-private",
            source_message_id="message-private",
            settings=SimpleNamespace(
                timezone="Asia/Shanghai",
                llm_base_url="https://example.invalid",
            ),
            llm_client=SimpleNamespace(native_http_client=object()),
            now=NOW,
            conversation_kind="direct",
            message_occurred_at=NOW,
        )
    persisted = build_canary_persisted_response_payload(outcome)
    observation = persisted["_agent2_turn_observation_v1"]

    assert outcome.actual_write is False
    assert outcome.tool_blocked_count == 2
    assert observation["pre_execution_blocks"] == [safe_block]
    serialized = json.dumps(observation, ensure_ascii=False, sort_keys=True)
    for sensitive_value in (
        SOURCE_MESSAGE,
        SENSITIVE_MATTER,
        str(PLAN_ID),
        str(USER_ID),
        "测试姓名不应进入观察记录",
        "dingtalk-private-id",
        "conversation-private",
    ):
        assert sensitive_value not in serialized
        assert sensitive_value not in "\n".join(
            record.getMessage() for record in caplog.records
        )


def test_internal_block_observation_is_not_returned_to_the_reply_model() -> None:
    call = _blocked_call()
    receipt = ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name=call.tool_name,
        changed=False,
        error_code="WEEKLY_PLAN_DAY_NOT_EXPLICIT",
        safe_user_facts={
            "actual_write": False,
            "error_code": "WEEKLY_PLAN_DAY_NOT_EXPLICIT",
            "execution_mode": "canary_execute",
            "pre_execution_block_observation": {
                "arguments_sha256": "a" * 64,
                "error_code": "WEEKLY_PLAN_DAY_NOT_EXPLICIT",
            },
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    messages = _canary_tool_result_messages(
        (call,),
        (receipt,),
        write_batch_closed=True,
    )
    model_payload = json.loads(messages[0]["content"])

    assert model_payload["safe_user_facts"] == {
        "actual_write": False,
        "operation_outcome": "not_executed",
    }
    assert "pre_execution_block_observation" not in messages[0]["content"]


def test_non_registry_operation_label_is_not_persisted_as_diagnostics() -> None:
    poisoned_block = {
        "schema_version": "agent2.pre_execution_block.observation.v1",
        "tool_name": "apply_next_weekly_plan",
        "arguments_sha256": "a" * 64,
        "target_type": "weekly_plan",
        "target_plan_ref_sha256": "b" * 64,
        "target_week_start": "2026-08-17",
        "target_version": 0,
        "operation_type_counts": {"confidentialmatter": 1},
        "operation_count": 1,
        "error_code": "WEEKLY_PLAN_DAY_NOT_EXPLICIT",
        "actual_write": False,
    }
    receipt = ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name="apply_next_weekly_plan",
        changed=False,
        error_code="WEEKLY_PLAN_DAY_NOT_EXPLICIT",
        safe_user_facts={
            "actual_write": False,
            "pre_execution_block_observation": poisoned_block,
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )

    assert canary_service._pre_execution_block_observations((receipt,)) == ()


def test_successful_turn_keeps_the_block_observation_list_empty() -> None:
    outcome = CanaryIngressOutcome(
        owner="tool_call_core",
        reason="enabled",
        message="已更新计划。",
        handled=True,
        actual_write=True,
        messages_enabled=True,
        tool_success_count=1,
        user_visible_result="success",
        reply_formed=True,
    )

    observation = build_canary_persisted_response_payload(outcome)[
        "_agent2_turn_observation_v1"
    ]

    assert observation["pre_execution_blocks"] == []
    assert observation["business_write_committed"] is True


def test_webhook_and_stream_both_persist_the_internal_turn_observation() -> None:
    project_root = Path(__file__).resolve().parents[1]
    webhook_source = (project_root / "app" / "api" / "webhook.py").read_text(
        encoding="utf-8"
    )
    stream_source = (project_root / "app" / "stream_runner.py").read_text(
        encoding="utf-8"
    )

    assert "build_canary_persisted_response_payload(" in webhook_source
    assert "response_payload=persisted_response_payload" in webhook_source
    assert "build_canary_persisted_response_payload(" in stream_source
    assert "response_payload=response_payload" in stream_source
