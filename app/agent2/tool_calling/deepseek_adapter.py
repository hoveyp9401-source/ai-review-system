from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any

import httpx

from app.agent2.tool_calling.context import TrustedContext
from app.agent2.tool_calling.contracts import ExecutionMode, ToolReceipt
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
        raw_tool_call_audit: tuple["RawToolCallAudit", ...] = (),
        model_call_count: int = 0,
        turn_plan: TurnExecutionPlan | None = None,
        model_turns: tuple["ModelTurnAudit", ...] = (),
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
        if type(runtime) is not ShadowRuntime or runtime.mode != ExecutionMode.SHADOW_PROPOSAL:
            raise TypeError("run_shadow_turn requires the exact zero-write ShadowRuntime capability")
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
                        if (
                            write_batch_seen
                            or managed_daily_reply_retry_count
                        )
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
                    _assert_shadow_plan(turn_plan, context, expected_receipt_count=len(turn_plan.tool_calls))
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
            or getattr(runtime_session, "mode", None)
            != ExecutionMode.CANARY_EXECUTE
        ):
            raise TypeError(
                "run_canary_turn requires a Canary context and runtime session"
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
        managed_daily_reply_retry_count = 0
        write_reply_retry_count = 0
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
                                or managed_daily_reply_retry_count
                                or write_reply_retry_count
                            )
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
                    protocol_warning = _validate_completion_protocol(
                        completion,
                        parsed,
                        allow_usable_direct_text=True,
                    )
                    if protocol_warning is not None:
                        model_turns[-1] = replace(
                            model_turns[-1],
                            response_metadata={
                                **model_turns[-1].response_metadata,
                                "protocol_warning": protocol_warning,
                            },
                        )
                except DeepSeekToolCallingError as exc:
                    raise _with_canary_turn_state(
                        exc,
                        audits=audits,
                        model_turns=model_turns,
                    ) from exc

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
                        marker in content
                        for marker in _TEXTUAL_TOOL_PROTOCOL_MARKERS
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
                    if write_batch_seen:
                        envelope, write_validation_errors = (
                            validate_write_reply(content, tuple(receipts))
                        )
                        if envelope is not None:
                            reply_for_validation = envelope.reply
                    managed_validation_errors = (
                        validate_managed_daily_reply(
                            reply_for_validation,
                            tuple(receipts),
                        )
                        if not write_validation_errors
                        else ()
                    )
                    validation_errors = (
                        *write_validation_errors,
                        *managed_validation_errors,
                    )
                    if validation_errors:
                        if write_batch_seen:
                            if write_reply_retry_count == 0:
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
                        content,
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
                            int(
                                item.response_metadata.get(
                                    "request_attempt_count", 1
                                )
                            )
                            for item in model_turns
                        ),
                        transport_retry_count=sum(
                            int(
                                item.response_metadata.get(
                                    "transport_retry_count", 0
                                )
                            )
                            for item in model_turns
                        ),
                    )

                if tool_loops >= self._max_tool_loops:
                    raise _with_canary_turn_state(
                        MaxToolLoopsExceeded(
                            "maximum tool-call loops exceeded"
                        ),
                        audits=audits,
                        model_turns=model_turns,
                    )
                if managed_daily_reply_retry_count or write_reply_retry_count:
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

                current_has_write = any(
                    TOOL_REGISTRY[call.tool_name].read_or_write == "write"
                    for call in parsed.tool_calls
                )
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
                        _canary_post_write_protocol_message(
                            tuple(receipts)
                        )
                    )
                model_turns[-1] = replace(
                    model_turns[-1],
                    tool_results=tuple(tool_results),
                )
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
                "served_model": body.get("model"),
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
            and protocol.get("write_batch_closed") is True
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
        error.response.status_code
        if isinstance(error, httpx.HTTPStatusError)
        else None
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
                    **(
                        {"turn_protocol": turn_protocol}
                        if turn_protocol
                        else {}
                    ),
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
                    "terminal_response_contract": write_reply_protocol(
                        receipts
                    ),
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
            for transport_error in turn.response_metadata.get(
                "transport_errors", ()
            )
        )
        + error.transport_errors,
        model_elapsed_seconds=round(
            _model_turn_elapsed_seconds(model_turns)
            + error.model_elapsed_seconds,
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
            _model_turn_elapsed_seconds(model_turns)
            + error.model_elapsed_seconds,
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
                float(
                    turn.response_metadata.get("elapsed_seconds")
                    or 0.0
                ),
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
        response_metadata=json.loads(
            json.dumps(response_metadata, ensure_ascii=False)
        ),
    )


def _assert_shadow_plan(
    plan: TurnExecutionPlan,
    context: TrustedContext,
    *,
    expected_receipt_count: int,
) -> None:
    if type(plan) is not TurnExecutionPlan:
        raise ShadowCapabilityViolationError("ShadowRuntime returned an invalid plan type")
    if len(plan.receipts) != expected_receipt_count:
        raise ShadowCapabilityViolationError("ShadowRuntime returned a receipt count mismatch")
    if (
        plan.mode != ExecutionMode.SHADOW_PROPOSAL
        or plan.namespace != context.namespace
        or plan.actual_write
        or plan.business_handler_call_count
        or plan.pending_write_count
        or plan.conversation_state_write_count
        or plan.message_send_count
    ):
        raise ShadowCapabilityViolationError("ShadowRuntime violated zero-write plan invariants")
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
