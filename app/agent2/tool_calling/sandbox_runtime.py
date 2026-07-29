from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ForgetPersonalMemoryArgs,
    QueryPersonalMemoryArgs,
    ReceiptStatus,
    RememberPersonalMemoryArgs,
    ToolReceipt,
)
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    ToolArgumentsValidationError,
    UnknownToolError,
    registry_contract_digest,
    validate_tool_arguments,
)
from app.agent2.tool_calling.sandbox_daily_executor import (
    SandboxDailyExecutor,
    SandboxDateResolverPort,
    SandboxExecutionError,
    SandboxHandlerOutcome,
    canonical_arguments_hash,
    operation_fingerprint,
    request_fingerprint,
)
from app.agent2.tool_calling.sandbox_handlers import SandboxHandlerRequest
from app.agent2.tool_calling.sandbox_memory_executor import (
    SandboxPersonalMemoryExecutor,
)
from app.agent2.tool_calling.sandbox_memory_evidence import (
    personal_memory_safe_user_facts,
    personal_memory_state_changed,
    personal_memory_version,
    validate_personal_memory_outcome,
)
from app.agent2.tool_calling.sandbox_contracts import (
    SandboxCapabilityAuthority,
    SandboxCapabilityError,
    SandboxExecutionCapability,
    SandboxExecutionContext,
    SandboxReceiptFacts,
    SandboxRuntimeResult,
    SandboxSnapshot,
)
from app.agent2.tool_calling.sandbox_store import (
    SandboxIsolationError,
    SandboxStore,
    SandboxExecutionSession,
    SandboxTransactionWork,
)
from app.agent2.tool_calling.validation import NativeToolCall


class SandboxRuntime:
    """Isolated Sandbox runtime with Registry handlers and no message dependency."""

    def __init__(
        self,
        *,
        store: SandboxStore,
        capability_authority: SandboxCapabilityAuthority,
        date_resolver: SandboxDateResolverPort | None = None,
    ) -> None:
        self._store = store
        self._capability_authority = capability_authority
        self._date_resolver = date_resolver

    async def execute(
        self,
        context: object,
        tool_calls: object,
        capability: object,
    ) -> SandboxRuntimeResult:
        if not isinstance(context, SandboxExecutionContext):
            return _blocked("SANDBOX_CONTEXT_OBJECT_REQUIRED")
        if not isinstance(capability, SandboxExecutionCapability):
            return _blocked("SANDBOX_CAPABILITY_OBJECT_REQUIRED")
        if not isinstance(tool_calls, tuple) or any(
            not isinstance(call, NativeToolCall) for call in tool_calls
        ):
            return _blocked("SEALED_NATIVE_TOOL_CALLS_REQUIRED")
        try:
            validated_arguments = tuple(
                validate_tool_arguments(call.tool_name, call.arguments)
                for call in tool_calls
            )
        except UnknownToolError:
            return _blocked("UNKNOWN_TOOL")
        except ToolArgumentsValidationError:
            return _blocked("INVALID_TOOL_ARGUMENTS")

        try:
            self._capability_authority.validate(
                capability,
                context=context,
                database_fingerprint=self._store.database_fingerprint,
                schema_name=capability.schema_name,
                registry_digest=registry_contract_digest(),
                now=context.now,
            )
        except SandboxCapabilityError as exc:
            return _blocked(exc.code)

        if capability.allowed_tool_names:
            return await self._execute_full_registry(
                context=context,
                calls=tool_calls,
                validated_arguments=validated_arguments,
                capability=capability,
            )

        async def no_business_work(transaction: SandboxTransactionWork) -> None:
            await transaction.stage_rollback_probe(capability.sandbox_run_id)

        try:
            outcome = await self._store.transaction_manager(capability).execute(
                atomic_group_id=f"foundation:{capability.sandbox_run_id}",
                work=no_business_work,
                commit=False,
            )
        except SandboxIsolationError as exc:
            return SandboxRuntimeResult(
                status="failed",
                error_code=exc.code,
                transaction_opened=exc.transaction_opened,
                rolled_back=exc.rolled_back,
            )
        if outcome.error_code is not None:
            return SandboxRuntimeResult(
                status="failed",
                error_code=outcome.error_code,
                transaction_opened=True,
                committed=outcome.committed,
                rolled_back=outcome.rolled_back,
                before_snapshot_hash=outcome.before.canonical_hash,
                attempted_snapshot_hash=outcome.attempted_after.canonical_hash,
                after_snapshot_hash=outcome.after.canonical_hash,
                rollback_probe_staged=(
                    outcome.attempted_after.canonical_hash
                    != outcome.before.canonical_hash
                ),
            )
        return SandboxRuntimeResult(
            status="foundation_ready",
            transaction_opened=True,
            committed=False,
            rolled_back=True,
            before_snapshot_hash=outcome.before.canonical_hash,
            attempted_snapshot_hash=outcome.attempted_after.canonical_hash,
            after_snapshot_hash=outcome.after.canonical_hash,
            rollback_probe_staged=True,
        )

    async def _execute_full_registry(
        self,
        *,
        context: SandboxExecutionContext,
        calls: tuple[NativeToolCall, ...],
        validated_arguments: tuple[dict[str, object], ...],
        capability: SandboxExecutionCapability,
    ) -> SandboxRuntimeResult:
        if self._date_resolver is None:
            return _blocked("SANDBOX_DATE_RESOLVER_REQUIRED")
        if not calls:
            return _blocked("SANDBOX_TOOL_CALL_REQUIRED")
        seen_call_ids: dict[str, str] = {}
        seen_operations: set[str] = set()
        for call, arguments in zip(calls, validated_arguments, strict=True):
            definition = TOOL_REGISTRY[call.tool_name]
            if ExecutionMode.SANDBOX_EXECUTE not in definition.enabled_modes:
                return _blocked("TOOL_MODE_NOT_ENABLED")
            if call.tool_name not in capability.allowed_tool_names:
                return _blocked("TOOL_NOT_ALLOWED_BY_CAPABILITY")
            if (
                definition.read_or_write == "write"
                and call.tool_name not in capability.write_gate_tool_names
            ):
                return _blocked("SANDBOX_WRITE_GATE_BLOCKED")
            call_signature = canonical_arguments_hash(
                {"tool_name": call.tool_name, "arguments": arguments}
            )
            prior = seen_call_ids.get(call.tool_call_id)
            if prior is not None and prior != call_signature:
                return _blocked("IDEMPOTENCY_KEY_REUSE_MISMATCH")
            seen_call_ids[call.tool_call_id] = call_signature
            operation_signature = call_signature
            if operation_signature in seen_operations:
                return _blocked("DUPLICATE_TOOL_CALL")
            seen_operations.add(operation_signature)

        handler_call_count = 0
        business_write_count = 0
        pending_write_count = 0
        memory_write_count = 0
        audit_write_count = 0

        async def execute_calls(session: SandboxExecutionSession) -> None:
            nonlocal handler_call_count
            nonlocal business_write_count
            nonlocal pending_write_count
            nonlocal memory_write_count
            nonlocal audit_write_count
            executor = SandboxDailyExecutor(
                session=session,
                context=context,
                date_resolver=self._date_resolver,
            )
            memory_executor = SandboxPersonalMemoryExecutor(
                session=session,
                context=context,
            )
            replay_receipts: list[SandboxReceiptFacts | None] = []
            for call, arguments in zip(calls, validated_arguments, strict=True):
                arguments_hash = canonical_arguments_hash(arguments)
                fingerprint = request_fingerprint(
                    context=context,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    arguments=arguments,
                )
                operation_id = operation_fingerprint(
                    context=context,
                    tool_name=call.tool_name,
                    arguments=arguments,
                )
                prior_by_call = await session.find_receipt_by_call_identity(
                    tenant_id=context.tenant_id,
                    user_id=str(context.user_id),
                    conversation_id=context.conversation_id,
                    source_message_id=context.source_message_id,
                    tool_call_id=call.tool_call_id,
                )
                if prior_by_call is not None and (
                    prior_by_call.tool_name != call.tool_name
                    or prior_by_call.canonical_arguments_hash != arguments_hash
                    or prior_by_call.request_fingerprint != fingerprint
                ):
                    raise SandboxExecutionError(
                        "IDEMPOTENCY_KEY_REUSE_MISMATCH"
                    )
                prior_by_operation = (
                    await session.find_receipt_by_operation_fingerprint(
                        operation_id
                    )
                )
                prior = prior_by_call or prior_by_operation
                if prior is not None and (
                    prior.canonical_arguments_hash != arguments_hash
                    or prior.operation_fingerprint != operation_id
                ):
                    raise SandboxExecutionError(
                        "IDEMPOTENCY_KEY_REUSE_MISMATCH"
                    )
                replay_receipts.append(prior)
            present_count = sum(
                receipt is not None for receipt in replay_receipts
            )
            if 0 < present_count < len(replay_receipts):
                raise SandboxExecutionError(
                    "IDEMPOTENCY_GROUP_PARTIAL_REPLAY"
                )
            if present_count == len(replay_receipts):
                current = await session.snapshot()
                for call, arguments, prior in zip(
                    calls,
                    validated_arguments,
                    replay_receipts,
                    strict=True,
                ):
                    assert prior is not None
                    await session.seal_receipt(
                        executor_before=current,
                        executor_after=current,
                        receipt=_replayed_receipt(
                            prior,
                            current,
                            context=context,
                            call=call,
                            arguments=arguments,
                        ),
                    )
                return

            for call, arguments in zip(calls, validated_arguments, strict=True):
                definition = TOOL_REGISTRY[call.tool_name]
                arguments_hash = canonical_arguments_hash(arguments)
                fingerprint = request_fingerprint(
                    context=context,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    arguments=arguments,
                )
                operation_id = operation_fingerprint(
                    context=context,
                    tool_name=call.tool_name,
                    arguments=arguments,
                )
                executor_before = await session.snapshot()
                typed_arguments = definition.input_model.model_validate(arguments)
                handler_call_count += 1
                outcome = await definition.sandbox_handler(
                    SandboxHandlerRequest(
                        tool_call_id=call.tool_call_id,
                        tool_name=call.tool_name,
                        arguments=typed_arguments,
                        executor=executor,
                        memory_executor=memory_executor,
                        pending_ttl_seconds=definition.pending_ttl_seconds,
                    )
                )
                if not isinstance(outcome, SandboxHandlerOutcome):
                    raise SandboxExecutionError("HANDLER_OUTCOME_INVALID")
                executor_after = await session.snapshot()
                _validate_outcome_binding(
                    outcome,
                    executor_before,
                    executor_after,
                    context=context,
                    arguments=typed_arguments,
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    read_only=definition.read_or_write == "read",
                )
                before_version = _target_version(
                    executor_before,
                    outcome,
                    context=context,
                    binding_snapshot=executor_after,
                )
                after_version = _target_version(
                    executor_after,
                    outcome,
                    context=context,
                )
                changed = (
                    executor_before.state_hash != executor_after.state_hash
                )
                affected_item_ids = _affected_item_ids(
                    executor_before,
                    executor_after,
                )
                if (
                    executor_before.table_rows("daily_reports")
                    != executor_after.table_rows("daily_reports")
                    or executor_before.table_rows("daily_items")
                    != executor_after.table_rows("daily_items")
                ):
                    business_write_count += 1
                if (
                    executor_before.table_rows("sandbox_pending")
                    != executor_after.table_rows("sandbox_pending")
                ):
                    pending_write_count += 1
                if (
                    executor_before.table_rows("personal_memory")
                    != executor_after.table_rows("personal_memory")
                ):
                    memory_write_count += 1
                if (
                    executor_before.table_rows("personal_memory_audit")
                    != executor_after.table_rows("personal_memory_audit")
                ):
                    audit_write_count += 1
                status = (
                    ReceiptStatus.SUCCESS
                    if changed
                    else outcome.status_if_unchanged
                )
                safe_user_facts = _safe_user_facts(
                    outcome,
                    executor_before,
                    executor_after,
                    context=context,
                    affected_item_ids=affected_item_ids,
                    actual_write=changed,
                )
                receipt = SandboxReceiptFacts(
                    receipt_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            (
                                "agent2-sandbox-receipt:"
                                f"{outcome.idempotency_key or fingerprint}"
                            ),
                        )
                    ),
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    tenant_id=context.tenant_id,
                    user_id=str(context.user_id),
                    conversation_id=context.conversation_id,
                    source_message_id=context.source_message_id,
                    status=status,
                    changed=changed,
                    target_type=outcome.target_type,
                    target_id=outcome.target_id,
                    before_version=before_version,
                    after_version=after_version,
                    affected_item_ids=affected_item_ids,
                    safe_user_facts=safe_user_facts,
                    idempotency_key=outcome.idempotency_key,
                    request_fingerprint=fingerprint,
                    operation_fingerprint=operation_id,
                    canonical_arguments_hash=arguments_hash,
                    before_state_hash=executor_before.state_hash,
                    after_state_hash=executor_after.state_hash,
                )
                await session.seal_receipt(
                    executor_before=executor_before,
                    executor_after=executor_after,
                    receipt=receipt,
                )

        try:
            outcome = await self._store.transaction_manager(
                capability
            ).execute_registry_batch(
                atomic_group_id=(
                    f"sandbox-turn:{capability.sandbox_run_id}:{context.turn_id}"
                ),
                work=execute_calls,
            )
        except SandboxIsolationError as exc:
            return SandboxRuntimeResult(
                status="failed",
                error_code=exc.code,
                transaction_opened=exc.transaction_opened,
                rolled_back=exc.rolled_back,
            )

        receipts = tuple(_tool_receipt(facts) for facts in outcome.receipts)
        if outcome.error_code is not None:
            return SandboxRuntimeResult(
                status="failed",
                error_code=outcome.error_code,
                transaction_opened=True,
                committed=outcome.committed,
                rolled_back=outcome.rolled_back,
                before_snapshot_hash=outcome.before.canonical_hash,
                attempted_snapshot_hash=outcome.attempted_after.canonical_hash,
                after_snapshot_hash=outcome.after.canonical_hash,
                receipts=receipts,
                actual_write=(
                    business_write_count > 0
                    or pending_write_count > 0
                    or memory_write_count > 0
                    or audit_write_count > 0
                )
                if outcome.committed
                else False,
                handler_call_count=handler_call_count,
                business_write_count=(
                    business_write_count if outcome.committed else 0
                ),
                pending_write_count=(
                    pending_write_count if outcome.committed else 0
                ),
                memory_write_count=(
                    memory_write_count if outcome.committed else 0
                ),
                audit_write_count=(
                    audit_write_count if outcome.committed else 0
                ),
            )
        return SandboxRuntimeResult(
            status="success",
            transaction_opened=True,
            committed=True,
            rolled_back=False,
            before_snapshot_hash=outcome.before.canonical_hash,
            attempted_snapshot_hash=outcome.attempted_after.canonical_hash,
            after_snapshot_hash=outcome.after.canonical_hash,
            receipts=receipts,
            actual_write=(
                business_write_count > 0
                or pending_write_count > 0
                or memory_write_count > 0
                or audit_write_count > 0
            ),
            handler_call_count=handler_call_count,
            business_write_count=business_write_count,
            pending_write_count=pending_write_count,
            memory_write_count=memory_write_count,
            audit_write_count=audit_write_count,
        )


def _blocked(error_code: str) -> SandboxRuntimeResult:
    return SandboxRuntimeResult(
        status="blocked",
        error_code=error_code,
    )


def _report_version(snapshot: SandboxSnapshot, report_id: str | None) -> int | None:
    if report_id is None:
        return None
    for report in snapshot.table_rows("daily_reports"):
        if report.get("report_id") == report_id:
            return int(report["version"])
    return 0


def _receipt_report_id(
    outcome: SandboxHandlerOutcome,
    snapshot: SandboxSnapshot,
) -> str | None:
    if outcome.target_type == "daily_report":
        return outcome.target_id
    pending = next(
        (
            row
            for row in snapshot.table_rows("sandbox_pending")
            if row.get("pending_id") == outcome.target_id
        ),
        None,
    )
    if pending is not None:
        return str(pending["report_id"])
    return None


def _target_version(
    snapshot: SandboxSnapshot,
    outcome: SandboxHandlerOutcome,
    *,
    context: SandboxExecutionContext,
    binding_snapshot: SandboxSnapshot | None = None,
) -> int | None:
    if outcome.target_type == "personal_memory":
        return personal_memory_version(
            snapshot,
            outcome.target_id,
            context=context,
        )
    report_id = _receipt_report_id(outcome, snapshot)
    if report_id is None and binding_snapshot is not None:
        report_id = _receipt_report_id(outcome, binding_snapshot)
    return _report_version(snapshot, report_id)


def _affected_item_ids(
    before: SandboxSnapshot,
    after: SandboxSnapshot,
) -> tuple[str, ...]:
    before_rows = {
        str(row["item_id"]): row for row in before.table_rows("daily_items")
    }
    after_rows = {
        str(row["item_id"]): row for row in after.table_rows("daily_items")
    }
    return tuple(
        sorted(
            item_id
            for item_id in before_rows.keys() | after_rows.keys()
            if before_rows.get(item_id) != after_rows.get(item_id)
        )
    )


def _safe_user_facts(
    outcome: SandboxHandlerOutcome,
    before: SandboxSnapshot,
    after: SandboxSnapshot,
    *,
    context: SandboxExecutionContext,
    affected_item_ids: tuple[str, ...],
    actual_write: bool,
) -> dict[str, object]:
    facts: dict[str, object] = {
        "actual_write": actual_write,
        "target_type": outcome.target_type,
        "target_id": outcome.target_id,
    }
    if outcome.target_type == "daily_report":
        report = next(
            (
                row
                for row in after.table_rows("daily_reports")
                if row.get("report_id") == outcome.target_id
            ),
            None,
        )
        field_order = {"today_work": 0, "problems": 1, "tomorrow_plan": 2}
        report_items = sorted(
            (
                row
                for row in after.table_rows("daily_items")
                if row.get("report_id") == outcome.target_id
            ),
            key=lambda row: (
                field_order[str(row["field"])],
                int(row.get("position", 0)),
                str(row["item_id"]),
            ),
        )
        items = tuple(
            {
                "item_id": row["item_id"],
                "field": row["field"],
                "content": row["content"],
                "position": row.get("position", 0),
            }
            for row in report_items
        )
        facts["report"] = (
            {
                "report_id": report["report_id"],
                "report_date": report["report_date"],
                "version": report["version"],
                "status": report["status"],
            }
            if report is not None
            else None
        )
        facts["items"] = items
        source_bindings = {
            (
                str(provenance["source_report_id"]),
                int(provenance["source_report_version"]),
            )
            for row in after.table_rows("daily_items")
            if row.get("item_id") in affected_item_ids
            and isinstance((provenance := row.get("provenance")), dict)
            and isinstance(provenance.get("source_report_id"), str)
            and isinstance(provenance.get("source_report_version"), int)
        }
        if len(source_bindings) == 1:
            source_report_id, source_version = next(iter(source_bindings))
            facts["source_report_id"] = source_report_id
            facts["source_version"] = source_version
        consumed_pending = next(
            (
                row
                for row in after.table_rows("sandbox_pending")
                if row.get("report_id") == outcome.target_id
                and row.get("consumed_by_message_id")
                == context.source_message_id
                and row.get("consumed_by_turn_id") == context.turn_id
            ),
            None,
        )
        if consumed_pending is not None:
            facts["clear_confirmation"] = {
                "pending_id": consumed_pending["pending_id"],
                "target_date": consumed_pending["target_date"],
                "consumed": True,
            }
    elif outcome.target_type == "sandbox_pending":
        pending = next(
            (
                row
                for row in after.table_rows("sandbox_pending")
                if row.get("pending_id") == outcome.target_id
            ),
            None,
        )
        facts["pending"] = (
            {
                "report_id": pending["report_id"],
                "report_version": pending["report_version"],
                "target_date": pending["target_date"],
                "expiry_time": pending["expiry_time"],
                "confirmation_required": pending.get("consumed_at") is None,
            }
            if pending is not None
            else None
        )
        if pending is not None:
            facts["report_id"] = pending["report_id"]
            facts["report_version"] = pending["report_version"]
            facts["target_date"] = pending["target_date"]
    elif outcome.target_type == "personal_memory":
        facts.update(
            personal_memory_safe_user_facts(
                outcome,
                after,
                context=context,
                actual_write=actual_write,
            )
        )
    return facts


def _tool_receipt(facts: SandboxReceiptFacts) -> ToolReceipt:
    return ToolReceipt(
        status=facts.status,
        tool_name=facts.tool_name,
        changed=facts.changed,
        target_type=facts.target_type,
        target_id=facts.target_id,
        before_version=facts.before_version,
        after_version=facts.after_version,
        affected_item_ids=facts.affected_item_ids,
        error_code=facts.error_code,
        safe_user_facts=facts.safe_user_facts,
        server_evidence={
            "receipt_id": facts.receipt_id,
            "before_state_hash": facts.before_state_hash,
            "after_state_hash": facts.after_state_hash,
            "request_fingerprint": facts.request_fingerprint,
        },
        execution_mode=ExecutionMode.SANDBOX_EXECUTE,
        idempotency_key=facts.idempotency_key,
    )


def _replayed_receipt(
    prior: SandboxReceiptFacts,
    current: SandboxSnapshot,
    *,
    context: SandboxExecutionContext,
    call: NativeToolCall,
    arguments: dict[str, object],
) -> SandboxReceiptFacts:
    replay_outcome = SandboxHandlerOutcome(
        target_type=prior.target_type,
        target_id=prior.target_id,
        idempotency_key=prior.idempotency_key,
    )
    current_version = _target_version(
        current,
        replay_outcome,
        context=context,
    )
    safe_user_facts = _safe_user_facts(
        replay_outcome,
        current,
        current,
        context=context,
        affected_item_ids=(),
        actual_write=False,
    )
    safe_user_facts["idempotent_replay"] = True
    fingerprint = request_fingerprint(
        context=context,
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        arguments=arguments,
    )
    operation_id = operation_fingerprint(
        context=context,
        tool_name=call.tool_name,
        arguments=arguments,
    )
    return prior.model_copy(
        update={
            "receipt_id": str(
                uuid5(
                    NAMESPACE_URL,
                    (
                        "agent2-sandbox-replay-receipt:"
                        f"{fingerprint}:{current.state_hash}"
                    ),
                )
            ),
            "tool_call_id": call.tool_call_id,
            "tool_name": call.tool_name,
            "status": ReceiptStatus.NO_OP,
            "changed": False,
            "before_version": current_version,
            "after_version": current_version,
            "affected_item_ids": (),
            "request_fingerprint": fingerprint,
            "operation_fingerprint": operation_id,
            "canonical_arguments_hash": canonical_arguments_hash(arguments),
            "before_state_hash": current.state_hash,
            "after_state_hash": current.state_hash,
            "safe_user_facts": safe_user_facts,
        }
    )


def _validate_outcome_binding(
    outcome: SandboxHandlerOutcome,
    before: SandboxSnapshot,
    after: SandboxSnapshot,
    *,
    context: SandboxExecutionContext,
    arguments: object,
    tool_call_id: str,
    tool_name: str,
    read_only: bool,
) -> None:
    is_memory_call = isinstance(
        arguments,
        (
            QueryPersonalMemoryArgs,
            RememberPersonalMemoryArgs,
            ForgetPersonalMemoryArgs,
        ),
    )
    if is_memory_call != (outcome.target_type == "personal_memory"):
        raise SandboxExecutionError(
            "HANDLER_BINDING_EVIDENCE_MISMATCH"
        )
    if outcome.target_type not in {
        "daily_report",
        "sandbox_pending",
        "personal_memory",
    }:
        raise SandboxExecutionError("HANDLER_BINDING_EVIDENCE_MISMATCH")
    before_reports = {
        str(row["report_id"]): row for row in before.table_rows("daily_reports")
    }
    after_reports = {
        str(row["report_id"]): row for row in after.table_rows("daily_reports")
    }
    changed_report_ids = {
        report_id
        for report_id in before_reports.keys() | after_reports.keys()
        if before_reports.get(report_id) != after_reports.get(report_id)
    }
    before_items = {
        str(row["item_id"]): row for row in before.table_rows("daily_items")
    }
    after_items = {
        str(row["item_id"]): row for row in after.table_rows("daily_items")
    }
    changed_item_report_ids = {
        str(row["report_id"])
        for item_id in before_items.keys() | after_items.keys()
        for row in (after_items.get(item_id) or before_items.get(item_id),)
        if before_items.get(item_id) != after_items.get(item_id)
    }
    before_pending = {
        str(row["pending_id"]): row for row in before.table_rows("sandbox_pending")
    }
    after_pending = {
        str(row["pending_id"]): row for row in after.table_rows("sandbox_pending")
    }
    changed_pending_ids = {
        pending_id
        for pending_id in before_pending.keys() | after_pending.keys()
        if before_pending.get(pending_id) != after_pending.get(pending_id)
    }

    if read_only and before.state_hash != after.state_hash:
        raise SandboxExecutionError("READ_TOOL_CHANGED_SANDBOX_STATE")
    if outcome.target_type == "personal_memory":
        if changed_report_ids or changed_item_report_ids or changed_pending_ids:
            raise SandboxExecutionError(
                "HANDLER_BINDING_EVIDENCE_MISMATCH"
            )
        validate_personal_memory_outcome(
            outcome,
            before,
            after,
            context=context,
            arguments=arguments,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        return

    if personal_memory_state_changed(before, after):
        raise SandboxExecutionError(
            "HANDLER_BINDING_EVIDENCE_MISMATCH"
        )
    if outcome.target_type == "daily_report":
        target_report = (
            after_reports.get(outcome.target_id)
            or before_reports.get(outcome.target_id)
        )
        if target_report is not None and not _row_is_owned(
            target_report,
            context,
        ):
            raise SandboxExecutionError("HANDLER_BINDING_EVIDENCE_MISMATCH")
        if changed_report_ids and changed_report_ids != {outcome.target_id}:
            raise SandboxExecutionError("HANDLER_BINDING_EVIDENCE_MISMATCH")
        if (
            changed_item_report_ids
            and changed_item_report_ids != {outcome.target_id}
        ):
            raise SandboxExecutionError("HANDLER_BINDING_EVIDENCE_MISMATCH")
        if changed_pending_ids and any(
            str(
                (
                    after_pending.get(pending_id)
                    or before_pending[pending_id]
                )["report_id"]
            )
            != outcome.target_id
            for pending_id in changed_pending_ids
        ):
            raise SandboxExecutionError("HANDLER_BINDING_EVIDENCE_MISMATCH")
        return

    target_pending = (
        after_pending.get(outcome.target_id)
        or before_pending.get(outcome.target_id)
    )
    if target_pending is None or not _row_is_owned(target_pending, context):
        raise SandboxExecutionError("HANDLER_BINDING_EVIDENCE_MISMATCH")
    if changed_pending_ids and changed_pending_ids != {outcome.target_id}:
        raise SandboxExecutionError("HANDLER_BINDING_EVIDENCE_MISMATCH")
    if changed_report_ids or changed_item_report_ids:
        raise SandboxExecutionError("HANDLER_BINDING_EVIDENCE_MISMATCH")


def _row_is_owned(
    row: dict[str, object],
    context: SandboxExecutionContext,
) -> bool:
    return (
        row.get("tenant_id") == context.tenant_id
        and row.get("user_id") == str(context.user_id)
    )
