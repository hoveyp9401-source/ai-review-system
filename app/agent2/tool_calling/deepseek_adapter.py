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
from pydantic import ValidationError

from app.agent2.report_domain import period_bounds
from app.agent2.tool_calling import completed_daily_follow_through
from app.agent2.tool_calling.context import TrustedContext
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.current_turn_source import (
    CurrentTurnSource,
    CurrentTurnSourceEvidenceError,
)
from app.agent2.tool_calling.daily_add_model_contract import (
    compile_focused_daily_plan_arguments,
    compile_model_add_daily_items,
    focused_daily_plan_parameters_schema,
    focused_daily_review_parameters_schema,
    focused_daily_submission_evidence,
    parse_focused_daily_add_decision,
    parse_focused_daily_review_arguments,
    parse_focused_daily_review_decision,
)
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
from app.agent2.tool_calling.reporting_date import default_daily_write_date
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
    complete_write_reply_retry_envelope,
    model_safe_user_facts,
    validate_write_reply,
    write_reply_protocol,
    write_reply_retry_messages,
)

_TEXTUAL_TOOL_PROTOCOL_MARKERS = (
    "<｜DSML｜tool_calls",
    "<｜DSML｜invoke",
    "<｜tool▁calls▁begin｜>",
    "<｜tool▁call▁begin｜>",
    "<|tool_calls",
)

_FOCUSED_DAILY_PLAN_TOOL_NAME = "plan_daily_report"
_FOCUSED_DAILY_REVIEW_TOOL_NAME = "review_daily_plan"


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


def _assert_no_trusted_daily_identifier_leak(
    reply: str,
    receipts: tuple[ToolReceipt, ...],
) -> None:
    """Compatibility seam for focused adapter regressions."""

    completed_daily_follow_through._assert_no_trusted_daily_identifier_leak(
        reply,
        receipts,
        error_factory=DeepSeekResponseError,
    )


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
    focused_success_reply: str | None = None
    focused_submission_evidence: dict[str, Any] | None = None


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
        focused_semantic_repair_count = 0
        incomplete_confirm_review_count = 0
        daily_submit_section_review_count = 0
        daily_weekly_write_review_count = 0
        bounded_daily_probe_pending = _should_run_bounded_daily_probe(
            user_text=user_text,
            user_messages=user_messages,
            context=context,
            thinking_enabled=thinking_enabled,
        )
        bounded_daily_turn_active = False
        bounded_daily_reply_composer_active = False
        bounded_daily_success_reply: str | None = None
        prefetched_terminal_completion: _CompletionResponse | None = None
        completed_daily_flow = completed_daily_follow_through.CompletedDailyFollowThrough(
            context=context,
            user_text=user_text,
            user_messages=user_messages,
        )
        model_call_budget = completed_daily_follow_through.TurnModelCallBudget(limit=9)
        tool_schemas = deepseek_tool_schemas(context.allowed_tool_names)

        async def complete_model(
            completion_messages: list[dict[str, Any]],
            *,
            tool_schemas: list[dict[str, Any]],
            thinking_enabled: bool,
        ) -> _CompletionResponse:
            try:
                model_call_budget.claim()
            except completed_daily_follow_through.ModelCallBudgetExceeded as exc:
                raise DeepSeekResponseError(
                    "turn model call budget exceeded"
                ) from exc
            return await self._complete(
                completion_messages,
                tool_schemas=tool_schemas,
                thinking_enabled=thinking_enabled,
            )

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

        async def review_zero_write_terminal_reply(
            candidate_reply: str,
            *,
            write_domains: tuple[str, ...],
        ) -> str:
            """Run one bounded semantic review, plus one review of any replacement."""

            nonlocal iterations
            replacement_review_started = False
            try:
                review_completion = await complete_model(
                    _zero_tool_write_invitation_review_messages(
                        user_text=user_text,
                        user_messages=user_messages,
                        candidate_reply=candidate_reply,
                        context=context,
                        write_domains=write_domains,
                    ),
                    tool_schemas=[],
                    thinking_enabled=not bounded_daily_turn_active,
                )
                iterations += 1
                model_turns.append(
                    _model_turn_audit(
                        iterations,
                        review_completion.message,
                        response_metadata={
                            **review_completion.metadata,
                            "zero_tool_write_invitation_review": True,
                        },
                    )
                )
                reviewed_reply = _parse_assistant_turn(
                    review_completion.message
                )
                _validate_completion_protocol(
                    review_completion,
                    reviewed_reply,
                )
                audits.extend(reviewed_reply.audit)
                if reviewed_reply.tool_calls:
                    raise ValueError(
                        "write invitation review cannot call tools"
                    )
                review_content = reviewed_reply.assistant_message.get(
                    "content"
                )
                if not isinstance(review_content, str):
                    raise ValueError(
                        "write invitation review requires text"
                    )
                reviewed_content = _apply_zero_tool_write_invitation_review(
                    review_content=review_content,
                    candidate_reply=candidate_reply,
                    context=context,
                )
                if reviewed_content == candidate_reply:
                    return reviewed_content

                replacement_review_started = True
                replacement_completion = await complete_model(
                    _zero_tool_write_invitation_review_messages(
                        user_text=user_text,
                        user_messages=user_messages,
                        candidate_reply=reviewed_content,
                        context=context,
                        write_domains=write_domains,
                    ),
                    tool_schemas=[],
                    thinking_enabled=not bounded_daily_turn_active,
                )
                iterations += 1
                model_turns.append(
                    _model_turn_audit(
                        iterations,
                        replacement_completion.message,
                        response_metadata={
                            **replacement_completion.metadata,
                            "zero_tool_write_invitation_replacement_review": True,
                        },
                    )
                )
                replacement_review = _parse_assistant_turn(
                    replacement_completion.message
                )
                _validate_completion_protocol(
                    replacement_completion,
                    replacement_review,
                )
                audits.extend(replacement_review.audit)
                if replacement_review.tool_calls:
                    raise ValueError(
                        "replacement safety review cannot call tools"
                    )
                replacement_review_content = (
                    replacement_review.assistant_message.get("content")
                )
                if not isinstance(replacement_review_content, str):
                    raise ValueError(
                        "replacement safety review requires text"
                    )
                independently_reviewed = (
                    _apply_zero_tool_write_invitation_review(
                        review_content=replacement_review_content,
                        candidate_reply=reviewed_content,
                        context=context,
                    )
                )
                if independently_reviewed != reviewed_content:
                    raise ValueError(
                        "replacement safety review must keep the candidate"
                    )
                return reviewed_content
            except (DeepSeekToolCallingError, ValueError) as exc:
                message = (
                    "zero-tool replacement safety review failed"
                    if replacement_review_started
                    else "zero-tool write invitation review failed"
                )
                raise _with_canary_turn_state(
                    DeepSeekResponseError(message),
                    audits=audits,
                    model_turns=model_turns,
                ) from exc

        async def review_daily_content_follow_through(
            candidate_reply: str,
            *,
            allowed_write_tool_names: frozenset[str],
            query_state: str,
            pending_write_review: bool = False,
        ) -> str:
            """Ask one isolated model whether a dated-report write was dropped."""

            nonlocal iterations
            try:
                review_completion = await complete_model(
                    completed_daily_flow.review_messages(
                        candidate_reply=candidate_reply,
                        receipts=tuple(receipts),
                        allowed_write_tool_names=allowed_write_tool_names,
                        query_state=query_state,
                        pending_write_review=pending_write_review,
                    ),
                    tool_schemas=[],
                    thinking_enabled=not bounded_daily_turn_active,
                )
                iterations += 1
                model_turns.append(
                    _model_turn_audit(
                        iterations,
                        review_completion.message,
                        response_metadata={
                            **review_completion.metadata,
                            "daily_content_follow_through_review": True,
                        },
                    )
                )
                reviewed = _parse_assistant_turn(review_completion.message)
                _validate_completion_protocol(review_completion, reviewed)
                audits.extend(reviewed.audit)
                if reviewed.tool_calls:
                    raise ValueError(
                        "daily content follow-through review cannot call tools"
                    )
                review_content = reviewed.assistant_message.get("content")
                if not isinstance(review_content, str):
                    raise TypeError(
                        "daily content follow-through review requires text"
                    )
                decision = completed_daily_flow.parse_review(
                    review_content=review_content,
                    candidate_reply=candidate_reply,
                )
                model_turns[-1] = replace(
                    model_turns[-1],
                    response_metadata={
                        **model_turns[-1].response_metadata,
                        "daily_content_follow_through_decision": decision,
                    },
                )
                return decision
            except (DeepSeekToolCallingError, TypeError, ValueError) as exc:
                raise _with_canary_turn_state(
                    DeepSeekResponseError("daily content follow-through review failed"),
                    audits=audits,
                    model_turns=model_turns,
                ) from exc

        async def adjudicate_dropped_daily_adds(
            *,
            original: _ParsedAssistantTurn,
            reviewed: _ParsedAssistantTurn,
        ) -> _ParsedAssistantTurn:
            """Optionally restore a dropped Daily add without invalidating reviewed work."""

            nonlocal iterations
            original_daily_adds = tuple(
                call
                for call in original.tool_calls
                if call.tool_name == "add_daily_items"
            )
            reviewed_daily_adds = tuple(
                call
                for call in reviewed.tool_calls
                if call.tool_name == "add_daily_items"
            )
            if not original_daily_adds or reviewed_daily_adds:
                return reviewed
            if len(original_daily_adds) != 1:
                if not reviewed.tool_calls:
                    return reviewed
                raise _with_canary_turn_state(
                    DeepSeekResponseError(
                        "multiple Daily add drafts cannot enter adjudication"
                    ),
                    audits=audits,
                    model_turns=model_turns,
                )

            try:
                adjudication_completion = await complete_model(
                    _dropped_daily_add_adjudication_messages(
                        user_text=user_text,
                        user_messages=user_messages,
                        context=context,
                    ),
                    tool_schemas=deepseek_tool_schemas(
                        frozenset({"add_daily_items"})
                    ),
                    thinking_enabled=not bounded_daily_turn_active,
                )
                iterations += 1
                model_turns.append(
                    _model_turn_audit(
                        iterations,
                        adjudication_completion.message,
                        response_metadata={
                            **adjudication_completion.metadata,
                            "dropped_daily_add_independent_adjudication": True,
                            "draft_executed": False,
                        },
                    )
                )
                adjudicated = _parse_assistant_turn(
                    adjudication_completion.message
                )
                _validate_completion_protocol(
                    adjudication_completion,
                    adjudicated,
                )
                audits.extend(adjudicated.audit)
                _validate_daily_weekly_write_review(
                    reviewed=adjudicated,
                    allowed_tool_names=frozenset({"add_daily_items"}),
                    original_has_domain_writes=True,
                )
            except DeepSeekToolCallingError as exc:
                if reviewed.tool_calls and model_turns:
                    model_turns[-1] = replace(
                        model_turns[-1],
                        response_metadata={
                            **model_turns[-1].response_metadata,
                            "dropped_daily_add_adjudication_fell_back": True,
                            "dropped_daily_add_adjudication_error": type(exc).__name__,
                        },
                    )
                    return reviewed
                raise _with_canary_turn_state(
                    exc,
                    audits=audits,
                    model_turns=model_turns,
                ) from exc
            except ValueError as exc:
                if reviewed.tool_calls and model_turns:
                    model_turns[-1] = replace(
                        model_turns[-1],
                        response_metadata={
                            **model_turns[-1].response_metadata,
                            "dropped_daily_add_adjudication_fell_back": True,
                            "dropped_daily_add_adjudication_error": type(exc).__name__,
                        },
                    )
                    return reviewed
                raise _with_canary_turn_state(
                    DeepSeekResponseError(
                        "dropped Daily add adjudication returned an invalid decision"
                    ),
                    audits=audits,
                    model_turns=model_turns,
                ) from exc

            if not adjudicated.tool_calls:
                return reviewed if reviewed.tool_calls else adjudicated
            try:
                return _restore_dropped_daily_adds(
                    original=original,
                    reviewed=reviewed,
                    adjudicated=adjudicated,
                )
            except ValueError as exc:
                if model_turns:
                    model_turns[-1] = replace(
                        model_turns[-1],
                        response_metadata={
                            **model_turns[-1].response_metadata,
                            "dropped_daily_add_adjudication_fell_back": True,
                            "dropped_daily_add_adjudication_error": type(exc).__name__,
                        },
                    )
                return reviewed

        async def review_weekly_reclassification(
            *,
            original: _ParsedAssistantTurn,
            reviewed: _ParsedAssistantTurn,
            allowed_tool_names: frozenset[str],
        ) -> _ParsedAssistantTurn:
            """Focus once when a fallible Daily draft was reclassified as Weekly."""

            nonlocal iterations
            original_domains = {
                domain
                for call in original.tool_calls
                if (domain := _daily_weekly_write_domain(call.tool_name))
                is not None
            }
            reviewed_domains = {
                domain
                for call in reviewed.tool_calls
                if (domain := _daily_weekly_write_domain(call.tool_name))
                is not None
            }
            if original_domains != {"daily"} or reviewed_domains != {"weekly"}:
                return reviewed
            weekly_tool_names = frozenset(
                name
                for name in allowed_tool_names
                if _daily_weekly_review_domain(name) == "weekly"
            )
            if not weekly_tool_names:
                return reviewed
            review_messages = _daily_weekly_write_review_messages(
                user_text=user_text,
                user_messages=user_messages,
                calls=reviewed.tool_calls,
                context=context,
                trusted_completed_daily_query_results=[],
                selected_targets=[],
                allowed_tool_names=weekly_tool_names,
            )
            for attempt in range(1, 3):
                try:
                    completion = await complete_model(
                        review_messages,
                        tool_schemas=deepseek_tool_schemas(
                            weekly_tool_names
                        ),
                        thinking_enabled=False,
                    )
                    iterations += 1
                    model_turns.append(
                        _model_turn_audit(
                            iterations,
                            completion.message,
                            response_metadata={
                                **completion.metadata,
                                "weekly_reclassification_focused_review": True,
                                "weekly_reclassification_review_attempt": attempt,
                                "draft_executed": False,
                            },
                        )
                    )
                    focused = _parse_assistant_turn(completion.message)
                    _validate_completion_protocol(completion, focused)
                    audits.extend(focused.audit)
                    _validate_daily_weekly_write_review(
                        reviewed=focused,
                        allowed_tool_names=weekly_tool_names,
                        original_has_domain_writes=True,
                    )
                    if not any(
                        call.tool_name == "apply_next_weekly_plan"
                        for call in focused.tool_calls
                    ):
                        raise ValueError(
                            "weekly reclassification review dropped the apply call"
                        )
                    return _constrain_weekly_plan_review_submission(
                        original=reviewed,
                        reviewed=focused,
                    )
                except (DeepSeekToolCallingError, ValueError) as exc:
                    if model_turns:
                        model_turns[-1] = replace(
                            model_turns[-1],
                            response_metadata={
                                **model_turns[-1].response_metadata,
                                "weekly_reclassification_review_retry": attempt == 1,
                                "weekly_reclassification_review_error": type(exc).__name__,
                            },
                        )
                    if attempt == 1:
                        continue
            if model_turns:
                model_turns[-1] = replace(
                    model_turns[-1],
                    response_metadata={
                        **model_turns[-1].response_metadata,
                        "weekly_reclassification_review_fell_back": True,
                    },
                )
            return reviewed

        try:
            while True:
                iterations += 1
                try:
                    completion_messages = (
                        _bounded_daily_probe_messages(
                            user_text=user_text,
                            user_messages=user_messages,
                            context=context,
                        )
                        if bounded_daily_probe_pending
                        else messages
                    )
                    completion_tool_schemas = (
                        _focused_daily_plan_tool_schemas()
                        if bounded_daily_probe_pending
                        else completed_daily_flow.prepare_model_tools(
                            tool_schemas,
                            tools_disabled=(
                                write_batch_seen
                                or briefing_fact_batch_seen
                                or daily_briefing_reply_retry_count
                                or managed_daily_reply_retry_count
                                or write_reply_retry_count
                            ),
                        )
                    )
                    if prefetched_terminal_completion is not None:
                        completion = prefetched_terminal_completion
                        prefetched_terminal_completion = None
                    else:
                        completion = await complete_model(
                            completion_messages,
                            tool_schemas=completion_tool_schemas,
                            thinking_enabled=(
                                thinking_enabled
                                if bounded_daily_probe_pending
                                else (
                                    thinking_enabled
                                    or briefing_fact_batch_seen
                                )
                                and not bounded_daily_turn_active
                            ),
                        )
                    model_turns.append(
                        _model_turn_audit(
                            iterations,
                            completion.message,
                            response_metadata={
                                **completion.metadata,
                                "system_prompt_sha256": (
                                    hashlib.sha256(
                                        str(
                                            completion_messages[0].get(
                                                "content",
                                                "",
                                            )
                                        ).encode("utf-8")
                                    ).hexdigest()
                                ),
                                "bounded_daily_probe": (
                                    bounded_daily_probe_pending
                                ),
                                **(
                                    {
                                        "production_system_prompt_sha256": (
                                            system_prompt_sha256
                                        )
                                    }
                                    if bounded_daily_probe_pending
                                    else {}
                                ),
                            },
                        )
                    )
                    parsed = (
                        _parse_focused_daily_completion(
                            completion.message,
                            call_scope="probe",
                        )
                        if bounded_daily_probe_pending
                        else _parse_assistant_turn(completion.message)
                    )
                    audits.extend(parsed.audit)
                    if bounded_daily_probe_pending:
                        bounded_daily_success_reply = (
                            parsed.focused_success_reply
                        )
                    protocol_warning = (
                        _validate_focused_daily_completion_protocol(
                            completion
                        )
                        if bounded_daily_probe_pending
                        else _validate_completion_protocol(
                            completion,
                            parsed,
                            allow_usable_direct_text=True,
                            allow_empty_terminal_for_retry=(
                                (
                                    briefing_fact_batch_seen
                                    and daily_briefing_reply_retry_count == 0
                                )
                                or (
                                    write_batch_seen
                                    and write_reply_retry_count < 2
                                )
                            ),
                        )
                    )
                    if bounded_daily_probe_pending:
                        _validate_focused_daily_source(
                            parsed,
                            user_text=user_text,
                            user_messages=user_messages,
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
                    daily_repair_succeeded = False
                    if (
                        tool_argument_repair_count == 0
                        and not write_batch_seen
                        and not briefing_fact_batch_seen
                        and completed_daily_flow.allows_argument_repair()
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
                        if (
                            bounded_daily_probe_pending
                            and _only_daily_add_argument_error(exc)
                        ):
                            try:
                                repair_completion = await complete_model(
                                    _compact_daily_add_argument_repair_messages(
                                        user_text=user_text,
                                        user_messages=user_messages,
                                        context=context,
                                        previous_decision=(
                                            exc.raw_tool_call_audit[-1].raw_arguments
                                            if exc.raw_tool_call_audit
                                            else ""
                                        ),
                                        validation_error=str(exc),
                                    ),
                                    tool_schemas=_focused_daily_plan_tool_schemas(),
                                    thinking_enabled=(
                                        "focused Daily source validation failed"
                                        not in str(exc)
                                        and completion.metadata.get(
                                            "finish_reason"
                                        )
                                        != "length"
                                    ),
                                )
                                iterations += 1
                                model_turns.append(
                                    _model_turn_audit(
                                        iterations,
                                        repair_completion.message,
                                        response_metadata={
                                            **repair_completion.metadata,
                                            "compact_daily_add_argument_repair": True,
                                            "draft_executed": False,
                                        },
                                    )
                                )
                                parsed = _parse_focused_daily_completion(
                                    repair_completion.message,
                                    call_scope="repair",
                                )
                                _validate_focused_daily_completion_protocol(
                                    repair_completion
                                )
                                _validate_focused_daily_source(
                                    parsed,
                                    user_text=user_text,
                                    user_messages=user_messages,
                                )
                                audits.extend(parsed.audit)
                                bounded_daily_success_reply = (
                                    parsed.focused_success_reply
                                )
                                if (
                                    len(parsed.tool_calls) != 1
                                    or parsed.tool_calls[0].tool_name
                                    != "add_daily_items"
                                ):
                                    raise ValueError(
                                        "compact Daily repair requires one complete "
                                        "add call"
                                    )
                            except DeepSeekToolCallingError as repair_exc:
                                raise _with_canary_turn_state(
                                    repair_exc,
                                    audits=audits,
                                    model_turns=model_turns,
                                ) from repair_exc
                            except ValueError as repair_exc:
                                raise _with_canary_turn_state(
                                    DeepSeekResponseError(
                                        "compact Daily argument repair failed"
                                    ),
                                    audits=audits,
                                    model_turns=model_turns,
                                ) from repair_exc
                            tool_argument_repair_count += 1
                            bounded_daily_probe_pending = False
                            bounded_daily_turn_active = True
                            daily_repair_succeeded = True
                        else:
                            messages.append(
                                _pre_execution_tool_argument_repair_message()
                            )
                            tool_argument_repair_count += 1
                            bounded_daily_probe_pending = False
                            continue
                    if not daily_repair_succeeded:
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

                if bounded_daily_probe_pending:
                    bounded_daily_probe_pending = False
                    if any(
                        call.tool_name == "add_daily_items"
                        for call in parsed.tool_calls
                    ):
                        bounded_daily_turn_active = True
                        model_turns[-1] = replace(
                            model_turns[-1],
                            response_metadata={
                                **model_turns[-1].response_metadata,
                                "bounded_daily_probe_accepted": True,
                            },
                        )
                    else:
                        bounded_daily_success_reply = None
                        model_turns[-1] = replace(
                            model_turns[-1],
                            response_metadata={
                                **model_turns[-1].response_metadata,
                                "bounded_daily_probe_fell_back": True,
                            },
                        )
                        continue

                memory_daily_focus_tools = _memory_daily_focus_review_tool_names(
                    parsed.tool_calls,
                    context=context,
                )
                if memory_daily_focus_tools:
                    focus_reviewed: _ParsedAssistantTurn | None = None
                    try:
                        focus_completion = await complete_model(
                            _memory_daily_focus_review_messages(
                                user_text=user_text,
                                user_messages=user_messages,
                                context=context,
                                calls=parsed.tool_calls,
                            ),
                            tool_schemas=deepseek_tool_schemas(
                                memory_daily_focus_tools
                            ),
                            thinking_enabled=not bounded_daily_turn_active,
                        )
                        iterations += 1
                        model_turns.append(
                            _model_turn_audit(
                                iterations,
                                focus_completion.message,
                                response_metadata={
                                    **focus_completion.metadata,
                                    "personal_memory_daily_focus_review": True,
                                    "draft_executed": False,
                                },
                            )
                        )
                        focus_reviewed = _parse_assistant_turn(
                            focus_completion.message
                        )
                        _validate_completion_protocol(
                            focus_completion,
                            focus_reviewed,
                        )
                        audits.extend(focus_reviewed.audit)
                        _validate_memory_daily_focus_review(
                            original=parsed,
                            reviewed=focus_reviewed,
                            allowed_tool_names=memory_daily_focus_tools,
                        )
                        parsed = focus_reviewed
                    except DeepSeekToolCallingError as exc:
                        raise _with_canary_turn_state(
                            exc,
                            audits=audits,
                            model_turns=model_turns,
                        ) from exc
                    except ValueError as exc:
                        focus_content = (
                            focus_reviewed.assistant_message.get("content")
                            if focus_reviewed is not None
                            else None
                        )
                        if (
                            focus_reviewed is None
                            or focus_reviewed.tool_calls
                            or not isinstance(focus_content, str)
                            or not focus_content.strip()
                        ):
                            raise _with_canary_turn_state(
                                DeepSeekResponseError(
                                    "personal-memory and Daily focus review failed"
                                ),
                                audits=audits,
                                model_turns=model_turns,
                            ) from exc
                        recovery_tools = frozenset(
                            memory_daily_focus_tools
                            - _PERSONAL_MEMORY_WRITE_TOOLS
                        )
                        try:
                            recovery_completion = await complete_model(
                                _memory_daily_focus_recovery_messages(
                                    user_text=user_text,
                                    user_messages=user_messages,
                                    context=context,
                                ),
                                tool_schemas=deepseek_tool_schemas(
                                    recovery_tools
                                ),
                                thinking_enabled=not bounded_daily_turn_active,
                            )
                            iterations += 1
                            model_turns.append(
                                _model_turn_audit(
                                    iterations,
                                    recovery_completion.message,
                                    response_metadata={
                                        **recovery_completion.metadata,
                                        "personal_memory_daily_focus_recovery": True,
                                        "draft_executed": False,
                                    },
                                )
                            )
                            recovered = _parse_assistant_turn(
                                recovery_completion.message
                            )
                            _validate_completion_protocol(
                                recovery_completion,
                                recovered,
                            )
                            audits.extend(recovered.audit)
                            _validate_memory_daily_focus_recovery(
                                reviewed=recovered,
                                allowed_tool_names=recovery_tools,
                            )
                            parsed = recovered
                        except (DeepSeekToolCallingError, ValueError) as recovery_exc:
                            raise _with_canary_turn_state(
                                DeepSeekResponseError(
                                    "personal-memory and Daily focus recovery failed"
                                ),
                                audits=audits,
                                model_turns=model_turns,
                            ) from recovery_exc

                default_daily_weekly_review_tool_names = (
                    _daily_weekly_write_review_tool_names(
                        parsed.tool_calls,
                        context=context,
                    )
                )
                try:
                    completed_daily_tool_inspection = (
                        completed_daily_flow.inspect_tool_turn(
                            calls=parsed.tool_calls,
                            reviewed_calls=tuple(
                                call
                                for call in parsed.tool_calls
                                if _daily_weekly_write_domain(call.tool_name)
                                is not None
                            ),
                            receipts=tuple(receipts),
                            default_review_tool_names=(
                                default_daily_weekly_review_tool_names
                            ),
                        )
                    )
                except ValueError as exc:
                    raise _with_canary_turn_state(
                        DeepSeekResponseError(
                            "Daily content draft does not match the trusted completed query"
                        ),
                        audits=audits,
                        model_turns=model_turns,
                    ) from exc
                if completed_daily_tool_inspection.integrity_error is not None:
                    raise _with_canary_turn_state(
                        DeepSeekResponseError(
                            completed_daily_tool_inspection.integrity_error
                        ),
                        audits=audits,
                        model_turns=model_turns,
                    )
                daily_weekly_review_tool_names = (
                    completed_daily_tool_inspection.review_tool_names
                )
                trusted_daily_selected_targets = (
                    completed_daily_tool_inspection.selected_targets
                )
                if (
                    (
                        daily_weekly_write_review_count == 0
                        or (
                            completed_daily_tool_inspection.repeat_write_review
                            and not write_batch_seen
                        )
                    )
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
                    focused_daily_review = (
                        bounded_daily_turn_active
                        and daily_weekly_review_tool_names
                        == frozenset({"add_daily_items"})
                    )
                    focused_weekly_review = (
                        {
                            domain
                            for call in parsed.tool_calls
                            if (
                                domain := _daily_weekly_write_domain(
                                    call.tool_name
                                )
                            )
                            is not None
                        }
                        == {"weekly"}
                    )
                    review_messages = (
                        _bounded_daily_add_review_messages(
                            user_text=user_text,
                            user_messages=user_messages,
                            context=context,
                            calls=parsed.tool_calls,
                            submission_evidence=(
                                parsed.focused_submission_evidence
                            ),
                        )
                        if focused_daily_review
                        else _daily_weekly_write_review_messages(
                            user_text=user_text,
                            user_messages=user_messages,
                            calls=parsed.tool_calls,
                            context=context,
                            trusted_completed_daily_query_results=(
                                completed_daily_tool_inspection.trusted_query_results
                            ),
                            selected_targets=trusted_daily_selected_targets,
                            allowed_tool_names=daily_weekly_review_tool_names,
                        )
                    )
                    bounded_daily_review_fallback = False
                    try:
                        review_completion = await complete_model(
                            review_messages,
                            tool_schemas=(
                                _focused_daily_review_tool_schemas()
                                if focused_daily_review
                                else deepseek_tool_schemas(
                                    daily_weekly_review_tool_names
                                )
                            ),
                            thinking_enabled=(
                                True
                                if focused_daily_review
                                else (
                                    False
                                    if focused_weekly_review
                                    else not bounded_daily_turn_active
                                )
                            ),
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
                        if (
                            focused_daily_review
                            and _focused_daily_review_needs_fast_retry(
                                review_completion
                            )
                        ):
                            review_completion = await complete_model(
                                review_messages,
                                tool_schemas=(
                                    _focused_daily_review_tool_schemas()
                                ),
                                thinking_enabled=False,
                            )
                            iterations += 1
                            model_turns.append(
                                _model_turn_audit(
                                    iterations,
                                    review_completion.message,
                                    response_metadata={
                                        **review_completion.metadata,
                                        "focused_daily_review_fast_retry": True,
                                        "draft_executed": False,
                                    },
                                )
                            )
                        if focused_daily_review:
                            focused_review_decision, focused_review_reason = (
                                _parse_focused_daily_review_completion(
                                    review_completion
                                )
                            )
                            if focused_review_decision == "approve":
                                reviewed = parsed
                            elif focused_review_decision == "repair":
                                if focused_semantic_repair_count != 0:
                                    raise DeepSeekResponseError(
                                        "focused Daily repair budget exhausted"
                                    )
                                try:
                                    semantic_repair_completion = await complete_model(
                                        _compact_daily_add_argument_repair_messages(
                                            user_text=user_text,
                                            user_messages=user_messages,
                                            context=context,
                                            previous_decision=json.dumps(
                                                {
                                                    "candidate": (
                                                        _focused_daily_review_candidate(
                                                            parsed.tool_calls,
                                                            submission_evidence=(
                                                                parsed.focused_submission_evidence
                                                            ),
                                                        )
                                                    ),
                                                    "reply": (
                                                        bounded_daily_success_reply
                                                    ),
                                                },
                                                ensure_ascii=False,
                                                sort_keys=True,
                                                separators=(",", ":"),
                                            ),
                                            validation_error=(
                                                "focused semantic review: "
                                                + str(focused_review_reason or "repair")
                                            ),
                                        ),
                                        tool_schemas=(
                                            _focused_daily_plan_tool_schemas()
                                        ),
                                        thinking_enabled=True,
                                    )
                                    iterations += 1
                                    model_turns.append(
                                        _model_turn_audit(
                                            iterations,
                                            semantic_repair_completion.message,
                                            response_metadata={
                                                **semantic_repair_completion.metadata,
                                                "focused_daily_semantic_repair": True,
                                                "draft_executed": False,
                                            },
                                        )
                                    )
                                    reviewed = _parse_focused_daily_completion(
                                        semantic_repair_completion.message,
                                        call_scope="semantic-repair",
                                    )
                                    _validate_focused_daily_completion_protocol(
                                        semantic_repair_completion
                                    )
                                    _validate_focused_daily_source(
                                        reviewed,
                                        user_text=user_text,
                                        user_messages=user_messages,
                                    )
                                    audits.extend(reviewed.audit)
                                    if (
                                        len(reviewed.tool_calls) != 1
                                        or reviewed.tool_calls[0].tool_name
                                        != "add_daily_items"
                                    ):
                                        raise ValueError(
                                            "focused semantic repair requires one "
                                            "complete Daily add"
                                        )
                                    bounded_daily_success_reply = (
                                        reviewed.focused_success_reply
                                    )
                                    focused_semantic_repair_count += 1
                                    repair_review_completion = await complete_model(
                                        _bounded_daily_add_review_messages(
                                            user_text=user_text,
                                            user_messages=user_messages,
                                            context=context,
                                            calls=reviewed.tool_calls,
                                            submission_evidence=(
                                                reviewed.focused_submission_evidence
                                            ),
                                        ),
                                        tool_schemas=(
                                            _focused_daily_review_tool_schemas()
                                        ),
                                        thinking_enabled=False,
                                    )
                                    iterations += 1
                                    model_turns.append(
                                        _model_turn_audit(
                                            iterations,
                                            repair_review_completion.message,
                                            response_metadata={
                                                **repair_review_completion.metadata,
                                                "focused_daily_semantic_repair_review": True,
                                                "draft_executed": False,
                                            },
                                        )
                                    )
                                    repair_review_decision, _ = (
                                        _parse_focused_daily_review_completion(
                                            repair_review_completion
                                        )
                                    )
                                    if (
                                        repair_review_decision != "approve"
                                    ):
                                        raise ValueError(
                                            "focused semantic repair did not pass "
                                            "independent review"
                                        )
                                except DeepSeekToolCallingError as repair_exc:
                                    audits.extend(repair_exc.raw_tool_call_audit)
                                    raise DeepSeekResponseError(
                                        "focused Daily semantic repair failed"
                                    ) from repair_exc
                                except ValueError as repair_exc:
                                    raise DeepSeekResponseError(
                                        "focused Daily semantic repair failed"
                                    ) from repair_exc
                            else:
                                reviewed = _ParsedAssistantTurn(
                                    assistant_message={
                                        "role": "assistant",
                                        "content": json.dumps(
                                            {"decision": "not_daily"}
                                        ),
                                    },
                                    tool_calls=(),
                                    audit=(),
                                )
                        else:
                            reviewed = _parse_assistant_turn(
                                review_completion.message,
                                allow_review_arguments_envelope=True,
                            )
                            _validate_completion_protocol(
                                review_completion,
                                reviewed,
                            )
                            audits.extend(reviewed.audit)
                        bounded_daily_review_fallback = (
                            bounded_daily_turn_active
                            and _is_bounded_daily_not_daily_review(reviewed)
                        )
                        daily_add_review_drop = (
                            any(
                                call.tool_name == "add_daily_items"
                                for call in parsed.tool_calls
                            )
                            and _is_keep_original_review(reviewed)
                        )
                        if (
                            not bounded_daily_review_fallback
                            and not daily_add_review_drop
                        ):
                            _validate_daily_weekly_write_review(
                                reviewed=reviewed,
                                allowed_tool_names=daily_weekly_review_tool_names,
                                original_has_domain_writes=any(
                                    _daily_weekly_write_domain(call.tool_name)
                                    is not None
                                    for call in parsed.tool_calls
                                ),
                            )
                    except InvalidNativeToolArgumentsError as exc:
                        invalid_review_tools = {
                            item.tool_name for item in exc.raw_tool_call_audit
                        }
                        add_argument_fields = set(
                            TOOL_REGISTRY["add_daily_items"].input_model.model_fields
                        )
                        retryable_arguments = True
                        for item in exc.raw_tool_call_audit:
                            try:
                                decoded_arguments = json.loads(item.raw_arguments)
                            except (json.JSONDecodeError, TypeError):
                                retryable_arguments = False
                                break
                            if not isinstance(decoded_arguments, dict):
                                retryable_arguments = False
                                break
                            if set(decoded_arguments) == {"arguments"}:
                                decoded_arguments = decoded_arguments.get("arguments")
                            elif set(decoded_arguments) == {"tool_name", "arguments"}:
                                if decoded_arguments.get("tool_name") != item.tool_name:
                                    retryable_arguments = False
                                    break
                                decoded_arguments = decoded_arguments.get("arguments")
                            if (
                                not isinstance(decoded_arguments, dict)
                                or not set(decoded_arguments).issubset(add_argument_fields)
                            ):
                                retryable_arguments = False
                                break
                        if (
                            invalid_review_tools != {"add_daily_items"}
                            or not retryable_arguments
                        ):
                            raise _with_canary_turn_state(
                                exc,
                                audits=audits,
                                model_turns=model_turns,
                            ) from exc
                        audits.extend(exc.raw_tool_call_audit)
                        model_turns[-1] = replace(
                            model_turns[-1],
                            response_metadata={
                                **model_turns[-1].response_metadata,
                                "daily_weekly_write_review_invalid_arguments": True,
                                "draft_executed": False,
                            },
                        )
                        try:
                            retry_completion = await complete_model(
                                [
                                    *review_messages,
                                    {
                                        "role": "user",
                                        "content": (
                                            "The previous Daily review returned invalid arguments. "
                                            "Perform one fresh complete independent review from the "
                                            "trusted input above. Return either one schema-valid allowed "
                                            "tool batch or the exact no-tool review decision envelope."
                                        ),
                                    },
                                ],
                                tool_schemas=deepseek_tool_schemas(
                                    daily_weekly_review_tool_names
                                ),
                                thinking_enabled=not bounded_daily_turn_active,
                            )
                            iterations += 1
                            model_turns.append(
                                _model_turn_audit(
                                    iterations,
                                    retry_completion.message,
                                    response_metadata={
                                        **retry_completion.metadata,
                                        "daily_weekly_write_semantic_review_retry": True,
                                    },
                                )
                            )
                            reviewed = _parse_assistant_turn(
                                retry_completion.message,
                                allow_review_arguments_envelope=True,
                            )
                            _validate_completion_protocol(
                                retry_completion,
                                reviewed,
                            )
                            audits.extend(reviewed.audit)
                            bounded_daily_review_fallback = (
                                bounded_daily_turn_active
                                and _is_bounded_daily_not_daily_review(
                                    reviewed
                                )
                            )
                            if not bounded_daily_review_fallback:
                                _validate_daily_weekly_write_review(
                                    reviewed=reviewed,
                                    allowed_tool_names=(
                                        daily_weekly_review_tool_names
                                    ),
                                    original_has_domain_writes=any(
                                        _daily_weekly_write_domain(
                                            call.tool_name
                                        )
                                        is not None
                                        for call in parsed.tool_calls
                                    ),
                                )
                        except (DeepSeekToolCallingError, ValueError) as retry_exc:
                            raise _with_canary_turn_state(
                                DeepSeekResponseError(
                                    "daily and weekly write semantic review retry returned an invalid replacement"
                                ),
                                audits=audits,
                                model_turns=model_turns,
                            ) from retry_exc
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
                                repair_completion = await complete_model(
                                    _daily_weekly_review_envelope_repair_messages(
                                        raw_content=review_content,
                                    ),
                                    tool_schemas=[],
                                    thinking_enabled=not bounded_daily_turn_active,
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
                    if bounded_daily_review_fallback:
                        model_turns[-1] = replace(
                            model_turns[-1],
                            response_metadata={
                                **model_turns[-1].response_metadata,
                                "bounded_daily_review_fell_back": True,
                                "draft_executed": False,
                            },
                        )
                        bounded_daily_turn_active = False
                        bounded_daily_success_reply = None
                        continue
                    reviewed = await adjudicate_dropped_daily_adds(
                        original=parsed,
                        reviewed=reviewed,
                    )
                    reviewed = await review_weekly_reclassification(
                        original=parsed,
                        reviewed=reviewed,
                        allowed_tool_names=daily_weekly_review_tool_names,
                    )
                    completed_daily_flow.record_write_review(
                        reviewed,
                        receipts=tuple(receipts),
                    )
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
                            confirmation_completion = await complete_model(
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
                                thinking_enabled=not bounded_daily_turn_active,
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
                            no_write_quorum = bool(
                                not parsed.tool_calls
                                and reviewed.tool_calls
                                and not confirmed.tool_calls
                            )
                            if no_write_quorum:
                                reviewed = confirmed
                                model_turns[-1] = replace(
                                    model_turns[-1],
                                    response_metadata={
                                        **model_turns[-1].response_metadata,
                                        "daily_weekly_zero_draft_no_write_quorum": True,
                                        "draft_executed": False,
                                    },
                                )
                            else:
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
                                    clarification_completion = await complete_model(
                                        _weekly_target_disagreement_clarification_messages(
                                            user_text=user_text,
                                            user_messages=user_messages,
                                            context=context,
                                            clarification_kind=clarification_kind,
                                        ),
                                        tool_schemas=[],
                                        thinking_enabled=not bounded_daily_turn_active,
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
                    try:
                        parsed = _merge_daily_weekly_write_review(
                            original=parsed,
                            reviewed=reviewed,
                            reviewed_tool_names=(
                                daily_weekly_review_tool_names
                            ),
                            context=context,
                        )
                        parsed = _mark_reviewed_periodic_report_content(parsed)
                        parsed = _mark_reviewed_weekly_plan_content(parsed)
                        if bounded_daily_turn_active:
                            parsed = _mark_reviewed_daily_content(parsed)
                    except ValueError as exc:
                        if not bounded_daily_turn_active:
                            raise
                        model_turns[-1] = replace(
                            model_turns[-1],
                            response_metadata={
                                **model_turns[-1].response_metadata,
                                "bounded_daily_review_merge_fell_back": True,
                                "bounded_daily_review_merge_error": str(exc),
                                "draft_executed": False,
                            },
                        )
                        bounded_daily_turn_active = False
                        bounded_daily_success_reply = None
                        daily_weekly_write_review_count = 0
                        tool_argument_repair_count = 0
                        completed_daily_flow = (
                            completed_daily_follow_through.CompletedDailyFollowThrough(
                                context=context,
                                user_text=user_text,
                                user_messages=user_messages,
                            )
                        )
                        continue

                completed_daily_tool_turn_error = (
                    completed_daily_flow.reviewed_tool_turn_error(
                        parsed.tool_calls,
                        write_batch_seen=write_batch_seen,
                    )
                )
                if completed_daily_tool_turn_error is not None:
                        raise _with_canary_turn_state(
                            DeepSeekResponseError(
                                completed_daily_tool_turn_error
                            ),
                            audits=audits,
                            model_turns=model_turns,
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
                        validation_content = (
                            complete_write_reply_retry_envelope(
                                content,
                                tuple(receipts),
                            )
                            if write_reply_retry_count
                            or bounded_daily_reply_composer_active
                            else content
                        )
                        envelope, write_validation_errors = validate_write_reply(
                            validation_content, tuple(receipts)
                        )
                        if envelope is not None:
                            content = validation_content
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
                                messages = write_reply_retry_messages(
                                    errors=tuple(validation_errors),
                                    receipts=tuple(receipts),
                                    retry_number=write_reply_retry_count + 1,
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

                    terminal_content = (
                        reply_for_validation
                        if briefing_envelope is not None
                        else content
                    )
                    completed_daily_terminal = completed_daily_flow.inspect_terminal(
                        terminal_content,
                        receipts=tuple(receipts),
                        write_batch_seen=write_batch_seen,
                        error_factory=DeepSeekResponseError,
                    )
                    if not write_batch_seen:
                        if completed_daily_terminal.needs_clarification_review:
                            clarification_decision = (
                                await review_daily_content_follow_through(
                                    terminal_content,
                                    allowed_write_tool_names=(
                                        completed_daily_terminal.allowed_write_tool_names
                                    ),
                                    query_state="qualified",
                                )
                            )
                            if clarification_decision != "keep_clarification":
                                raise _with_canary_turn_state(
                                    DeepSeekResponseError(
                                        "daily content follow-through clarification was not independently confirmed"
                                    ),
                                    audits=audits,
                                    model_turns=model_turns,
                                )
                        follow_through_tool_names = (
                            completed_daily_terminal.allowed_write_tool_names
                        )
                        if completed_daily_terminal.needs_initial_review:
                            follow_through_decision = (
                                await review_daily_content_follow_through(
                                    terminal_content,
                                    allowed_write_tool_names=(
                                        follow_through_tool_names
                                    ),
                                    query_state=completed_daily_terminal.query_state,
                                )
                            )
                            if follow_through_decision == "continue_once":
                                if completed_daily_terminal.query_state == "multiple":
                                    raise _with_canary_turn_state(
                                        DeepSeekResponseError(
                                            "multiple Daily queries cannot authorize write follow-through"
                                        ),
                                        audits=audits,
                                        model_turns=model_turns,
                                    )
                                if not follow_through_tool_names:
                                    raise _with_canary_turn_state(
                                        DeepSeekResponseError(
                                            "daily content follow-through has no allowed write tool"
                                        ),
                                        audits=audits,
                                        model_turns=model_turns,
                                    )
                                # The corrected Managed Daily reply has already passed
                                # its factual validator. Its bounded retry must not hide
                                # the one tool-enabled follow-through turn that this
                                # independent reviewer has just authorized.
                                managed_daily_reply_retry_count = 0
                                messages.append(
                                    completed_daily_flow.activate(
                                        candidate_reply=terminal_content,
                                        allowed_write_tool_names=(
                                            follow_through_tool_names
                                        ),
                                    )
                                )
                                continue
                        reviewed_terminal_content = (
                            await review_zero_write_terminal_reply(
                                terminal_content,
                                write_domains=(
                                    _exposed_business_write_domains(context)
                                ),
                            )
                        )
                        completed_daily_flow.assert_safe_reply(
                            reviewed_terminal_content,
                            receipts=tuple(receipts),
                            error_factory=DeepSeekResponseError,
                        )
                        if (
                            briefing_envelope is not None
                            and reviewed_terminal_content != terminal_content
                        ):
                            raise _with_canary_turn_state(
                                DeepSeekResponseError(
                                    "daily briefing safety review cannot replace a fact-bound reply"
                                ),
                                audits=audits,
                                model_turns=model_turns,
                            )
                        replacement_validation_errors = (
                            validate_managed_daily_reply(
                                reviewed_terminal_content,
                                tuple(receipts),
                            )
                        )
                        if replacement_validation_errors:
                            raise _with_canary_turn_state(
                                DeepSeekResponseError(
                                    "zero-tool safety replacement failed factual validation"
                                ),
                                audits=audits,
                                model_turns=model_turns,
                            )
                        terminal_content = reviewed_terminal_content
                    else:
                        if completed_daily_terminal.needs_pending_review:
                            follow_through_decision = (
                                await review_daily_content_follow_through(
                                    reply_for_validation,
                                    allowed_write_tool_names=(
                                        completed_daily_terminal.allowed_write_tool_names
                                    ),
                                    query_state="qualified",
                                    pending_write_review=True,
                                )
                            )
                            if follow_through_decision != "keep_no_write":
                                raise _with_canary_turn_state(
                                    DeepSeekResponseError(
                                        "Daily content write remained unfulfilled before commit"
                                    ),
                                    audits=audits,
                                    model_turns=model_turns,
                                )
                        await commit_pending()
                    final_content, model_hash = finalize_canary_content(
                        terminal_content,
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
                        review_completion = await complete_model(
                            daily_incomplete_confirm_review_messages(
                                ordered_messages=user_messages or (user_text,),
                                targets=incomplete_confirm_targets,
                            ),
                            tool_schemas=deepseek_tool_schemas(review_tool_names),
                            thinking_enabled=not bounded_daily_turn_active,
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
                    and not bounded_daily_turn_active
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
                            review_completion = await complete_model(
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
                completed_daily_flow.record_state(
                    receipts=tuple(receipts),
                    current_has_write=current_has_write,
                )
                tool_results = _canary_tool_result_messages(
                    parsed.tool_calls,
                    runtime_result.receipts,
                    write_batch_closed=current_has_write,
                )
                messages.extend(tool_results)
                if current_has_write:
                    if bounded_daily_turn_active and all(
                        call.tool_name == "add_daily_items"
                        for call in parsed.tool_calls
                    ):
                        messages = write_reply_retry_messages(
                            errors=(),
                            receipts=tuple(receipts),
                            retry_number=0,
                        )
                        bounded_daily_reply_composer_active = True
                        if (
                            bounded_daily_success_reply is not None
                            and any(
                                receipt.changed
                                for receipt in runtime_result.receipts
                            )
                        ):
                            prefetched_content = (
                                complete_write_reply_retry_envelope(
                                    json.dumps(
                                        {
                                            "reply": (
                                                bounded_daily_success_reply
                                            )
                                        },
                                        ensure_ascii=False,
                                    ),
                                    tuple(receipts),
                                )
                            )
                            prefetched_terminal_completion = (
                                _CompletionResponse(
                                    message={
                                        "role": "assistant",
                                        "content": prefetched_content,
                                    },
                                    metadata={
                                        "finish_reason": "stop",
                                        "request_attempt_count": 0,
                                        "transport_retry_count": 0,
                                        "transport_errors": [],
                                        "elapsed_seconds": 0.0,
                                        "model_call_performed": False,
                                        "reused_focused_daily_reply": True,
                                    },
                                )
                            )
                            bounded_daily_success_reply = None
                    else:
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
            payload["reasoning_effort"] = (
                "medium"
                if _has_focused_daily_review_tool(tool_schemas)
                else (
                    "low"
                    if _has_focused_daily_plan_tool(tool_schemas)
                    else "high"
                )
            )
        else:
            payload["thinking"] = {"type": "disabled"}
        if _has_focused_daily_plan_tool(tool_schemas):
            payload["max_tokens"] = 16384
        elif _has_focused_daily_review_tool(tool_schemas):
            payload["max_tokens"] = 8192
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


def _focused_daily_plan_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _FOCUSED_DAILY_PLAN_TOOL_NAME,
                "description": (
                    "Read one self-contained Daily Report source completely and "
                    "return every coherent work topic or outcome in the correct "
                    "section with exact source evidence. Empty sections stay empty."
                ),
                "strict": True,
                "parameters": focused_daily_plan_parameters_schema(),
            },
        }
    ]


def _has_focused_daily_plan_tool(
    tool_schemas: list[dict[str, Any]],
) -> bool:
    return any(
        isinstance(tool, dict)
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == _FOCUSED_DAILY_PLAN_TOOL_NAME
        for tool in tool_schemas
    )


def _focused_daily_review_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _FOCUSED_DAILY_REVIEW_TOOL_NAME,
                "description": (
                    "Return only a verdict on one Daily Report plan: approve it, "
                    "request one focused repair, or return it to the full Agent2 path."
                ),
                "strict": True,
                "parameters": focused_daily_review_parameters_schema(),
            },
        }
    ]


def _has_focused_daily_review_tool(
    tool_schemas: list[dict[str, Any]],
) -> bool:
    return any(
        isinstance(tool, dict)
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == _FOCUSED_DAILY_REVIEW_TOOL_NAME
        for tool in tool_schemas
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


def _validate_focused_daily_completion_protocol(
    completion: _CompletionResponse,
) -> None:
    raw_calls = completion.message.get("tool_calls")
    if raw_calls:
        if completion.metadata.get("finish_reason") != "tool_calls":
            raise DeepSeekResponseError(
                "focused Daily tool response did not finish as a tool call"
            )
        return
    if completion.metadata.get("finish_reason") != "stop":
        raise DeepSeekResponseError(
            "focused Daily response did not reach complete JSON termination"
        )
    content = completion.message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekResponseError("focused Daily response returned empty JSON")


def _parse_focused_daily_completion(
    message: dict[str, Any],
    *,
    call_scope: str,
) -> _ParsedAssistantTurn:
    raw_calls = message.get("tool_calls") or ()
    if raw_calls:
        if not isinstance(raw_calls, (list, tuple)) or len(raw_calls) != 1:
            raise InvalidNativeToolArgumentsError(
                "focused Daily plan requires exactly one tool call"
            )
        raw_call = raw_calls[0]
        function = raw_call.get("function") if isinstance(raw_call, dict) else None
        raw_arguments = (
            function.get("arguments") if isinstance(function, dict) else None
        )
        raw_name = function.get("name") if isinstance(function, dict) else None
        call_id = (
            str(raw_call.get("id") or "").strip()
            if isinstance(raw_call, dict)
            else ""
        )
        raw_text = raw_arguments if isinstance(raw_arguments, str) else ""
        digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        audit = RawToolCallAudit(
            tool_call_id=call_id or f"focused-daily-{call_scope}-{digest[:20]}",
            tool_name="add_daily_items",
            raw_arguments=raw_text,
            arguments_sha256=digest,
            parse_status="received_focused_daily_tool",
        )
        if raw_name != _FOCUSED_DAILY_PLAN_TOOL_NAME or not raw_text:
            raise InvalidNativeToolArgumentsError(
                "DeepSeek returned an invalid focused Daily planning tool",
                raw_tool_call_audit=(audit,),
            )
        try:
            decoder = json.JSONDecoder()
            cursor = len(raw_text) - len(raw_text.lstrip())
            decoded, cursor = decoder.raw_decode(raw_text, cursor)
            salvaged_duplicate = False
            while raw_text[cursor:].strip():
                cursor += len(raw_text[cursor:]) - len(
                    raw_text[cursor:].lstrip()
                )
                _, cursor = decoder.raw_decode(raw_text, cursor)
                salvaged_duplicate = True
            arguments, reply = compile_focused_daily_plan_arguments(decoded)
            submission_evidence = focused_daily_submission_evidence(
                decoded
            )
        except (TypeError, ValueError) as exc:
            raise InvalidNativeToolArgumentsError(
                "DeepSeek returned invalid focused Daily planning arguments",
                raw_tool_call_audit=(audit,),
            ) from exc
        if (
            not arguments.get("items")
            and not arguments.get("acknowledged_empty_fields")
        ):
            return _ParsedAssistantTurn(
                assistant_message={
                    "role": "assistant",
                    "content": json.dumps({"decision": "not_daily"}),
                },
                tool_calls=(),
                audit=(
                    replace(
                        audit,
                        parse_status="focused_submit_only_fallback",
                    ),
                ),
            )
        execution_call_id = audit.tool_call_id
        canonical_arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _ParsedAssistantTurn(
            assistant_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": execution_call_id,
                        "type": "function",
                        "function": {
                            "name": "add_daily_items",
                            "arguments": canonical_arguments,
                        },
                    }
                ],
            },
            tool_calls=(
                NativeToolCall(
                    tool_call_id=execution_call_id,
                    tool_name="add_daily_items",
                    arguments=arguments,
                ),
            ),
            audit=(
                replace(
                    audit,
                    parse_status=(
                        "salvaged_focused_daily_tool"
                        if salvaged_duplicate
                        else "validated_focused_daily_tool"
                    ),
                ),
            ),
            focused_success_reply=reply,
            focused_submission_evidence=submission_evidence,
        )

    content = message.get("content")
    raw_content = content if isinstance(content, str) else ""
    digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    call_id = f"focused-daily-{call_scope}-{digest[:20]}"
    audit = RawToolCallAudit(
        tool_call_id=call_id,
        tool_name="add_daily_items",
        raw_arguments=raw_content,
        arguments_sha256=digest,
        parse_status="received_focused_daily_json",
    )
    try:
        decision, arguments, reply = parse_focused_daily_add_decision(
            raw_content
        )
        decoded_content = json.loads(raw_content)
        raw_decision_arguments = (
            decoded_content.get("arguments")
            if isinstance(decoded_content, dict)
            else None
        )
        submission_evidence = (
            raw_decision_arguments.get("submission_evidence")
            if isinstance(raw_decision_arguments, dict)
            else None
        )
    except (TypeError, ValueError) as exc:
        raise InvalidNativeToolArgumentsError(
            "DeepSeek returned an invalid focused Daily decision",
            raw_tool_call_audit=(audit,),
        ) from exc
    if decision != "daily_add":
        return _ParsedAssistantTurn(
            assistant_message={
                "role": "assistant",
                "content": raw_content,
            },
            tool_calls=(),
            audit=(),
        )
    if arguments is None:
        raise InvalidNativeToolArgumentsError(
            "focused Daily add omitted arguments",
            raw_tool_call_audit=(audit,),
        )
    raw_arguments = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    call = NativeToolCall(
        tool_call_id=call_id,
        tool_name="add_daily_items",
        arguments=arguments,
    )
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "add_daily_items",
                    "arguments": raw_arguments,
                },
            }
        ],
    }
    return _ParsedAssistantTurn(
        assistant_message=assistant_message,
        tool_calls=(call,),
        audit=(
            replace(
                audit,
                parse_status="validated_focused_daily_json",
            ),
        ),
        focused_success_reply=reply,
        focused_submission_evidence=submission_evidence,
    )


def _validate_focused_daily_source(
    parsed: _ParsedAssistantTurn,
    *,
    user_text: str,
    user_messages: tuple[str, ...],
) -> None:
    if not parsed.tool_calls:
        return
    source = CurrentTurnSource(user_messages or (user_text,))
    try:
        for call in parsed.tool_calls:
            submit_after_write = bool(
                call.arguments.get("submit_after_write")
            )
            submission_evidence = parsed.focused_submission_evidence
            if submit_after_write != (submission_evidence is not None):
                raise CurrentTurnSourceEvidenceError(
                    "DAILY_SUBMISSION_EVIDENCE_REQUIRED"
                )
            if submission_evidence is not None:
                source_index = submission_evidence.get(
                    "source_message_index"
                )
                exact_quote = submission_evidence.get("exact_quote")
                if (
                    not isinstance(source_index, int)
                    or source_index < 1
                    or source_index > len(source.messages)
                    or not isinstance(exact_quote, str)
                    or not exact_quote.strip()
                    or exact_quote not in source.messages[source_index - 1]
                ):
                    raise CurrentTurnSourceEvidenceError(
                        "DAILY_SUBMISSION_EVIDENCE_MISMATCH"
                    )
            source.validate_tool_arguments(
                call.tool_name,
                call.arguments,
                allow_approximate_daily_quotes=True,
            )
    except CurrentTurnSourceEvidenceError as exc:
        raise InvalidNativeToolArgumentsError(
            f"focused Daily source validation failed: {exc.code}",
            raw_tool_call_audit=parsed.audit,
        ) from exc


def _parse_focused_daily_review_completion(
    completion: _CompletionResponse,
) -> tuple[str, str | None]:
    _validate_focused_daily_completion_protocol(completion)
    raw_calls = completion.message.get("tool_calls") or ()
    if raw_calls:
        try:
            if not isinstance(raw_calls, (list, tuple)) or len(raw_calls) != 1:
                raise ValueError("focused Daily review requires one tool call")
            function = raw_calls[0]["function"]
            if function.get("name") != _FOCUSED_DAILY_REVIEW_TOOL_NAME:
                raise ValueError("focused Daily review used the wrong tool")
            raw_arguments = function.get("arguments")
            if not isinstance(raw_arguments, str) or not raw_arguments.strip():
                raise ValueError("focused Daily review omitted arguments")
            decoder = json.JSONDecoder()
            cursor = len(raw_arguments) - len(raw_arguments.lstrip())
            decoded, cursor = decoder.raw_decode(raw_arguments, cursor)
            while raw_arguments[cursor:].strip():
                cursor += len(raw_arguments[cursor:]) - len(
                    raw_arguments[cursor:].lstrip()
                )
                duplicate, cursor = decoder.raw_decode(raw_arguments, cursor)
                if duplicate != decoded:
                    raise ValueError(
                        "focused Daily review returned conflicting arguments"
                    )
            return parse_focused_daily_review_arguments(decoded)
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidNativeToolArgumentsError(
                "focused Daily review returned an invalid tool verdict"
            ) from exc
    content = completion.message.get("content")
    if not isinstance(content, str):
        raise InvalidNativeToolArgumentsError(
            "focused Daily review returned no JSON"
        )
    try:
        return parse_focused_daily_review_decision(content)
    except (TypeError, ValueError) as exc:
        raise InvalidNativeToolArgumentsError(
            "focused Daily review returned an invalid verdict"
        ) from exc


def _parse_assistant_turn(
    message: dict[str, Any],
    *,
    allow_review_arguments_envelope: bool = False,
) -> _ParsedAssistantTurn:
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        raw_calls = ()
    if not isinstance(raw_calls, (list, tuple)):
        raise MalformedToolCallError("tool_calls must be an array")
    calls: list[NativeToolCall] = []
    audits: list[RawToolCallAudit] = []
    for raw_call in raw_calls:
        try:
            call, audit = _parse_native_tool_call(
                raw_call,
                allow_review_arguments_envelope=(
                    allow_review_arguments_envelope
                ),
            )
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


def _parse_native_tool_call(
    raw_call: Any,
    *,
    allow_review_arguments_envelope: bool = False,
) -> tuple[NativeToolCall, RawToolCallAudit]:
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
    parse_status = "validated"
    if (
        allow_review_arguments_envelope
        and isinstance(decoded, dict)
        and (
            set(decoded) == {"arguments"}
            or (
                set(decoded) == {"tool_name", "arguments"}
                and decoded.get("tool_name") == name
            )
        )
        and isinstance(decoded.get("arguments"), dict)
    ):
        decoded = decoded["arguments"]
        parse_status = "validated_review_arguments_envelope"
    elif (
        allow_review_arguments_envelope
        and name == "add_daily_items"
        and isinstance(decoded, dict)
        and "arguments_without_fallible_date_target" in decoded
        and isinstance(
            decoded.get("arguments_without_fallible_date_target"),
            dict,
        )
    ):
        content_arguments = decoded[
            "arguments_without_fallible_date_target"
        ]
        date_target_keys = {
            "date_selection",
            "date_expression",
            "proposed_date",
            "report_id",
            "expected_version",
            "retry_candidate_id",
            "date_evidence",
        }
        outer_target_keys = set(decoded) - {
            "arguments_without_fallible_date_target"
        }
        if "tool_name" in outer_target_keys:
            if decoded.get("tool_name") != name:
                raise InvalidNativeToolArgumentsError(
                    "DeepSeek returned invalid tool arguments",
                    raw_tool_call_audit=(audit,),
                )
            outer_target_keys.remove("tool_name")
        if (
            not outer_target_keys.issubset(date_target_keys)
            or outer_target_keys.intersection(content_arguments)
        ):
            raise InvalidNativeToolArgumentsError(
                "DeepSeek returned invalid tool arguments",
                raw_tool_call_audit=(audit,),
            )
        decoded = {
            **content_arguments,
            **{
                key: decoded[key]
                for key in outer_target_keys
            },
        }
        parse_status = "validated_review_date_target_envelope"
    try:
        if name == "add_daily_items":
            decoded = compile_model_add_daily_items(decoded)
            parse_status = (
                "validated_compact_daily_add"
                if all(
                    isinstance(item, dict) and "content" not in item
                    for item in json.loads(raw_arguments).get("items", ())
                )
                else parse_status
            )
        elif name in {
            "apply_current_weekly_report",
            "apply_next_weekly_plan",
        } and isinstance(decoded, dict):
            decoded = {
                key: value
                for key, value in decoded.items()
                if key != "content_reviewed"
            }
        validated = validate_tool_arguments(name, decoded)
    except UnknownToolError as exc:
        raise UnknownNativeToolError(
            "DeepSeek returned an unknown tool",
            raw_tool_call_audit=(audit,),
        ) from exc
    except (ToolArgumentsValidationError, ValidationError) as exc:
        raise InvalidNativeToolArgumentsError(
            "DeepSeek returned invalid tool arguments",
            raw_tool_call_audit=(audit,),
        ) from exc
    return NativeToolCall(call_id, name, validated), RawToolCallAudit(
        tool_call_id=audit.tool_call_id,
        tool_name=audit.tool_name,
        raw_arguments=audit.raw_arguments,
        arguments_sha256=audit.arguments_sha256,
        parse_status=parse_status,
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
            "日报事项还必须填写 content：只可做保守的专业化整理，去掉口语赘词、"
            "重复和明显语病，不得改变任何人物、项目、动作、对象、日期、数字、"
            "否定、条件、完成状态、风险或计划；exact_quote 仍保留完整原话。"
            "不要改用文本描述工具调用，也不要假称已经执行。"
        ),
    }


def _should_run_bounded_daily_probe(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    context: TrustedContext,
    thinking_enabled: bool,
) -> bool:
    """Let one semantic planner admit every pure Daily add, regardless of length."""

    del user_text, user_messages
    return bool(
        thinking_enabled
        and "add_daily_items" in context.allowed_tool_names
        and context.today_report is None
        and not context.historical_reports
        and not _has_daily_replacement_followup_context(context)
    )


def _only_daily_add_argument_error(error: DeepSeekToolCallingError) -> bool:
    audits = error.raw_tool_call_audit
    return bool(audits) and all(
        item.tool_name == "add_daily_items" for item in audits
    )


def _focused_daily_user_content(
    ordered_messages: tuple[str, ...],
) -> str:
    if len(ordered_messages) == 1:
        return ordered_messages[0]
    return json.dumps(
        {
            "ordered_current_user_messages": [
                {"sequence": index, "content": content}
                for index, content in enumerate(
                    ordered_messages,
                    start=1,
                )
            ]
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _compact_daily_add_argument_repair_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    context: TrustedContext,
    previous_decision: str,
    validation_error: str,
) -> list[dict[str, str]]:
    ordered_messages = user_messages or (user_text,)
    return [
        {
            "role": "system",
            "content": (
                "The previous focused Daily plan was missing, structurally invalid, "
                "or failed exact-source validation, and nothing executed. The user "
                "payload supplies the exact source, "
                "previous decision, and validation error. Correct that error without "
                "dropping valid matters. When source spans overlap, never reuse the "
                "same passage in two fields: choose the semantically correct field or "
                "use complete non-overlapping sub-passages. Re-read the entire source and "
                "call plan_daily_report exactly once. Its fields must contain today_work, "
                "problems, and tomorrow_plan. Each field is an array with one object "
                "per independently editable matter; every object contains content plus "
                "source_evidence. content is concise professional Daily wording and may "
                "only remove oral filler, repetition, or obvious grammar noise without "
                "changing any fact. source_evidence uses the one-based source_message_index "
                "of the exact current user message that contains its contiguous quote. "
                "Use index 1 only when there is one source message. "
                "Use an empty "
                "array only when the source truly has no matter for that field. "
                "An operation-level statement that the user has no edits, changes, "
                "additions, deletions, or moves is not evidence that any Daily Report "
                "content field is empty. If the source supplies no report content and "
                "only asks to submit an existing report, return not_daily without a tool "
                "call. Do not merge or duplicate matters across fields. Respect the user's own "
                "grouping. Each numbered or bulleted entry is one item unless it contains "
                "explicit subitems. Clear user-written list, paragraph, sentence, or "
                "semicolon boundaries may separate items. One item is the smallest coherent "
                "work topic or outcome the user would update as one report line, not the "
                "smallest verb-object pair. Missing punctuation does not merge a switch to an "
                "unrelated goal, project, case group, deliverable, or workstream. Keep several "
                "coordinated actions together when they form one user-described topic or shared "
                "workstream. Apply this in two passes: first partition every top-level topic or "
                "workstream, then preserve related coordinated actions inside each partition. "
                "The first pass has priority: one item must never span two unrelated work "
                "domains merely because the source has no punctuation or uses a transition. "
                "Preserve every clause-level modifier and "
                "transition; do not trim it merely because the remaining words still form "
                "a substring. Keep a clause together "
                "when its latter part only qualifies the status, condition, negation, or "
                "completion state of the same action. An explicit statement that a field "
                "has nothing to report belongs only in empty_field_evidence, never as an "
                "item. Set submit_after_write=true only when this source explicitly asks "
                "to submit or confirm this report now, and then submission_evidence must "
                "copy that exact contiguous authorization quote. Otherwise set it false "
                "and omit submission_evidence. If any field array remains empty without "
                "matching explicit-empty "
                "evidence and submit_after_write is false, reply must naturally say that "
                "the available content was saved and place the exact token "
                "{{daily_missing_section_labels}} once where the missing field names "
                "belong. Do not name a missing field elsewhere. Otherwise use a short "
                "success acknowledgement. Use date_selection=server_default. This tool only repairs the "
                "unexecuted plan and cannot write anything."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "source": _focused_daily_user_content(
                        ordered_messages
                    ),
                    "previous_decision": previous_decision,
                    "validation_error": validation_error,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _bounded_daily_probe_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    context: TrustedContext,
) -> list[dict[str, str]]:
    ordered_messages = user_messages or (user_text,)
    return [
        {
            "role": "system",
            "content": (
                "You are Agent2's fast planner for one self-contained pure Daily "
                "Report add. If the message also asks another task, depends on "
                "history, retries an earlier write, targets an existing report, or "
                "assigns a non-default report date, return exactly "
                "{\"decision\":\"not_daily\"} without a tool call. Otherwise call "
                "plan_daily_report exactly once. This tool only prepares a plan and "
                "cannot write anything. Its fields must contain all three keys "
                "today_work, problems, "
                "and tomorrow_plan. Each value is an array with one object containing "
                "content and source_evidence per independently editable matter. content "
                "is concise professional Daily wording: remove only oral filler, repetition, "
                "or obvious grammar noise, and never add, remove, generalize, or change an "
                "actor, project, action, object, date, number, negation, condition, completion "
                "state, risk, or plan. Every source_evidence uses the one-based "
                "source_message_index of the exact "
                "current user message containing exact_quote; use index 1 only when there "
                "is one source message. Use an "
                "empty array only when the source truly has no matter for that field. "
                "empty_field_evidence must contain one field plus its current source "
                "message index only when the user explicitly says that field is empty, such as "
                "no new risks or none; do not treat that empty statement as a report item. "
                "A statement that there are no edits, changes, additions, deletions, or "
                "moves describes operation state, not an empty report field. If the source "
                "contains no report content and only asks to submit an existing report, "
                "return not_daily without calling the planning tool. "
                "Read the entire source after any numbered work list; do not merge "
                "separate matters. Treat each user-authored numbered or bulleted list entry "
                "as one grouping unit; keep its dependent actions, outputs, checks, and "
                "qualifiers together unless the source itself marks separate subitems. Every "
                "numbered entry remains a report matter even when it is terse, fragmentary, or "
                "ends with an ellipsis; preserve it verbatim when safe polishing is unclear. "
                "For unnumbered prose, one item is the smallest coherent work topic or outcome "
                "the user would update as one report line, not the smallest verb-object pair. "
                "Split at a switch to an unrelated goal, project, case group, deliverable, or "
                "workstream even when punctuation is missing. Keep several coordinated actions "
                "together when they form one user-described topic or shared workstream, and keep "
                "qualifiers of that topic together. Apply this in two passes: first partition "
                "every top-level topic or workstream, then preserve related coordinated actions "
                "inside each partition. The first pass has priority: one item must never span "
                "two unrelated work domains merely because punctuation is missing or a transition "
                "connects them. "
                "Preserve every clause-level modifier and transition; do not trim it merely "
                "because the remaining words still form a substring. Keep a clause "
                "together when its latter part only "
                "qualifies the status, condition, negation, or completion state of the "
                "same action. Preserve complete actors, attribution, negation, "
                "conditions, deadlines, consequences, exceptions, risks, and plans "
                "inside contiguous exact quotes. Use date_selection=server_default. "
                "Set submit_after_write=false unless this same source explicitly asks "
                "to submit or confirm the report now; merely mentioning that work or a "
                "Daily Report was done is not submission authorization. "
                "When submit_after_write is true, submission_evidence must copy one "
                "exact contiguous current-message quote that explicitly authorizes "
                "submission now. When it is false, omit submission_evidence. "
                "Preserve explicit empty-field evidence. If any field array remains empty without "
                "matching explicit-empty evidence and submit_after_write is false, reply "
                "must naturally say that the available content was saved and place the "
                "exact token {{daily_missing_section_labels}} once where the missing "
                "field names belong. Do not name a missing field elsewhere. Otherwise "
                "reply with a short success acknowledgement. Do not claim submission "
                "unless submit_after_write is true. No markdown."
            ),
        },
        {
            "role": "user",
            "content": _focused_daily_user_content(ordered_messages),
        },
    ]


def _bounded_daily_add_review_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    context: TrustedContext,
    calls: tuple[NativeToolCall, ...],
    submission_evidence: dict[str, Any] | None = None,
    challenge_approval: bool = False,
) -> list[dict[str, str]]:
    del context
    ordered_messages = user_messages or (user_text,)
    return [
        {
            "role": "system",
            "content": (
                (
                    "Act as the final independent completeness challenger. A prior "
                    "review approved the candidate, but you must ignore that verdict "
                    "and actively try to find any omitted, merged, or misfielded source "
                    "matter before approving. "
                    if challenge_approval
                    else ""
                )
                +
                "You are an independent Agent2 Daily plan verifier. Never rewrite the "
                "candidate. Call review_daily_plan exactly once. First judge scope. "
                "Use decision=fallback with a concise "
                "reason only when the source also asks a non-Daily task, depends on "
                "conversation history, retries an earlier failed write, targets an "
                "existing report, or assigns a non-default report date. For a "
                "source that supplies no report content and only asks to submit an "
                "existing report, also use fallback; a statement that there are no edits, "
                "changes, additions, deletions, or moves is operation state, not empty-field "
                "evidence. For a "
                "self-contained pure Daily add, compare the entire source with the "
                "candidate. An explicit request to write must remain usable: when the "
                "candidate contains at least one grounded Daily item, never request repair "
                "solely because another source matter was omitted. Omitted matters can be "
                "supplemented naturally in a later turn. Approve a best-effort partial write "
                "when every included item is faithful. Return decision=repair only when an "
                "included item is invented, in the wrong field, arbitrarily merged or split "
                "in a way that changes meaning, loses a qualifier from its own exact quote, "
                "or otherwise adds, removes, generalizes, or changes a source fact, or when an "
                "explicit empty field or submission intent is wrong. For a self-contained "
                "Daily source that includes at least one report matter and explicitly asks "
                "to submit now, candidate.reviewed_omitted_empty_fields lists fields that "
                "the source omitted and that will therefore be submitted empty. Approve "
                "those only when the source truly contains no matter for those fields; "
                "repair if a listed field actually has content or if an omitted field is "
                "missing from that list. This omission rule never applies to drafts, "
                "follow-ups, or submit-only messages. When submit_after_write is false, "
                "reviewed_omitted_empty_fields must remain empty: asking to fill, write, "
                "record, draft, or save a Daily Report is not a request to submit it now, "
                "and omitted fields remain missing rather than acknowledged empty. Never "
                "reject or repair a non-submitting draft merely because one or more Daily "
                "fields are absent. "
                "approve submit_after_write unless submission_evidence is an exact source "
                "quote that explicitly authorizes submitting or confirming this report "
                "now; recommendations, conclusions, and requests to save/fill do not. "
                "When submit_after_write is false, submission_evidence must be absent. "
                "Respect the user's "
                "own grouping: each numbered or bulleted entry is one item unless it has "
                "explicit subitems; clear list, paragraph, sentence, or semicolon boundaries "
                "may separate items. Judge the user's coherent work topics, not individual verbs: "
                "split a switch to an unrelated goal, project, case group, deliverable, or "
                "workstream even without punctuation, while keeping coordinated actions together "
                "inside one user-described topic or shared workstream. Use a source-wide pass to "
                "improve completeness, but do not turn an omission alone into a write blocker. "
                "Then perform an item-level entailment pass: for every "
                "candidate item, compare its exact_quote clause by clause with content and repair "
                "if content drops any actor, condition, status, qualification, consequence, "
                "exception, date, number, or pending action from that quote. Do not assume an "
                "omitted clause is covered merely because another item overlaps the same source "
                "region. Repair whenever one item spans two unrelated work domains merely "
                "because punctuation is missing or a transition connects them. The candidate content may only "
                "remove oral filler, repetition, and obvious grammar noise while preserving "
                "all actors, projects, actions, objects, dates, numbers, negation, conditions, "
                "completion states, risks, and plans. Exact quotes retain "
                "clause-level modifiers, transitions, conditions, and status qualifiers. Return "
                "{\"decision\":\"approve\"} only when scope and candidate are both "
                "fully correct. For approve, reason may be empty. A repair reason "
                "identifies the defect but must not supply a rewritten candidate."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "source": _focused_daily_user_content(
                        ordered_messages
                    ),
                    "candidate": _focused_daily_review_candidate(
                        calls,
                        submission_evidence=submission_evidence,
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _focused_daily_review_needs_fast_retry(
    completion: _CompletionResponse,
) -> bool:
    raw_calls = completion.message.get("tool_calls")
    return (
        completion.metadata.get("finish_reason") != "tool_calls"
        or not isinstance(raw_calls, (list, tuple))
        or len(raw_calls) != 1
    )


def _focused_daily_review_candidate(
    calls: tuple[NativeToolCall, ...],
    *,
    submission_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    daily_calls = tuple(
        call for call in calls if call.tool_name == "add_daily_items"
    )
    if len(daily_calls) != 1:
        raise ValueError("focused Daily review requires one add candidate")
    arguments = daily_calls[0].arguments
    return {
        "date_selection": arguments.get("date_selection"),
        "items": [
            {
                "field": item.get("field"),
                "content": item.get("content"),
                "source_evidence": item.get("source_evidence"),
            }
            for item in (arguments.get("items") or [])
        ],
        "acknowledged_empty_fields": arguments.get(
            "acknowledged_empty_fields",
            [],
        ),
        "empty_field_evidence": arguments.get(
            "empty_field_evidence",
            [],
        ),
        "reviewed_omitted_empty_fields": arguments.get(
            "reviewed_omitted_empty_fields",
            [],
        ),
        "submit_after_write": bool(
            arguments.get("submit_after_write")
        ),
        "submission_evidence": submission_evidence,
    }


def _focused_daily_missing_numbered_entries(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    calls: tuple[NativeToolCall, ...],
) -> list[str]:
    ordered_messages = user_messages or (user_text,)
    covered_quotes: dict[int, list[str]] = {}
    for call in calls:
        if call.tool_name != "add_daily_items":
            continue
        for item in call.arguments.get("items", ()):
            evidence = item.get("source_evidence") or {}
            source_index = evidence.get("source_message_index")
            exact_quote = evidence.get("exact_quote")
            if isinstance(source_index, int) and isinstance(exact_quote, str):
                covered_quotes.setdefault(source_index, []).append(exact_quote)
    missing: list[str] = []
    for source_index, message in enumerate(ordered_messages, start=1):
        segments = message.splitlines()
        for segment in segments:
            normalized = segment.strip()
            cursor = 0
            while cursor < len(normalized) and normalized[cursor].isdigit():
                cursor += 1
            if (
                cursor == 0
                or cursor >= len(normalized)
                or normalized[cursor] not in {".", "。", "、", ")", "）"}
            ):
                continue
            content = normalized[cursor + 1 :].strip()
            if not content:
                continue
            content_core = content.rstrip("。；; ")
            if not any(
                content_core in quote or quote in content_core
                for quote in covered_quotes.get(source_index, ())
            ):
                missing.append(content)
    return missing


_DAILY_REPORT_TRANSACTION_TARGETS = frozenset(
    {
        "bound_report",
        "resolved_report",
        "today_report",
        "pending_report",
        "source_and_target_reports",
    }
)

_ZERO_TOOL_WRITE_INVITATION_REVIEW_KEYS = frozenset(
    {
        "decision",
        "classification",
        "reviewed_reply_sha256",
        "pending_reference",
        "replacement_reply",
    }
)

def _exposed_business_write_domains(
    context: TrustedContext,
) -> tuple[str, ...]:
    domains: set[str] = set()
    for tool_name in context.allowed_tool_names:
        definition = TOOL_REGISTRY.get(tool_name)
        if (
            definition is None
            or definition.read_or_write != "write"
            or context.gate_decisions.get(tool_name) is not True
        ):
            continue
        target = definition.transaction_target_policy
        if target in _DAILY_REPORT_TRANSACTION_TARGETS:
            domains.add("daily_report")
        elif target == "weekly_plan":
            domains.add("weekly_plan")
        elif target == "periodic_report":
            domains.add("periodic_report")
        elif target == "personal_memory":
            domains.add("personal_memory")
        else:
            domains.add("business_record")
    return tuple(sorted(domains))


def _trusted_persisted_pending_summary(
    context: TrustedContext,
) -> tuple[dict[str, Any], ...]:
    def tool_is_exposed(tool_name: str) -> bool:
        return (
            tool_name in context.allowed_tool_names
            and context.gate_decisions.get(tool_name) is True
        )

    summaries: list[dict[str, Any]] = []

    def review_reference() -> str:
        # This value is deliberately scoped to this reviewer payload.  The
        # provider never needs a database, report, plan, or pending identifier;
        # the server recomputes the same ordered allow-list before accepting it.
        return f"pending_{len(summaries) + 1}"

    clear_pending = context.active_clear_pending
    if clear_pending is not None:
        clear_report = context.report_by_id(clear_pending.report_id)
        executable = (
            clear_pending.namespace == context.namespace
            and not clear_pending.consumed
            and clear_pending.expires_at > context.now
            and clear_pending.source_message_id
            != context.principal.source_message_id
            and clear_report is not None
            and clear_report.report_date == clear_pending.target_date
            and clear_report.version == clear_pending.report_version
            and clear_report.status
            in {"collecting", "pending_confirmation", "completed"}
            and tool_is_exposed("confirm_clear_report")
        )
        summaries.append(
            {
                "pending_reference": review_reference(),
                "pending_kind": "daily_report_clear_confirmation",
                "target": {
                    "report_date": clear_pending.target_date.isoformat(),
                },
                "expires_at": clear_pending.expires_at.isoformat(),
                "executable_now": executable,
                "allows_bare_confirmation": executable,
                "provenance": "server_pending",
            }
        )

    daily_confirmation_exposed = tool_is_exposed("confirm_report")
    for report in context.all_reports():
        if report.status != "pending_confirmation":
            continue
        populated_fields = {item.field for item in report.items}
        report_is_complete = all(
            field_name in populated_fields
            or field_name in report.acknowledged_empty_fields
            for field_name in (
                "today_work",
                "problems",
                "tomorrow_plan",
            )
        )
        executable = daily_confirmation_exposed and report_is_complete
        summaries.append(
            {
                "pending_reference": review_reference(),
                "pending_kind": "daily_report_submission_confirmation",
                "target": {
                    "report_date": report.report_date.isoformat(),
                },
                "expires_at": None,
                "executable_now": executable,
                "allows_bare_confirmation": executable,
                "provenance": "server_report_state",
            }
        )

    weekly_confirmation_exposed = tool_is_exposed("submit_next_weekly_plan")
    for plan in context.all_weekly_plans():
        if plan.status != "pending_confirmation":
            continue
        local_date = context.now.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).date()
        executable = (
            weekly_confirmation_exposed
            and local_date <= plan.target_week_start
            and all(day.state != "unfilled" for day in plan.days)
        )
        summaries.append(
            {
                "pending_reference": review_reference(),
                "pending_kind": "weekly_plan_submission_confirmation",
                "target": {
                    "target_week_start": plan.target_week_start.isoformat(),
                },
                "expires_at": None,
                "executable_now": executable,
                "allows_bare_confirmation": executable,
                "provenance": "server_weekly_plan_state",
            }
        )

    retry = context.retryable_daily_write
    if retry is not None:
        retry_report = context.report_by_date(retry.target_date)
        retry_target_matches = (
            retry.target_was_absent
            and retry_report is None
        ) or (
            not retry.target_was_absent
            and retry_report is not None
            and retry_report.version == retry.target_version
            and retry_report.state_sha256 == retry.target_state_sha256
        )
        summaries.append(
            {
                "pending_reference": review_reference(),
                "pending_kind": "daily_write_retry_candidate",
                "target": {
                    "report_date": retry.target_date.isoformat(),
                },
                "expires_at": None,
                "executable_now": (
                    tool_is_exposed("add_daily_items")
                    and retry_target_matches
                    and retry.retry_chain_depth < 3
                    and retry.origin_source_message_id
                    != context.principal.source_message_id
                ),
                "allows_bare_confirmation": False,
                "provenance": "server_block_observation",
            }
        )
    return tuple(summaries)


def _zero_tool_write_invitation_review_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    candidate_reply: str,
    context: TrustedContext,
    write_domains: tuple[str, ...],
) -> list[dict[str, str]]:
    ordered_messages = user_messages or (user_text,)
    candidate_sha256 = hashlib.sha256(
        candidate_reply.encode("utf-8")
    ).hexdigest()
    return [
        {
            "role": "system",
            "content": (
                "You are the isolated Agent2 safety reviewer for one proposed "
                "zero-tool terminal reply. Judge the whole semantic meaning; never "
                "use keywords, phrase lists, or regular expressions. Your only task "
                "is to prevent an assistant-authored reply from inventing cross-turn "
                "write authority or claiming an unexecuted state change. This turn has "
                "no successful business-write receipt. A reply is unsafe when it claims "
                "or implies that any preference, setting, durable memory, business record, "
                "or future automatic reminder behavior has already changed, been enabled, "
                "or been disabled. A later admission that the setting was not changed does "
                "not make an earlier promise about future behavior safe. A reply is also "
                "unsafe when it asks, invites, or promises "
                "that a later bare acknowledgement, agreement, or confirmation alone "
                "will write, submit, clear, remember, or otherwise change a business "
                "record, but no exactly matching persisted_pending entry has both "
                "executable_now=true and allows_bare_confirmation=true. A failed-write "
                "retry candidate explicitly has allows_bare_confirmation=false and "
                "cannot support such an invitation. Never treat the proposed reply "
                "itself or prior assistant wording as a Pending. Keep ordinary answers, "
                "explanations, wording help, and clarifications that ask the user to "
                "state a complete fresh request without promising that bare assent is "
                "enough. Keep conditional explanations of what the system could do in a "
                "future turn when they do not claim that the current turn changed anything. "
                "A genuinely matching formal Pending may keep its invitation, "
                "but identify that exact pending_reference. If unsafe, replace the reply "
                "with concise natural Chinese that answers only the current request, "
                "truthfully states that nothing was saved or made pending when relevant, "
                "and creates no new write invitation. Do not call tools or execute any "
                "write. Return exactly one JSON object with exactly these keys: decision, "
                "classification, reviewed_reply_sha256, pending_reference, and "
                "replacement_reply. decision is keep or replace. classification is "
                "ordinary_reply, matched_persisted_pending, "
                "unbacked_future_write_invitation, or unbacked_state_change_claim. "
                "Copy reviewed_reply_sha256 exactly. "
                "For keep+ordinary_reply, both nullable fields are null. For "
                "keep+matched_persisted_pending, pending_reference is one exact supplied "
                "eligible reference and replacement_reply is null. For "
                "For replace with either unsafe classification, pending_reference is null "
                "and replacement_reply is the complete safe replacement."
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
                    "proposed_assistant_reply": candidate_reply,
                    "reviewed_reply_sha256": candidate_sha256,
                    "allowed_write_domains": list(write_domains),
                    "persisted_pending": list(
                        _trusted_persisted_pending_summary(context)
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _apply_zero_tool_write_invitation_review(
    *,
    review_content: str,
    candidate_reply: str,
    context: TrustedContext,
) -> str:
    try:
        payload = json.loads(review_content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("write invitation review must return JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _ZERO_TOOL_WRITE_INVITATION_REVIEW_KEYS
    ):
        raise ValueError("write invitation review has an invalid envelope")
    candidate_sha256 = hashlib.sha256(
        candidate_reply.encode("utf-8")
    ).hexdigest()
    if payload["reviewed_reply_sha256"] != candidate_sha256:
        raise ValueError("write invitation review is not bound to the candidate")

    decision = payload["decision"]
    classification = payload["classification"]
    pending_reference = payload["pending_reference"]
    replacement_reply = payload["replacement_reply"]
    pending_summaries = _trusted_persisted_pending_summary(context)
    generated_pending_references = {
        str(item["pending_reference"])
        for item in pending_summaries
    }
    eligible_pending_references = {
        str(item["pending_reference"])
        for item in pending_summaries
        if item["executable_now"] and item["allows_bare_confirmation"]
    }

    def reject_generated_reference_leak(reply: str) -> None:
        if any(reference in reply for reference in generated_pending_references):
            raise ValueError(
                "write invitation review leaked a temporary Pending reference"
            )

    if decision == "keep" and classification == "ordinary_reply":
        if pending_reference is not None or replacement_reply is not None:
            raise ValueError("ordinary reply review cannot attach extra output")
        reject_generated_reference_leak(candidate_reply)
        return candidate_reply
    if decision == "keep" and classification == "matched_persisted_pending":
        if (
            not isinstance(pending_reference, str)
            or pending_reference not in eligible_pending_references
            or replacement_reply is not None
        ):
            raise ValueError("write invitation review did not bind an eligible Pending")
        reject_generated_reference_leak(candidate_reply)
        return candidate_reply
    if (
        decision == "replace"
        and classification in {
            "unbacked_future_write_invitation",
            "unbacked_state_change_claim",
        }
        and pending_reference is None
        and isinstance(replacement_reply, str)
        and replacement_reply.strip()
        and len(replacement_reply) <= 8000
        and replacement_reply != candidate_reply
    ):
        reject_generated_reference_leak(replacement_reply)
        return replacement_reply
    raise ValueError("write invitation review decision is inconsistent")


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
        if available_domains == {"daily"}:
            source_fidelity_operations = current_reviewed_operations.intersection(
                completed_daily_follow_through.DAILY_CONTENT_WRITE_TOOLS
            )
            if source_fidelity_operations:
                targeted_operations = {
                    "edit_daily_items",
                    "delete_daily_items",
                    "move_daily_items",
                }
                if source_fidelity_operations.intersection(targeted_operations):
                    return frozenset(
                        current_reviewed_operations
                        | {
                            name
                            for name in targeted_operations
                            if name in context.allowed_tool_names
                            and context.gate_decisions.get(name) is True
                        }
                    )
                if (
                    "add_daily_items" in source_fidelity_operations
                    and _has_daily_replacement_followup_context(context)
                    and "delete_daily_items" in context.allowed_tool_names
                    and context.gate_decisions.get("delete_daily_items") is True
                ):
                    return frozenset(
                        current_reviewed_operations | {"delete_daily_items"}
                    )
                return frozenset(current_reviewed_operations)
            if not calls:
                return _daily_focus_write_tool_names(context)
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


def _has_daily_replacement_followup_context(
    context: TrustedContext,
) -> bool:
    """Expose delete review only for one fresh receipt-bound Daily dialogue."""

    if (
        context.principal.conversation_kind != "direct"
        or not context.recent_messages
        or not any(
            message.role == "assistant"
            for message in context.recent_messages[-6:]
        )
    ):
        return False
    for operation in reversed(context.recent_operations):
        reference = operation.report_reference
        if (
            operation.tool_name != "add_daily_items"
            or operation.status != ReceiptStatus.SUCCESS
            or not operation.changed
            or reference is None
        ):
            continue
        report = context.report_by_id(reference.report_id)
        if (
            report is None
            or reference.report_version != report.version
            or reference.report_state_sha256 != report.state_sha256
        ):
            continue
        current_ids = {item.item_id for item in report.items}
        return bool(operation.affected_item_ids) and set(
            operation.affected_item_ids
        ).issubset(current_ids)
    return False


_PERSONAL_MEMORY_WRITE_TOOLS = frozenset(
    {"remember_personal_memory", "forget_personal_memory"}
)


def _daily_focus_write_tool_names(
    context: TrustedContext,
) -> frozenset[str]:
    if context.principal.conversation_kind != "direct":
        return frozenset()
    references = {
        operation.report_reference.report_id
        for operation in context.recent_operations
        if operation.report_reference is not None
        and operation.target_type == "daily_report"
        and operation.status in {ReceiptStatus.SUCCESS, ReceiptStatus.NO_OP}
        and context.report_by_id(operation.report_reference.report_id) is not None
    }
    if len(references) != 1:
        return frozenset()
    report = context.report_by_id(next(iter(references)))
    if report is None or report.status not in {
        "collecting",
        "pending_confirmation",
        "completed",
    }:
        return frozenset()
    return frozenset(
        name
        for name in completed_daily_follow_through.DAILY_CONTENT_WRITE_TOOLS
        if name in context.allowed_tool_names
        and context.gate_decisions.get(name) is True
    )


def _memory_daily_focus_review_tool_names(
    calls: tuple[NativeToolCall, ...],
    *,
    context: TrustedContext,
) -> frozenset[str]:
    """Open one semantic correction gate for a memory draft in Daily focus."""

    if (
        len(calls) != 1
        or calls[0].tool_name not in _PERSONAL_MEMORY_WRITE_TOOLS
    ):
        return frozenset()
    daily_tools = _daily_focus_write_tool_names(context)
    if not daily_tools:
        return frozenset()
    return frozenset({calls[0].tool_name, *daily_tools})


def _memory_daily_focus_review_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    context: TrustedContext,
    calls: tuple[NativeToolCall, ...],
) -> list[dict[str, str]]:
    ordered_messages = user_messages or (user_text,)
    return [
        {
            "role": "system",
            "content": (
                "You are an isolated Agent2 semantic reviewer for one unexecuted "
                "personal-memory draft made while one server-verified Daily Report "
                "is the unique recent conversation focus. The focus is evidence, "
                "not an instruction to keep editing it. Independently decide from "
                "the whole current user message whether the user explicitly changes "
                "a durable personal preference, name, or reminder setting, or instead "
                "continues or corrects that Daily Report. Never decide by keywords, "
                "phrases, or regular expressions. Treat the supplied memory draft as "
                "fallible and unexecuted. If the memory write is correct, return the "
                "same memory tool and arguments exactly. If a Daily content write is "
                "clearly required and fully bound by trusted context, return one "
                "complete Daily content tool call; it will undergo the normal Daily "
                "semantic review and server binding afterward. If neither is clear, "
                "return exactly one JSON object with decision=clarification and a "
                "concise natural Chinese question. Never claim anything was saved."
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
                    "trusted_context": context.model_payload(),
                    "unexecuted_personal_memory_draft": [
                        {
                            "tool_name": call.tool_name,
                            "arguments": call.arguments,
                        }
                        for call in calls
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _memory_daily_focus_recovery_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    context: TrustedContext,
) -> list[dict[str, str]]:
    ordered_messages = user_messages or (user_text,)
    return [
        {
            "role": "system",
            "content": (
                "You are the final isolated Agent2 Daily-focus adjudicator. A prior "
                "unexecuted personal-memory draft was not independently confirmed. "
                "Ignore that draft and decide afresh from the complete current user "
                "message plus trusted context. If the user clearly continues, adds, "
                "edits, deletes, or moves Daily Report content, return exactly one "
                "complete native Daily content tool call. Never infer meaning from a "
                "keyword, phrase list, or regular expression. If the Daily action or "
                "target is not clear, return exactly one JSON object with "
                "decision=clarification and a concise natural Chinese question. "
                "Personal-memory tools and all other domains are unavailable. Nothing "
                "has executed; never claim that anything was saved."
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
                    "trusted_context": context.model_payload(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _validate_memory_daily_focus_recovery(
    *,
    reviewed: _ParsedAssistantTurn,
    allowed_tool_names: frozenset[str],
) -> None:
    _validate_daily_weekly_write_review(
        reviewed=reviewed,
        allowed_tool_names=allowed_tool_names,
        original_has_domain_writes=True,
    )


def _validate_memory_daily_focus_review(
    *,
    original: _ParsedAssistantTurn,
    reviewed: _ParsedAssistantTurn,
    allowed_tool_names: frozenset[str],
) -> None:
    if not reviewed.tool_calls:
        _validate_daily_weekly_write_review(
            reviewed=reviewed,
            allowed_tool_names=frozenset(),
            original_has_domain_writes=True,
        )
        return
    if any(call.tool_name not in allowed_tool_names for call in reviewed.tool_calls):
        raise ValueError("memory/Daily focus review returned an out-of-scope tool")
    memory_calls = tuple(
        call
        for call in reviewed.tool_calls
        if call.tool_name in _PERSONAL_MEMORY_WRITE_TOOLS
    )
    daily_calls = tuple(
        call
        for call in reviewed.tool_calls
        if call.tool_name
        in completed_daily_follow_through.DAILY_CONTENT_WRITE_TOOLS
    )
    if len(memory_calls) + len(daily_calls) != len(reviewed.tool_calls):
        raise ValueError("memory/Daily focus review mixed another domain")
    if memory_calls and daily_calls:
        raise ValueError("memory/Daily focus review returned a mixed write batch")
    if memory_calls:
        if len(original.tool_calls) != 1 or len(memory_calls) != 1:
            raise ValueError("memory/Daily focus review changed the memory batch")
        if (
            memory_calls[0].tool_name != original.tool_calls[0].tool_name
            or memory_calls[0].arguments != original.tool_calls[0].arguments
        ):
            raise ValueError("memory/Daily focus review changed memory arguments")
        return
    if not daily_calls:
        raise ValueError("memory/Daily focus review returned no supported decision")


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
    trusted_completed_daily_query_results: list[dict[str, Any]],
    selected_targets: list[dict[str, Any]],
    allowed_tool_names: frozenset[str],
) -> list[dict[str, str]]:
    ordered_messages = user_messages or (user_text,)
    allowed_domains = _daily_weekly_review_domains(allowed_tool_names)
    draft_domains = {
        domain
        for call in calls
        if (domain := _daily_weekly_write_domain(call.tool_name)) is not None
    }
    if draft_domains == {"weekly"}:
        return _weekly_plan_write_review_messages(
            ordered_messages=ordered_messages,
            calls=calls,
            context=context,
            allowed_tool_names=allowed_tool_names,
            allow_cross_domain_restoration=allowed_domains != {"weekly"},
        )
    daily_only_constraint = (
        "For this Daily-only correction review, do not invent a report date or "
        "target. Preserve the draft target unless trusted_context proves that the "
        "current multi-turn collection belongs to one exact collecting or "
        "pending-confirmation report; in that case you may replace server_default "
        "only with that exact trusted_report report_id and version. Any other date "
        "disagreement requires clarification. Do not change retry_candidate_id or "
        "submit_after_write. You may correct items "
        "and explicit-empty-field evidence for add_daily_items. Every exact_quote must preserve "
        "the complete meaning of its item, including every negation, condition, "
        "deadline, consequence, exception, and pending action even when separated "
        "by punctuation. One item is the smallest coherent work topic or outcome the "
        "user would update as one report line, not the smallest verb-object pair. Count "
        "those topics in the current user messages and cover each exactly once. Split a "
        "switch to an unrelated goal, project, case group, deliverable, or workstream even "
        "without punctuation. Keep several coordinated actions together when they form one "
        "user-described topic or shared workstream. The exact_quote source spans for different "
        "items must not overlap, and each quote must contain only its coherent topic. Do not "
        "preserve the draft's item "
        "grouping without independently recounting the source matters. Each item's "
        "content must be concise professional wording grounded by exact_quote. It may "
        "remove oral filler, repetition, or obvious grammar noise, but must preserve "
        "every actor, project, action, object, date, number, attribution, negation, "
        "condition, completion state, risk, and plan. "
        if allowed_domains == {"daily"}
        and any(call.tool_name == "add_daily_items" for call in calls)
        else ""
    )
    edit_source_constraint = (
        "For edit_daily_items, the report, version, and target stable item IDs "
        "are immutable; correct only replacement_evidence. Its exact_quote must "
        "be the complete contiguous new replacement stated by the user, excluding "
        "the target description, old content, ordinal, and edit instruction. It "
        "must preserve every negation, condition, deadline, consequence, and "
        "exception attached to that replacement. If no complete replacement-only "
        "span exists, ask a clarification and return no tools. "
        if "edit_daily_items" in allowed_tool_names
        else ""
    )
    targeted_operation_constraint = (
        "For edit_daily_items, delete_daily_items, and move_daily_items, the draft "
        "operation name is fallible but its report ID, version, and stable target "
        "item IDs are immutable. You may correct a delete_daily_items draft to "
        "edit_daily_items when the current message supplies a complete replacement, "
        "while copying that exact target identity. All other operation-type changes "
        "require clarification. Never add, drop, or substitute a target item. "
        if {
            "edit_daily_items",
            "delete_daily_items",
            "move_daily_items",
        }.intersection(allowed_tool_names)
        else ""
    )
    trusted_completed_daily_query_constraint = (
        "The payload includes one de-identified, server-validated completed Daily "
        "Report query result. Use its field positions and content to evaluate the "
        "user's semantic ordinal or content reference. The draft's internal report, "
        "version, and stable target item IDs remain immutable: copy those draft values "
        "exactly so the existing binder can verify them against trusted state before "
        "execution, and do not ask the user to supply internal IDs. "
        if trusted_completed_daily_query_results
        else ""
    )
    selected_target_constraint = (
        "The payload's selected_targets deterministically maps only the opaque target "
        "IDs already present in each draft to de-identified field positions and "
        "content. Use that mapping to check whether the draft selected the matter the "
        "user actually described. Never generate or substitute an ID, version, or "
        "source evidence from this mapping. If the selected position or content does "
        "not match the user's meaning, return no tools and ask a clarification; for an "
        "execute decision preserve every immutable target value from the draft. "
        if selected_targets
        else ""
    )
    weekly_plan_content_constraint = (
        "For apply_next_weekly_plan, every add or replacement content value contains "
        "only the user's planned work matter. Instructions that control the operation—"
        "adding, editing, moving, deleting, saving, previewing, confirming, submitting, "
        "or deliberately not submitting—must never be stored as plan content. Preserve "
        "all conditions, amounts, negations, dependencies, and deadlines that belong to "
        "the work matter. Correct a draft that mixes operation control into content. "
        if "apply_next_weekly_plan" in allowed_tool_names
        else ""
    )
    weekly_plan_recurrence_evidence_constraint = (
        "For repeated Weekly Work Plan additions, recurrence_scope_quote contains "
        "only the complete date scope and stops before the action or work matter. "
        "For 下周每天做日常用印审核, use 下周每天, never "
        "下周每天做日常用印审核. Preserve every date qualifier or exclusion. "
        if "apply_next_weekly_plan" in allowed_tool_names
        else ""
    )
    weekly_plan_change_before_submit_constraint = (
        "When the user requests clear Weekly Work Plan changes and immediate "
        "submission but submission must wait for an updated complete preview, "
        "preserve every safe apply_next_weekly_plan change and omit only the "
        "submit_next_weekly_plan call. Never reject or remove the safe changes "
        "merely because submission cannot yet proceed. "
        if "apply_next_weekly_plan" in allowed_tool_names
        else ""
    )
    draft_calls = [
        _daily_weekly_review_draft_payload(call)
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
                "weekday. Selecting a retryable_daily_write candidate is fresh write "
                "authorization: require the current message to unmistakably identify that "
                "failed write as the action to repeat. Broad delegation, general permission, "
                "acknowledgement, or leaving the action unspecified is not enough; request "
                "clarification instead of selecting the candidate. "
                f"{_daily_weekly_review_domain_policy(allowed_domains)} "
                "Treat the supplied draft as fallible: it may omit one domain, omit a "
                "matter, or contain the wrong otherwise-valid arguments. The draft has "
                "not executed and has written nothing. "
                "When there is no reviewed draft and trusted context identifies one "
                "unique recent Daily Report, a brief standalone follow-up may correct "
                "a speech-recognition error, entity name, or other content in that "
                "immediately preceding report. Compare it semantically with the trusted "
                "report and recent conversation; if exactly one correction is clear, "
                "return the fully bound edit call, otherwise clarify. Do not keep the "
                "original merely because the follow-up is short. "
                "Also treat the current message as a possible direct answer to the latest "
                "assistant request in trusted_context.recent_messages. When that dialogue "
                "shows the assistant was waiting for replacement parts to correct one unique "
                "item in a trusted Daily Report, and the current message supplies those parts, "
                "history may select only the report, obsolete item, and correction operation; "
                "the current message alone supplies replacement content. Return one atomic "
                "batch that deletes the obsolete combined item and adds every replacement item "
                "against the same trusted report/version. Never append replacements while "
                "retaining the obsolete item. Each replacement inherits the obsolete item's "
                "Daily field unless the current message explicitly assigns another field. "
                "Preserve current-message replacement order; a field heading or scope on the "
                "first numbered part governs following sibling parts until the user changes it. "
                "If the target is not unique, clarify. "
                "For targeted_daily_items the "
                "fallible draft operation name is deliberately omitted: independently "
                "choose edit, delete, or move from the exact user meaning while copying "
                "immutable_target exactly. For add_daily_items the fallible date target "
                "is deliberately omitted: independently choose the date only from the "
                "user messages and trusted_context. If all intended writes and their "
                "dates, fields, actors, conditions, evidence, stable IDs, and versions are "
                "clear, return exactly one complete corrected native tool-call batch using "
                "only the supplied tools. Preserve exact current-message grounding and do "
                f"{daily_only_constraint}{edit_source_constraint}"
                f"{targeted_operation_constraint}"
                f"{trusted_completed_daily_query_constraint}"
                f"{selected_target_constraint}"
                f"{weekly_plan_content_constraint}"
                f"{weekly_plan_recurrence_evidence_constraint}"
                f"{weekly_plan_change_before_submit_constraint}"
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
                    **(
                        {
                            "trusted_completed_daily_query_results": (
                                trusted_completed_daily_query_results
                            )
                        }
                        if trusted_completed_daily_query_results
                        else {}
                    ),
                    **(
                        {"selected_targets": selected_targets}
                        if selected_targets
                        else {}
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _weekly_plan_write_review_messages(
    *,
    ordered_messages: tuple[str, ...],
    calls: tuple[NativeToolCall, ...],
    context: TrustedContext,
    allowed_tool_names: frozenset[str],
    allow_cross_domain_restoration: bool,
) -> list[dict[str, str]]:
    """Keep the Weekly Work Plan review focused on its own record and evidence."""

    model_context = context.model_payload()
    weekly_context = {
        key: model_context[key]
        for key in (
            "current_time",
            "timezone",
            "recent_messages",
            "resource_namespace",
            "runtime_identity",
            "authenticated_user",
            "weekly_plan_targets",
        )
        if key in model_context
    }
    draft_calls = [
        _daily_weekly_review_draft_payload(call)
        for call in calls
        if _daily_weekly_write_domain(call.tool_name) == "weekly"
    ]
    cross_domain_instruction = (
        "The current messages may also state a separate explicit Daily Report fact "
        "completed today or a retrospective Current Weekly Report matter that the "
        "draft omitted. Independently restore each such clear separate record using "
        "the supplied tools, while keeping the Weekly Work Plan operations. Never turn "
        "planned work into completed work, and never move a retrospective weekly-report "
        "matter into the forward plan. "
        if allow_cross_domain_restoration
        else ""
    )
    return [
        {
            "role": "system",
            "content": (
                "You are the focused independent Agent2 reviewer for one unexecuted "
                "Monday-to-Saturday Weekly Work Plan draft. Nothing has been written. "
                "Reread every exact current user message and the trusted weekly targets; "
                "do not rely on keywords or on the draft being correct. Keep Daily Reports "
                "and retrospective Weekly Reports outside this review. Select only the exact "
                "trusted target the user means and copy its plan_id and current version. "
                "Review the whole batch atomically: preserve every clear matter, date, "
                "recurrence, exception, condition, amount, negation, dependency, deadline, "
                "stable item target, explicit empty day, and explicit submission decision. "
                "Do not omit a valid operation because another operation needs correction. "
                f"{cross_domain_instruction}"
                "For repeated work, emit one add for every selected exact date. Each repeated "
                "add cites the complete current message as exact_clause_quote and cites the "
                "same date-only recurrence_scope_quote. That scope stops before the action "
                "and work matter: for 下周每天做日常用印审核 use 下周每天, never "
                "下周每天做日常用印审核; for 下周一到周五每天做两项工作 use "
                "下周一到周五每天, never 下周一到周五. Preserve every date qualifier "
                "or exclusion. "
                "Each add or edit content contains only the planned work matter. Instructions "
                "to add, edit, move, delete, save, preview, confirm, submit, or deliberately "
                "not submit control the operation and must never be stored as content. "
                "If the user asks for a clear change and immediate submission but the updated "
                "complete preview must come first, preserve the safe change and omit only the "
                "submission. Never discard the change for that reason. A reviewer may not use "
                "submission as a substitute for a plan change. "
                "If all meanings and bindings are clear, return exactly one complete corrected "
                "native tool-call batch using only the supplied tools. For execute decisions, "
                "return tool_calls only, with no JSON/text decision. If a material target, date, "
                "matter, or action is genuinely ambiguous, return no tools and exactly one JSON "
                "object with decision=clarification and a concise natural Chinese question. "
                "Use {\"decision\":\"keep_original\"} only when no Weekly Work Plan operation "
                "or clarification is needed. Never claim anything was saved or submitted."
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
                    "trusted_weekly_context": weekly_context,
                    "unexecuted_weekly_plan_operation_draft": draft_calls,
                    "allowed_weekly_tools": sorted(allowed_tool_names),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _daily_weekly_review_draft_payload(
    call: NativeToolCall,
) -> dict[str, Any]:
    """Remove fallible operation/date choices while preserving server-bound targets."""

    if call.tool_name in {
        "edit_daily_items",
        "delete_daily_items",
        "move_daily_items",
    }:
        return {
            "draft_kind": "targeted_daily_items",
            "immutable_target": {
                key: call.arguments.get(key)
                for key in (
                    "report_id",
                    "expected_version",
                    "target_item_ids",
                )
            },
        }
    if call.tool_name == "add_daily_items":
        hidden_target_keys = {
            "date_selection",
            "date_expression",
            "proposed_date",
            "report_id",
            "expected_version",
            "retry_candidate_id",
            "date_evidence",
        }
        return {
            "tool_name": call.tool_name,
            "arguments_without_fallible_date_target": {
                key: value
                for key, value in call.arguments.items()
                if key not in hidden_target_keys and key != "content_reviewed"
            },
        }
    return {"tool_name": call.tool_name, "arguments": call.arguments}


def _dropped_daily_add_adjudication_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
    context: TrustedContext,
) -> list[dict[str, str]]:
    """Ask one fresh reviewer to resolve a Daily-add/no-Daily-add split."""

    ordered_messages = user_messages or (user_text,)
    return [
        {
            "role": "system",
            "content": (
                "You are the final isolated Agent2 semantic reviewer and Daily Report "
                "adjudicator. One "
                "unexecuted model decision selected add_daily_items and another "
                "independent review selected no Daily add. You are not shown either "
                "decision or any draft arguments. Independently reread every exact "
                "current user message and the trusted context. Decide semantically "
                "from the whole utterance; never use keywords, phrase lists, or regular "
                "expressions. Only when the user clearly and presently authorizes Daily "
                "Report content, return exactly one complete native add_daily_items call "
                "using the supplied tool. Put all Daily matters in that call's one complete "
                "items array; never split them across calls. Preserve every independently "
                "editable matter, "
                "its Daily field, exact contiguous source quote and source-message index, "
                "every explicit empty field and its evidence, the requested report date, "
                "trusted target/version or retry candidate, and submit intent. The server "
                "will compare this fresh decision with the other independently grounded "
                "decision. Return conservative professional content plus exact source evidence "
                "for each item; content may remove oral filler, repetition, and obvious grammar "
                "noise but must preserve every source fact. "
                "Do not reproduce only a partial items array. If a Daily write or any material "
                "part remains unclear, return no tool calls and exactly one JSON object "
                "with keys decision and reply, where decision is clarification and reply "
                "is one concise natural Chinese question. Never claim that anything was "
                "saved, submitted, or executed."
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


def _is_bounded_daily_not_daily_review(
    reviewed: _ParsedAssistantTurn,
) -> bool:
    if reviewed.tool_calls:
        return False
    content = reviewed.assistant_message.get("content")
    try:
        payload = json.loads(content) if isinstance(content, str) else None
    except json.JSONDecodeError:
        return False
    return payload == {"decision": "not_daily"}


def _is_keep_original_review(
    reviewed: _ParsedAssistantTurn,
) -> bool:
    if reviewed.tool_calls:
        return False
    content = reviewed.assistant_message.get("content")
    try:
        payload = json.loads(content) if isinstance(content, str) else None
    except json.JSONDecodeError:
        return False
    return payload == {"decision": "keep_original"}


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


def _daily_add_agreement_signature(call: NativeToolCall) -> dict[str, Any]:
    """Compare independently grounded field, wording, and source decisions."""

    if call.tool_name != "add_daily_items":
        raise ValueError("Daily add agreement requires add_daily_items")
    arguments = call.arguments
    date_target = {
        key: arguments.get(key)
        for key in (
            "date_selection",
            "date_expression",
            "proposed_date",
            "report_id",
            "expected_version",
            "retry_candidate_id",
            "date_evidence",
            "submit_after_write",
        )
    }
    items = []
    for item in arguments.get("items", ()):  # already schema-validated
        evidence = item.get("source_evidence") or {}
        items.append(
            {
                "field": item.get("field"),
                "content": item.get("content"),
                "source_message_index": evidence.get("source_message_index"),
                "exact_quote": evidence.get("exact_quote"),
            }
        )
    empty_evidence = sorted(
        (
            item.get("field"),
            (item.get("source_evidence") or {}).get("source_message_index"),
        )
        for item in arguments.get("empty_field_evidence", ())
    )
    return {
        "date_target": date_target,
        "items": items,
        "acknowledged_empty_fields": sorted(
            arguments.get("acknowledged_empty_fields", ())
        ),
        "reviewed_omitted_empty_fields": sorted(
            arguments.get("reviewed_omitted_empty_fields", ())
        ),
        "empty_field_evidence": empty_evidence,
    }


def _constrain_daily_add_call_group(
    *,
    original_daily: tuple[NativeToolCall, ...],
    reviewed_daily: tuple[NativeToolCall, ...],
    context: TrustedContext | None = None,
) -> tuple[NativeToolCall, ...]:
    """Keep every trusted target while taking grounded content from the review."""

    if len(original_daily) != 1 or len(reviewed_daily) != 1:
        raise ValueError("Daily review must replace exactly one Daily draft")
    constrained: list[NativeToolCall] = []
    for draft_call, reviewed_call in zip(
        original_daily,
        reviewed_daily,
        strict=True,
    ):
        draft_arguments = dict(draft_call.arguments)
        reviewed_arguments = reviewed_call.arguments
        date_target_keys = (
            "date_selection",
            "date_expression",
            "proposed_date",
            "report_id",
            "expected_version",
            "retry_candidate_id",
            "date_evidence",
        )
        draft_target = {
            key: draft_arguments.get(key) for key in date_target_keys
        }
        reviewed_target = {
            key: reviewed_arguments.get(key) for key in date_target_keys
        }
        if reviewed_target != draft_target:
            draft_date = _daily_add_target_date(
                draft_arguments,
                context=context,
            )
            reviewed_date = _daily_add_target_date(
                reviewed_arguments,
                context=context,
            )
            reviewed_report_id = reviewed_arguments.get("report_id")
            try:
                trusted_report = (
                    context.report_by_id(UUID(str(reviewed_report_id)))
                    if context is not None and reviewed_report_id is not None
                    else None
                )
            except ValueError:
                trusted_report = None
            same_resolved_date = (
                draft_date is not None
                and reviewed_date is not None
                and draft_date == reviewed_date
            )
            reviewed_trusted_valid = (
                reviewed_arguments.get("date_selection") == "trusted_report"
                and trusted_report is not None
                and trusted_report.status
                in {"collecting", "pending_confirmation", "completed"}
                and reviewed_arguments.get("expected_version")
                == trusted_report.version
            )
            if not same_resolved_date and not (
                draft_arguments.get("date_selection") == "server_default"
                and reviewed_trusted_valid
            ):
                raise ValueError(
                    "Daily review cannot change an untrusted report-date binding"
                )
            if reviewed_arguments.get("date_selection") == "trusted_report":
                if not reviewed_trusted_valid:
                    raise ValueError(
                        "Daily review cannot change an untrusted report-date binding"
                    )
                for key in date_target_keys:
                    draft_arguments[key] = reviewed_arguments.get(key)
        reviewed_empty_evidence = reviewed_arguments.get(
            "empty_field_evidence",
            [],
        )
        if not isinstance(reviewed_empty_evidence, list):
            raise ValueError("reviewed Daily empty-field evidence must be an array")
        draft_arguments["items"] = reviewed_arguments.get("items", [])
        draft_arguments["empty_field_evidence"] = reviewed_empty_evidence
        reviewed_omitted_empty_fields = list(
            reviewed_arguments.get("reviewed_omitted_empty_fields", ())
        )
        draft_arguments["reviewed_omitted_empty_fields"] = (
            reviewed_omitted_empty_fields
        )
        draft_arguments["acknowledged_empty_fields"] = [
            evidence.get("field")
            for evidence in reviewed_empty_evidence
            if isinstance(evidence, dict)
        ] + reviewed_omitted_empty_fields
        constrained_arguments = validate_tool_arguments(
            "add_daily_items",
            draft_arguments,
        )
        constrained.append(
            NativeToolCall(
                reviewed_call.tool_call_id,
                "add_daily_items",
                constrained_arguments,
            )
        )
    return tuple(constrained)


def _daily_add_target_date(
    arguments: dict[str, Any],
    *,
    context: TrustedContext | None,
) -> date | None:
    selection = arguments.get("date_selection")
    if selection == "server_default" and context is not None:
        return default_daily_write_date(
            now=context.now,
            timezone=context.principal.timezone,
        )
    if selection in {"agent2_semantic", "user_explicit"}:
        raw = arguments.get("proposed_date")
        try:
            return raw if isinstance(raw, date) else date.fromisoformat(str(raw))
        except ValueError:
            return None
    if selection == "trusted_report" and context is not None:
        raw_id = arguments.get("report_id")
        try:
            report = context.report_by_id(UUID(str(raw_id)))
        except ValueError:
            return None
        return report.report_date if report is not None else None
    if selection == "trusted_failed_write" and context is not None:
        candidate = context.retryable_daily_write
        return candidate.target_date if candidate is not None else None
    return None


def _restore_dropped_daily_adds(
    *,
    original: _ParsedAssistantTurn,
    reviewed: _ParsedAssistantTurn,
    adjudicated: _ParsedAssistantTurn,
) -> _ParsedAssistantTurn:
    """Restore a fully matching fresh Daily decision into the first review batch."""

    original_daily = tuple(
        call for call in original.tool_calls if call.tool_name == "add_daily_items"
    )
    reviewed_daily = tuple(
        call for call in reviewed.tool_calls if call.tool_name == "add_daily_items"
    )
    adjudicated_daily = tuple(
        call
        for call in adjudicated.tool_calls
        if call.tool_name == "add_daily_items"
    )
    if reviewed_daily:
        raise ValueError("Daily add recovery requires a full first-review deletion")
    if len(original_daily) != 1 or len(adjudicated_daily) != 1:
        raise ValueError(
            "Daily add adjudication requires exactly one original and fresh call"
        )
    if len(adjudicated_daily) != len(adjudicated.tool_calls):
        raise ValueError("Daily add adjudication cannot introduce another tool")
    if [
        _daily_add_agreement_signature(call) for call in original_daily
    ] != [
        _daily_add_agreement_signature(call) for call in adjudicated_daily
    ]:
        raise ValueError("Daily add adjudication does not match the original decision")

    constrained_daily = _constrain_daily_add_call_group(
        original_daily=original_daily,
        reviewed_daily=adjudicated_daily,
    )
    restored_calls = list(reviewed.tool_calls)
    inserted = 0
    original_reviewed_calls = tuple(
        call
        for call in original.tool_calls
        if _daily_weekly_write_domain(call.tool_name) is not None
    )
    for original_call, recovered_call in zip(
        original_daily,
        constrained_daily,
        strict=True,
    ):
        original_position = next(
            index
            for index, call in enumerate(original_reviewed_calls)
            if call is original_call
        )
        preceding_non_adds = sum(
            call.tool_name != "add_daily_items"
            for call in original_reviewed_calls[:original_position]
        )
        insertion_index = min(
            preceding_non_adds + inserted,
            len(restored_calls),
        )
        restored_calls.insert(insertion_index, recovered_call)
        inserted += 1
    return replace(
        reviewed,
        tool_calls=tuple(restored_calls),
        audit=(*reviewed.audit, *adjudicated.audit),
    )


def _merge_daily_weekly_write_review(
    *,
    original: _ParsedAssistantTurn,
    reviewed: _ParsedAssistantTurn,
    reviewed_tool_names: frozenset[str],
    context: TrustedContext,
) -> _ParsedAssistantTurn:
    reviewed = _constrain_weekly_plan_review_submission(
        original=original,
        reviewed=reviewed,
    )
    if _has_daily_replacement_followup_context(context):
        original = _normalize_daily_replacement_call_order(original)
        reviewed = _normalize_daily_replacement_call_order(reviewed)
    original_reviewed_domains = {
        domain
        for call in original.tool_calls
        if (domain := _daily_weekly_write_domain(call.tool_name)) is not None
    }
    reviewed_domains = {
        domain
        for call in reviewed.tool_calls
        if (domain := _daily_weekly_write_domain(call.tool_name)) is not None
    }
    original_daily_writes = tuple(
        call
        for call in original.tool_calls
        if _daily_weekly_write_domain(call.tool_name) == "daily"
    )
    adjudicated_daily_add_reclassification = bool(
        original_daily_writes
        and all(
            call.tool_name == "add_daily_items"
            for call in original_daily_writes
        )
        and "daily" not in reviewed_domains
        and reviewed_domains
    )
    if (
        reviewed.tool_calls
        and not original_reviewed_domains.issubset(reviewed_domains)
        and not adjudicated_daily_add_reclassification
    ):
        raise ValueError(
            "review must preserve every reviewed write domain; "
            "preserve non-edit Daily write names and order"
        )
    if any(
        call.tool_name
        in {"edit_daily_items", "delete_daily_items", "move_daily_items"}
        for call in original.tool_calls
    ):
        reviewed = _constrain_daily_edit_review(
            original=original,
            reviewed=reviewed,
        )
    if any(call.tool_name == "add_daily_items" for call in original.tool_calls) and any(
        call.tool_name == "add_daily_items" for call in reviewed.tool_calls
    ):
        reviewed = _constrain_daily_write_review(
            original=original,
            reviewed=reviewed,
            context=context,
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


def _constrain_weekly_plan_review_submission(
    *,
    original: _ParsedAssistantTurn,
    reviewed: _ParsedAssistantTurn,
) -> _ParsedAssistantTurn:
    """A reviewer may correct a plan change but cannot newly authorize submission."""

    original_has_submission = any(
        call.tool_name == "submit_next_weekly_plan"
        for call in original.tool_calls
    )
    reviewed_has_new_submission = any(
        call.tool_name == "submit_next_weekly_plan"
        for call in reviewed.tool_calls
    )
    if original_has_submission or not reviewed_has_new_submission:
        return reviewed

    kept_calls = tuple(
        call
        for call in reviewed.tool_calls
        if call.tool_name != "submit_next_weekly_plan"
    )
    if not kept_calls:
        return _ParsedAssistantTurn(
            assistant_message={
                "role": "assistant",
                "content": json.dumps(
                    {"decision": "keep_original"},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
            tool_calls=(),
            audit=reviewed.audit,
        )

    assistant_message = dict(reviewed.assistant_message)
    assistant_message["tool_calls"] = [
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
        for call in kept_calls
    ]
    return replace(
        reviewed,
        assistant_message=assistant_message,
        tool_calls=kept_calls,
    )


def _normalize_daily_replacement_call_order(
    parsed: _ParsedAssistantTurn,
) -> _ParsedAssistantTurn:
    """Canonicalize one same-target delete-plus-add batch before comparison."""

    if len(parsed.tool_calls) != 2:
        return parsed
    by_name = {call.tool_name: call for call in parsed.tool_calls}
    if set(by_name) != {"delete_daily_items", "add_daily_items"}:
        return parsed
    delete_call = by_name["delete_daily_items"]
    add_call = by_name["add_daily_items"]
    if (
        delete_call.arguments.get("report_id")
        != add_call.arguments.get("report_id")
        or delete_call.arguments.get("expected_version")
        != add_call.arguments.get("expected_version")
    ):
        return parsed
    return replace(parsed, tool_calls=(delete_call, add_call))


def _mark_reviewed_daily_content(
    parsed: _ParsedAssistantTurn,
) -> _ParsedAssistantTurn:
    """Attach server-only proof after the independent semantic review passes."""

    reviewed_calls = tuple(
        NativeToolCall(
            call.tool_call_id,
            call.tool_name,
            validate_tool_arguments(
                "add_daily_items",
                {**call.arguments, "content_reviewed": True},
            ),
        )
        if call.tool_name == "add_daily_items"
        else call
        for call in parsed.tool_calls
    )
    if reviewed_calls == parsed.tool_calls:
        return parsed
    assistant_message = dict(parsed.assistant_message)
    assistant_message["tool_calls"] = [
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
        for call in reviewed_calls
    ]
    return replace(
        parsed,
        assistant_message=assistant_message,
        tool_calls=reviewed_calls,
    )


def _mark_reviewed_periodic_report_content(
    parsed: _ParsedAssistantTurn,
) -> _ParsedAssistantTurn:
    """Attach server-only proof after Weekly Report semantic review passes."""

    reviewed_calls = tuple(
        NativeToolCall(
            call.tool_call_id,
            call.tool_name,
            validate_tool_arguments(
                "apply_current_weekly_report",
                {**call.arguments, "content_reviewed": True},
            ),
        )
        if call.tool_name == "apply_current_weekly_report"
        else call
        for call in parsed.tool_calls
    )
    if reviewed_calls == parsed.tool_calls:
        return parsed
    assistant_message = dict(parsed.assistant_message)
    assistant_message["tool_calls"] = [
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
        for call in reviewed_calls
    ]
    return replace(
        parsed,
        assistant_message=assistant_message,
        tool_calls=reviewed_calls,
    )


def _mark_reviewed_weekly_plan_content(
    parsed: _ParsedAssistantTurn,
) -> _ParsedAssistantTurn:
    """Attach server-only proof after weekly-plan semantic review passes."""

    reviewed_calls = tuple(
        NativeToolCall(
            call.tool_call_id,
            call.tool_name,
            validate_tool_arguments(
                "apply_next_weekly_plan",
                {**call.arguments, "content_reviewed": True},
            ),
        )
        if call.tool_name == "apply_next_weekly_plan"
        else call
        for call in parsed.tool_calls
    )
    if reviewed_calls == parsed.tool_calls:
        return parsed
    assistant_message = dict(parsed.assistant_message)
    assistant_message["tool_calls"] = [
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
        for call in reviewed_calls
    ]
    return replace(
        parsed,
        assistant_message=assistant_message,
        tool_calls=reviewed_calls,
    )


def _constrain_daily_edit_review(
    *,
    original: _ParsedAssistantTurn,
    reviewed: _ParsedAssistantTurn,
) -> _ParsedAssistantTurn:
    """Keep trusted item targets while accepting reviewed Daily semantics."""

    if not reviewed.tool_calls:
        return reviewed
    targeted_tools = {
        "edit_daily_items",
        "delete_daily_items",
        "move_daily_items",
    }

    def target_key(call: NativeToolCall) -> tuple[Any, Any, tuple[Any, ...]]:
        raw_item_ids = call.arguments.get("target_item_ids")
        item_ids = tuple(raw_item_ids) if isinstance(raw_item_ids, list) else ()
        return (
            call.arguments.get("report_id"),
            call.arguments.get("expected_version"),
            item_ids,
        )

    def daily_sequence(parsed: _ParsedAssistantTurn) -> tuple[Any, ...]:
        return tuple(
            ("targeted",)
            if call.tool_name in targeted_tools
            else ("fixed", call.tool_name)
            for call in parsed.tool_calls
            if _daily_weekly_write_domain(call.tool_name) == "daily"
        )

    if daily_sequence(original) != daily_sequence(reviewed):
        raise ValueError(
            "Daily edit review must preserve non-edit Daily write names and order"
        )
    original_siblings = tuple(
        call
        for call in original.tool_calls
        if _daily_weekly_write_domain(call.tool_name) == "daily"
        and call.tool_name not in targeted_tools
    )
    reviewed_siblings = tuple(
        call
        for call in reviewed.tool_calls
        if _daily_weekly_write_domain(call.tool_name) == "daily"
        and call.tool_name not in targeted_tools
    )
    for original_call, reviewed_call in zip(
        original_siblings,
        reviewed_siblings,
        strict=True,
    ):
        if original_call.tool_name == "add_daily_items":
            continue
        if original_call.arguments != reviewed_call.arguments:
            raise ValueError(
                "Daily edit review cannot change non-edit Daily write arguments"
            )

    original_edits = tuple(
        call for call in original.tool_calls if call.tool_name in targeted_tools
    )
    reviewed_edits = tuple(
        call for call in reviewed.tool_calls if call.tool_name in targeted_tools
    )
    if len(original_edits) != len(reviewed_edits):
        raise ValueError("Daily targeted review must preserve every target")

    original_by_target = {target_key(call): call for call in original_edits}
    reviewed_by_target = {target_key(call): call for call in reviewed_edits}
    if (
        len(original_by_target) != len(original_edits)
        or len(reviewed_by_target) != len(reviewed_edits)
        or set(original_by_target) != set(reviewed_by_target)
    ):
        raise ValueError(
            "Daily targeted review cannot change report, version, or stable item IDs; "
            "cannot change non-edit Daily write arguments"
        )

    constrained_by_review_id: dict[str, NativeToolCall] = {}
    for target, reviewed_call in reviewed_by_target.items():
        draft_call = original_by_target[target]
        if (
            reviewed_call.tool_name != draft_call.tool_name
            and (
                draft_call.tool_name,
                reviewed_call.tool_name,
            )
            != ("delete_daily_items", "edit_daily_items")
        ):
            raise ValueError(
                "Daily targeted review cannot escalate or redirect the draft operation"
            )
        constrained_arguments = {
            "report_id": draft_call.arguments.get("report_id"),
            "expected_version": draft_call.arguments.get("expected_version"),
            "target_item_ids": draft_call.arguments.get("target_item_ids"),
        }
        if reviewed_call.tool_name == "edit_daily_items":
            constrained_arguments.update(
                {
                    "replacement": (
                        draft_call.arguments.get("replacement")
                        if draft_call.tool_name == "edit_daily_items"
                        else reviewed_call.arguments.get("replacement")
                    ),
                    "replacement_evidence": reviewed_call.arguments.get(
                        "replacement_evidence"
                    ),
                }
            )
        elif reviewed_call.tool_name == "move_daily_items":
            constrained_arguments.update(
                {
                    "source_field": reviewed_call.arguments.get("source_field"),
                    "target_field": reviewed_call.arguments.get("target_field"),
                }
            )
        validated_arguments = validate_tool_arguments(
            reviewed_call.tool_name,
            constrained_arguments,
        )
        constrained_by_review_id[reviewed_call.tool_call_id] = NativeToolCall(
            reviewed_call.tool_call_id,
            reviewed_call.tool_name,
            validated_arguments,
        )

    return replace(
        reviewed,
        tool_calls=tuple(
            constrained_by_review_id.get(call.tool_call_id, call)
            for call in reviewed.tool_calls
        ),
    )


def _constrain_daily_write_review(
    *,
    original: _ParsedAssistantTurn,
    reviewed: _ParsedAssistantTurn,
    context: TrustedContext,
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
    constrained_calls = iter(
        _constrain_daily_add_call_group(
            original_daily=original_daily,
            reviewed_daily=reviewed_daily,
            context=context,
        )
    )
    return replace(
        reviewed,
        tool_calls=tuple(
            next(constrained_calls)
            if call.tool_name == "add_daily_items"
            else call
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
