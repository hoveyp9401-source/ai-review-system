from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.daily_write_retry import (
    RECOVERABLE_DAILY_SOURCE_ERROR_CODES,
    continued_daily_retry_evidence,
    daily_retry_candidate_id,
)
from app.agent2.tool_calling.production_contracts import (
    ProductionExecutionCapability,
    ProductionRuntimeResult,
)
from app.agent2.tool_calling.production_daily_executor import (
    ProductionDailyExecutor,
    ProductionExecutionError,
    ProductionHandlerOutcome,
)
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.production_memory_evidence import (
    is_personal_memory_call,
    personal_memory_evidence_matches,
    personal_memory_safe_user_facts,
)
from app.agent2.tool_calling.production_memory_executor import (
    ProductionPersonalMemoryExecutor,
)
from app.agent2.tool_calling.production_performance_executor import (
    ProductionPerformanceExecutor,
)
from app.agent2.tool_calling.production_periodic_report_executor import (
    ProductionPeriodicReportExecutor,
)
from app.agent2.tool_calling.production_store import (
    ProductionContextStore,
    ProductionDateResolver,
    ToolCallCanaryReceipt,
    capture_production_state,
    load_tool_call_receipt_by_call,
    load_tool_call_receipt_by_operation,
    load_typed_receipts,
    report_state_hash,
)
from app.agent2.tool_calling.production_weekly_plan_executor import (
    ProductionWeeklyPlanExecutor,
)
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    runtime_registry_contract_digest,
)
from app.agent2.tool_calling.receipt_provenance import principal_scope_sha256
from app.agent2.tool_calling.reporting_date import default_daily_write_date
from app.agent2.tool_calling.runtime import conflicting_tool_call_ids
from app.agent2.tool_calling.validation import (
    BoundCall,
    NativeToolCall,
    ShadowCallBinder,
)

logger = logging.getLogger(__name__)


class ProductionCapabilityError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _PreparedCall:
    bound: BoundCall
    arguments_hash: str
    request_fingerprint: str
    operation_fingerprint: str


@dataclass(frozen=True)
class _PendingExecution:
    transaction: Any
    committed_result: ProductionRuntimeResult


class ProductionRuntime:
    """Canary-only Registry executor; it has no LLM or message sender."""

    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(self, *, date_resolver: ProductionDateResolver | None = None) -> None:
        self._date_resolver = date_resolver or ProductionDateResolver()

    def open_session(
        self,
        *,
        session: Any,
        user: Any,
        settings: object,
        context: TrustedContext,
        capability: ProductionExecutionCapability,
        source_channel: str,
        source_text_hash: str,
        current_turn_source: CurrentTurnSource,
    ) -> "ProductionRuntimeSession":
        if current_turn_source.sha256 != source_text_hash:
            raise ProductionCapabilityError(
                "CURRENT_TURN_SOURCE_HASH_MISMATCH"
            )
        _validate_capability(
            context=context,
            capability=capability,
            user=user,
            settings=settings,
            source_text_hash=source_text_hash,
        )
        read_port = ProductionContextStore(
            session,
            user=user,
            tenant_id=context.principal.tenant_id,
            settings=settings,
        )
        return ProductionRuntimeSession(
            session=session,
            user=user,
            settings=settings,
            context=context,
            capability=capability,
            source_channel=source_channel,
            source_text_hash=source_text_hash,
            current_turn_source=current_turn_source,
            binder=ShadowCallBinder(
                context,
                self._date_resolver,
                read_port,
                execution_mode=ExecutionMode.CANARY_EXECUTE,
                current_turn_source=current_turn_source,
            ),
            date_resolver=self._date_resolver,
        )


class ProductionRuntimeSession:
    mode = ExecutionMode.CANARY_EXECUTE

    def __init__(
        self,
        *,
        session: Any,
        user: Any,
        settings: object,
        context: TrustedContext,
        capability: ProductionExecutionCapability,
        source_channel: str,
        source_text_hash: str,
        current_turn_source: CurrentTurnSource,
        binder: ShadowCallBinder,
        date_resolver: ProductionDateResolver,
    ) -> None:
        self._session = session
        self._user = user
        self._settings = settings
        self._context = context
        self._capability = capability
        self._source_channel = source_channel
        self._source_text_hash = source_text_hash
        self._binder = binder
        self._current_turn_source = current_turn_source
        self._date_resolver = date_resolver
        self._pending_execution: _PendingExecution | None = None

    async def execute(
        self,
        tool_calls: tuple[NativeToolCall, ...],
        *,
        commit_to_outer_transaction: bool = True,
        defer_finalization: bool = False,
    ) -> ProductionRuntimeResult:
        if self._pending_execution is not None:
            return ProductionRuntimeResult(
                status="failed",
                error_code="PENDING_TRANSACTION_REQUIRES_FINALIZATION",
            )
        if defer_finalization and not commit_to_outer_transaction:
            return ProductionRuntimeResult(
                status="failed",
                error_code="INVALID_TRANSACTION_FINALIZATION_MODE",
            )
        if not isinstance(tool_calls, tuple) or not tool_calls or any(
            not isinstance(call, NativeToolCall) for call in tool_calls
        ):
            return _blocked("SEALED_NATIVE_TOOL_CALLS_REQUIRED")

        self._binder.begin_batch()
        prepared: list[_PreparedCall] = []
        staged: list[_PreparedCall | ToolReceipt] = []
        seen_call_ids: set[str] = set()
        seen_operations: set[str] = set()
        for call in tool_calls:
            if call.tool_call_id in seen_call_ids:
                return _blocked("DUPLICATE_TOOL_CALL")
            seen_call_ids.add(call.tool_call_id)
            definition = TOOL_REGISTRY.get(call.tool_name)
            if (
                definition is None
                or ExecutionMode.CANARY_EXECUTE not in definition.enabled_modes
            ):
                return _blocked("UNKNOWN_OR_DISABLED_TOOL")
            bound, failure = await self._binder.bind(call)
            if failure is not None:
                staged.append(
                    _canary_failure_receipt(
                        failure,
                        context=self._context,
                        call=call,
                        source_text_hash=self._source_text_hash,
                        current_turn_source=self._current_turn_source,
                    )
                )
                continue
            assert bound is not None
            prepared_call = _prepare_call(self._context, bound)
            if prepared_call.operation_fingerprint in seen_operations:
                return _blocked("DUPLICATE_TOOL_CALL")
            seen_operations.add(prepared_call.operation_fingerprint)
            prepared.append(prepared_call)
            staged.append(prepared_call)

        if any(isinstance(item, ToolReceipt) for item in staged):
            failure_receipts = tuple(
                item
                if isinstance(item, ToolReceipt)
                else _canary_block_receipt(
                    item.bound.call,
                    "ATOMIC_GROUP_PREVALIDATION_FAILED",
                )
                for item in staged
            )
            return ProductionRuntimeResult(
                status="blocked",
                receipts=failure_receipts,
                error_code=failure_receipts[0].error_code,
            )
        conflicts = conflicting_tool_call_ids(
            self._context,
            [item.bound for item in prepared]
        )
        if conflicts:
            return _blocked("TOOL_CALL_CONFLICT")

        handler_call_count = 0
        business_write_count = 0
        pending_write_count = 0
        memory_write_count = 0
        memory_audit_write_count = 0
        receipts: tuple[ToolReceipt, ...] = ()
        nested = await self._session.begin_nested()
        try:
            if not await self._control_is_open():
                await nested.rollback()
                return ProductionRuntimeResult(
                    status="blocked",
                    error_code="CANARY_SWITCH_CLOSED",
                    transaction_opened=True,
                    rolled_back=True,
                )
            if any(
                TOOL_REGISTRY[item.bound.call.tool_name].read_or_write
                == "write"
                for item in prepared
            ):
                await self._lock_turn()
            replay_rows = await self._load_replays(prepared)
            replay_count = sum(row is not None for row in replay_rows)
            if 0 < replay_count < len(prepared):
                raise ProductionExecutionError(
                    "IDEMPOTENCY_GROUP_PARTIAL_REPLAY"
                )
            if replay_count == len(prepared):
                receipts = tuple(
                    _receipt_from_row(row, replayed=True)
                    for row in replay_rows
                    if row is not None
                )
            else:
                executor = ProductionDailyExecutor(
                    session=self._session,
                    user=self._user,
                    context=self._context,
                    settings=self._settings,
                    bound_calls={
                        item.bound.call.tool_call_id: item.bound
                        for item in prepared
                    },
                    source_channel=self._source_channel,
                    source_text_hash=self._source_text_hash,
                    date_resolver=self._date_resolver,
                )
                memory_executor = ProductionPersonalMemoryExecutor(
                    session=self._session,
                    context=self._context,
                )
                performance_executor = (
                    ProductionPerformanceExecutor(
                        session=self._session,
                        user=self._user,
                        context=self._context,
                        settings=self._settings,
                    )
                )
                weekly_plan_executor = ProductionWeeklyPlanExecutor(
                    session=self._session,
                    user=self._user,
                    context=self._context,
                    settings=self._settings,
                    bound_calls={
                        item.bound.call.tool_call_id: item.bound
                        for item in prepared
                    },
                    current_turn_source=self._current_turn_source,
                )
                periodic_report_executor = ProductionPeriodicReportExecutor(
                    session=self._session,
                    user=self._user,
                    context=self._context,
                    bound_calls={
                        item.bound.call.tool_call_id: item.bound
                        for item in prepared
                    },
                    source_channel=self._source_channel,
                )
                generated: list[ToolReceipt] = []
                for item in prepared:
                    before = await self._state()
                    definition = TOOL_REGISTRY[item.bound.call.tool_name]
                    typed_arguments = definition.input_model.model_validate(
                        item.bound.arguments
                    )
                    handler_call_count += 1
                    outcome = await definition.production_handler(
                        ProductionHandlerRequest(
                            tool_call_id=item.bound.call.tool_call_id,
                            tool_name=item.bound.call.tool_name,
                            arguments=typed_arguments,
                            executor=executor,
                            memory_executor=memory_executor,
                            performance_executor=(
                                performance_executor
                            ),
                            weekly_plan_executor=weekly_plan_executor,
                            periodic_report_executor=(
                                periodic_report_executor
                            ),
                            pending_ttl_seconds=definition.pending_ttl_seconds,
                        )
                    )
                    if not isinstance(outcome, ProductionHandlerOutcome):
                        raise ProductionExecutionError(
                            "HANDLER_OUTCOME_INVALID"
                        )
                    await self._session.flush()
                    after = await self._state()
                    daily_changed = (
                        before.payload().get("daily_reports", [])
                        != after.payload().get("daily_reports", [])
                    )
                    weekly_changed = (
                        before.payload().get("weekly_plans", [])
                        != after.payload().get("weekly_plans", [])
                    )
                    periodic_changed = (
                        before.payload().get("periodic_reports", [])
                        != after.payload().get("periodic_reports", [])
                    )
                    pending_changed = (
                        before.payload().get("clear_pendings", [])
                        != after.payload().get("clear_pendings", [])
                    )
                    memory_changed = (
                        before.payload().get("personal_memories", [])
                        != after.payload().get("personal_memories", [])
                    )
                    memory_audit_changed = (
                        before.payload().get("personal_memory_audits", [])
                        != after.payload().get("personal_memory_audits", [])
                    )
                    business_write_count += int(
                        daily_changed or weekly_changed or periodic_changed
                    )
                    pending_write_count += int(pending_changed)
                    memory_write_count += int(memory_changed)
                    memory_audit_write_count += int(
                        memory_audit_changed
                    )
                    receipt = await self._persist_and_verify_receipt(
                        item=item,
                        outcome=outcome,
                        before=before,
                        after=after,
                        changed=(
                            daily_changed
                            or weekly_changed
                            or periodic_changed
                            or pending_changed
                            or memory_changed
                            or memory_audit_changed
                        ),
                    )
                    generated.append(receipt)
                receipts = tuple(generated)

            successful_ids = frozenset(
                call.tool_call_id
                for call, receipt in zip(tool_calls, receipts, strict=True)
                if receipt.status
                in {ReceiptStatus.SUCCESS, ReceiptStatus.NO_OP}
            )
            self._binder.promote_query_results(
                [item.bound for item in prepared],
                successful_ids,
            )
            committed_result = ProductionRuntimeResult(
                status="success",
                receipts=receipts,
                transaction_opened=True,
                committed_to_outer_transaction=True,
                rolled_back=False,
                handler_call_count=handler_call_count,
                business_write_count=business_write_count,
                pending_write_count=pending_write_count,
                memory_write_count=memory_write_count,
                memory_audit_write_count=memory_audit_write_count,
                receipt_write_count=(
                    len(receipts) if handler_call_count else 0
                ),
            )
            if defer_finalization:
                self._pending_execution = _PendingExecution(
                    transaction=nested,
                    committed_result=committed_result,
                )
                return replace(
                    committed_result,
                    transaction_pending=True,
                    committed_to_outer_transaction=False,
                    business_write_count=0,
                    pending_write_count=0,
                    memory_write_count=0,
                    memory_audit_write_count=0,
                    receipt_write_count=0,
                )
            if commit_to_outer_transaction:
                await nested.commit()
                return committed_result
            await nested.rollback()
            return replace(
                committed_result,
                committed_to_outer_transaction=False,
                rolled_back=True,
                business_write_count=0,
                pending_write_count=0,
                memory_write_count=0,
                memory_audit_write_count=0,
                receipt_write_count=0,
            )
        except Exception as exc:
            if nested.is_active:
                await nested.rollback()
            logger.warning(
                "Agent2 production runtime rolled back; error_type=%s",
                type(exc).__name__,
            )
            code = (
                exc.code
                if isinstance(exc, ProductionExecutionError)
                else "PRODUCTION_RUNTIME_FAILED"
            )
            return ProductionRuntimeResult(
                status="failed",
                error_code=code,
                transaction_opened=True,
                rolled_back=True,
                handler_call_count=handler_call_count,
            )

    async def commit_pending(self) -> ProductionRuntimeResult:
        pending = self._pending_execution
        if pending is None:
            raise ProductionExecutionError("PENDING_TRANSACTION_REQUIRED")
        try:
            await pending.transaction.commit()
        except Exception:
            if pending.transaction.is_active:
                await pending.transaction.rollback()
            raise
        finally:
            self._pending_execution = None
        return pending.committed_result

    async def rollback_pending(self) -> None:
        pending = self._pending_execution
        if pending is None:
            return
        try:
            if pending.transaction.is_active:
                await pending.transaction.rollback()
        finally:
            self._pending_execution = None

    async def _lock_turn(self) -> None:
        principal = self._context.principal
        key = (
            f"agent2-tool-call:{principal.tenant_id}:"
            f"{principal.user_id}:{principal.conversation_id}"
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )

    async def _control_is_open(self) -> bool:
        principal = self._context.principal
        control = await self._session.scalar(
            select(ToolCallCanaryControl)
            .where(
                ToolCallCanaryControl.control_key
                == self._capability.control_key,
                ToolCallCanaryControl.tenant_id == principal.tenant_id,
                ToolCallCanaryControl.user_id == str(principal.user_id),
            )
            .with_for_update()
        )
        return bool(
            control is not None
            and control.enabled
            and control.runtime == ExecutionMode.CANARY_EXECUTE.value
            and control.version == self._capability.control_version
            and control.messages_enabled
            == self._capability.messages_enabled
            and control.registry_digest
            == self._capability.registry_digest
            and control.prompt_sha256 == self._capability.prompt_sha256
            and control.model_name == self._capability.model_name
        )

    async def _load_replays(
        self,
        prepared: list[_PreparedCall],
    ) -> tuple[ToolCallCanaryReceipt | None, ...]:
        principal = self._context.principal
        rows: list[ToolCallCanaryReceipt | None] = []
        for item in prepared:
            by_call = await load_tool_call_receipt_by_call(
                self._session,
                tenant_id=principal.tenant_id,
                user_id=str(principal.user_id),
                conversation_id=principal.conversation_id,
                source_message_id=principal.source_message_id,
                tool_call_id=item.bound.call.tool_call_id,
            )
            if by_call is not None and (
                by_call.request_fingerprint != item.request_fingerprint
                or by_call.canonical_arguments_hash != item.arguments_hash
            ):
                raise ProductionExecutionError(
                    "IDEMPOTENCY_KEY_REUSE_MISMATCH"
                )
            by_operation = await load_tool_call_receipt_by_operation(
                self._session,
                tenant_id=principal.tenant_id,
                operation_fingerprint=item.operation_fingerprint,
            )
            if by_operation is not None and (
                by_operation.canonical_arguments_hash != item.arguments_hash
                or by_operation.tool_name != item.bound.call.tool_name
            ):
                raise ProductionExecutionError(
                    "IDEMPOTENCY_KEY_REUSE_MISMATCH"
                )
            rows.append(by_call or by_operation)
        return tuple(rows)

    async def _persist_and_verify_receipt(
        self,
        *,
        item: _PreparedCall,
        outcome: ProductionHandlerOutcome,
        before,
        after,
        changed: bool,
    ) -> ToolReceipt:
        call = item.bound.call
        principal = self._context.principal
        idempotency_key = outcome.idempotency_key or (
            f"agent2-tool-call-read-v1:{item.request_fingerprint}"
        )
        receipt_id = uuid5(
            NAMESPACE_URL,
            f"agent2-tool-call-receipt:{principal.tenant_id}:{idempotency_key}",
        )
        status = (
            ReceiptStatus.SUCCESS
            if changed
            else outcome.status_if_unchanged
        )
        before_hash = before.canonical_hash
        after_hash = after.canonical_hash
        safe_facts = (
            personal_memory_safe_user_facts(
                bound=item.bound,
                outcome=outcome,
                after_payload=after.payload(),
                changed=changed,
            )
            if is_personal_memory_call(item.bound)
            else dict(outcome.safe_user_facts or {})
        )
        safe_facts["actual_write"] = changed
        if outcome.after_report is not None:
            safe_facts["report_snapshot"] = _safe_report_snapshot(
                outcome.after_report
            )
        before_version = (
            outcome.before_version
            if outcome.before_version is not None
            else (
                outcome.before_report.version
                if outcome.before_report is not None
                else None
            )
        )
        after_version = (
            outcome.after_version
            if outcome.after_version is not None
            else (
                outcome.after_report.version
                if outcome.after_report is not None
                else None
            )
        )
        row = ToolCallCanaryReceipt(
            receipt_id=receipt_id,
            tenant_id=principal.tenant_id,
            user_id=str(principal.user_id),
            conversation_id=principal.conversation_id,
            source_message_id=principal.source_message_id,
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            idempotency_key=idempotency_key,
            canonical_arguments_hash=item.arguments_hash,
            request_fingerprint=item.request_fingerprint,
            operation_fingerprint=item.operation_fingerprint,
            status=status.value,
            changed=changed,
            target_type=outcome.target_type,
            target_id=outcome.target_id,
            before_version=before_version,
            after_version=after_version,
            affected_item_ids=list(outcome.affected_item_ids),
            safe_user_facts=safe_facts,
            before_state_hash=before_hash,
            after_state_hash=after_hash,
            typed_receipt_ids=list(outcome.typed_receipt_ids),
            error_code=outcome.error_code,
            execution_mode=ExecutionMode.CANARY_EXECUTE.value,
        )
        self._session.add(row)
        await self._session.flush()
        authoritative_state = await self._state()
        typed_rows = await load_typed_receipts(
            self._session,
            tenant_id=principal.tenant_id,
            receipt_ids=outcome.typed_receipt_ids,
        )
        authoritative_row = await self._session.scalar(
            select(ToolCallCanaryReceipt).where(
                ToolCallCanaryReceipt.receipt_id == receipt_id
            )
        )
        if (
            authoritative_state.canonical_hash != after_hash
            or authoritative_row is None
            or authoritative_row.after_state_hash != after_hash
            or authoritative_row.before_state_hash != before_hash
            or authoritative_row.status != status.value
            or authoritative_row.changed is not changed
            or len(typed_rows) != len(outcome.typed_receipt_ids)
            or any(
                str(typed.receipt_id) not in outcome.typed_receipt_ids
                for typed in typed_rows
            )
            or (
                changed
                and outcome.target_type == "daily_report"
                and report_state_hash(outcome.before_report)
                == report_state_hash(outcome.after_report)
            )
            or not personal_memory_evidence_matches(
                bound=item.bound,
                outcome=outcome,
                before_payload=before.payload(),
                after_payload=after.payload(),
                changed=changed,
                before_version=before_version,
                after_version=after_version,
                context=self._context,
            )
        ):
            raise ProductionExecutionError("RECEIPT_EVIDENCE_MISMATCH")
        return _receipt_from_row(authoritative_row)

    async def _state(self):
        principal = self._context.principal
        return await capture_production_state(
            self._session,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            conversation_id=principal.conversation_id,
            include_weekly_plan=(self._context.weekly_plan is not None),
            include_periodic_report=(
                self._context.current_weekly_report is not None
            ),
        )


def _validate_capability(
    *,
    context: TrustedContext,
    capability: ProductionExecutionCapability,
    user: Any,
    settings: object,
    source_text_hash: str,
) -> None:
    if type(context) is not TrustedContext:
        raise ProductionCapabilityError("TRUSTED_CONTEXT_REQUIRED")
    if type(capability) is not ProductionExecutionCapability:
        raise ProductionCapabilityError("SERVER_CAPABILITY_REQUIRED")
    principal = context.principal
    checks = (
        (
            context.namespace == CANARY_STATE_NAMESPACE,
            "CANARY_CONTEXT_NAMESPACE_REQUIRED",
        ),
        (
            capability.runtime == ExecutionMode.CANARY_EXECUTE.value,
            "CANARY_MODE_REQUIRED",
        ),
        (capability.enabled, "CANARY_SWITCH_CLOSED"),
        (
            capability.registry_digest
            == runtime_registry_contract_digest(settings),
            "REGISTRY_DIGEST_MISMATCH",
        ),
        (
            capability.tenant_id == principal.tenant_id
            and capability.user_id == str(principal.user_id)
            and capability.conversation_id == principal.conversation_id
            and capability.source_message_id == principal.source_message_id,
            "CAPABILITY_SCOPE_MISMATCH",
        ),
        (
            str(getattr(user, "id", "")) == str(principal.user_id)
            and bool(getattr(user, "active", False)),
            "AUTHENTICATED_USER_MISMATCH",
        ),
        (
            capability.expires_at > context.now,
            "CAPABILITY_EXPIRED",
        ),
        (
            len(source_text_hash) == 64,
            "SOURCE_TEXT_HASH_REQUIRED",
        ),
    )
    failed = next((code for passed, code in checks if not passed), None)
    if failed is not None:
        raise ProductionCapabilityError(failed)


def _prepare_call(
    context: TrustedContext,
    bound: BoundCall,
) -> _PreparedCall:
    arguments_hash = _sha256(bound.arguments)
    principal = context.principal
    is_date_correction = (
        bound.call.tool_name == "correct_daily_report_date"
    )
    relocation_report = bound.source_report or bound.report
    fingerprint_date_facts = dict(bound.date_facts)
    if is_date_correction:
        fingerprint_date_facts.pop(
            "receipt_bound_source_state_sha256",
            None,
        )
        fingerprint_date_facts.pop(
            "idempotent_date_correction_replay",
            None,
        )
    base = {
        "tenant_id": principal.tenant_id,
        "user_id": str(principal.user_id),
        "conversation_id": principal.conversation_id,
        "source_message_id": principal.source_message_id,
        "tool_name": bound.call.tool_name,
        "arguments": bound.arguments,
        "server_binding": {
            "report_id": (
                str(relocation_report.report_id)
                if is_date_correction and relocation_report is not None
                else (
                    str(bound.report.report_id)
                    if bound.report is not None
                    else None
                )
            ),
            "report_version": (
                None
                if is_date_correction
                else (
                    bound.report.version
                    if bound.report is not None
                    else None
                )
            ),
            "source_report_id": (
                str(relocation_report.report_id)
                if is_date_correction and relocation_report is not None
                else (
                    str(bound.source_report.report_id)
                    if bound.source_report is not None
                    else None
                )
            ),
            "source_report_version": (
                None
                if is_date_correction
                else (
                    bound.source_report.version
                    if bound.source_report is not None
                    else None
                )
            ),
            "date_facts": fingerprint_date_facts,
            "periodic_report_id": (
                str(bound.periodic_report.report_id)
                if bound.periodic_report is not None
                else None
            ),
            "periodic_report_version": (
                bound.periodic_report.version
                if bound.periodic_report is not None
                else None
            ),
            "weekly_plan_id": (
                bound.weekly_plan.plan_id
                if bound.weekly_plan is not None
                else None
            ),
            "weekly_plan_version": (
                bound.weekly_plan.version
                if bound.weekly_plan is not None
                else None
            ),
        },
    }
    return _PreparedCall(
        bound=bound,
        arguments_hash=arguments_hash,
        request_fingerprint=_sha256(
            {**base, "tool_call_id": bound.call.tool_call_id}
        ),
        operation_fingerprint=_sha256(base),
    )


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canary_failure_receipt(
    receipt: ToolReceipt,
    *,
    context: TrustedContext,
    call: NativeToolCall,
    source_text_hash: str,
    current_turn_source: CurrentTurnSource,
) -> ToolReceipt:
    safe_user_facts = {
        **receipt.safe_user_facts,
        "execution_mode": ExecutionMode.CANARY_EXECUTE.value,
        "actual_write": False,
    }
    if call.tool_name in {
        "add_daily_items",
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
    }:
        safe_user_facts["pre_execution_block_observation"] = (
            _pre_execution_block_observation(
                context=context,
                call=call,
                error_code=str(receipt.error_code or ""),
                source_text_hash=source_text_hash,
                current_turn_source=current_turn_source,
            )
        )
    return receipt.model_copy(
        update={
            "safe_user_facts": safe_user_facts,
            "execution_mode": ExecutionMode.CANARY_EXECUTE,
        }
    )


def _pre_execution_block_observation(
    *,
    context: TrustedContext,
    call: NativeToolCall,
    error_code: str,
    source_text_hash: str,
    current_turn_source: CurrentTurnSource,
) -> dict[str, Any]:
    if call.tool_name == "add_daily_items":
        item_rows = call.arguments.get("items")
        items = (
            tuple(item_rows)
            if isinstance(item_rows, (list, tuple))
            else ()
        )
        field_item_counts = {
            "today_work": 0,
            "problems": 0,
            "tomorrow_plan": 0,
        }
        for item in items:
            if not isinstance(item, Mapping):
                continue
            field_name = str(item.get("field") or "")
            if field_name in field_item_counts:
                field_item_counts[field_name] += 1

        trusted_report = None
        if call.arguments.get("date_selection") == "trusted_report":
            try:
                report_id = UUID(str(call.arguments.get("report_id") or ""))
            except (TypeError, ValueError):
                report_id = None
            if report_id is not None:
                trusted_report = context.report_by_id(report_id)
        retry_evidence = _daily_retry_candidate_evidence(
            context=context,
            call=call,
            error_code=error_code,
            source_text_hash=source_text_hash,
            current_turn_source=current_turn_source,
        )
        observation = {
            "schema_version": "agent2.pre_execution_block.observation.v1",
            "tool_name": call.tool_name,
            "arguments_sha256": _sha256(call.arguments),
            "target_type": "daily_report",
            "target_report_date": (
                trusted_report.report_date.isoformat()
                if trusted_report is not None
                else ""
            ),
            "target_version": (
                trusted_report.version
                if trusted_report is not None
                else None
            ),
            "field_item_counts": field_item_counts,
            "item_count": len(items),
            "error_code": error_code,
            "actual_write": False,
        }
        if retry_evidence is not None:
            observation["retry_candidate"] = retry_evidence
            observation["target_report_date"] = retry_evidence[
                "target_report_date"
            ]
            observation["target_version"] = retry_evidence[
                "target_version"
            ]
        return observation

    operations = call.arguments.get("operations")
    operation_rows = (
        tuple(operations)
        if isinstance(operations, (list, tuple))
        else ()
    )
    operation_type_counts: dict[str, int] = {}
    for operation in operation_rows:
        if not isinstance(operation, Mapping):
            continue
        operation_type = str(operation.get("operation") or "").strip()
        if not operation_type:
            continue
        operation_type_counts[operation_type] = (
            operation_type_counts.get(operation_type, 0) + 1
        )

    plan_ref = str(call.arguments.get("plan_id") or "").strip()
    trusted_plan = (
        context.weekly_plan_by_id(plan_ref) if plan_ref else None
    )
    expected_version = call.arguments.get("expected_version")
    target_version = (
        expected_version
        if isinstance(expected_version, int)
        and not isinstance(expected_version, bool)
        else None
    )
    return {
        "schema_version": "agent2.pre_execution_block.observation.v1",
        "tool_name": call.tool_name,
        "arguments_sha256": _sha256(call.arguments),
        "target_type": (
            "weekly_plan"
            if call.tool_name
            in {
                "apply_next_weekly_plan",
                "submit_next_weekly_plan",
            }
            else ""
        ),
        "target_plan_ref_sha256": (
            hashlib.sha256(plan_ref.encode("utf-8")).hexdigest()
            if plan_ref
            else ""
        ),
        "target_week_start": (
            trusted_plan.target_week_start.isoformat()
            if trusted_plan is not None
            else ""
        ),
        "target_version": target_version,
        "operation_type_counts": operation_type_counts,
        "operation_count": len(operation_rows),
        "error_code": error_code,
        "actual_write": False,
    }


def _daily_retry_candidate_evidence(
    *,
    context: TrustedContext,
    call: NativeToolCall,
    error_code: str,
    source_text_hash: str,
    current_turn_source: CurrentTurnSource,
) -> dict[str, Any] | None:
    if (
        error_code not in RECOVERABLE_DAILY_SOURCE_ERROR_CODES
        or context.principal.conversation_kind != "direct"
        or len(current_turn_source.messages) != 1
    ):
        return None

    active_retry = context.retryable_daily_write
    if (
        call.arguments.get("date_selection") == "trusted_failed_write"
        and active_retry is not None
        and call.arguments.get("retry_candidate_id")
        == active_retry.candidate_id
        and active_retry.retry_chain_depth < 3
    ):
        return continued_daily_retry_evidence(active_retry)

    selection = call.arguments.get("date_selection")
    local_date = context.now.astimezone(
        ZoneInfo(context.principal.timezone)
    ).date()
    if selection == "server_default":
        target_date = default_daily_write_date(
            now=context.now,
            timezone=context.principal.timezone,
        )
        trusted_report = context.report_by_date(target_date)
        if target_date != local_date and trusted_report is None:
            return None
    elif selection == "trusted_report":
        try:
            report_id = UUID(str(call.arguments.get("report_id") or ""))
        except (TypeError, ValueError):
            return None
        trusted_report = context.report_by_id(report_id)
        if trusted_report is None:
            return None
        expected_version = call.arguments.get("expected_version")
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version != trusted_report.version
        ):
            return None
        target_date = trusted_report.report_date
    else:
        return None

    target_state_sha256 = report_state_hash(trusted_report)
    target_report_date = target_date.isoformat()
    candidate_id = daily_retry_candidate_id(
        tenant_id=context.principal.tenant_id,
        user_id=str(context.principal.user_id),
        conversation_id=context.principal.conversation_id,
        origin_source_message_id=context.principal.source_message_id,
        source_bundle_sha256=source_text_hash,
        target_report_date=target_report_date,
        target_state_sha256=target_state_sha256,
    )
    return {
        "schema_version": "agent2.daily_write_retry_candidate.v1",
        "candidate_id": candidate_id,
        "block_stage": "source_binding",
        "retry_class": "source_binding_recoverable",
        "source_bundle_sha256": source_text_hash,
        "source_message_count": 1,
        "target_report_date": target_report_date,
        "target_was_absent": trusted_report is None,
        "target_version": (
            trusted_report.version if trusted_report is not None else None
        ),
        "target_state_sha256": target_state_sha256,
        "failed_local_date": local_date.isoformat(),
        "retry_chain_depth": 0,
        "retry_of_candidate_id": "",
    }


def _safe_report_snapshot(
    report: TrustedReportSnapshot,
) -> dict[str, object]:
    fields: dict[str, list[dict[str, str]]] = {
        "today_work": [],
        "problems": [],
        "tomorrow_plan": [],
    }
    for item in report.items:
        fields[item.field].append(
            {
                "item_id": item.item_id,
                "content": item.content,
            }
        )
    return {
        "report_id": str(report.report_id),
        "report_date": report.report_date.isoformat(),
        "version": report.version,
        "status": report.status,
        "report_state_sha256": report_state_hash(report),
        "fields": fields,
        "acknowledged_empty_fields": sorted(
            report.acknowledged_empty_fields
        ),
    }


def _canary_block_receipt(
    call: NativeToolCall,
    error_code: str,
) -> ToolReceipt:
    return ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name=call.tool_name,
        changed=False,
        error_code=error_code,
        safe_user_facts={
            "actual_write": False,
            "execution_mode": ExecutionMode.CANARY_EXECUTE.value,
            "error_code": error_code,
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
    )


def _receipt_from_row(
    row: ToolCallCanaryReceipt,
    *,
    replayed: bool = False,
) -> ToolReceipt:
    safe_facts = dict(row.safe_user_facts or {})
    if replayed:
        safe_facts["idempotent_replay"] = True
        safe_facts["actual_write"] = False
    return ToolReceipt(
        status=ReceiptStatus(row.status),
        tool_name=row.tool_name,
        changed=False if replayed else bool(row.changed),
        target_type=row.target_type,
        target_id=row.target_id,
        before_version=row.before_version,
        after_version=row.after_version,
        affected_item_ids=tuple(row.affected_item_ids or ()),
        error_code=row.error_code,
        safe_user_facts=safe_facts,
        server_evidence={
            "receipt_id": str(row.receipt_id),
            "before_state_hash": row.before_state_hash,
            "after_state_hash": row.after_state_hash,
            "typed_receipt_ids": list(row.typed_receipt_ids or ()),
            "idempotent_replay": replayed,
            "principal_scope_sha256": principal_scope_sha256(
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                conversation_id=row.conversation_id,
                source_message_id=row.source_message_id,
            ),
        },
        execution_mode=ExecutionMode.CANARY_EXECUTE,
        would_change=False,
        idempotency_key=row.idempotency_key,
    )


def _blocked(error_code: str) -> ProductionRuntimeResult:
    return ProductionRuntimeResult(
        status="blocked",
        error_code=error_code,
    )
