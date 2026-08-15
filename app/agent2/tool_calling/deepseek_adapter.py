from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date
from time import perf_counter
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

from app.agent2.report_domain import period_bounds
from app.agent2.tool_calling.context import TrustedContext
from app.agent2.tool_calling.contracts import ExecutionMode, ToolReceipt
from app.agent2.tool_calling.daily_briefing_reply import (
    daily_briefing_composer_messages,
    daily_briefing_reply_retry_instruction,
    render_daily_briefing_reply,
    validate_daily_briefing_reply,
)
from app.agent2.tool_calling.daily_incomplete_confirm_review import (
    daily_incomplete_confirm_review_messages,
    incomplete_confirm_review_targets,
    validate_daily_incomplete_confirm_replacements,
)
from app.agent2.tool_calling.managed_daily_reply import (
    managed_daily_reply_retry_instruction,
    validate_managed_daily_reply,
)
from app.agent2.tool_calling.production_contracts import ProductionRuntimeResult
from app.agent2.tool_calling.receipt_reply import (
    finalize_canary_content,
    finalize_shadow_content,
)
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    ToolArgumentsValidationError,
    UnknownToolError,
    deepseek_tool_schemas,
    validate_tool_arguments,
)
from app.agent2.tool_calling.runtime import (
    NativeToolCall,
    ShadowRuntime,
    TurnExecutionPlan,
    merge_turn_plans,
)
from app.agent2.tool_calling.write_reply import (
    model_safe_user_facts,
    validate_write_reply,
    write_reply_protocol,
    write_reply_retry_instruction,
)

_TEXTUAL_TOOL_PROTOCOL_MARKERS = (
    "<｜DSML｜tool_calls",
    "<｜DSML｜invoke",
    "<｜tool▁calls▁begin｜>",
    "<｜tool▁call▁begin｜>",
    "<|tool_calls",
)


class DeepSeekToolCallingError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        raw_tool_call_audit: tuple[RawToolCallAudit, ...] = (),
        model_call_count: int = 0,
        turn_plan: TurnExecutionPlan | None = None,
        model_turns: tuple[ModelTurnAudit, ...] = (),
        request_attempt_count: int = 0,
        transport_retry_count: int = 0,
        transport_errors: tuple[dict[str, Any], ...] = (),
        model_elapsed_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.raw_tool_call_audit = raw_tool_call_audit
        self.model_call_count = model_call_count
        self.turn_plan = turn_plan
        self.model_turns = model_turns
        self.request_attempt_count = request_attempt_count
        self.transport_retry_count = transport_retry_count
        self.transport_errors = transport_errors
        self.model_elapsed_seconds = max(
            0.0,
            float(model_elapsed_seconds),
        )


class DeepSeekTimeoutError(DeepSeekToolCallingError):
    pass


class DeepSeekResponseError(DeepSeekToolCallingError):
    pass


class MalformedToolCallError(DeepSeekToolCallingError):
    pass


class UnknownNativeToolError(DeepSeekToolCallingError):
    pass


class InvalidNativeToolArgumentsError(DeepSeekToolCallingError):
    pass


class RepeatedToolCallError(DeepSeekToolCallingError):
    pass


class MaxToolLoopsExceeded(DeepSeekToolCallingError):
    pass


class ToolCallsAfterWriteBatchError(DeepSeekToolCallingError):
    pass


class ShadowCapabilityViolationError(DeepSeekToolCallingError):
    pass


class ProductionRuntimeExecutionError(DeepSeekToolCallingError):
    pass


@dataclass(frozen=True)
class RawToolCallAudit:
    tool_call_id: str
    tool_name: str
    raw_arguments: str
    arguments_sha256: str
    parse_status: str


@dataclass(frozen=True)
class ModelTurnAudit:
    iteration: int
    raw_assistant_message: dict[str, Any]
    assistant_message_sha256: str
    reasoning_content_summary: str
    reasoning_content_sha256: str | None
    response_metadata: dict[str, Any] = field(default_factory=dict)
    tool_results: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class DeepSeekToolCallingResult:
    final_content: str
    model_content_sha256: str
    iterations: int
    raw_tool_call_audit: tuple[RawToolCallAudit, ...]
    plan: TurnExecutionPlan | None
    model_turns: tuple[ModelTurnAudit, ...]
    request_attempt_count: int = 0
    transport_retry_count: int = 0


@dataclass(frozen=True)
class DeepSeekCanaryResult:
    final_content: str
    model_content_sha256: str
    iterations: int
    raw_tool_call_audit: tuple[RawToolCallAudit, ...]
    receipts: tuple[ToolReceipt, ...]
    runtime_results: tuple[ProductionRuntimeResult, ...]
    model_turns: tuple[ModelTurnAudit, ...]
    request_attempt_count: int = 0
    transport_retry_count: int = 0


@dataclass(frozen=True)
class _ParsedAssistantTurn:
    assistant_message: dict[str, Any]
    tool_calls: tuple[NativeToolCall, ...]
    audit: tuple[RawToolCallAudit, ...]


@dataclass(frozen=True)
class _CompletionResponse:
    message: dict[str, Any]
    metadata: dict[str, Any]


def _canary_user_input_payload(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    context: TrustedContext,
) -> dict[str, Any]:
    normalized = tuple(str(value) for value in user_messages)
    if normalized:
        if any(not value.strip() for value in normalized):
            raise ValueError("user_messages cannot contain empty fragments")
        return {
            "user_messages": [
                {"sequence": index, "content": value}
                for index, value in enumerate(normalized, start=1)
            ],
            "trusted_context": context.model_payload(),
        }
    return {
        "user_message": user_text,
        "trusted_context": context.model_payload(),
    }


class DeepSeekToolCallingAdapter:
    """Native DeepSeek tool-calling transport with fail-closed parsing."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        model: str,
        timeout_seconds: float,
        max_tool_loops: int,
        max_request_attempts: int = 2,
        retry_backoff_seconds: float = 0.25,
        endpoint: str = "",
    ) -> None:
        if not model:
            raise ValueError("model is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_tool_loops < 1:
            raise ValueError("max_tool_loops must be positive")
        if max_request_attempts < 1 or max_request_attempts > 3:
            raise ValueError("max_request_attempts must be between 1 and 3")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be non-negative")
        endpoint = endpoint.strip()
        if not endpoint:
            raise ValueError("endpoint is required")
        self._http_client = http_client
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tool_loops = max_tool_loops
        self._max_request_attempts = max_request_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._endpoint = endpoint

    async def run_shadow_turn(
        self,
        *,
        system_prompt: str,
        user_text: str,
        context: TrustedContext,
        runtime: ShadowRuntime,
        thinking_enabled: bool = False,
    ) -> DeepSeekToolCallingResult:
        if (
            type(runtime) is not ShadowRuntime
            or runtime.mode != ExecutionMode.SHADOW_PROPOSAL
        ):
            raise TypeError(
                "run_shadow_turn requires the exact zero-write ShadowRuntime capability"
            )
        session = runtime.open_session(context)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "user_message": user_text,
                        "trusted_context": context.model_payload(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]
        audits: list[RawToolCallAudit] = []
        model_turns: list[ModelTurnAudit] = []
        plans: list[TurnExecutionPlan] = []
        seen_ids: set[str] = set()
        seen_fingerprints: set[str] = set()
        iterations = 0
        tool_loops = 0
        write_batch_seen = False
        managed_daily_reply_retry_count = 0
        tool_schemas = deepseek_tool_schemas(context.allowed_tool_names)

        while True:
            iterations += 1
            try:
                completion = await self._complete(
                    messages,
                    tool_schemas=(
                        []
                        if (write_batch_seen or managed_daily_reply_retry_count)
                        else tool_schemas
                    ),
                    thinking_enabled=thinking_enabled,
                )
                model_turns.append(
                    _model_turn_audit(
                        iterations,
                        completion.message,
                        response_metadata=completion.metadata,
                    )
                )
                parsed = _parse_assistant_turn(completion.message)
                audits.extend(parsed.audit)
                _validate_completion_protocol(completion, parsed)
            except DeepSeekToolCallingError as exc:
                raise _with_turn_state(
                    exc, audits, iterations, context, plans, model_turns
                ) from exc
            if not parsed.tool_calls:
                content = parsed.assistant_message.get("content")
                if not isinstance(content, str):
                    error = DeepSeekResponseError(
                        "assistant response has neither tool calls nor text content"
                    )
                    raise _with_turn_state(
                        error, audits, iterations, context, plans, model_turns
                    )
                textual_tool_protocol = any(
                    marker in content for marker in _TEXTUAL_TOOL_PROTOCOL_MARKERS
                )
                if textual_tool_protocol:
                    error = (
                        ToolCallsAfterWriteBatchError(
                            "textual tool protocol appeared after the write batch closed"
                        )
                        if write_batch_seen
                        else MalformedToolCallError(
                            "textual tool protocol is not a native Tool Call"
                        )
                    )
                    raise _with_turn_state(
                        error, audits, iterations, context, plans, model_turns
                    )
                turn_plan = merge_turn_plans(context, tuple(plans))
                if turn_plan is not None:
                    _assert_shadow_plan(
                        turn_plan,
                        context,
                        expected_receipt_count=len(turn_plan.tool_calls),
                    )
                final_content, model_hash = finalize_shadow_content(content, turn_plan)
                return DeepSeekToolCallingResult(
                    final_content=final_content,
                    iterations=iterations,
                    raw_tool_call_audit=tuple(audits),
                    plan=turn_plan,
                    model_content_sha256=model_hash,
                    model_turns=tuple(model_turns),
                    request_attempt_count=sum(
                        int(item.response_metadata.get("request_attempt_count", 1))
                        for item in model_turns
                    ),
                    transport_retry_count=sum(
                        int(item.response_metadata.get("transport_retry_count", 0))
                        for item in model_turns
                    ),
                )

            if tool_loops >= self._max_tool_loops:
                raise _with_turn_state(
                    MaxToolLoopsExceeded("maximum tool-call loops exceeded"),
                    audits,
                    iterations,
                    context,
                    plans,
                    model_turns,
                )
            if write_batch_seen:
                raise _with_turn_state(
                    ToolCallsAfterWriteBatchError(
                        "a user turn may contain only one complete write-tool batch"
                    ),
                    audits,
                    iterations,
                    context,
                    plans,
                    model_turns,
                )
            try:
                _reject_repeated_calls(
                    parsed.tool_calls,
                    seen_ids=seen_ids,
                    seen_fingerprints=seen_fingerprints,
                    audit=(),
                )
            except DeepSeekToolCallingError as exc:
                raise _with_turn_state(
                    exc, audits, iterations, context, plans, model_turns
                ) from exc
            current_has_write = any(
                TOOL_REGISTRY[call.tool_name].read_or_write == "write"
                for call in parsed.tool_calls
            )
            messages.append(parsed.assistant_message)
            try:
                plan = await session.propose(parsed.tool_calls)
                _assert_shadow_plan(
                    plan,
                    context,
                    expected_receipt_count=len(parsed.tool_calls),
                )
            except DeepSeekToolCallingError as exc:
                raise _with_turn_state(
                    exc, audits, iterations, context, plans, model_turns
                ) from exc
            except Exception as exc:
                error = ShadowCapabilityViolationError("ShadowRuntime failed closed")
                raise _with_turn_state(
                    error, audits, iterations, context, plans, model_turns
                ) from exc
            plans.append(plan)
            tool_results = _tool_result_messages(
                parsed.tool_calls,
                plan.receipts,
                write_batch_closed=current_has_write,
            )
            messages.extend(tool_results)
            if current_has_write:
                messages.append(_post_write_protocol_message())
            model_turns[-1] = replace(
                model_turns[-1],
                tool_results=tuple(tool_results),
            )
            tool_loops += 1
            write_batch_seen = write_batch_seen or current_has_write

    async def run_canary_turn(
        self,
        *,
        system_prompt: str,
        user_text: str,
        user_messages: tuple[str, ...] = (),
        context: TrustedContext,
        runtime_session: Any,
        thinking_enabled: bool = False,
    ) -> DeepSeekCanaryResult:
        if (
            context.namespace != "agent2.tool_calling.canary.v1"
            or getattr(runtime_session, "mode", None) != ExecutionMode.CANARY_EXECUTE
        ):
            raise TypeError(
                "run_canary_turn requires a Canary context and runtime session"
            )
        system_prompt_sha256 = hashlib.sha256(
            system_prompt.encode("utf-8")
        ).hexdigest()
        briefing_user_question = (
            "\n".join(user_messages) if user_messages else user_text
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    _canary_user_input_payload(
                        user_text=user_text,
                        user_messages=user_messages,
                        context=context,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]
        audits: list[RawToolCallAudit] = []
        model_turns: list[ModelTurnAudit] = []
        runtime_results: list[ProductionRuntimeResult] = []
        receipts: list[ToolReceipt] = []
        seen_ids: set[str] = set()
        seen_fingerprints: set[str] = set()
        iterations = 0
        tool_loops = 0
        write_batch_seen = False
        briefing_fact_batch_seen = False
        daily_briefing_reply_retry_count = 0
        managed_daily_reply_retry_count = 0
        write_reply_retry_count = 0
        tool_argument_repair_count = 0
        incomplete_confirm_review_count = 0
        daily_submit_section_review_count = 0
        daily_weekly_write_review_count = 0
        tool_schemas = deepseek_tool_schemas(context.allowed_tool_names)

        async def rollback_pending() -> None:
            if not runtime_results or not runtime_results[-1].transaction_pending:
                return
            rollback = getattr(runtime_session, "rollback_pending", None)
            if rollback is not None:
                await rollback()

        async def commit_pending() -> None:
            if not runtime_results or not runtime_results[-1].transaction_pending:
                return
            commit = getattr(runtime_session, "commit_pending", None)
            if commit is None:
                raise ProductionRuntimeExecutionError(
                    "production runtime cannot finalize a pending turn"
                )
            committed = await commit()
            if (
                not isinstance(committed, ProductionRuntimeResult)
                or committed.status != "success"
                or committed.transaction_pending
                or not committed.committed_to_outer_transaction
                or committed.receipts != runtime_results[-1].receipts
            ):
                raise ProductionRuntimeExecutionError(
                    "production runtime returned an invalid finalization result"
                )
            runtime_results[-1] = committed

        try:
            while True:
                iterations += 1
                try:
                    completion = await self._complete(
                        messages,
                        tool_schemas=(
                            []
                            if (
                                write_batch_seen
                                or briefing_fact_batch_seen
                                or daily_briefing_reply_retry_count
                                or managed_daily_reply_retry_count
                                or write_reply_retry_count
                            )
                            else tool_schemas
                        ),
                        thinking_enabled=(thinking_enabled or briefing_fact_batch_seen),
                    )
                    model_turns.append(
                        _model_turn_audit(
                            iterations,
                            completion.message,
                            response_metadata={
                                **completion.metadata,
                                "system_prompt_sha256": (
                                    system_prompt_sha256
                                ),
                            },
                        )
                    )
                    parsed = _parse_assistant_turn(completion.message)
                    audits.extend(parsed.audit)
                    protocol_warning = _validate_completion_protocol(
                        completion,
                        parsed,
                        allow_usable_direct_text=True,
                        allow_empty_terminal_for_retry=(
                            (
                                briefing_fact_batch_seen
                                and daily_briefing_reply_retry_count == 0
                            )
                            or (write_batch_seen and write_reply_retry_count < 2)
                        ),
                    )
                    if protocol_warning is not None:
                        model_turns[-1] = replace(
                            model_turns[-1],
                            response_metadata={
                                **model_turns[-1].response_metadata,
                                "protocol_warning": protocol_warning,
                            },
                        )
                except (
                    MalformedToolCallError,
                    InvalidNativeToolArgumentsError,
                ) as exc:
                    if (
                        tool_argument_repair_count == 0
                        and not write_batch_seen
                        and not briefing_fact_batch_seen
                    ):
                        audits.extend(exc.raw_tool_call_audit)
                        model_turns[-1] = replace(
                            model_turns[-1],
                            response_metadata={
                                **model_turns[-1].response_metadata,
                                "pre_execution_tool_argument_repair": True,
                                "tool_argument_error_type": type(exc).__name__,
                            },
                        )
                        messages.append(_pre_execution_tool_argument_repair_message())
                        tool_argument_repair_count += 1
                        continue
                    raise _with_canary_turn_state(
                        exc,
                        audits=audits,
                        model_turns=model_turns,
                    ) from exc
                except DeepSeekToolCallingError as exc:
                    raise _with_canary_turn_state(
                        exc,
                        audits=audits,
                        model_turns=model_turns,
                    ) from exc

                daily_weekly_review_tool_names = (
                    _daily_weekly_write_review_tool_names(
                        parsed.tool_calls,
                        context=context,
                    )
                )
                if (
                    daily_weekly_write_review_count == 0
                    and daily_weekly_review_tool_names
                    and (parsed.tool_calls or tool_loops == 0)
                ):
                    model_turns[-1] = replace(
                        model_turns[-1],
                        response_metadata={
                            **model_turns[-1].response_metadata,
                            "pre_execution_daily_weekly_write_review": True,
                            "draft_executed": False,
                        },
                    )
                    try:
                        review_completion = await self._complete(
                            _daily_weekly_write_review_messages(
                                user_text=user_text,
                                user_messages=user_messages,
                                calls=parsed.tool_calls,
                                context=context,
                                allowed_tool_names=(
                                    daily_weekly_review_tool_names
                                ),
                            ),
                            tool_schemas=deepseek_tool_schemas(
                                daily_weekly_review_tool_names
                            ),
                            thinking_enabled=True,
                        )
                        iterations += 1
                        model_turns.append(
                            _model_turn_audit(
                                iterations,
                                review_completion.message,
                                response_metadata={
                                    **review_completion.metadata,
                                    "daily_weekly_write_semantic_review": True,
                                },
                            )
                        )
                        reviewed = _parse_assistant_turn(
                            review_completion.message
                        )
                        _validate_completion_protocol(
                            review_completion,
                            reviewed,
                        )
                        audits.extend(reviewed.audit)
                        _validate_daily_weekly_write_review(
                            reviewed=reviewed,
                            allowed_tool_names=daily_weekly_review_tool_names,
                            original_has_domain_writes=any(
                                _daily_weekly_write_domain(call.tool_name)
                                is not None
                                for call in parsed.tool_calls
                            ),
                        )
                    except DeepSeekToolCallingError as exc:
                        raise _with_canary_turn_state(
                            exc,
                            audits=audits,
                            model_turns=model_turns,
                        ) from exc
                    except ValueError as exc:
                        review_content = review_completion.message.get("content")
                        if (
                            not reviewed.tool_calls
                            and isinstance(review_content, str)
                            and review_content.strip()
                        ):
                            try:
                                repair_completion = await self._complete(
                                    _daily_weekly_review_envelope_repair_messages(
                                        raw_content=review_content,
                                    ),
                                    tool_schemas=[],
                                    thinking_enabled=True,
                                )
                                iterations += 1
                                model_turns.append(
                                    _model_turn_audit(
                                        iterations,
                                        repair_completion.message,
                                        response_metadata={
                                            **repair_completion.metadata,
                                            "daily_weekly_write_review_envelope_repair": True,
                                        },
                                    )
                                )
                                repaired = _parse_assistant_turn(
                                    repair_completion.message
                                )
                                _validate_completion_protocol(
                                    repair_completion,
                                    repaired,
                                )
                                audits.extend(repaired.audit)
                                if repaired.tool_calls:
                                    raise ValueError(
                                        "clarification envelope repair cannot introduce tool calls"
                                    )
                                _validate_daily_weekly_write_review(
                                    reviewed=repaired,
                                    allowed_tool_names=daily_weekly_review_tool_names,
                                    original_has_domain_writes=any(
                                        _daily_weekly_write_domain(call.tool_name)
                                        is not None
                                        for call in parsed.tool_calls
                                    ),
                                )
                                reviewed = repaired
                            except (DeepSeekToolCallingError, ValueError) as repair_exc:
                                raise _with_canary_turn_state(
                                    DeepSeekResponseError(
                                        "daily and weekly write semantic review returned an invalid replacement"
                                    ),
                                    audits=audits,
                                    model_turns=model_turns,
                                ) from repair_exc
                        else:
                            raise _with_canary_turn_state(
                                DeepSeekResponseError(
                                    "daily and weekly write semantic review returned an invalid replacement"
                                ),
                                audits=audits,
                                model_turns=model_turns,
                            ) from exc
                    original_has_domain_writes = any(
                        _daily_weekly_write_domain(call.tool_name) is not None
                        for call in parsed.tool_calls
                    )
                    reviewed_has_writes = any(
                        _daily_weekly_write_domain(call.tool_name) is not None
                        for call in reviewed.tool_calls
                    )
                    if original_has_domain_writes or reviewed_has_writes:
                        daily_weekly_write_review_count += 1
                    if (
                        not original_has_domain_writes
                        and reviewed.tool_calls
                        and reviewed_has_writes
                    ):
                        try:
                            confirmation_completion = await self._complete(
                                _daily_weekly_zero_draft_confirmation_messages(
                                    user_text=user_text,
                                    user_messages=user_messages,
                                    context=context,
                                    allowed_tool_names=(
                                        daily_weekly_review_tool_names
                                    ),
                                ),
                                tool_schemas=deepseek_tool_schemas(
                                    daily_weekly_review_tool_names
                                ),
                                thinking_enabled=True,
                            )
                            iterations += 1
                            model_turns.append(
                                _model_turn_audit(
                                    iterations,
                                    confirmation_completion.message,
                                    response_metadata={
                                        **confirmation_completion.metadata,
                                        "daily_weekly_zero_draft_write_confirmation": True,
                                    },
                                )
                            )
                            confirmed = _parse_assistant_turn(
                                confirmation_completion.message
                            )
                            _validate_completion_protocol(
                                confirmation_completion,
                                confirmed,
                            )
                            audits.extend(confirmed.audit)
                            _validate_daily_weekly_zero_draft_agreement(
                                first=reviewed.tool_calls,
                                second=confirmed.tool_calls,
                                allowed_tool_names=daily_weekly_review_tool_names,
                            )
                        except DeepSeekToolCallingError as exc:
                            raise _with_canary_turn_state(
                                exc,
                                audits=audits,
                                model_turns=model_turns,
                            ) from exc
                        except ValueError as exc:
                            clarification_kind = _zero_draft_disagreement_kind(
                                first=reviewed.tool_calls,
                                second=confirmed.tool_calls,
                                context=context,
                            )
                            if clarification_kind is None:
                                raise _with_canary_turn_state(
                                    DeepSeekResponseError(
                                        "daily and weekly zero-draft reviewers did not independently agree"
                                    ),
                                    audits=audits,
                                    model_turns=model_turns,
                                ) from exc
                            try:
                                clarification_completion = await self._complete(
                                    _weekly_target_disagreement_clarification_messages(
                                        user_text=user_text,
                                        user_messages=user_messages,
                                        context=context,
                                        clarification_kind=clarification_kind,
                                    ),
                                    tool_schemas=[],
                                    thinking_enabled=True,
                                )
                                iterations += 1
                                model_turns.append(
                                    _model_turn_audit(
                                        iterations,
                                        clarification_completion.message,
                                        response_metadata={
                                            **clarification_completion.metadata,
                                            "daily_weekly_zero_draft_disagreement_clarification": True,
                                        },
                                    )
                                )
                                clarified = _parse_assistant_turn(
                                    clarification_completion.message
                                )
                                _validate_completion_protocol(
                                    clarification_completion,
                                    clarified,
                                )
                                audits.extend(clarified.audit)
                                _validate_week_target_disagreement_clarification(
                                    clarified,
                                    clarification_kind=clarification_kind,
                                )
                                reviewed = clarified
                            except (DeepSeekToolCallingError, ValueError) as clarify_exc:
                                raise _with_canary_turn_state(
                                    DeepSeekResponseError(
                                        "daily and weekly zero-draft reviewers did not independently agree"
                                    ),
                                    audits=audits,
                                    model_turns=model_turns,
                                ) from clarify_exc
                    parsed = _merge_daily_weekly_write_review(
                        original=parsed,
                        reviewed=reviewed,
                        reviewed_tool_names=daily_weekly_review_tool_names,
                    )

                if not parsed.tool_calls:
                    content = parsed.assistant_message.get("content")
                    if not isinstance(content, str):
                        raise _with_canary_turn_state(
                            DeepSeekResponseError(
                                "assistant response has neither tool calls nor text content"
                            ),
                            audits=audits,
                            model_turns=model_turns,
                        )
                    if any(
                        marker in content for marker in _TEXTUAL_TOOL_PROTOCOL_MARKERS
                    ):
                        error = (
                            ToolCallsAfterWriteBatchError(
                                "textual tool protocol appeared after the write batch closed"
                            )
                            if write_batch_seen
                            else MalformedToolCallError(
                                "textual tool protocol is not a native Tool Call"
                            )
                        )
                        raise _with_canary_turn_state(
                            error,
                            audits=audits,
                            model_turns=model_turns,
                        )

                    reply_for_validation = content
                    write_validation_errors: tuple[str, ...] = ()
                    briefing_validation_errors: tuple[str, ...] = ()
                    briefing_envelope = None
                    if write_batch_seen:
                        envelope, write_validation_errors = validate_write_reply(
                            content, tuple(receipts)
                        )
                        if envelope is not None:
                            reply_for_validation = envelope.reply
                    elif briefing_fact_batch_seen:
                        (
                            briefing_envelope,
                            briefing_validation_errors,
                        ) = validate_daily_briefing_reply(
                            content,
                            tuple(receipts),
                        )
                        if briefing_envelope is not None:
                            reply_for_validation = render_daily_briefing_reply(
                                briefing_envelope
                            )
                    managed_validation_errors = (
                        validate_managed_daily_reply(
                            reply_for_validation,
                            tuple(receipts),
                        )
                        if not write_validation_errors
                        and not briefing_validation_errors
                        else ()
                    )
                    validation_errors = (
                        *write_validation_errors,
                        *briefing_validation_errors,
                        *managed_validation_errors,
                    )
                    if validation_errors:
                        validation_metadata = {
                            "terminal_reply_validation": list(validation_errors),
                        }
                        if briefing_fact_batch_seen:
                            validation_metadata["daily_briefing_reply_validation"] = (
                                list(validation_errors)
                            )
                        model_turns[-1] = replace(
                            model_turns[-1],
                            response_metadata={
                                **model_turns[-1].response_metadata,
                                **validation_metadata,
                            },
                        )
                        if write_batch_seen:
                            if write_reply_retry_count < 2:
                                if content.strip():
                                    messages.append(parsed.assistant_message)
                                messages.append(
                                    {
                                        "role": "system",
                                        "content": write_reply_retry_instruction(
                                            tuple(validation_errors),
                                            tuple(receipts),
                                        ),
                                    }
                                )
                                model_turns[-1] = replace(
                                    model_turns[-1],
                                    response_metadata={
                                        **model_turns[-1].response_metadata,
                                        "write_reply_validation": list(
                                            validation_errors
                                        ),
                                        "write_reply_retry": True,
                                    },
                                )
                                write_reply_retry_count += 1
                                continue
                            raise _with_canary_turn_state(
                                DeepSeekResponseError(
                                    "write reply failed receipt validation"
                                ),
                                audits=audits,
                                model_turns=model_turns,
                            )
                        if briefing_fact_batch_seen:
                            if daily_briefing_reply_retry_count < 2:
                                messages = daily_briefing_composer_messages(
                                    user_question=briefing_user_question,
                                    receipts=tuple(receipts),
                                )
                                messages.append(
                                    {
                                        "role": "system",
                                        "content": (
                                            daily_briefing_reply_retry_instruction(
                                                tuple(validation_errors),
                                                tuple(receipts),
                                                retry_number=(
                                                    daily_briefing_reply_retry_count + 1
                                                ),
                                            )
                                        ),
                                    }
                                )
                                model_turns[-1] = replace(
                                    model_turns[-1],
                                    response_metadata={
                                        **model_turns[-1].response_metadata,
                                        "daily_briefing_reply_validation": list(
                                            validation_errors
                                        ),
                                        "daily_briefing_reply_retry": True,
                                    },
                                )
                                daily_briefing_reply_retry_count += 1
                                continue
                            raise _with_canary_turn_state(
                                DeepSeekResponseError(
                                    "daily briefing reply failed evidence validation"
                                ),
                                audits=audits,
                                model_turns=model_turns,
                            )
                        if managed_daily_reply_retry_count == 0:
                            messages.append(parsed.assistant_message)
                            messages.append(
                                {
                                    "role": "system",
                                    "content": (
                                        managed_daily_reply_retry_instruction(
                                            tuple(validation_errors)
                                        )
                                    ),
                                }
                            )
                            model_turns[-1] = replace(
                                model_turns[-1],
                                response_metadata={
                                    **model_turns[-1].response_metadata,
                                    "managed_daily_reply_validation": list(
                                        validation_errors
                                    ),
                                    "managed_daily_reply_retry": True,
                                },
                            )
                            managed_daily_reply_retry_count += 1
                            continue
                        raise _with_canary_turn_state(
                            DeepSeekResponseError(
                                "managed daily reply failed factual validation"
                            ),
                            audits=audits,
                            model_turns=model_turns,
                        )

                    if write_batch_seen:
                        await commit_pending()
                    final_content, model_hash = finalize_canary_content(
                        (
                            reply_for_validation
                            if briefing_envelope is not None
                            else content
                        ),
                        tuple(receipts),
                        write_batch_seen=write_batch_seen,
                        personal_memory=context.personal_memory,
                    )
                    return DeepSeekCanaryResult(
                        final_content=final_content,
                        model_content_sha256=model_hash,
                        iterations=iterations,
                        raw_tool_call_audit=tuple(audits),
                        receipts=tuple(receipts),
                        runtime_results=tuple(runtime_results),
                        model_turns=tuple(model_turns),
                        request_attempt_count=sum(
                            int(item.response_metadata.get("request_attempt_count", 1))
                            for item in model_turns
                        ),
                        transport_retry_count=sum(
                            int(item.response_metadata.get("transport_retry_count", 0))
                            for item in model_turns
                        ),
                    )

                if tool_loops >= self._max_tool_loops:
                    raise _with_canary_turn_state(
                        MaxToolLoopsExceeded("maximum tool-call loops exceeded"),
                        audits=audits,
                        model_turns=model_turns,
                    )
                if (
                    briefing_fact_batch_seen
                    or daily_briefing_reply_retry_count
                    or managed_daily_reply_retry_count
                    or write_reply_retry_count
                ):
                    raise _with_canary_turn_state(
                        DeepSeekResponseError(
                            "a terminal reply retry emitted a tool call"
                        ),
                        audits=audits,
                        model_turns=model_turns,
                    )
                if write_batch_seen:
                    raise _with_canary_turn_state(
                        ToolCallsAfterWriteBatchError(
                            "a user turn may contain only one complete write-tool batch"
                        ),
                        audits=audits,
                        model_turns=model_turns,
                    )
                current_has_write = any(
                    TOOL_REGISTRY[call.tool_name].read_or_write == "write"
                    for call in parsed.tool_calls
                )
                incomplete_confirm_targets = incomplete_confirm_review_targets(
                    parsed.tool_calls,
                    context=context,
                )
                if incomplete_confirm_review_count == 0 and incomplete_confirm_targets:
                    model_turns[-1] = replace(
                        model_turns[-1],
                        response_metadata={
                            **model_turns[-1].response_metadata,
                            "pre_execution_incomplete_confirm_review": True,
                            "draft_executed": False,
                        },
                    )
                    incomplete_confirm_review_count += 1
                    review_tool_names = frozenset(
                        {"add_daily_items", "confirm_report"}
                    ).intersection(context.allowed_tool_names)
                    try:
                        review_completion = await self._complete(
                            daily_incomplete_confirm_review_messages(
                                ordered_messages=user_messages or (user_text,),
                                targets=incomplete_confirm_targets,
                            ),
                            tool_schemas=deepseek_tool_schemas(review_tool_names),
                            thinking_enabled=True,
                        )
                        iterations += 1
                        model_turns.append(
                            _model_turn_audit(
                                iterations,
                                review_completion.message,
                                response_metadata={
                                    **review_completion.metadata,
                                    "incomplete_confirm_semantic_review": True,
                                },
                            )
                        )
                        reviewed = _parse_assistant_turn(review_completion.message)
                        _validate_completion_protocol(
                            review_completion,
                            reviewed,
                        )
                        audits.extend(reviewed.audit)
                        validate_daily_incomplete_confirm_replacements(
                            targets=incomplete_confirm_targets,
                            replacements=tuple(
                                (call.tool_name, call.arguments)
                                for call in reviewed.tool_calls
                            ),
                            allowed_tool_names=context.allowed_tool_names,
                        )
                    except ValueError as exc:
                        raise _with_canary_turn_state(
                            DeepSeekResponseError(
                                "incomplete confirm semantic review returned an invalid replacement"
                            ),
                            audits=audits,
                            model_turns=model_turns,
                        ) from exc
                    except DeepSeekToolCallingError as exc:
                        raise _with_canary_turn_state(
                            exc,
                            audits=audits,
                            model_turns=model_turns,
                        ) from exc
                    parsed = _merge_reviewed_calls(
                        original=parsed,
                        targets=tuple(
                            target.original_call
                            for target in incomplete_confirm_targets
                        ),
                        replacements=reviewed.tool_calls,
                        assistant_message=reviewed.assistant_message,
                        review_audit=reviewed.audit,
                    )
                    current_has_write = any(
                        TOOL_REGISTRY[call.tool_name].read_or_write == "write"
                        for call in parsed.tool_calls
                    )
                if (
                    daily_submit_section_review_count == 0
                    and _needs_daily_submit_section_review(
                        parsed.tool_calls,
                        context=context,
                    )
                ):
                    review_targets = _daily_submit_section_review_targets(
                        parsed.tool_calls,
                        context=context,
                    )
                    model_turns[-1] = replace(
                        model_turns[-1],
                        response_metadata={
                            **model_turns[-1].response_metadata,
                            "pre_execution_daily_section_review": True,
                            "draft_executed": False,
                        },
                    )
                    daily_submit_section_review_count += 1
                    reviewed: _ParsedAssistantTurn | None = None
                    review_feedback: dict[str, Any] | None = None
                    for review_attempt in range(1, 3):
                        try:
                            review_completion = await self._complete(
                                _daily_submit_section_review_messages(
                                    user_text=user_text,
                                    user_messages=user_messages,
                                    calls=review_targets,
                                    structural_feedback=review_feedback,
                                ),
                                tool_schemas=deepseek_tool_schemas(
                                    frozenset({"add_daily_items"})
                                ),
                                thinking_enabled=thinking_enabled,
                            )
                            iterations += 1
                            model_turns.append(
                                _model_turn_audit(
                                    iterations,
                                    review_completion.message,
                                    response_metadata={
                                        **review_completion.metadata,
                                        "daily_section_semantic_review": True,
                                        "daily_section_semantic_review_attempt": review_attempt,
                                    },
                                )
                            )
                            reviewed = _parse_assistant_turn(review_completion.message)
                            _validate_completion_protocol(
                                review_completion,
                                reviewed,
                            )
                            audits.extend(reviewed.audit)
                        except DeepSeekToolCallingError as exc:
                            raise _with_canary_turn_state(
                                exc,
                                audits=audits,
                                model_turns=model_turns,
                            ) from exc
                        if _is_complete_daily_submit_section_review(
                            reviewed.tool_calls,
                            expected_count=len(review_targets),
                            context=context,
                        ):
                            break
                        if review_attempt == 2:
                            raise _with_canary_turn_state(
                                DeepSeekResponseError(
                                    "daily submit semantic review did not return complete corrected submissions"
                                ),
                                audits=audits,
                                model_turns=model_turns,
                            )
                        review_feedback = _daily_submit_section_review_feedback(
                            reviewed.tool_calls,
                            expected_count=len(review_targets),
                            context=context,
                        )
                    if reviewed is None:
                        raise _with_canary_turn_state(
                            DeepSeekResponseError(
                                "daily submit semantic review produced no result"
                            ),
                            audits=audits,
                            model_turns=model_turns,
                        )
                    parsed = _merge_daily_submit_section_review(
                        original=parsed,
                        reviewed=reviewed,
                        context=context,
                    )
                    current_has_write = any(
                        TOOL_REGISTRY[call.tool_name].read_or_write == "write"
                        for call in parsed.tool_calls
                    )
                try:
                    _reject_repeated_calls(
                        parsed.tool_calls,
                        seen_ids=seen_ids,
                        seen_fingerprints=seen_fingerprints,
                        audit=(),
                    )
                except DeepSeekToolCallingError as exc:
                    raise _with_canary_turn_state(
                        exc,
                        audits=audits,
                        model_turns=model_turns,
                    ) from exc

                messages.append(parsed.assistant_message)
                runtime_result = (
                    await runtime_session.execute(
                        parsed.tool_calls,
                        defer_finalization=True,
                    )
                    if current_has_write
                    else await runtime_session.execute(parsed.tool_calls)
                )
                if (
                    not isinstance(runtime_result, ProductionRuntimeResult)
                    or runtime_result.status == "failed"
                    or len(runtime_result.receipts) != len(parsed.tool_calls)
                ):
                    code = (
                        runtime_result.error_code
                        if isinstance(runtime_result, ProductionRuntimeResult)
                        else "INVALID_PRODUCTION_RUNTIME_RESULT"
                    )
                    raise _with_canary_turn_state(
                        ProductionRuntimeExecutionError(
                            f"production runtime failed closed: {code}"
                        ),
                        audits=audits,
                        model_turns=model_turns,
                    )
                runtime_results.append(runtime_result)
                receipts.extend(runtime_result.receipts)
                tool_results = _canary_tool_result_messages(
                    parsed.tool_calls,
                    runtime_result.receipts,
                    write_batch_closed=current_has_write,
                )
                messages.extend(tool_results)
                if current_has_write:
                    messages.append(
                        _canary_post_write_protocol_message(tuple(receipts))
                    )
                current_has_briefing_fact = any(
                    call.tool_name == "query_daily_briefing_facts"
                    for call in parsed.tool_calls
                )
                model_turns[-1] = replace(
                    model_turns[-1],
                    tool_results=tuple(tool_results),
                )
                if current_has_briefing_fact and not current_has_write:
                    messages = daily_briefing_composer_messages(
                        user_question=briefing_user_question,
                        receipts=tuple(receipts),
                    )
                    briefing_fact_batch_seen = True
                tool_loops += 1
                write_batch_seen = write_batch_seen or current_has_write
        except BaseException:
            await rollback_pending()
            raise

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tool_schemas: list[dict[str, Any]],
        thinking_enabled: bool,
    ) -> _CompletionResponse:
        completion_started = perf_counter()
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0,
        }
        if tool_schemas:
            payload["tools"] = tool_schemas
            if not thinking_enabled:
                payload["tool_choice"] = "auto"
        if thinking_enabled:
            payload["thinking"] = {"type": "enabled"}
            # DeepSeek maps lower labels to this same supported low-cost
            # thinking tier. Send it explicitly so model comparisons and
            # production behavior cannot silently depend on provider defaults.
            payload["reasoning_effort"] = "high"
        if _server_requests_json_object(messages):
            payload["response_format"] = {"type": "json_object"}
        transport_errors: list[dict[str, Any]] = []
        response: httpx.Response | None = None
        for attempt in range(1, self._max_request_attempts + 1):
            try:
                response = await self._http_client.post(
                    self._endpoint,
                    json=payload,
                    timeout=self._timeout_seconds,
                )
                response.raise_for_status()
                break
            except httpx.HTTPError as exc:
                retryable = _is_retryable_transport_error(exc)
                transport_errors.append(
                    _transport_error_audit(exc, attempt=attempt, retryable=retryable)
                )
                if retryable and attempt < self._max_request_attempts:
                    if self._retry_backoff_seconds:
                        await asyncio.sleep(self._retry_backoff_seconds * attempt)
                    continue
                error_type = (
                    DeepSeekTimeoutError
                    if isinstance(exc, httpx.TimeoutException)
                    else DeepSeekResponseError
                )
                message = (
                    "DeepSeek request timed out"
                    if error_type is DeepSeekTimeoutError
                    else "DeepSeek request failed"
                )
                raise error_type(
                    message,
                    request_attempt_count=attempt,
                    transport_retry_count=attempt - 1,
                    transport_errors=tuple(transport_errors),
                    model_elapsed_seconds=round(
                        max(0.0, perf_counter() - completion_started),
                        4,
                    ),
                ) from exc
        if response is None:
            raise DeepSeekResponseError(
                "DeepSeek request produced no response",
                model_elapsed_seconds=round(
                    max(0.0, perf_counter() - completion_started),
                    4,
                ),
            )
        request_attempt_count = len(transport_errors) + 1
        transport_retry_count = len(transport_errors)
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DeepSeekResponseError(
                "malformed DeepSeek response",
                request_attempt_count=request_attempt_count,
                transport_retry_count=transport_retry_count,
                transport_errors=tuple(transport_errors),
                model_elapsed_seconds=round(
                    max(0.0, perf_counter() - completion_started),
                    4,
                ),
            ) from exc
        if not isinstance(message, dict):
            raise DeepSeekResponseError(
                "malformed DeepSeek assistant message",
                request_attempt_count=request_attempt_count,
                transport_retry_count=transport_retry_count,
                transport_errors=tuple(transport_errors),
                model_elapsed_seconds=round(
                    max(0.0, perf_counter() - completion_started),
                    4,
                ),
            )
        served_model = str(body.get("model") or "").strip()
        if served_model != self._model:
            raise DeepSeekResponseError(
                "DeepSeek served an unexpected model",
                request_attempt_count=request_attempt_count,
                transport_retry_count=transport_retry_count,
                transport_errors=tuple(transport_errors),
                model_elapsed_seconds=round(
                    max(0.0, perf_counter() - completion_started),
                    4,
                ),
            )
        canonical_body = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _CompletionResponse(
            message=message,
            metadata={
                "response_id": body.get("id"),
                "served_model": served_model,
                "created": body.get("created"),
                "finish_reason": choice.get("finish_reason"),
                "usage": body.get("usage"),
                "response_body_sha256": hashlib.sha256(
                    canonical_body.encode("utf-8")
                ).hexdigest(),
                "request_attempt_count": request_attempt_count,
                "transport_retry_count": transport_retry_count,
                "transport_errors": transport_errors,
                "elapsed_seconds": round(
                    max(0.0, perf_counter() - completion_started),
                    4,
                ),
            },
        )


def _server_requests_json_object(messages: list[dict[str, Any]]) -> bool:
    """Recognize only the server-owned terminal write protocol."""

    for message in reversed(messages):
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        protocol = payload.get("canary_turn_protocol")
        if not isinstance(protocol, dict):
            continue
        contract = protocol.get("terminal_response_contract")
        if not isinstance(contract, dict):
            continue
        if (
            protocol.get("final_response_required") is True
            and (
                protocol.get("write_batch_closed") is True
                or protocol.get("briefing_fact_batch_closed") is True
            )
            and contract.get("format") == "json_object"
        ):
            return True
    return False


def _parse_assistant_turn(message: dict[str, Any]) -> _ParsedAssistantTurn:
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        raw_calls = ()
    if not isinstance(raw_calls, (list, tuple)):
        raise MalformedToolCallError("tool_calls must be an array")
    calls: list[NativeToolCall] = []
    audits: list[RawToolCallAudit] = []
    for raw_call in raw_calls:
        try:
            call, audit = _parse_native_tool_call(raw_call)
        except DeepSeekToolCallingError as exc:
            raise _with_accumulated_audit(exc, audits) from exc
        audits.append(audit)
        calls.append(call)
    assistant_message = {
        key: value
        for key, value in message.items()
        if key in {"role", "content", "reasoning_content", "tool_calls"}
    }
    assistant_message["role"] = "assistant"
    if calls and assistant_message.get("content") is None:
        assistant_message["content"] = ""
    return _ParsedAssistantTurn(assistant_message, tuple(calls), tuple(audits))


def _is_retryable_transport_error(error: httpx.HTTPError) -> bool:
    if isinstance(error, httpx.TransportError):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        return status_code in {408, 429} or status_code >= 500
    return False


def _transport_error_audit(
    error: httpx.HTTPError,
    *,
    attempt: int,
    retryable: bool,
) -> dict[str, Any]:
    status_code = (
        error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
    )
    return {
        "attempt": attempt,
        "error_type": type(error).__name__,
        "retryable": retryable,
        "status_code": status_code,
    }


def _validate_completion_protocol(
    completion: _CompletionResponse,
    parsed: _ParsedAssistantTurn,
    *,
    allow_usable_direct_text: bool = False,
    allow_empty_terminal_for_retry: bool = False,
) -> str | None:
    finish_reason = completion.metadata.get("finish_reason")
    if parsed.tool_calls:
        if finish_reason != "tool_calls":
            raise DeepSeekResponseError(
                "DeepSeek finish_reason is inconsistent with native tool calls"
            )
        return None
    if finish_reason != "stop":
        content = parsed.assistant_message.get("content")
        if (
            allow_usable_direct_text
            and finish_reason != "tool_calls"
            and isinstance(content, str)
            and content.strip()
        ):
            return "nonstandard_finish_reason_with_usable_direct_text"
        raise DeepSeekResponseError(
            "DeepSeek response did not reach a complete text termination"
        )
    content = parsed.assistant_message.get("content")
    if not isinstance(content, str) or not content.strip():
        if allow_empty_terminal_for_retry and isinstance(content, str):
            return "empty_terminal_response_for_retry"
        raise DeepSeekResponseError("DeepSeek returned an empty terminal response")
    return None


def _parse_native_tool_call(raw_call: Any) -> tuple[NativeToolCall, RawToolCallAudit]:
    if not isinstance(raw_call, dict):
        raise MalformedToolCallError("tool call must be an object")
    try:
        call_id = raw_call["id"]
        call_type = raw_call["type"]
        function = raw_call["function"]
        name = function["name"]
        raw_arguments = function["arguments"]
    except (KeyError, TypeError) as exc:
        raise MalformedToolCallError("tool call shape is malformed") from exc
    if call_type != "function" or not all(
        isinstance(value, str) and value for value in (call_id, name, raw_arguments)
    ):
        raise MalformedToolCallError("tool call fields are malformed")
    audit = RawToolCallAudit(
        tool_call_id=call_id,
        tool_name=name,
        raw_arguments=raw_arguments,
        arguments_sha256=hashlib.sha256(raw_arguments.encode("utf-8")).hexdigest(),
        parse_status="received",
    )
    try:
        decoded = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MalformedToolCallError(
            "tool arguments are not valid JSON",
            raw_tool_call_audit=(audit,),
        ) from exc
    try:
        validated = validate_tool_arguments(name, decoded)
    except UnknownToolError as exc:
        raise UnknownNativeToolError(
            "DeepSeek returned an unknown tool",
            raw_tool_call_audit=(audit,),
        ) from exc
    except ToolArgumentsValidationError as exc:
        raise InvalidNativeToolArgumentsError(
            "DeepSeek returned invalid tool arguments",
            raw_tool_call_audit=(audit,),
        ) from exc
    return NativeToolCall(call_id, name, validated), RawToolCallAudit(
        tool_call_id=audit.tool_call_id,
        tool_name=audit.tool_name,
        raw_arguments=audit.raw_arguments,
        arguments_sha256=audit.arguments_sha256,
        parse_status="validated",
    )


def _reject_repeated_calls(
    calls: tuple[NativeToolCall, ...],
    *,
    seen_ids: set[str],
    seen_fingerprints: set[str],
    audit: tuple[RawToolCallAudit, ...],
) -> None:
    current_ids: set[str] = set()
    current_fingerprints: set[str] = set()
    for call in calls:
        fingerprint = json.dumps(
            {"name": call.tool_name, "arguments": call.arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if (
            call.tool_call_id in seen_ids
            or call.tool_call_id in current_ids
            or fingerprint in seen_fingerprints
            or fingerprint in current_fingerprints
        ):
            raise RepeatedToolCallError(
                "repeated tool call detected",
                raw_tool_call_audit=audit,
            )
        current_ids.add(call.tool_call_id)
        current_fingerprints.add(fingerprint)
    seen_ids.update(current_ids)
    seen_fingerprints.update(current_fingerprints)


def _tool_result_messages(
    calls: tuple[NativeToolCall, ...],
    receipts: tuple[ToolReceipt, ...],
    *,
    write_batch_closed: bool,
) -> list[dict[str, Any]]:
    turn_protocol = (
        {
            "actual_write": False,
            "execution_mode": ExecutionMode.SHADOW_PROPOSAL.value,
            "final_response_required": True,
            "final_response_source": "safe_user_facts_only",
            "further_tool_calls_allowed": False,
            "same_turn_pending_confirmation_allowed": False,
            "write_batch_closed": True,
        }
        if write_batch_closed
        else None
    )
    return [
        {
            "role": "tool",
            "tool_call_id": call.tool_call_id,
            "content": json.dumps(
                {
                    "safe_user_facts": receipt.safe_user_facts,
                    **({"turn_protocol": turn_protocol} if turn_protocol else {}),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        for call, receipt in zip(calls, receipts, strict=True)
    ]


def _post_write_protocol_message() -> dict[str, str]:
    return {
        "role": "system",
        "content": json.dumps(
            {
                "shadow_turn_protocol": {
                    "actual_write": False,
                    "execution_mode": ExecutionMode.SHADOW_PROPOSAL.value,
                    "final_response_required": True,
                    "final_response_source": "safe_user_facts_only",
                    "further_tool_calls_allowed": False,
                    "native_or_textual_tool_calls_allowed": False,
                    "same_turn_pending_confirmation_allowed": False,
                    "write_batch_closed": True,
                }
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _pre_execution_tool_argument_repair_message() -> dict[str, str]:
    return {
        "role": "system",
        "content": (
            "上一条原生工具调用尚未执行，也没有产生任何写入。其 arguments "
            "不是合法 JSON 或未满足当前工具结构。请重新读取当前用户消息与当前工具 "
            "schema，重新生成一次合法的原生工具调用。所有字符串必须正确 JSON 转义，"
            "所有必填来源凭证必须完整。日报事项的 source_evidence 必须同时填写 "
            "source_message_index 和 exact_quote；exact_quote 必须复制当前用户消息中"
            "表达该事项完整含义的一段连续原文，保留否定、条件、期限和引号。"
            "不要把中文弯引号改成英文双引号。"
            "日报 content 可用冒号保留引述归属，不要把未转义引号放进 JSON 字符串。"
            "不要改用文本描述工具调用，也不要假称已经执行。"
        ),
    }


_DAILY_REPORT_TRANSACTION_TARGETS = frozenset(
    {
        "bound_report",
        "resolved_report",
        "today_report",
        "pending_report",
        "source_and_target_reports",
    }
)


def _daily_weekly_write_review_tool_names(
    calls: tuple[NativeToolCall, ...],
    *,
    context: TrustedContext,
) -> frozenset[str]:
    """Select schemas for one independent report-write semantic review.

    This gate uses only trusted capabilities and registry metadata. It never
    interprets words in the user's message; that judgment remains with the
    reviewer model.
    """

    available_domains = {
        domain
        for name in context.allowed_tool_names
        if (domain := _daily_weekly_review_domain(name)) is not None
    }
    current_reviewed_operations = {
        call.tool_name
        for call in calls
        if _daily_weekly_review_domain(call.tool_name) is not None
        and call.tool_name in context.allowed_tool_names
        and context.gate_decisions.get(call.tool_name) is True
    }
    if len(available_domains) < 2:
        if (
            available_domains == {"daily"}
            and "add_daily_items" in current_reviewed_operations
        ):
            return frozenset({"add_daily_items"})
        return frozenset()
    if not current_reviewed_operations:
        if calls or not _in_daily_weekly_zero_tool_review_window(context):
            return frozenset()

    # Preserve the exact operation vocabulary already selected while exposing
    # a small canonical set that can restore an omitted record domain.
    canonical_capture_tools = {
        name
        for name in (
            "add_daily_items",
            "apply_current_weekly_report",
            "submit_current_weekly_report",
            "apply_next_weekly_plan",
            "submit_next_weekly_plan",
            "record_weekly_plan_items_as_today_work",
        )
        if name in context.allowed_tool_names
        and context.gate_decisions.get(name) is True
    }
    if not current_reviewed_operations:
        canonical_capture_tools.update(
            name
            for name in (
                "query_current_weekly_report",
                "query_next_weekly_plan",
            )
            if name in context.allowed_tool_names
            and context.gate_decisions.get(name) is True
        )
    review_names = current_reviewed_operations | canonical_capture_tools
    review_domains = {
        domain
        for name in review_names
        if (domain := _daily_weekly_review_domain(name)) is not None
    }
    if len(review_domains) < 2:
        return frozenset()
    return frozenset(review_names)


def _in_daily_weekly_zero_tool_review_window(context: TrustedContext) -> bool:
    """Bound zero-draft review to an open report or planning collision window."""

    if context.principal.conversation_kind != "direct":
        return False
    open_roles = {"active_collection", "natural_next"}
    weekly_plan_open = (
        "apply_next_weekly_plan" in context.allowed_tool_names
        and context.gate_decisions.get("apply_next_weekly_plan") is True
        and any(
            weekly.status in {"draft", "collecting", "pending_confirmation"}
            and bool(open_roles.intersection(weekly.roles))
            for weekly in context.all_weekly_plans()
        )
    )
    periodic_report_open = (
        context.current_weekly_report is not None
        and context.current_weekly_report.status == "collecting"
        and context.current_weekly_report.report_type == "weekly"
        and any(
            name in context.allowed_tool_names
            and context.gate_decisions.get(name) is True
            for name in (
                "query_current_weekly_report",
                "apply_current_weekly_report",
                "submit_current_weekly_report",
            )
        )
    )
    if periodic_report_open:
        try:
            local_date = context.now.astimezone(
                ZoneInfo(context.principal.timezone)
            ).date()
            current_period_key, _, _ = period_bounds("weekly", local_date)
        except (KeyError, ValueError):
            periodic_report_open = False
        else:
            periodic_report_open = (
                context.current_weekly_report.period_key == current_period_key
            )
    if not weekly_plan_open and not periodic_report_open:
        return False
    if periodic_report_open:
        return True
    try:
        local_now = context.now.astimezone(ZoneInfo(context.principal.timezone))
    except (KeyError, ValueError):
        return False
    # Friday through Monday is when report writing most often collides with
    # planning. Monday also permits late filling of the current-week plan while
    # a relative "next week" can select another trusted target.
    return local_now.weekday() in {0, 4, 5, 6}


def _daily_weekly_write_domain(tool_name: str) -> str | None:
    definition = TOOL_REGISTRY.get(tool_name)
    if definition is None or definition.read_or_write != "write":
        return None
    if definition.transaction_target_policy == "weekly_plan":
        return "weekly"
    if definition.transaction_target_policy == "periodic_report":
        return "periodic"
    if definition.transaction_target_policy in _DAILY_REPORT_TRANSACTION_TARGETS:
        return "daily"
    return None


def _daily_weekly_review_domain(tool_name: str) -> str | None:
    """Map a registry tool to one reviewed record without reading user text."""

    write_domain = _daily_weekly_write_domain(tool_name)
    if write_domain is not None:
        return write_domain
    if tool_name == "query_current_weekly_report":
        definition = TOOL_REGISTRY.get(tool_name)
        if definition is not None and definition.read_or_write == "read":
            return "periodic"
    if tool_name == "query_next_weekly_plan":
        definition = TOOL_REGISTRY.get(tool_name)
        if definition is not None and definition.read_or_write == "read":
            return "weekly"
    return None


def _daily_weekly_review_domains(
    allowed_tool_names: frozenset[str],
) -> frozenset[str]:
    return frozenset(
        domain
        for tool_name in allowed_tool_names
        if (domain := _daily_weekly_review_domain(tool_name)) is not None
    )


def _review_domain_list(domains: frozenset[str]) -> str:
    labels = tuple(
        label
        for domain, label in (
            ("daily", "the Daily Report"),
            ("periodic", "the Current Weekly Report"),
            ("weekly", "the Weekly Work Plan"),
        )
        if domain in domains
    )
    if not labels:
        raise ValueError("semantic review requires at least one allowed domain")
    if len(labels) == 1:
        return f"{labels[0]} domain"
    return f"{', '.join(labels[:-1])}, and {labels[-1]} domains"


def _daily_weekly_review_domain_policy(domains: frozenset[str]) -> str:
    policies: list[str] = []
    if "daily" in domains:
        policies.append("A Daily Report records one reporting day.")
    if "periodic" in domains:
        policies.append(
            "A Current Weekly Report reviews the current ISO week in "
            "accomplishments, risks, next_plan, and metrics. Use "
            "query_current_weekly_report to open or view it, "
            "apply_current_weekly_report for explicit changes, and "
            "submit_current_weekly_report only for explicit submission."
        )
    if "weekly" in domains:
        policies.append(
            "A Monday-to-Saturday Weekly Work Plan records exact dated "
            "commitments. Trusted weekly_plan_targets may contain a Monday "
            "active_collection target for the current week and a separate "
            "natural_next target for the following week; select the exact "
            "plan_id, version, and dates semantically. A bounded every-day or "
            "weekday-range recurrence must remain a dated plan: emit one add "
            "for every selected exact date with the entire current message "
            "evidence and the same complete recurrence_scope_quote. That scope "
            "quote must include every attached bound, exception, or qualifier; "
            "never cite only the positive 'every day' substring from a restricted "
            "scope. One leading day applies to every clearly parallel "
            "matter in that clause until another date or record scope appears; "
            "do not drop a later matter or turn it into an undated suggestion."
        )
    if {"daily", "weekly"}.issubset(domains):
        policies.append(
            "A standalone bare expression such as 'Friday: do X' is ambiguous "
            "between the current Friday's daily report and a future plan, so "
            "ask a natural clarification instead of guessing. When one sentence "
            "explicitly contrasts current Friday work with 'next Friday', "
            "trusted server time plus that contrast may route the two matters "
            "to daily and weekly records respectively."
        )
    if {"periodic", "weekly"}.issubset(domains):
        policies.append(
            "The Current Weekly Report's next_plan remains inside that report "
            "unless a separate dated Weekly Work Plan request is explicit. Never "
            "route Weekly Report content through weekly-plan tools."
        )
    if len(domains) > 1:
        policies.append(
            "These are independent records. One turn may authorize more than one; "
            "retain every matter in its intended allowed domain and do not force "
            "a domain that is not clear."
        )
    return " ".join(policies)


def _daily_weekly_write_review_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    calls: tuple[NativeToolCall, ...],
    context: TrustedContext,
    allowed_tool_names: frozenset[str],
) -> list[dict[str, str]]:
    ordered_messages = user_messages or (user_text,)
    allowed_domains = _daily_weekly_review_domains(allowed_tool_names)
    daily_only_constraint = (
        "For this Daily-only correction review, the draft's report-date "
        "binding is trusted and immutable. Do not change date_selection, "
        "date_expression, proposed_date, date_evidence, report_id, "
        "expected_version, or submit_after_write. You may correct only items "
        "and explicit-empty-field evidence. Every exact_quote must preserve "
        "the complete meaning of its item, including every negation, condition, "
        "deadline, consequence, exception, and pending action even when separated "
        "by punctuation. Put each independently editable action-object pair in a "
        "separate Daily item; never use one quote to hide two separate matters. "
        "Count the independently editable matters in the current user messages "
        "before producing calls, then ensure the corrected item count covers each "
        "one exactly once. Coordinating wording does not merge different actions "
        "or different objects into one matter. The exact_quote source spans for "
        "different items must not overlap, and each quote must contain only the "
        "one matter persisted by that item. "
        if allowed_domains == {"daily"}
        else ""
    )
    draft_calls = [
        {"tool_name": call.tool_name, "arguments": call.arguments}
        for call in calls
        if _daily_weekly_write_domain(call.tool_name) is not None
    ]
    return [
        {
            "role": "system",
            "content": " ".join(
                (
                "You are the isolated Agent2 semantic reviewer for one unexecuted "
                f"operation draft spanning {_review_domain_list(allowed_domains)}. "
                "Reread every exact current user message independently. Decide meaning "
                "semantically from the whole utterance and trusted context; never use a "
                "keyword, phrase list, regular expression, or the mere presence of a "
                "weekday. "
                f"{_daily_weekly_review_domain_policy(allowed_domains)} "
                "Treat the supplied draft as fallible: it may omit one domain, omit a "
                "matter, or contain the wrong otherwise-valid arguments. The draft has "
                "not executed and has written nothing. If all intended writes and their "
                "dates, fields, actors, conditions, evidence, stable IDs, and versions are "
                "clear, return exactly one complete corrected native tool-call batch using "
                "only the supplied tools. Preserve exact current-message grounding and do "
                f"{daily_only_constraint}"
                "not manufacture completion, certainty, or a formal weekday. If any "
                "material routing or meaning remains ambiguous, return no tool calls and "
                "exactly one JSON object with keys decision and reply, where decision is "
                "clarification and reply is a concise natural Chinese question. "
                "If the original draft had no reviewed operation and the message clearly "
                "needs none of these records, return no tool calls and exactly "
                "one JSON object {\"decision\":\"keep_original\"}; do not reproduce, "
                "rewrite, or evaluate the original conversational answer. "
                "Never use keep_original when a reviewed operation or clarification is "
                "semantically required. "
                "For an execute decision, use native tool_calls only; never put "
                "decision=execute or a tool description in JSON/text content. JSON "
                "content is allowed only for clarification or keep_original. "
                "Never say that anything was saved, submitted, or executed. Do not return a partial "
                "operation batch when clarification is required. Unrelated "
                "calls are outside this review and must not be reproduced.",
                )
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ordered_current_user_messages": [
                        {"sequence": index, "content": content}
                        for index, content in enumerate(ordered_messages, start=1)
                    ],
                    "trusted_context": context.model_payload(),
                    "unexecuted_daily_periodic_weekly_operation_draft": draft_calls,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _validate_daily_weekly_write_review(
    *,
    reviewed: _ParsedAssistantTurn,
    allowed_tool_names: frozenset[str],
    original_has_domain_writes: bool,
) -> None:
    if reviewed.tool_calls:
        if any(
            call.tool_name not in allowed_tool_names
            or _daily_weekly_review_domain(call.tool_name) is None
            for call in reviewed.tool_calls
        ):
            raise ValueError("review returned an out-of-scope tool")
        if original_has_domain_writes and any(
            _daily_weekly_write_domain(call.tool_name) is None
            for call in reviewed.tool_calls
        ):
            raise ValueError("write review returned a read tool")
        return
    content = reviewed.assistant_message.get("content")
    try:
        payload = json.loads(content) if isinstance(content, str) else None
    except json.JSONDecodeError as exc:
        raise ValueError("clarification review must return JSON") from exc
    if (
        not original_has_domain_writes
        and payload == {"decision": "keep_original"}
    ):
        return
    if not isinstance(payload, dict) or set(payload) != {"decision", "reply"}:
        raise ValueError("clarification review has an invalid envelope")
    if payload.get("decision") != "clarification":
        raise ValueError("review without tools must request clarification")
    reply = payload.get("reply")
    if not isinstance(reply, str) or not reply.strip() or len(reply) > 8000:
        raise ValueError("clarification review requires a bounded reply")


def _daily_weekly_review_envelope_repair_messages(
    *,
    raw_content: str,
) -> list[dict[str, str]]:
    """Ask the model to normalize its own clarification without adding meaning."""

    return [
        {
            "role": "system",
            "content": (
                "You are formatting one already-written Agent2 semantic-review "
                "clarification. Do not reinterpret the user, add facts, choose a "
                "record, or call a tool. If the supplied text is a clarification "
                "question, return exactly one JSON object with decision set to "
                "clarification and reply containing that same question. Otherwise "
                "return exactly {\"decision\":\"invalid\"}."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"unformatted_clarification": raw_content},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _merge_daily_weekly_write_review(
    *,
    original: _ParsedAssistantTurn,
    reviewed: _ParsedAssistantTurn,
    reviewed_tool_names: frozenset[str],
) -> _ParsedAssistantTurn:
    if any(call.tool_name == "add_daily_items" for call in original.tool_calls) and any(
        call.tool_name == "add_daily_items" for call in reviewed.tool_calls
    ):
        reviewed = _constrain_daily_write_review(
            original=original,
            reviewed=reviewed,
        )
    if not reviewed.tool_calls:
        payload = json.loads(reviewed.assistant_message["content"])
        if payload.get("decision") == "keep_original":
            return _ParsedAssistantTurn(
                assistant_message=original.assistant_message,
                tool_calls=original.tool_calls,
                audit=(*original.audit, *reviewed.audit),
            )
        return _ParsedAssistantTurn(
            assistant_message={"role": "assistant", "content": payload["reply"]},
            tool_calls=(),
            audit=(*original.audit, *reviewed.audit),
        )

    original_has_reviewed_writes = any(
        _daily_weekly_write_domain(call.tool_name) is not None
        for call in original.tool_calls
    )
    is_review_target = (
        (lambda call: _daily_weekly_write_domain(call.tool_name) is not None)
        if original_has_reviewed_writes
        else (lambda call: _daily_weekly_review_domain(call.tool_name) is not None)
    )
    target_indexes = [
        index
        for index, call in enumerate(original.tool_calls)
        if is_review_target(call)
    ]
    insertion_index = target_indexes[0] if target_indexes else len(original.tool_calls)
    merged: list[NativeToolCall] = []
    for index, call in enumerate(original.tool_calls):
        if index == insertion_index:
            merged.extend(reviewed.tool_calls)
        if not is_review_target(call):
            merged.append(call)
    if insertion_index == len(original.tool_calls):
        merged.extend(reviewed.tool_calls)
    if any(call.tool_name not in reviewed_tool_names for call in reviewed.tool_calls):
        raise ValueError("review used a tool outside its supplied schemas")

    merged_calls = tuple(merged)
    merged_message = {
        key: value
        for key, value in original.assistant_message.items()
        if key in {"role", "content", "reasoning_content"}
    }
    merged_message["role"] = "assistant"
    merged_message["content"] = merged_message.get("content") or ""
    merged_message["tool_calls"] = [
        {
            "id": call.tool_call_id,
            "type": "function",
            "function": {
                "name": call.tool_name,
                "arguments": json.dumps(
                    call.arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        }
        for call in merged_calls
    ]
    return _ParsedAssistantTurn(
        assistant_message=merged_message,
        tool_calls=merged_calls,
        audit=(*original.audit, *reviewed.audit),
    )


def _constrain_daily_write_review(
    *,
    original: _ParsedAssistantTurn,
    reviewed: _ParsedAssistantTurn,
) -> _ParsedAssistantTurn:
    """Keep each Daily draft target while accepting reviewed Daily meaning."""

    if not reviewed.tool_calls:
        return reviewed
    original_daily = tuple(
        call for call in original.tool_calls if call.tool_name == "add_daily_items"
    )
    reviewed_daily = tuple(
        call for call in reviewed.tool_calls if call.tool_name == "add_daily_items"
    )
    if len(original_daily) != 1 or len(reviewed_daily) != 1:
        raise ValueError("Daily review must replace exactly one Daily draft")

    draft_arguments = dict(original_daily[0].arguments)
    reviewed_arguments = reviewed_daily[0].arguments
    reviewed_empty_evidence = reviewed_arguments.get("empty_field_evidence", [])
    if not isinstance(reviewed_empty_evidence, list):
        raise ValueError("reviewed Daily empty-field evidence must be an array")
    draft_arguments["items"] = reviewed_arguments.get("items", [])
    draft_arguments["empty_field_evidence"] = reviewed_empty_evidence
    draft_arguments["acknowledged_empty_fields"] = [
        evidence.get("field")
        for evidence in reviewed_empty_evidence
        if isinstance(evidence, dict)
    ]
    constrained_arguments = validate_tool_arguments(
        "add_daily_items",
        draft_arguments,
    )
    constrained_call = NativeToolCall(
        reviewed_daily[0].tool_call_id,
        "add_daily_items",
        constrained_arguments,
    )
    return replace(
        reviewed,
        tool_calls=tuple(
            constrained_call if call is reviewed_daily[0] else call
            for call in reviewed.tool_calls
        ),
    )


def _daily_weekly_zero_draft_confirmation_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    context: TrustedContext,
    allowed_tool_names: frozenset[str],
) -> list[dict[str, str]]:
    ordered_messages = user_messages or (user_text,)
    allowed_domains = _daily_weekly_review_domains(allowed_tool_names)
    return [
        {
            "role": "system",
            "content": " ".join(
                (
                "You are a second isolated Agent2 reviewer. A main model returned "
                "no tool calls, while another reviewer proposed an operation in "
                f"{_review_domain_list(allowed_domains)}. You are not shown either "
                "prior answer. "
                "Independently reread the exact current user messages and trusted context. "
                "Only if the user clearly and presently authorizes the operation, return "
                "one complete native tool-call batch using the supplied tools. Preserve every "
                "matter, domain, date, actor, qualifier, exact source evidence, stable ID, "
                "and version. Do not infer from keywords or a weekday alone. "
                f"{_daily_weekly_review_domain_policy(allowed_domains)} "
                "For an execute decision, use native tool_calls only; never put "
                "decision=execute or a tool description in JSON/text content. "
                "If any operation or routing is ambiguous, "
                "return no tools and a concise clarification JSON. Do not claim execution.",
                )
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ordered_current_user_messages": [
                        {"sequence": index, "content": content}
                        for index, content in enumerate(ordered_messages, start=1)
                    ],
                    "trusted_context": context.model_payload(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _validate_daily_weekly_zero_draft_agreement(
    *,
    first: tuple[NativeToolCall, ...],
    second: tuple[NativeToolCall, ...],
    allowed_tool_names: frozenset[str],
) -> None:
    if not first or not second:
        raise ValueError("both reviewers must return writes")
    for calls in (first, second):
        if any(
            call.tool_name not in allowed_tool_names
            or _daily_weekly_review_domain(call.tool_name) is None
            for call in calls
        ):
            raise ValueError("review confirmation returned an out-of-scope tool")

    def semantic_payload(calls: tuple[NativeToolCall, ...]) -> list[dict[str, Any]]:
        return [
            {
                "tool_name": call.tool_name,
                "arguments": _semantic_review_arguments(call),
            }
            for call in calls
        ]

    if semantic_payload(first) != semantic_payload(second):
        raise ValueError("independent review batches differ")


def _zero_draft_disagreement_kind(
    *,
    first: tuple[NativeToolCall, ...],
    second: tuple[NativeToolCall, ...],
    context: TrustedContext,
) -> str | None:
    """Classify the two safe ambiguity shapes that may become a clarification."""

    if context.principal.conversation_kind != "direct" or not first:
        return None
    if _zero_draft_disagreement_is_daily_vs_weekly(first=first, second=second):
        return "daily_vs_weekly"
    if (
        len(first) != len(second)
        or len({plan.target_week_start for plan in context.all_weekly_plans()}) < 2
    ):
        return None

    selected_different_week = False
    for first_call, second_call in zip(first, second, strict=True):
        if first_call.tool_name != second_call.tool_name:
            return None
        if first_call.tool_name not in {
            "apply_next_weekly_plan",
            "submit_next_weekly_plan",
        }:
            if first_call.arguments != second_call.arguments:
                return None
            continue

        first_normalized = _normalize_week_target_arguments(
            call=first_call,
            context=context,
        )
        second_normalized = _normalize_week_target_arguments(
            call=second_call,
            context=context,
        )
        if first_normalized is None or second_normalized is None:
            return None
        first_payload, first_week_start = first_normalized
        second_payload, second_week_start = second_normalized
        if first_payload != second_payload:
            return None
        if first_week_start != second_week_start:
            selected_different_week = True

    return "weekly_target" if selected_different_week else None


def _zero_draft_disagreement_is_daily_vs_weekly(
    *,
    first: tuple[NativeToolCall, ...],
    second: tuple[NativeToolCall, ...],
) -> bool:
    """Recognize one current-message matter routed to two different records."""

    if len(first) != 1 or len(second) != 1:
        return False
    by_name = {first[0].tool_name: first[0], second[0].tool_name: second[0]}
    if set(by_name) != {"add_daily_items", "apply_next_weekly_plan"}:
        return False

    daily_items = by_name["add_daily_items"].arguments.get("items")
    weekly_operations = by_name["apply_next_weekly_plan"].arguments.get("operations")
    if (
        not isinstance(daily_items, list)
        or len(daily_items) != 1
        or not isinstance(weekly_operations, list)
        or len(weekly_operations) != 1
    ):
        return False
    daily_item = daily_items[0]
    weekly_operation = weekly_operations[0]
    if not isinstance(daily_item, dict) or not isinstance(weekly_operation, dict):
        return False
    if daily_item.get("field") != "today_work" or weekly_operation.get(
        "operation"
    ) != "add":
        return False
    daily_evidence = daily_item.get("source_evidence")
    weekly_evidence = weekly_operation.get("source_evidence")
    daily_content = daily_item.get("content")
    weekly_content = weekly_operation.get("content")
    weekly_clause = (
        weekly_evidence.get("exact_clause_quote")
        if isinstance(weekly_evidence, dict)
        else None
    )
    return (
        isinstance(daily_evidence, dict)
        and isinstance(weekly_evidence, dict)
        and daily_evidence.get("source_message_index")
        == weekly_evidence.get("source_message_index")
        and isinstance(daily_content, str)
        and bool(daily_content.strip())
        and isinstance(weekly_content, str)
        and bool(weekly_content.strip())
        and isinstance(weekly_clause, str)
        and daily_content.strip() in weekly_clause
        and weekly_content.strip() in weekly_clause
    )


def _semantic_review_arguments(call: NativeToolCall) -> dict[str, Any]:
    """Remove reviewer-local labels while preserving every business fact."""

    arguments = json.loads(
        json.dumps(
            call.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if call.tool_name not in {
        "apply_current_weekly_report",
        "apply_next_weekly_plan",
    }:
        return arguments
    operations = arguments.get("operations")
    if not isinstance(operations, list):
        return arguments
    for index, operation in enumerate(operations):
        if isinstance(operation, dict) and "operation_id" in operation:
            operation["operation_id"] = f"<review-operation-{index}>"
    return arguments


def _normalize_week_target_arguments(
    *,
    call: NativeToolCall,
    context: TrustedContext,
) -> tuple[dict[str, Any], Any] | None:
    plan_id = call.arguments.get("plan_id")
    if not isinstance(plan_id, str):
        return None
    target = context.weekly_plan_by_id(plan_id)
    if target is None or call.arguments.get("expected_version") != target.version:
        return None

    normalized = dict(call.arguments)
    normalized["plan_id"] = "<trusted-weekly-plan>"
    normalized["expected_version"] = "<trusted-version>"
    operations = normalized.get("operations")
    if operations is not None:
        if not isinstance(operations, list):
            return None
        normalized_operations: list[dict[str, Any]] = []
        for operation in operations:
            if not isinstance(operation, dict):
                return None
            normalized_operation = dict(operation)
            raw_plan_date = normalized_operation.pop("plan_date", None)
            if not isinstance(raw_plan_date, str):
                return None
            try:
                plan_date = date.fromisoformat(raw_plan_date)
            except ValueError:
                return None
            day_offset = (plan_date - target.target_week_start).days
            if day_offset not in range(6):
                return None
            normalized_operation["plan_day_offset"] = day_offset
            normalized_operations.append(normalized_operation)
        normalized["operations"] = normalized_operations
    return normalized, target.target_week_start


def _weekly_target_disagreement_clarification_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    context: TrustedContext,
    clarification_kind: str,
) -> list[dict[str, str]]:
    ordered_messages = user_messages or (user_text,)
    if clarification_kind == "weekly_target":
        conflict = "different trusted Weekly Work Plan target weeks"
        choices = "本周 and 下周"
        safety_fact = (
            "independent reviewers selected different trusted target weeks; "
            "zero operations executed"
        )
    elif clarification_kind == "daily_vs_weekly":
        conflict = "the Daily Report and the dated Weekly Work Plan"
        choices = "今日日报 and 下周工作计划"
        safety_fact = (
            "independent reviewers selected different record types for the same "
            "current-message matter; zero operations executed"
        )
    else:
        raise ValueError("unknown zero-draft disagreement clarification kind")
    return [
        {
            "role": "system",
            "content": (
                "You are the final Agent2 clarification writer. Two independent safety "
                f"reviewers selected {conflict} for the same current-message matter. "
                "No tool ran and nothing was written. Do not choose a record or call a "
                f"tool. Ask one concise, natural Chinese question that contrasts {choices}. Return exactly "
                "one JSON object with decision set to clarification and reply containing "
                "the question. Do not claim that anything was saved or executed."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ordered_current_user_messages": [
                        {"sequence": index, "content": content}
                        for index, content in enumerate(ordered_messages, start=1)
                    ],
                    "trusted_weekly_plan_targets": context.model_payload().get(
                        "weekly_plan_targets",
                        [],
                    ),
                    "server_safety_fact": safety_fact,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _validate_week_target_disagreement_clarification(
    reviewed: _ParsedAssistantTurn,
    *,
    clarification_kind: str,
) -> None:
    _validate_daily_weekly_write_review(
        reviewed=reviewed,
        allowed_tool_names=frozenset(),
        original_has_domain_writes=True,
    )
    payload = json.loads(reviewed.assistant_message["content"])
    reply = payload["reply"]
    required_labels = (
        ("本周", "下周")
        if clarification_kind == "weekly_target"
        else ("今日日报", "下周工作计划")
    )
    if any(label not in reply for label in required_labels):
        raise ValueError(
            "record clarification must name every trusted choice"
        )


def _needs_daily_submit_section_review(
    calls: tuple[NativeToolCall, ...],
    *,
    context: TrustedContext,
) -> bool:
    """Request model review when an atomic submit draft does not cover all sections."""

    return bool(_daily_submit_section_review_targets(calls, context=context))


def _daily_submit_section_review_targets(
    calls: tuple[NativeToolCall, ...],
    *,
    context: TrustedContext,
) -> tuple[NativeToolCall, ...]:
    """Select only incomplete atomic daily submissions; preserve every other intent."""

    return tuple(
        call for call in calls if _is_incomplete_daily_submit(call, context=context)
    )


def _is_incomplete_daily_submit(
    call: NativeToolCall,
    *,
    context: TrustedContext,
) -> bool:
    """Check structural section coverage without interpreting user language."""

    if call.tool_name != "add_daily_items":
        return False
    arguments = call.arguments
    if not bool(arguments.get("submit_after_write", False)):
        return False
    return bool(_missing_daily_submit_sections(call, context=context))


def _missing_daily_submit_sections(
    call: NativeToolCall,
    *,
    context: TrustedContext,
) -> tuple[str, ...]:
    all_sections = ("today_work", "problems", "tomorrow_plan")
    covered_sections = {
        str(item.get("field") or "")
        for item in call.arguments.get("items", ())
        if isinstance(item, dict)
    }
    covered_sections.update(
        str(field_name)
        for field_name in call.arguments.get(
            "acknowledged_empty_fields",
            (),
        )
    )
    if call.arguments.get("date_selection") == "trusted_report":
        raw_report_id = call.arguments.get("report_id")
        try:
            report_id = UUID(str(raw_report_id))
        except (TypeError, ValueError):
            report_id = None
        report = context.report_by_id(report_id) if report_id is not None else None
        if (
            report is not None
            and call.arguments.get("expected_version") == report.version
        ):
            covered_sections.update(item.field for item in report.items)
            covered_sections.update(report.acknowledged_empty_fields)
    return tuple(section for section in all_sections if section not in covered_sections)


def _is_complete_daily_submit_section_review(
    calls: tuple[NativeToolCall, ...],
    *,
    expected_count: int,
    context: TrustedContext,
) -> bool:
    return len(calls) == expected_count and all(
        call.tool_name == "add_daily_items"
        and bool(call.arguments.get("submit_after_write", False))
        and not _missing_daily_submit_sections(call, context=context)
        for call in calls
    )


def _daily_submit_section_review_feedback(
    calls: tuple[NativeToolCall, ...],
    *,
    expected_count: int,
    context: TrustedContext,
) -> dict[str, Any]:
    """Describe only structural validation failures for one bounded model retry."""

    return {
        "expected_call_count": expected_count,
        "received_call_count": len(calls),
        "calls": [
            {
                "sequence": index,
                "tool_name": call.tool_name,
                "submit_after_write": bool(
                    call.arguments.get("submit_after_write", False)
                ),
                "missing_sections": list(
                    _missing_daily_submit_sections(call, context=context)
                ),
            }
            for index, call in enumerate(calls, start=1)
        ],
        "instruction": (
            "The previous independent review remained structurally incomplete. "
            "Reread the exact user messages and return a complete corrected "
            "submission. Do not infer meaning from this validation record."
        ),
    }


def _merge_daily_submit_section_review(
    *,
    original: _ParsedAssistantTurn,
    reviewed: _ParsedAssistantTurn,
    context: TrustedContext,
) -> _ParsedAssistantTurn:
    """Replace only reviewed daily submissions and keep unrelated model calls intact."""

    return _merge_reviewed_calls(
        original=original,
        targets=_daily_submit_section_review_targets(
            original.tool_calls,
            context=context,
        ),
        replacements=reviewed.tool_calls,
        assistant_message=reviewed.assistant_message,
        review_audit=reviewed.audit,
    )


def _merge_reviewed_calls(
    *,
    original: _ParsedAssistantTurn,
    targets: tuple[NativeToolCall, ...],
    replacements: tuple[NativeToolCall, ...],
    assistant_message: dict[str, Any],
    review_audit: tuple[RawToolCallAudit, ...],
) -> _ParsedAssistantTurn:
    if len(targets) != len(replacements):
        raise ValueError("review targets and replacements must have equal length")
    target_object_ids = {id(call) for call in targets}
    replacement_iterator = iter(replacements)
    merged_calls = tuple(
        next(replacement_iterator) if id(call) in target_object_ids else call
        for call in original.tool_calls
    )
    # The reviewer may replace an unexecuted tool draft, but it is not a new
    # conversational authority. Preserve the main Agent2 turn (especially its
    # reasoning_content) so the next tool-loop request continues from the
    # user's original semantic decision rather than from an isolated review.
    # The reviewer message remains available in model-turn audit only.
    _ = assistant_message
    merged_message = {
        key: value
        for key, value in original.assistant_message.items()
        if key in {"role", "content", "reasoning_content"}
    }
    merged_message["role"] = "assistant"
    merged_message["content"] = merged_message.get("content") or ""
    merged_message["tool_calls"] = [
        {
            "id": call.tool_call_id,
            "type": "function",
            "function": {
                "name": call.tool_name,
                "arguments": json.dumps(
                    call.arguments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        }
        for call in merged_calls
    ]
    return _ParsedAssistantTurn(
        assistant_message=merged_message,
        tool_calls=merged_calls,
        audit=(*original.audit, *review_audit),
    )


def _daily_submit_section_review_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    calls: tuple[NativeToolCall, ...],
    structural_feedback: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    draft_calls = [
        {
            "tool_name": call.tool_name,
            "arguments": call.arguments,
        }
        for call in calls
    ]
    ordered_messages = user_messages or (user_text,)
    return [
        {
            "role": "system",
            "content": (
                "You are the isolated Agent2 semantic reviewer for one "
                "unexecuted daily-report submission draft. Reread the exact "
                "user messages independently; do not inherit the draft's "
                "wording or field split. Treat today_work, problems, and "
                "tomorrow_plan as three separate semantic sections, including "
                "ordinary conversational assertions that a section has no "
                "content. A clear assertion that no current problem or risk "
                "exists is an explicit empty problems section even when it "
                "appears beside work content. It must not be stored inside a "
                "work item. Distinguish that from denial of a work event. "
                "This is semantic judgment, never phrase or keyword matching. "
                "Preserve all actors, facts, dates, plans, and source-evidence "
                "bindings. Return only one complete corrected native tool-call "
                "batch using the supplied tools. The draft has not executed."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "ordered_current_user_messages": [
                        {"sequence": index, "content": content}
                        for index, content in enumerate(
                            ordered_messages,
                            start=1,
                        )
                    ],
                    "unexecuted_draft_calls": draft_calls,
                    "previous_review_structural_feedback": structural_feedback,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _canary_tool_result_messages(
    calls: tuple[NativeToolCall, ...],
    receipts: tuple[ToolReceipt, ...],
    *,
    write_batch_closed: bool,
) -> list[dict[str, Any]]:
    turn_protocol = (
        {
            "actual_write": any(receipt.changed for receipt in receipts),
            "execution_mode": ExecutionMode.CANARY_EXECUTE.value,
            "final_response_required": True,
            "final_response_source": "safe_user_facts_only",
            "terminal_response_contract": write_reply_protocol(receipts),
            "further_tool_calls_allowed": False,
            "same_turn_pending_confirmation_allowed": False,
            "write_batch_closed": True,
        }
        if write_batch_closed
        else None
    )
    return [
        {
            "role": "tool",
            "tool_call_id": call.tool_call_id,
            "content": json.dumps(
                {
                    "safe_user_facts": (
                        model_safe_user_facts(receipt)
                        if write_batch_closed
                        else receipt.safe_user_facts
                    ),
                    **({"turn_protocol": turn_protocol} if turn_protocol else {}),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        for call, receipt in zip(calls, receipts, strict=True)
    ]


def _canary_post_write_protocol_message(
    receipts: tuple[ToolReceipt, ...],
) -> dict[str, str]:
    return {
        "role": "system",
        "content": json.dumps(
            {
                "canary_turn_protocol": {
                    "execution_mode": ExecutionMode.CANARY_EXECUTE.value,
                    "final_response_required": True,
                    "final_response_source": "safe_user_facts_only",
                    "terminal_response_contract": write_reply_protocol(receipts),
                    "further_tool_calls_allowed": False,
                    "native_or_textual_tool_calls_allowed": False,
                    "same_turn_pending_confirmation_allowed": False,
                    "write_batch_closed": True,
                }
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _with_accumulated_audit(
    error: DeepSeekToolCallingError,
    previous: list[RawToolCallAudit],
) -> DeepSeekToolCallingError:
    return type(error)(
        str(error),
        raw_tool_call_audit=tuple(previous) + error.raw_tool_call_audit,
        model_call_count=error.model_call_count,
        turn_plan=error.turn_plan,
        model_turns=error.model_turns,
        request_attempt_count=error.request_attempt_count,
        transport_retry_count=error.transport_retry_count,
        transport_errors=error.transport_errors,
        model_elapsed_seconds=error.model_elapsed_seconds,
    )


def _with_canary_turn_state(
    error: DeepSeekToolCallingError,
    *,
    audits: list[RawToolCallAudit],
    model_turns: list[ModelTurnAudit],
) -> DeepSeekToolCallingError:
    failed_completion_count = int(
        error.request_attempt_count > 0 and not error.model_turns
    )
    return type(error)(
        str(error),
        raw_tool_call_audit=tuple(audits) + error.raw_tool_call_audit,
        model_call_count=max(
            error.model_call_count,
            len(model_turns) + failed_completion_count,
        ),
        model_turns=tuple(model_turns) + error.model_turns,
        request_attempt_count=sum(
            int(turn.response_metadata.get("request_attempt_count", 1))
            for turn in model_turns
        )
        + error.request_attempt_count,
        transport_retry_count=sum(
            int(turn.response_metadata.get("transport_retry_count", 0))
            for turn in model_turns
        )
        + error.transport_retry_count,
        transport_errors=tuple(
            transport_error
            for turn in model_turns
            for transport_error in turn.response_metadata.get("transport_errors", ())
        )
        + error.transport_errors,
        model_elapsed_seconds=round(
            _model_turn_elapsed_seconds(model_turns) + error.model_elapsed_seconds,
            4,
        ),
    )


def _with_turn_state(
    error: DeepSeekToolCallingError,
    audits: list[RawToolCallAudit],
    model_call_count: int,
    context: TrustedContext,
    plans: list[TurnExecutionPlan],
    model_turns: list[ModelTurnAudit],
) -> DeepSeekToolCallingError:
    prior_transport_errors = tuple(
        transport_error
        for turn in model_turns
        for transport_error in turn.response_metadata.get("transport_errors", ())
    )
    return type(error)(
        str(error),
        raw_tool_call_audit=tuple(audits) + error.raw_tool_call_audit,
        model_call_count=model_call_count,
        turn_plan=merge_turn_plans(context, tuple(plans)),
        model_turns=tuple(model_turns) + error.model_turns,
        request_attempt_count=sum(
            int(turn.response_metadata.get("request_attempt_count", 1))
            for turn in model_turns
        )
        + error.request_attempt_count,
        transport_retry_count=sum(
            int(turn.response_metadata.get("transport_retry_count", 0))
            for turn in model_turns
        )
        + error.transport_retry_count,
        transport_errors=prior_transport_errors + error.transport_errors,
        model_elapsed_seconds=round(
            _model_turn_elapsed_seconds(model_turns) + error.model_elapsed_seconds,
            4,
        ),
    )


def _model_turn_elapsed_seconds(
    model_turns: list[ModelTurnAudit],
) -> float:
    elapsed = 0.0
    for turn in model_turns:
        try:
            elapsed += max(
                0.0,
                float(turn.response_metadata.get("elapsed_seconds") or 0.0),
            )
        except (TypeError, ValueError):
            continue
    return elapsed


def _model_turn_audit(
    iteration: int,
    message: dict[str, Any],
    *,
    response_metadata: dict[str, Any],
) -> ModelTurnAudit:
    canonical = json.dumps(
        message,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    reasoning = message.get("reasoning_content")
    reasoning_text = reasoning if isinstance(reasoning, str) else None
    raw_without_reasoning = {
        key: value for key, value in message.items() if key != "reasoning_content"
    }
    return ModelTurnAudit(
        iteration=iteration,
        raw_assistant_message=json.loads(
            json.dumps(raw_without_reasoning, ensure_ascii=False)
        ),
        assistant_message_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        reasoning_content_summary=(
            f"present:{len(reasoning_text)}_chars"
            if reasoning_text is not None
            else "absent"
        ),
        reasoning_content_sha256=(
            hashlib.sha256(reasoning_text.encode("utf-8")).hexdigest()
            if reasoning_text is not None
            else None
        ),
        response_metadata=json.loads(json.dumps(response_metadata, ensure_ascii=False)),
    )


def _assert_shadow_plan(
    plan: TurnExecutionPlan,
    context: TrustedContext,
    *,
    expected_receipt_count: int,
) -> None:
    if type(plan) is not TurnExecutionPlan:
        raise ShadowCapabilityViolationError(
            "ShadowRuntime returned an invalid plan type"
        )
    if len(plan.receipts) != expected_receipt_count:
        raise ShadowCapabilityViolationError(
            "ShadowRuntime returned a receipt count mismatch"
        )
    if (
        plan.mode != ExecutionMode.SHADOW_PROPOSAL
        or plan.namespace != context.namespace
        or plan.actual_write
        or plan.business_handler_call_count
        or plan.pending_write_count
        or plan.conversation_state_write_count
        or plan.message_send_count
    ):
        raise ShadowCapabilityViolationError(
            "ShadowRuntime violated zero-write plan invariants"
        )
    for receipt in plan.receipts:
        if (
            not isinstance(receipt, ToolReceipt)
            or receipt.execution_mode != ExecutionMode.SHADOW_PROPOSAL
            or receipt.changed
            or receipt.before_version != receipt.after_version
            or receipt.safe_user_facts.get("actual_write") is not False
        ):
            raise ShadowCapabilityViolationError(
                "ShadowRuntime violated zero-write Receipt invariants"
            )
