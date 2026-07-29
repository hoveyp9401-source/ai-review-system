from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import timedelta
from time import perf_counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.agent2.business.models import Agent2IdentityBinding
from app.agent2.memory import PersonalMemoryModule
from app.agent2.memory.postgres import PostgresPersonalMemoryReadStore
from app.agent2.tool_calling.assembly import (
    TrustedContextAssembler,
    TrustedContextRequest,
)
from app.agent2.tool_calling.canary_config import (
    CANARY_MAX_REQUEST_ATTEMPTS,
    CANARY_MAX_TOOL_LOOPS,
    CANARY_MODEL_NAME,
    CANARY_MODEL_PROVIDER,
    CANARY_RECENT_MESSAGE_LIMIT,
    CANARY_RECENT_OPERATION_LIMIT,
    CANARY_RETRY_BACKOFF_SECONDS,
    CANARY_THINKING_ENABLED,
    CANARY_TIMEOUT_SECONDS,
    canary_prompt_sha256,
    canary_system_prompt,
)
from app.agent2.tool_calling.canary_control import (
    CanaryControlSnapshot,
    CanaryIdentitySnapshot,
    CanaryRouteDecision,
    CanaryRuntimeAttestation,
    decide_canary_route,
)
from app.agent2.tool_calling.canary_metrics import (
    CanaryMetricEvent,
    CanaryMetricsRecorder,
)
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekToolCallingAdapter,
)
from app.agent2.tool_calling.production_contracts import (
    ProductionExecutionCapability,
)
from app.agent2.tool_calling.production_daily_executor import source_text_hash
from app.agent2.tool_calling.production_runtime import ProductionRuntime
from app.agent2.tool_calling.production_store import ProductionContextStore
from app.agent2.tool_calling.receipt_reply import canary_block_message
from app.agent2.tool_calling.registry import (
    runtime_registry_contract_digest,
)
from app.agent2.tool_calling.salutation_onboarding import (
    PersonalMemoryOnboarding,
    PostgresPersonalMemoryOnboardingStore,
)

_model_audit_logger = logging.getLogger(
    "agent2.tool_calling.model_audit"
)


@dataclass(frozen=True)
class CanaryRouteResolution:
    decision: CanaryRouteDecision
    control: ToolCallCanaryControl | None = None
    binding: Agent2IdentityBinding | None = None
    capability: ProductionExecutionCapability | None = None


@dataclass(frozen=True)
class CanaryIngressOutcome:
    owner: str
    reason: str
    message: str = ""
    report_id: str | None = None
    handled: bool = False
    actual_write: bool = False
    messages_enabled: bool = False


class CanaryIngressExecutionError(RuntimeError):
    """Carry the server-owned delivery policy across transport error handling."""

    reason = "tool_call_canary_execution_failed"

    def __init__(
        self,
        *,
        messages_enabled: bool,
        error_type: str,
        reason: str | None = None,
    ) -> None:
        self.reason = reason or self.reason
        super().__init__(self.reason)
        self.messages_enabled = messages_enabled
        self.error_type = error_type

    def outcome(self) -> CanaryIngressOutcome:
        return CanaryIngressOutcome(
            owner="blocked",
            reason=self.reason,
            message=canary_block_message(self.reason),
            handled=True,
            actual_write=False,
            messages_enabled=self.messages_enabled,
        )


def _record_canary_execution_failure(
    *,
    error: Exception,
    messages_enabled: bool,
    started: float,
    source_message_id: str,
    tenant_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
) -> CanaryIngressExecutionError:
    model_turns = tuple(getattr(error, "model_turns", ()) or ())
    failure_reason = _canary_execution_failure_reason(error)
    _model_audit_logger.info(
        "agent2_tool_call_model_audit %s",
        json.dumps(
            {
                "schema_version": "agent2.tool_call.model_audit.v1",
                "status": "failed",
                "error_type": type(error).__name__,
                "error_message": str(error),
                "failure_reason": failure_reason,
                "source_message_id": source_message_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "model_turns": [
                    asdict(turn)
                    for turn in model_turns
                    if is_dataclass(turn) and not isinstance(turn, type)
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
    )
    CanaryMetricsRecorder().record(
        CanaryMetricEvent(
            tool_names=(),
            success_count=0,
            failure_count=1,
            clarification_count=0,
            receipt_mismatch_count=0,
            rollback_count=0,
            latency_ms=max(
                0,
                int((perf_counter() - started) * 1000),
            ),
            model_error_count=1,
        )
    )
    return CanaryIngressExecutionError(
        messages_enabled=messages_enabled,
        error_type=type(error).__name__,
        reason=failure_reason,
    )


def _canary_execution_failure_reason(error: Exception) -> str:
    runtime_failure_prefix = "production runtime failed closed:"
    error_message = str(error).strip()
    if error_message.startswith(runtime_failure_prefix):
        error_code = error_message.removeprefix(
            runtime_failure_prefix
        ).strip()
        if error_code == "invalid_report_state":
            return "tool_call_canary_report_already_submitted"
    return "tool_call_canary_execution_failed"


_CANARY_TRANSPORT_MARKER = "_agent2_tool_call_canary"


def build_canary_response_payload(
    outcome: CanaryIngressOutcome,
) -> dict[str, Any]:
    """Build a cached transport payload without retaining suppressed text."""

    if outcome.messages_enabled:
        return {
            "msgtype": "text",
            "text": {"content": outcome.message},
        }
    return {
        _CANARY_TRANSPORT_MARKER: {
            "messages_enabled": False,
            "delivery": "suppressed",
        }
    }


def is_canary_message_delivery_suppressed(
    response_payload: Mapping[str, Any] | None,
) -> bool:
    """Recognize the server-owned no-delivery marker on persisted responses."""

    if not isinstance(response_payload, Mapping):
        return False
    marker = response_payload.get(_CANARY_TRANSPORT_MARKER)
    return (
        isinstance(marker, Mapping)
        and marker.get("messages_enabled") is False
        and marker.get("delivery") == "suppressed"
    )


def canary_route_suppresses_delivery(
    resolution: CanaryRouteResolution,
) -> bool:
    """Decide duplicate delivery from the current server-owned control."""

    return bool(
        resolution.decision.claimed
        and resolution.control is not None
        and resolution.control.enabled
        and not resolution.control.messages_enabled
    )


async def deliver_canary_message_if_enabled(
    outcome: CanaryIngressOutcome,
    sender: Callable[[], Awaitable[Any]],
) -> bool:
    """Call a DingTalk sender only when the server-owned delivery flag is open."""

    if not outcome.messages_enabled:
        return False
    await sender()
    return True


async def deliver_cached_canary_message_if_enabled(
    response_payload: Mapping[str, Any] | None,
    sender: Callable[[str], Awaitable[Any]],
) -> bool:
    """Deliver cached text unless its server marker forbids delivery."""

    if is_canary_message_delivery_suppressed(response_payload):
        return False
    if not isinstance(response_payload, Mapping):
        return False
    text_block = response_payload.get("text")
    if not isinstance(text_block, Mapping):
        return False
    content = text_block.get("content")
    if not isinstance(content, str) or not content:
        return False
    await sender(content)
    return True


async def resolve_tool_call_canary_route(
    session: Any,
    *,
    user: Any,
    dingtalk_user_id: str,
    settings: object,
    conversation_id: str,
    source_message_id: str,
    now,
) -> CanaryRouteResolution:
    bindings = list(
        (
            await session.scalars(
                select(Agent2IdentityBinding)
                .where(
                    Agent2IdentityBinding.dingtalk_user_id
                    == dingtalk_user_id,
                    Agent2IdentityBinding.user_id == str(user.id),
                    Agent2IdentityBinding.active.is_(True),
                )
                .limit(3)
            )
        ).all()
    )
    if not bindings:
        return CanaryRouteResolution(
            decide_canary_route(
                control=None,
                identity=None,
                runtime=_runtime_attestation(settings),
                active_canary_control_count=0,
            )
        )
    tenant_ids = tuple(
        dict.fromkeys(binding.tenant_id for binding in bindings)
    )
    controls = list(
        (
            await session.scalars(
                select(ToolCallCanaryControl).where(
                    ToolCallCanaryControl.user_id == str(user.id),
                    ToolCallCanaryControl.tenant_id.in_(tenant_ids),
                )
            )
        ).all()
    )
    if not controls:
        return CanaryRouteResolution(
            decide_canary_route(
                control=None,
                identity=None,
                runtime=_runtime_attestation(settings),
                active_canary_control_count=0,
            )
        )
    if len(controls) != 1:
        return CanaryRouteResolution(
            CanaryRouteDecision(
                owner="agent1",
                reason="tool_call_canary_control_ambiguous",
                claimed=True,
            )
        )
    control = controls[0]
    exact_bindings = tuple(
        binding
        for binding in bindings
        if binding.tenant_id == control.tenant_id
        and binding.user_id == control.user_id
    )
    binding = exact_bindings[0] if len(exact_bindings) == 1 else None
    identity = CanaryIdentitySnapshot(
        tenant_id=control.tenant_id,
        user_id=control.user_id,
        active=binding is not None,
        exact_binding_count=len(exact_bindings),
    )
    active_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ToolCallCanaryControl)
            .where(ToolCallCanaryControl.enabled.is_(True))
        )
        or 0
    )
    snapshot = CanaryControlSnapshot(
        tenant_id=control.tenant_id,
        user_id=control.user_id,
        enabled=bool(control.enabled),
        runtime=control.runtime,
        messages_enabled=bool(control.messages_enabled),
        registry_digest=control.registry_digest,
        prompt_sha256=control.prompt_sha256,
        model_name=control.model_name,
        version=control.version,
    )
    decision = decide_canary_route(
        control=snapshot,
        identity=identity,
        runtime=_runtime_attestation(settings),
        active_canary_control_count=active_count,
        active_canary_control_limit=_configured_active_control_limit(
            settings
        ),
    )
    capability = (
        ProductionExecutionCapability(
            tenant_id=control.tenant_id,
            user_id=control.user_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            control_key=control.control_key,
            control_version=control.version,
            registry_digest=control.registry_digest,
            prompt_sha256=control.prompt_sha256,
            model_name=control.model_name,
            expires_at=now + timedelta(minutes=2),
            enabled=True,
            messages_enabled=bool(control.messages_enabled),
        )
        if decision.owner == "tool_call_core"
        else None
    )
    return CanaryRouteResolution(decision, control, binding, capability)


async def process_tool_call_canary_ingress(
    session: Any,
    *,
    user: Any,
    dingtalk_user_id: str,
    user_text: str,
    user_messages: tuple[str, ...] = (),
    source_channel: str,
    conversation_id: str,
    source_message_id: str,
    settings: object,
    llm_client: Any,
    now,
) -> CanaryIngressOutcome:
    ordered_user_messages = _ordered_user_messages(
        user_text=user_text,
        user_messages=user_messages,
    )
    canonical_conversation_id = (
        conversation_id.strip()
        or f"dingtalk:{source_channel}:{dingtalk_user_id}"
    )
    started = perf_counter()
    try:
        resolution = await resolve_tool_call_canary_route(
            session,
            user=user,
            dingtalk_user_id=dingtalk_user_id,
            settings=settings,
            conversation_id=canonical_conversation_id,
            source_message_id=source_message_id,
            now=now,
        )
    except Exception as exc:
        raise _record_canary_execution_failure(
            error=exc,
            messages_enabled=False,
            started=started,
            source_message_id=source_message_id,
            user_id=str(getattr(user, "id", "") or ""),
            conversation_id=canonical_conversation_id,
        ) from exc
    decision = resolution.decision
    if decision.owner in {"existing_runtime", "agent1"}:
        return CanaryIngressOutcome(
            owner=decision.owner,
            reason=decision.reason,
        )
    if decision.owner == "blocked":
        return CanaryIngressOutcome(
            owner="blocked",
            reason=decision.reason,
            message=canary_block_message(decision.reason),
            handled=True,
            messages_enabled=bool(
                resolution.control
                and resolution.control.messages_enabled
            ),
        )
    if (
        resolution.binding is None
        or resolution.capability is None
        or resolution.control is None
    ):
        return CanaryIngressOutcome(
            owner="blocked",
            reason="tool_call_canary_server_scope_missing",
            message=canary_block_message(
                "tool_call_canary_server_scope_missing"
            ),
            handled=True,
            messages_enabled=False,
        )

    try:
        context_store = ProductionContextStore(
            session,
            user=user,
            tenant_id=resolution.binding.tenant_id,
            settings=settings,
        )
        context = await TrustedContextAssembler(
            read_port=context_store,
            policy_port=context_store,
            recent_message_limit=CANARY_RECENT_MESSAGE_LIMIT,
            recent_operation_limit=CANARY_RECENT_OPERATION_LIMIT,
            namespace=CANARY_STATE_NAMESPACE,
            personal_memory_module=PersonalMemoryModule(
                read_port=PostgresPersonalMemoryReadStore(session)
            ),
        ).assemble(
            TrustedContextRequest(
                tenant_id=resolution.binding.tenant_id,
                user_id=user.id,
                conversation_id=canonical_conversation_id,
                source_message_id=source_message_id,
                timezone=(
                    getattr(user, "timezone", "")
                    or getattr(settings, "timezone", "Asia/Shanghai")
                ),
                server_now=now,
                display_name=(
                    str(getattr(user, "name", "")).strip()
                    or None
                ),
                runtime_provider_name=CANARY_MODEL_PROVIDER,
                runtime_model_name=CANARY_MODEL_NAME,
            )
        )
        runtime_session = ProductionRuntime().open_session(
            session=session,
            user=user,
            settings=settings,
            context=context,
            capability=resolution.capability,
            source_channel=source_channel,
            source_text_hash=source_text_hash(
                _canonical_user_input(ordered_user_messages)
            ),
        )
        adapter = DeepSeekToolCallingAdapter(
            http_client=llm_client.native_http_client,
            model=CANARY_MODEL_NAME,
            timeout_seconds=CANARY_TIMEOUT_SECONDS,
            max_tool_loops=CANARY_MAX_TOOL_LOOPS,
            max_request_attempts=CANARY_MAX_REQUEST_ATTEMPTS,
            retry_backoff_seconds=CANARY_RETRY_BACKOFF_SECONDS,
            endpoint=(
                f"{str(getattr(settings, 'llm_base_url', '')).rstrip('/')}"
                "/chat/completions"
            ),
        )
        result = await adapter.run_canary_turn(
            system_prompt=canary_system_prompt(),
            user_text=(
                ordered_user_messages[0]
                if len(ordered_user_messages) == 1
                else ""
            ),
            user_messages=(
                ordered_user_messages
                if len(ordered_user_messages) > 1
                else ()
            ),
            context=context,
            runtime_session=runtime_session,
            thinking_enabled=CANARY_THINKING_ENABLED,
        )
        final_content = (
            _trusted_authoritative_read_response(result.receipts)
            or result.final_content
        )
        if resolution.control.messages_enabled:
            final_content = await PersonalMemoryOnboarding(
                store=PostgresPersonalMemoryOnboardingStore(session)
            ).apply(
                context=context,
                content=final_content,
            )
    except Exception as exc:
        failure = _record_canary_execution_failure(
            error=exc,
            messages_enabled=bool(
                resolution.control.messages_enabled
            ),
            started=started,
            source_message_id=source_message_id,
            tenant_id=resolution.binding.tenant_id,
            user_id=str(getattr(user, "id", "") or ""),
            conversation_id=canonical_conversation_id,
        )
        if failure.messages_enabled:
            return failure.outcome()
        raise failure from exc
    CanaryMetricsRecorder().record(
        CanaryMetricEvent(
            tool_names=tuple(
                receipt.tool_name for receipt in result.receipts
            ),
            success_count=sum(
                receipt.status.value in {"success", "no_op"}
                for receipt in result.receipts
            ),
            failure_count=sum(
                receipt.status.value in {"failed", "blocked"}
                for receipt in result.receipts
            ),
            clarification_count=sum(
                receipt.status.value == "clarification_required"
                for receipt in result.receipts
            ),
            receipt_mismatch_count=0,
            rollback_count=sum(
                runtime_result.rolled_back
                for runtime_result in result.runtime_results
            ),
            latency_ms=max(0, int((perf_counter() - started) * 1000)),
            model_error_count=0,
        )
    )
    report_id = next(
        (
            receipt.target_id
            for receipt in reversed(result.receipts)
            if receipt.target_type == "daily_report"
            and receipt.target_id
        ),
        None,
    )
    return CanaryIngressOutcome(
        owner="tool_call_core",
        reason=decision.reason,
        message=final_content,
        report_id=report_id,
        handled=True,
        actual_write=any(receipt.changed for receipt in result.receipts),
        messages_enabled=bool(resolution.control.messages_enabled),
    )


def _trusted_authoritative_read_response(
    receipts: tuple[Any, ...],
) -> str:
    """Prefer the server-rendered read result over model-authored wording."""

    for receipt in reversed(receipts):
        if bool(getattr(receipt, "changed", False)):
            continue
        safe_facts = getattr(receipt, "safe_user_facts", None)
        if not isinstance(safe_facts, dict):
            continue
        if (
            safe_facts.get("actual_write") is not False
            or safe_facts.get("authoritative_read_response")
            is not True
        ):
            continue
        response_text = str(
            safe_facts.get("response_text") or ""
        ).strip()
        if response_text:
            return response_text
    return ""


def _ordered_user_messages(
    *,
    user_text: str,
    user_messages: tuple[str, ...],
) -> tuple[str, ...]:
    normalized = tuple(str(value) for value in user_messages)
    if not normalized:
        normalized = (str(user_text),)
    if any(not value.strip() for value in normalized):
        raise ValueError("user input cannot contain empty fragments")
    return normalized


def _canonical_user_input(user_messages: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "ordered_user_messages": [
                {"sequence": index, "content": value}
                for index, value in enumerate(user_messages, start=1)
            ]
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _runtime_attestation(settings: object) -> CanaryRuntimeAttestation:
    return CanaryRuntimeAttestation(
        runtime_ready=True,
        runtime_mode="canary_execute",
        registry_digest=runtime_registry_contract_digest(settings),
        prompt_sha256=canary_prompt_sha256(),
        model_name=CANARY_MODEL_NAME,
        production_database_verified=_production_database_declared(settings),
        sandbox_configuration_present=any(
            "SANDBOX" in key.casefold().upper()
            and value.strip()
            for key, value in os.environ.items()
            if key.upper().startswith(("AGENT2_", "DATABASE_"))
        ),
        messages_sender_configured=bool(
            getattr(settings, "dingtalk_app_key", "")
            and getattr(settings, "dingtalk_app_secret", "")
        ),
        api_ingress_ready=True,
        stream_ingress_ready=True,
    )


def _configured_active_control_limit(settings: object) -> int:
    try:
        return int(
            getattr(
                settings,
                "agent2_tool_call_canary_max_active_users",
                1,
            )
        )
    except (TypeError, ValueError):
        return 0


def _production_database_declared(settings: object) -> bool:
    if str(getattr(settings, "app_env", "")).casefold() != "production":
        return False
    try:
        url = make_url(str(getattr(settings, "database_url", "")))
    except Exception:
        return False
    identity = "|".join(
        (
            str(url.host or ""),
            str(url.database or ""),
            str(url.username or ""),
        )
    ).casefold()
    return (
        url.get_backend_name() == "postgresql"
        and "sandbox" not in identity
    )
