from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.agent2.business.models import Agent2IdentityBinding
from app.agent2.memory import PersonalMemoryModule
from app.agent2.memory.postgres import PostgresPersonalMemoryReadStore
from app.agent2.periodic_report_context_loader import (
    ProductionPeriodicReportContextLoader,
)
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
from app.agent2.tool_calling.context import CANARY_STATE_NAMESPACE, TrustedContext
from app.agent2.tool_calling.current_turn_source import CurrentTurnSource
from app.agent2.tool_calling.daily_write_retry import (
    continued_daily_retry_evidence,
    validated_daily_retry_evidence,
)
from app.agent2.tool_calling.deepseek_adapter import (
    DeepSeekToolCallingAdapter,
)
from app.agent2.tool_calling.production_contracts import (
    ProductionExecutionCapability,
)
from app.agent2.tool_calling.production_runtime import ProductionRuntime
from app.agent2.tool_calling.production_store import ProductionContextStore
from app.agent2.tool_calling.receipt_reply import (
    canary_block_message,
)
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    runtime_registry_contract_digest,
    runtime_registry_tool_names,
)
from app.agent2.tool_calling.salutation_onboarding import (
    PersonalMemoryOnboarding,
    PostgresPersonalMemoryOnboardingStore,
)
from app.agent2.weekly_plan_context_loader import (
    ProductionWeeklyPlanContextLoader,
)
from app.agent2.weekly_plan_store import SqlWeeklyPlanStore
from app.utils.dingtalk_text import format_dingtalk_plain_text

_model_audit_logger = logging.getLogger(
    "agent2.tool_calling.model_audit"
)

_DEFENDANT_PERFORMANCE_GLOSSARY = {
    "被告绩效": (
        "指已发布被告案件报表中的确定性指标，不是员工评价，"
        "也不是日报内容。"
    ),
    "存量": (
        "截至统计截止日仍未结案的被告案件数量；问“多少”时只需"
        "给当前数量和必要比较，使用 summary。"
    ),
    "新增": (
        "本周或本月新增按登记日期落入查询区间统计；年度累计新增"
        "按当年1月1日至截止日统计。"
    ),
    "存量下降率": (
        "就是存量同比下降率，比较当前存量与去年同一截止日存量；"
        "它不是综合减损率，绝不能使用 explain_loss。"
    ),
    "新增下降率": (
        "就是年度累计新增同比下降率，比较今年与去年同期累计新增；"
        "它不是综合减损率。"
    ),
    "绩效完成情况": (
        "读取当前规则中存量同比目标和新增同比目标的状态；目标值"
        "来自已发布规则，不自行假定。"
    ),
    "周月维度": (
        "“本周、这周、周报”使用 week；“本月、这个月、月报”"
        "使用 month；未说明时使用 month。当前工具只读取当前已发布"
        "周或月，不得用当前数据冒充上周、上月或其他历史周期。"
        "即使用户问了暂不支持的历史周期，也要调用绩效查询工具，"
        "由服务器给出安全提示，不要直接自行回答。"
    ),
    "团队对比": (
        "“各团队、所有团队、各部门、团队对比”使用 "
        "query_defendant_performance，scope_type=department，"
        "mode=summary；部门范围的事实包已经包含全部团队，不要让"
        "用户逐个报团队名称。"
    ),
    "查询重点": (
        "简单问数量或整体完成情况使用 summary；问新增哪几件、"
        "从哪些分公司新增、为什么新增多，使用 explain_new；问存量"
        "构成或存量口径，使用 explain_stock；只有明确问综合减损率"
        "才使用 explain_loss，明确问实质减损金额才使用 "
        "explain_substantial。"
    ),
}


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
    model_call_count: int = 0
    model_request_attempt_count: int = 0
    model_transport_retry_count: int = 0
    model_elapsed_seconds: float = 0.0
    model_result_status: str = "not_called"
    tool_success_count: int = 0
    tool_no_op_count: int = 0
    tool_clarification_count: int = 0
    tool_blocked_count: int = 0
    tool_failure_count: int = 0
    source_turn_id: str | None = None
    tool_receipt_count: int = 0
    successful_pure_read: bool = False
    pre_execution_block_observations: tuple[dict[str, Any], ...] = ()
    daily_write_retry_continuation: dict[str, Any] | None = None
    user_visible_result: str = "unknown"
    reply_formed: bool = False


class CanaryIngressExecutionError(RuntimeError):
    """Carry the server-owned delivery policy across transport error handling."""

    reason = "tool_call_canary_execution_failed"

    def __init__(
        self,
        *,
        messages_enabled: bool,
        error_type: str,
        reason: str | None = None,
        model_call_count: int = 0,
        model_request_attempt_count: int = 0,
        model_transport_retry_count: int = 0,
        model_elapsed_seconds: float = 0.0,
        daily_write_retry_continuation: dict[str, Any] | None = None,
    ) -> None:
        self.reason = reason or self.reason
        super().__init__(self.reason)
        self.messages_enabled = messages_enabled
        self.error_type = error_type
        self.model_call_count = max(0, int(model_call_count))
        self.model_request_attempt_count = max(
            self.model_call_count,
            int(model_request_attempt_count),
        )
        self.model_transport_retry_count = max(
            0,
            int(model_transport_retry_count),
        )
        self.model_elapsed_seconds = max(
            0.0,
            float(model_elapsed_seconds),
        )
        self.daily_write_retry_continuation = (
            validated_daily_retry_evidence(
                daily_write_retry_continuation
            )
        )

    def outcome(self) -> CanaryIngressOutcome:
        return CanaryIngressOutcome(
            owner="blocked",
            reason=self.reason,
            message=canary_block_message(self.reason),
            handled=True,
            actual_write=False,
            messages_enabled=self.messages_enabled,
            model_call_count=self.model_call_count,
            model_request_attempt_count=self.model_request_attempt_count,
            model_transport_retry_count=self.model_transport_retry_count,
            model_elapsed_seconds=self.model_elapsed_seconds,
            model_result_status=(
                "failed"
                if self.model_request_attempt_count
                or self.model_call_count
                else "not_called"
            ),
            daily_write_retry_continuation=(
                self.daily_write_retry_continuation
            ),
            user_visible_result="failed",
            reply_formed=True,
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
    system_prompt_sha256: str | None = None,
    daily_write_retry_continuation: dict[str, Any] | None = None,
) -> CanaryIngressExecutionError:
    model_turns = tuple(getattr(error, "model_turns", ()) or ())
    model_call_count = max(
        len(model_turns),
        _nonnegative_count(getattr(error, "model_call_count", 0)),
    )
    model_request_attempt_count = max(
        model_call_count,
        _nonnegative_count(
            getattr(error, "request_attempt_count", 0)
        ),
    )
    model_transport_retry_count = _nonnegative_count(
        getattr(error, "transport_retry_count", 0)
    )
    model_elapsed_seconds = max(
        _model_elapsed_seconds(model_turns),
        _nonnegative_seconds(
            getattr(error, "model_elapsed_seconds", 0.0)
        ),
    )
    failure_reason = _canary_execution_failure_reason(error)
    _record_model_audit_safely(
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
            "system_prompt_sha256": system_prompt_sha256,
            "model_call_count": model_call_count,
            "model_request_attempt_count": model_request_attempt_count,
            "model_transport_retry_count": model_transport_retry_count,
            "model_elapsed_seconds": model_elapsed_seconds,
            "model_turns": [
                asdict(turn)
                for turn in model_turns
                if is_dataclass(turn) and not isinstance(turn, type)
            ],
        }
    )
    _record_canary_metric_safely(
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
    return CanaryIngressExecutionError(
        messages_enabled=messages_enabled,
        error_type=type(error).__name__,
        reason=failure_reason,
        model_call_count=model_call_count,
        model_request_attempt_count=model_request_attempt_count,
        model_transport_retry_count=model_transport_retry_count,
        model_elapsed_seconds=model_elapsed_seconds,
        daily_write_retry_continuation=(
            daily_write_retry_continuation
        ),
    )


def _nonnegative_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _nonnegative_seconds(value: Any) -> float:
    try:
        return round(max(0.0, float(value or 0.0)), 4)
    except (TypeError, ValueError):
        return 0.0


def _record_canary_metric_safely(**event_fields: Any) -> None:
    try:
        CanaryMetricsRecorder().record(
            CanaryMetricEvent(**event_fields)
        )
    except Exception:
        # Metrics are best-effort and cannot change the Agent2 turn result.
        pass


def _record_model_audit_safely(payload: Mapping[str, Any]) -> None:
    try:
        _model_audit_logger.info(
            "agent2_tool_call_model_audit %s",
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
    except Exception:
        # Audit transport failure cannot replace the original Agent2 result.
        pass


def _canary_execution_failure_reason(error: Exception) -> str:
    runtime_failure_prefix = "production runtime failed closed:"
    error_message = str(error).strip()
    if error_message.startswith(runtime_failure_prefix):
        error_code = error_message.removeprefix(
            runtime_failure_prefix
        ).strip()
        if error_code == "invalid_report_state":
            return "tool_call_canary_report_already_submitted"
        if error_code == "REPORT_INCOMPLETE":
            return "tool_call_canary_report_incomplete"
    return "tool_call_canary_execution_failed"


def _selected_daily_retry_continuation(
    *,
    error: Exception,
    context: TrustedContext | None,
) -> dict[str, Any] | None:
    """Keep a trusted candidate only after the model selected it exactly."""

    candidate = (
        getattr(context, "retryable_daily_write", None)
        if context is not None
        else None
    )
    if candidate is None:
        return None
    for audit in tuple(
        getattr(error, "raw_tool_call_audit", ()) or ()
    ):
        if getattr(audit, "tool_name", "") != "add_daily_items":
            continue
        try:
            arguments = json.loads(
                str(getattr(audit, "raw_arguments", "") or "")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(arguments, Mapping)
            and arguments.get("date_selection")
            == "trusted_failed_write"
            and arguments.get("retry_candidate_id")
            == candidate.candidate_id
        ):
            return continued_daily_retry_evidence(candidate)
    return None


_CANARY_TRANSPORT_MARKER = "_agent2_tool_call_canary"
_CANARY_TURN_OBSERVATION_MARKER = "_agent2_turn_observation_v1"


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
        },
    }


def build_canary_persisted_response_payload(
    outcome: CanaryIngressOutcome,
) -> dict[str, Any]:
    """Add internal status evidence only to the database copy."""

    payload = build_canary_response_payload(outcome)
    payload[_CANARY_TURN_OBSERVATION_MARKER] = (
        _canary_turn_observation(outcome)
    )
    return payload


def canary_provider_response_payload(
    persisted_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Remove internal monitoring fields before replying to DingTalk."""

    if not isinstance(persisted_payload, Mapping):
        return {}
    payload = dict(persisted_payload)
    payload.pop(_CANARY_TURN_OBSERVATION_MARKER, None)
    return payload


def _canary_turn_observation(
    outcome: CanaryIngressOutcome,
) -> dict[str, Any]:
    observation = {
        "schema_version": "agent2.turn.observation.v1",
        "message_processing_status": "consumed",
        "business_result_status": outcome.user_visible_result,
        "business_write_committed": bool(outcome.actual_write),
        "reply_status": "formed" if outcome.reply_formed else "missing",
        "transport_status": (
            "pending" if outcome.messages_enabled else "suppressed"
        ),
        "delivery_status": (
            "unverified" if outcome.messages_enabled else "suppressed"
        ),
        "model_call_count": max(0, outcome.model_call_count),
        "model_request_attempt_count": max(
            0,
            outcome.model_request_attempt_count,
        ),
        "model_transport_retry_count": max(
            0,
            outcome.model_transport_retry_count,
        ),
        "model_elapsed_seconds": max(
            0.0,
            outcome.model_elapsed_seconds,
        ),
        "model_result_status": outcome.model_result_status,
        "tool_success_count": max(0, outcome.tool_success_count),
        "tool_no_op_count": max(0, outcome.tool_no_op_count),
        "tool_clarification_count": max(
            0,
            outcome.tool_clarification_count,
        ),
        "tool_blocked_count": max(0, outcome.tool_blocked_count),
        "tool_failure_count": max(0, outcome.tool_failure_count),
        "pre_execution_blocks": [
            dict(item)
            for item in outcome.pre_execution_block_observations
        ],
    }
    if outcome.source_turn_id is not None:
        observation.update(
            {
                "source_turn_id": outcome.source_turn_id,
                "tool_receipt_count": max(
                    0,
                    outcome.tool_receipt_count,
                ),
                "successful_pure_read": bool(
                    outcome.successful_pure_read
                ),
            }
        )
    continuation = validated_daily_retry_evidence(
        outcome.daily_write_retry_continuation
    )
    if continuation is not None:
        observation["daily_write_retry_continuation"] = continuation
    return observation


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
                owner="blocked",
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
    conversation_kind: str = "unknown",
    message_occurred_at: Any | None = None,
    message_occurred_ats: tuple[Any, ...] = (),
) -> CanaryIngressOutcome:
    ordered_user_messages = _ordered_user_messages(
        user_text=user_text,
        user_messages=user_messages,
    )
    ordered_message_times = _ordered_message_times(
        message_count=len(ordered_user_messages),
        message_occurred_at=message_occurred_at,
        message_occurred_ats=message_occurred_ats,
    )
    current_turn_source = CurrentTurnSource(
        ordered_user_messages,
        occurred_at=ordered_message_times,
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
            messages_enabled=True,
            started=started,
            source_message_id=source_message_id,
            user_id=str(getattr(user, "id", "") or ""),
            conversation_id=canonical_conversation_id,
        ) from exc
    decision = resolution.decision
    if decision.owner == "blocked":
        message = canary_block_message(decision.reason)
        return CanaryIngressOutcome(
            owner="blocked",
            reason=decision.reason,
            message=message,
            handled=True,
            messages_enabled=(
                bool(resolution.control.messages_enabled)
                if resolution.control is not None
                else True
            ),
            user_visible_result="blocked",
            reply_formed=bool(message),
        )
    if (
        resolution.binding is None
        or resolution.capability is None
        or resolution.control is None
    ):
        message = canary_block_message(
            "tool_call_canary_server_scope_missing"
        )
        return CanaryIngressOutcome(
            owner="blocked",
            reason="tool_call_canary_server_scope_missing",
            message=message,
            handled=True,
            messages_enabled=True,
            user_visible_result="blocked",
            reply_formed=bool(message),
        )

    turn_transaction = await session.begin_nested()
    rendered_prompt_sha256: str | None = None
    context: TrustedContext | None = None
    try:
        context_store = ProductionContextStore(
            session,
            user=user,
            tenant_id=resolution.binding.tenant_id,
            settings=settings,
        )
        context_request = TrustedContextRequest(
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
            conversation_kind=conversation_kind,
            persisted_message_occurred_ats=ordered_message_times or (),
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
            weekly_plan_loader=ProductionWeeklyPlanContextLoader(
                SqlWeeklyPlanStore(session)
            ),
            periodic_report_loader=(
                ProductionPeriodicReportContextLoader(session)
            ),
        ).assemble(context_request)
        context = _attach_performance_glossary(
            context,
            settings=settings,
        )
        allowed_tool_names = frozenset(
            getattr(
                context,
                "allowed_tool_names",
                runtime_registry_tool_names(settings),
            )
        )
        system_prompt = canary_system_prompt(
            allowed_tool_names=allowed_tool_names
        )
        rendered_prompt_sha256 = hashlib.sha256(
            system_prompt.encode("utf-8")
        ).hexdigest()
        runtime_session = ProductionRuntime().open_session(
            session=session,
            user=user,
            settings=settings,
            context=context,
            capability=resolution.capability,
            source_channel=source_channel,
            source_text_hash=current_turn_source.sha256,
            current_turn_source=current_turn_source,
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
            system_prompt=system_prompt,
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
        final_content = _select_trusted_read_response(
            model_content=result.final_content,
            receipts=result.receipts,
        )
        if (
            resolution.control.messages_enabled
            and _should_apply_personal_salutation(
                result.receipts
            )
        ):
            final_content = await PersonalMemoryOnboarding(
                store=PostgresPersonalMemoryOnboardingStore(session)
            ).apply(
                context=context,
                content=final_content,
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
        formatted_message = format_dingtalk_plain_text(final_content)
        receipt_counts = _receipt_status_counts(result.receipts)
        metric_fields = {
            "tool_names": tuple(
                receipt.tool_name for receipt in result.receipts
            ),
            "success_count": sum(
                receipt.status.value in {"success", "no_op"}
                for receipt in result.receipts
            ),
            "failure_count": sum(
                receipt.status.value in {"failed", "blocked"}
                for receipt in result.receipts
            ),
            "clarification_count": sum(
                receipt.status.value == "clarification_required"
                for receipt in result.receipts
            ),
            "receipt_mismatch_count": 0,
            "rollback_count": sum(
                runtime_result.rolled_back
                for runtime_result in result.runtime_results
            ),
            "latency_ms": max(
                0,
                int((perf_counter() - started) * 1000),
            ),
            "model_error_count": 0,
        }
        outcome = CanaryIngressOutcome(
            owner="tool_call_core",
            reason=decision.reason,
            message=formatted_message,
            report_id=report_id,
            handled=True,
            actual_write=any(
                receipt.changed for receipt in result.receipts
            ),
            messages_enabled=bool(
                resolution.control.messages_enabled
            ),
            model_call_count=len(result.model_turns),
            model_request_attempt_count=(
                result.request_attempt_count
            ),
            model_transport_retry_count=(
                result.transport_retry_count
            ),
            model_elapsed_seconds=_model_elapsed_seconds(
                result.model_turns
            ),
            model_result_status="success",
            tool_success_count=receipt_counts["success"],
            tool_no_op_count=receipt_counts["no_op"],
            tool_clarification_count=receipt_counts[
                "clarification_required"
            ],
            tool_blocked_count=receipt_counts["blocked"],
            tool_failure_count=receipt_counts["failed"],
            source_turn_id=source_message_id,
            tool_receipt_count=len(result.receipts),
            successful_pure_read=(
                _is_successful_pure_read_turn(result.receipts)
            ),
            pre_execution_block_observations=(
                _pre_execution_block_observations(result.receipts)
            ),
            user_visible_result=_user_visible_result(receipt_counts),
            reply_formed=bool(formatted_message),
        )
        await turn_transaction.commit()
    except Exception as exc:
        if turn_transaction.is_active:
            await turn_transaction.rollback()
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
            system_prompt_sha256=rendered_prompt_sha256,
            daily_write_retry_continuation=(
                _selected_daily_retry_continuation(
                    error=exc,
                    context=context,
                )
            ),
        )
        if failure.messages_enabled:
            return failure.outcome()
        raise failure from exc
    _record_canary_metric_safely(**metric_fields)
    return outcome


def _receipt_status_counts(receipts: tuple[Any, ...]) -> dict[str, int]:
    counts = {
        "success": 0,
        "no_op": 0,
        "clarification_required": 0,
        "blocked": 0,
        "failed": 0,
    }
    for receipt in receipts:
        status = getattr(receipt, "status", "")
        value = str(getattr(status, "value", status) or "")
        if value in counts:
            counts[value] += 1
    return counts


def _is_successful_pure_read_turn(
    receipts: tuple[Any, ...],
) -> bool:
    """Classify the complete receipt set without interpreting reply text."""

    if not receipts:
        return False
    for receipt in receipts:
        tool_name = str(
            getattr(receipt, "tool_name", "") or ""
        )
        definition = TOOL_REGISTRY.get(tool_name)
        status = getattr(receipt, "status", "")
        status_value = str(
            getattr(status, "value", status) or ""
        )
        if (
            definition is None
            or definition.read_or_write != "read"
            or status_value not in {"success", "no_op"}
        ):
            return False
    return True


_PRE_EXECUTION_BLOCK_SCHEMA_VERSION = (
    "agent2.pre_execution_block.observation.v1"
)
_PRE_EXECUTION_BLOCK_TOOL_NAMES = frozenset(
    {
        "add_daily_items",
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
    }
)
_SAFE_DAILY_REPORT_FIELDS = frozenset(
    {"today_work", "problems", "tomorrow_plan"}
)
_SAFE_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_SAFE_WEEKLY_OPERATION_TYPES = frozenset(
    {
        "add",
        "edit",
        "move",
        "delete",
        "set_day_empty",
        "accept_suggestion",
        "reject_suggestion",
        "capture_suggestion",
    }
)


def _pre_execution_block_observations(
    receipts: tuple[Any, ...],
) -> tuple[dict[str, Any], ...]:
    """Project transient binder failures into the existing safe turn record."""

    observations: list[dict[str, Any]] = []
    for receipt in receipts:
        if len(observations) >= 50:
            break
        status = getattr(receipt, "status", "")
        status_value = str(getattr(status, "value", status) or "")
        if status_value not in {
            "blocked",
            "clarification_required",
            "failed",
        }:
            continue
        safe_facts = getattr(receipt, "safe_user_facts", None)
        if not isinstance(safe_facts, Mapping):
            continue
        candidate = safe_facts.get(
            "pre_execution_block_observation"
        )
        if not isinstance(candidate, Mapping):
            continue
        projected = _validated_pre_execution_block_observation(
            candidate,
            receipt=receipt,
        )
        if projected is not None:
            observations.append(projected)
    return tuple(observations)


def _validated_pre_execution_block_observation(
    candidate: Mapping[str, Any],
    *,
    receipt: Any,
) -> dict[str, Any] | None:
    tool_name = str(getattr(receipt, "tool_name", "") or "")
    if tool_name == "add_daily_items":
        return _validated_daily_block_observation(
            candidate,
            receipt=receipt,
        )
    error_code = str(getattr(receipt, "error_code", "") or "")
    arguments_sha256 = str(candidate.get("arguments_sha256") or "")
    target_plan_ref_sha256 = str(
        candidate.get("target_plan_ref_sha256") or ""
    )
    target_week_start = str(candidate.get("target_week_start") or "")
    target_version = candidate.get("target_version")
    operation_count = candidate.get("operation_count")
    raw_type_counts = candidate.get("operation_type_counts")
    if (
        candidate.get("schema_version")
        != _PRE_EXECUTION_BLOCK_SCHEMA_VERSION
        or candidate.get("tool_name") != tool_name
        or tool_name not in _PRE_EXECUTION_BLOCK_TOOL_NAMES
        or candidate.get("target_type") != "weekly_plan"
        or candidate.get("error_code") != error_code
        or _SAFE_ERROR_CODE_RE.fullmatch(error_code) is None
        or candidate.get("actual_write") is not False
        or _SAFE_SHA256_RE.fullmatch(arguments_sha256) is None
        or _SAFE_SHA256_RE.fullmatch(target_plan_ref_sha256) is None
        or not isinstance(target_version, int)
        or isinstance(target_version, bool)
        or target_version < 0
        or not isinstance(operation_count, int)
        or isinstance(operation_count, bool)
        or not 0 <= operation_count <= 50
        or not isinstance(raw_type_counts, Mapping)
    ):
        return None
    if target_week_start:
        try:
            target_week = date.fromisoformat(target_week_start)
        except ValueError:
            return None
        if target_week.weekday() != 0:
            return None
    operation_type_counts: dict[str, int] = {}
    for key, value in raw_type_counts.items():
        operation_type = str(key or "")
        if (
            operation_type not in _SAFE_WEEKLY_OPERATION_TYPES
            or not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 50
        ):
            return None
        operation_type_counts[operation_type] = value
    if sum(operation_type_counts.values()) != operation_count:
        return None
    return {
        "schema_version": _PRE_EXECUTION_BLOCK_SCHEMA_VERSION,
        "tool_name": tool_name,
        "arguments_sha256": arguments_sha256,
        "target_type": "weekly_plan",
        "target_plan_ref_sha256": target_plan_ref_sha256,
        "target_week_start": target_week_start,
        "target_version": target_version,
        "operation_type_counts": operation_type_counts,
        "operation_count": operation_count,
        "error_code": error_code,
        "actual_write": False,
    }


def _validated_daily_block_observation(
    candidate: Mapping[str, Any],
    *,
    receipt: Any,
) -> dict[str, Any] | None:
    tool_name = str(getattr(receipt, "tool_name", "") or "")
    error_code = str(getattr(receipt, "error_code", "") or "")
    arguments_sha256 = str(candidate.get("arguments_sha256") or "")
    target_report_date = str(candidate.get("target_report_date") or "")
    target_version = candidate.get("target_version")
    item_count = candidate.get("item_count")
    raw_field_counts = candidate.get("field_item_counts")
    retry_candidate = validated_daily_retry_evidence(
        candidate.get("retry_candidate")
    )
    if (
        candidate.get("schema_version")
        != _PRE_EXECUTION_BLOCK_SCHEMA_VERSION
        or candidate.get("tool_name") != tool_name
        or tool_name != "add_daily_items"
        or candidate.get("target_type") != "daily_report"
        or candidate.get("error_code") != error_code
        or _SAFE_ERROR_CODE_RE.fullmatch(error_code) is None
        or candidate.get("actual_write") is not False
        or _SAFE_SHA256_RE.fullmatch(arguments_sha256) is None
        or not isinstance(item_count, int)
        or isinstance(item_count, bool)
        or not 0 <= item_count <= 30
        or not isinstance(raw_field_counts, Mapping)
        or set(raw_field_counts) != _SAFE_DAILY_REPORT_FIELDS
    ):
        return None
    if target_report_date:
        try:
            date.fromisoformat(target_report_date)
        except ValueError:
            return None
        if (
            (
                not isinstance(target_version, int)
                or isinstance(target_version, bool)
                or target_version < 0
            )
            and not (
                retry_candidate is not None
                and retry_candidate["target_was_absent"] is True
                and target_version is None
            )
        ):
            return None
    elif target_version is not None:
        return None

    field_item_counts: dict[str, int] = {}
    for field_name in (
        "today_work",
        "problems",
        "tomorrow_plan",
    ):
        value = raw_field_counts.get(field_name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 30
        ):
            return None
        field_item_counts[field_name] = value
    if sum(field_item_counts.values()) != item_count:
        return None
    projected = {
        "schema_version": _PRE_EXECUTION_BLOCK_SCHEMA_VERSION,
        "tool_name": tool_name,
        "arguments_sha256": arguments_sha256,
        "target_type": "daily_report",
        "target_report_date": target_report_date,
        "target_version": target_version,
        "field_item_counts": field_item_counts,
        "item_count": item_count,
        "error_code": error_code,
        "actual_write": False,
    }
    if retry_candidate is not None:
        if (
            retry_candidate["target_report_date"]
            != target_report_date
            or retry_candidate["target_version"] != target_version
        ):
            return None
        projected["retry_candidate"] = retry_candidate
    return projected


def _model_elapsed_seconds(model_turns: tuple[Any, ...]) -> float:
    elapsed = 0.0
    for turn in model_turns:
        metadata = getattr(turn, "response_metadata", None)
        if not isinstance(metadata, Mapping):
            continue
        try:
            elapsed += max(
                0.0,
                float(metadata.get("elapsed_seconds") or 0.0),
            )
        except (TypeError, ValueError):
            continue
    return round(elapsed, 4)


def _user_visible_result(counts: Mapping[str, int]) -> str:
    if counts.get("failed", 0):
        return "failed"
    if counts.get("blocked", 0):
        return "blocked"
    if counts.get("clarification_required", 0):
        return "clarification"
    if counts.get("success", 0) or counts.get("no_op", 0):
        return "success"
    return "reply_only"


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
            or safe_facts.get("model_composition_allowed")
            is True
        ):
            continue
        response_text = str(
            safe_facts.get("response_text") or ""
        ).strip()
        if response_text:
            return response_text
    return ""


def _select_trusted_read_response(
    *,
    model_content: str,
    receipts: tuple[Any, ...],
) -> str:
    """Keep Agent2 wording while enforcing deterministic factual grounding."""

    if any(
        (
            definition := TOOL_REGISTRY.get(
                str(getattr(receipt, "tool_name", "") or "")
            )
        )
        is not None
        and definition.read_or_write == "write"
        for receipt in receipts
    ):
        return str(model_content or "").strip()
    performance = _model_composable_performance_facts(
        receipts
    )
    if performance is not None:
        safe_facts, response_text = performance
        grounded = _validated_performance_reply(
            model_content,
            facts=safe_facts,
        )
        if grounded is not None:
            return grounded
        return canary_block_message(
            "tool_call_canary_execution_failed"
        )
    if _contains_performance_receipt(receipts):
        return canary_block_message(
            "tool_call_canary_execution_failed"
        )
    return (
        _trusted_authoritative_read_response(receipts)
        or str(model_content or "").strip()
    )


def _should_apply_personal_salutation(
    receipts: tuple[Any, ...],
) -> bool:
    """Let performance dialogue read naturally without a forced title."""

    return not _contains_performance_receipt(receipts)


def _contains_performance_receipt(
    receipts: tuple[Any, ...],
) -> bool:
    return any(
        str(getattr(receipt, "tool_name", "") or "")
        == "query_defendant_performance"
        for receipt in receipts
    )


def _model_composable_performance_facts(
    receipts: tuple[Any, ...],
) -> tuple[dict[str, Any], str] | None:
    # Agent2 may compose several read-only results in one answer.  The
    # program only admits an unchanged, registered read batch and validates
    # the cited performance claims; it never substitutes business prose.
    for receipt in receipts:
        definition = TOOL_REGISTRY.get(
            str(getattr(receipt, "tool_name", "") or "")
        )
        if (
            bool(getattr(receipt, "changed", False))
            or definition is None
            or definition.read_or_write != "read"
        ):
            return None
    for receipt in reversed(receipts):
        if bool(getattr(receipt, "changed", False)):
            continue
        if (
            str(getattr(receipt, "tool_name", "") or "")
            != "query_defendant_performance"
        ):
            continue
        safe_facts = getattr(receipt, "safe_user_facts", None)
        if (
            not isinstance(safe_facts, dict)
            or safe_facts.get("actual_write") is not False
            or safe_facts.get("model_composition_allowed")
            is not True
            or not isinstance(
                safe_facts.get("performance_facts"),
                dict,
            )
        ):
            continue
        response_text = str(
            safe_facts.get("response_text") or ""
        ).strip()
        if not response_text:
            continue
        return (
            dict(safe_facts["performance_facts"]),
            response_text,
        )
    return None


def _grounded_performance_reply(
    content: str,
    *,
    facts: dict[str, Any],
) -> bool:
    return _validated_performance_reply(content, facts=facts) is not None


_PERFORMANCE_CITATION_RE = re.compile(
    r"\[依据:([A-Za-z0-9_.-]+)\]"
)
_INTERNAL_FACT_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"
    r"(?![A-Za-z0-9])"
)
_TEAM_NAME_RE = re.compile(r"法务[一二三四五六七八九十百]+部")
_DECLINE_WORDS = ("下降", "降低", "减少", "下滑")
_GROWTH_WORDS = ("增长", "上升", "增加", "上涨")
_CANONICAL_QUOTE_TRANSLATION = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
    }
)


def _validated_performance_reply(
    content: str,
    *,
    facts: dict[str, Any],
) -> str | None:
    """Validate cited model prose without selecting facts or rewriting it."""

    text = str(content or "").strip()
    if not text or len(text) > 12000:
        return None
    catalog = facts.get("claim_catalog")
    if not isinstance(catalog, dict) or not catalog:
        return None
    references = _PERFORMANCE_CITATION_RE.findall(text)
    if len(references) > 160:
        return None
    if any(
        not isinstance(catalog.get(reference), dict)
        for reference in references
    ):
        return None
    if not references:
        return None
    normalized_text = re.sub(
        r"[ \t]*(\[依据:[A-Za-z0-9_.-]+\])[ \t]*",
        r" \1 ",
        text,
    )
    segments = tuple(
        segment.strip()
        for segment in normalized_text.splitlines()
        if segment.strip()
    ) or (normalized_text.strip(),)
    for segment in segments:
        segment_ids = tuple(_PERFORMANCE_CITATION_RE.findall(segment))
        plain_segment = _PERFORMANCE_CITATION_RE.sub("", segment).strip()
        if not plain_segment:
            return None
        if not segment_ids:
            if not _presentation_only_performance_line(plain_segment):
                return None
            continue
        claims = tuple(
            catalog[claim_id]
            for claim_id in dict.fromkeys(segment_ids)
        )
        if not _performance_line_matches_claims(
            plain_segment,
            claims=claims,
            catalog=catalog,
        ):
            return None
    visible = _PERFORMANCE_CITATION_RE.sub("", text)
    visible = re.sub(r"[ \t]+(?=\r?$)", "", visible, flags=re.MULTILINE)
    return visible.strip()


def _select_performance_facts_for_question(
    selected_ids: list[str],
    *,
    catalog: dict[str, Any],
    user_query: str,
) -> list[str]:
    query = str(user_query or "")
    if not query:
        return selected_ids

    multi_intent = _multi_intent_performance_selection(
        query,
        selected_ids=selected_ids,
        catalog=catalog,
    )
    if multi_intent is not None:
        return multi_intent

    case_groups = _requested_case_groups(
        query,
        selected_ids=selected_ids,
        catalog=catalog,
    )
    if case_groups:
        count_ids = [
            (
                "scope.period_closed_count"
                if case_group == "period_closed"
                else "scope.period_new_count"
            )
            for case_group in case_groups
        ]
        selected_cases = [
            claim_id
            for claim_id in selected_ids
            if isinstance(catalog.get(claim_id), dict)
            and str(catalog[claim_id].get("kind") or "")
            == "case"
            and str(
                catalog[claim_id].get("case_group") or ""
            )
            in case_groups
        ]
        return list(
            dict.fromkeys(
                [
                    *(
                        claim_id
                        for claim_id in count_ids
                        if claim_id in catalog
                    ),
                    *selected_cases,
                ]
            )
        )

    if "综合减损率" in query:
        claim_id = "scope.comprehensive_loss_rate"
        return [claim_id] if claim_id in catalog else []

    if "实质减损" in query:
        claim_id = "scope.substantial_loss_amount"
        return [claim_id] if claim_id in catalog else []

    if "存量" in query and any(
        phrase in query
        for phrase in (
            "较上月",
            "比上月",
            "环比",
            "较上周",
            "比上周",
            "较上期",
            "比上期",
        )
    ):
        claim_id = "scope.stock_period_change"
        return [claim_id] if claim_id in catalog else []

    if "存量" in query and any(
        phrase in query
        for phrase in (
            "下降率",
            "同比",
            "下降多少",
            "下降了多少",
            "降了多少",
            "降幅",
        )
    ):
        selected = [
            claim_id
            for claim_id in (
                (
                    _definition_claim_id(
                        catalog,
                        term="同比下降率",
                    )
                    if any(
                        phrase in query
                        for phrase in (
                            "什么意思",
                            "是什么",
                            "怎么理解",
                            "怎么算",
                            "口径",
                        )
                    )
                    else ""
                ),
                "scope.stock_yoy",
                (
                    "scope.stock_target"
                    if any(
                        phrase in query
                        for phrase in (
                            "目标",
                            "完成",
                            "达标",
                        )
                    )
                    else ""
                ),
            )
            if claim_id and claim_id in catalog
        ]
        return selected

    if "存量" in query and any(
        phrase in query
        for phrase in ("目标", "达标", "完成了吗", "完成没有")
    ):
        claim_ids = [
            claim_id
            for claim_id in (
                "scope.stock_yoy",
                "scope.stock_target",
            )
            if claim_id in catalog
        ]
        return claim_ids

    if (
        "存量" in query
        and any(
            phrase in query
            for phrase in (
                "什么意思",
                "是什么",
                "怎么算",
                "口径",
                "怎么统计",
                "如何统计",
            )
        )
        and "同比" not in query
        and "下降率" not in query
    ):
        definition_id = _definition_claim_id(
            catalog,
            term="存量",
        )
        return (
            [definition_id]
            if definition_id
            else []
        )

    if "新增" in query and any(
        phrase in query
        for phrase in (
            "下降率",
            "同比",
            "下降多少",
            "下降了多少",
            "降了多少",
            "降幅",
        )
    ):
        selected = [
            claim_id
            for claim_id in (
                (
                    _definition_claim_id(
                        catalog,
                        term="同比下降率",
                    )
                    if any(
                        phrase in query
                        for phrase in (
                            "什么意思",
                            "是什么",
                            "怎么理解",
                            "怎么算",
                            "口径",
                        )
                    )
                    else ""
                ),
                "scope.new_yoy",
                (
                    "scope.new_target"
                    if any(
                        phrase in query
                        for phrase in (
                            "目标",
                            "完成",
                            "达标",
                        )
                    )
                    else ""
                ),
            )
            if claim_id and claim_id in catalog
        ]
        return selected

    if "新增" in query and any(
        phrase in query
        for phrase in ("目标", "达标", "完成了吗", "完成没有")
    ):
        claim_ids = [
            claim_id
            for claim_id in (
                "scope.new_yoy",
                "scope.new_target",
            )
            if claim_id in catalog
        ]
        return claim_ids

    if (
        "新增" in query
        and any(
            phrase in query
            for phrase in (
                "什么意思",
                "是什么",
                "怎么算",
                "口径",
                "怎么统计",
                "如何统计",
            )
        )
        and "同比" not in query
        and "下降率" not in query
    ):
        definition_id = _definition_claim_id(
            catalog,
            term="新增",
        )
        return [definition_id] if definition_id else []

    if "存量" in query and any(
        phrase in query
        for phrase in (
            "哪些分公司",
            "哪个分公司",
            "构成",
            "来自哪里",
            "分布",
        )
    ):
        branch_ids = [
            claim_id
            for claim_id, claim in catalog.items()
            if claim_id.startswith("branch.")
            and isinstance(claim, dict)
            and str(claim.get("metric_key") or "")
            == "stock_count"
            and _positive_performance_metric(claim)
        ]
        branch_ids.sort(
            key=lambda claim_id: (
                -_performance_metric_number(
                    catalog[claim_id]
                ),
                str(
                    catalog[claim_id].get("subject") or ""
                ),
            )
        )
        return [
            *(
                ["scope.stock_count"]
                if "scope.stock_count" in catalog
                else []
            ),
            *branch_ids,
        ]

    if (
        "存量" in query
        and any(
            phrase in query
            for phrase in ("多少", "几件", "数量")
        )
        and not any(
            phrase in query
            for phrase in (
                "下降率",
                "同比",
                "目标",
                "绩效",
                "新增",
                "结案",
            )
        )
    ):
        metric_id = f"scope.{_stock_count_metric_key(query)}"
        return (
            [metric_id]
            if metric_id in catalog
            else []
        )

    if (
        "新增" in query
        and any(
            phrase in query
            for phrase in ("多少", "几件", "数量")
        )
        and "绩效" not in query
    ):
        metric_id = (
            "scope."
            + _new_count_metric_key(
                query,
                selected_ids=selected_ids,
                catalog=catalog,
            )
        )
        return (
            [metric_id]
            if metric_id in catalog
            else []
        )

    if (
        "结案" in query
        and any(
            phrase in query
            for phrase in ("多少", "几件", "数量")
        )
        and "绩效" not in query
    ):
        return (
            ["scope.period_closed_count"]
            if "scope.period_closed_count" in catalog
            else []
        )

    asks_for_new_sources = (
        "新增" in query
        and any(
            phrase in query
            for phrase in (
                "从哪里",
                "哪些分公司",
                "哪个分公司",
                "为什么",
                "构成",
                "来源",
            )
        )
    )
    if asks_for_new_sources:
        branch_metric = _new_count_metric_key(
            query,
            selected_ids=selected_ids,
            catalog=catalog,
        )
        branch_ids = [
            claim_id
            for claim_id, claim in catalog.items()
            if claim_id.startswith("branch.")
            and isinstance(claim, dict)
            and str(claim.get("metric_key") or "")
            == branch_metric
            and _positive_performance_metric(claim)
        ]
        branch_ids.sort(
            key=lambda claim_id: (
                -_performance_metric_number(
                    catalog[claim_id]
                ),
                str(
                    catalog[claim_id].get("subject") or ""
                ),
            )
        )
        total_id = f"scope.{branch_metric}"
        return [
            *([total_id] if total_id in catalog else []),
            *branch_ids,
        ]

    asks_for_overview = (
        "绩效" in query
        and any(
            phrase in query
            for phrase in (
                "怎么样",
                "怎样",
                "情况",
                "完成",
                "概览",
                "整体",
            )
        )
    )
    if asks_for_overview:
        core_ids = [
            claim_id
            for claim_id in (
                "scope.stock_count",
                "scope.stock_yoy",
                "scope.stock_period_change",
                "scope.stock_target",
                "scope.year_to_date_new_count",
                "scope.new_yoy",
                "scope.new_target",
                "scope.period_new_count",
                "scope.period_closed_count",
            )
            if claim_id in catalog
        ]
        return core_ids

    return selected_ids


def _multi_intent_performance_selection(
    query: str,
    *,
    selected_ids: list[str],
    catalog: dict[str, Any],
) -> list[str] | None:
    chosen: list[str] = []
    recognized = False

    def add(claim_id: str) -> None:
        if claim_id in catalog and claim_id not in chosen:
            chosen.append(claim_id)

    case_groups = _requested_case_groups(
        query,
        selected_ids=selected_ids,
        catalog=catalog,
    )
    if case_groups:
        recognized = True
        for case_group in case_groups:
            add(
                "scope.period_closed_count"
                if case_group == "period_closed"
                else "scope.period_new_count"
            )
        for claim_id in selected_ids:
            claim = catalog.get(claim_id)
            if (
                isinstance(claim, dict)
                and str(claim.get("kind") or "") == "case"
                and str(claim.get("case_group") or "")
                in case_groups
            ):
                add(claim_id)
        return chosen

    all_teams = any(
        phrase in query
        for phrase in (
            "各团队",
            "所有团队",
            "全部团队",
            "团队对比",
            "团队分别",
            "其他团队",
            "各部门",
            "所有部门",
            "全部部门",
            "部门对比",
            "部门分别",
            "其他部门",
        )
    )
    if all_teams:
        recognized = True
        metrics: list[str] = []
        if "绩效" in query:
            metrics.extend(
                (
                    "stock_count",
                    "stock_yoy",
                    "stock_period_change",
                    "stock_target",
                    "year_to_date_new_count",
                    "new_yoy",
                    "new_target",
                    "period_new_count",
                    "period_closed_count",
                )
            )
        else:
            if "存量" in query:
                stock_team_rate = any(
                    phrase in query
                    for phrase in (
                        "同比",
                        "下降率",
                        "降幅",
                    )
                )
                stock_team_target = any(
                    phrase in query
                    for phrase in (
                        "目标",
                        "达标",
                        "完成",
                    )
                )
                stock_team_period = any(
                    phrase in query
                    for phrase in (
                        "环比",
                        "较上期",
                        "较上月",
                        "较上周",
                    )
                )
                if stock_team_period:
                    metrics.append("stock_period_change")
                if stock_team_rate or stock_team_target:
                    metrics.append("stock_yoy")
                    if stock_team_target:
                        metrics.append("stock_target")
                if (
                    _asks_explicit_metric_quantity(
                        query,
                        metric="存量",
                    )
                    or not (
                        stock_team_period
                        or stock_team_rate
                        or stock_team_target
                    )
                ):
                    metrics.append(_stock_count_metric_key(query))
            if "新增" in query:
                new_team_rate = any(
                    phrase in query
                    for phrase in (
                        "同比",
                        "下降率",
                        "降幅",
                    )
                )
                new_team_target = any(
                    phrase in query
                    for phrase in (
                        "目标",
                        "达标",
                        "完成",
                    )
                )
                if new_team_rate or new_team_target:
                    metrics.append("new_yoy")
                    if new_team_target:
                        metrics.append("new_target")
                if (
                    _asks_explicit_metric_quantity(
                        query,
                        metric="新增",
                    )
                    or not (new_team_rate or new_team_target)
                ):
                    metrics.append(
                        _new_count_metric_key(
                            query,
                            selected_ids=selected_ids,
                            catalog=catalog,
                        )
                    )
            if (
                not metrics
                and any(
                    phrase in query
                    for phrase in (
                        "环比",
                        "较上期",
                        "较上月",
                        "较上周",
                    )
                )
            ):
                metrics.append("stock_period_change")
            if any(
                phrase in query
                for phrase in (
                    "结案",
                    "已结",
                    "结了",
                    "办结",
                )
            ):
                metrics.append("period_closed_count")
        if not metrics:
            allowed_team_metrics = {
                "stock_count",
                "previous_stock_count",
                "last_year_stock_count",
                "stock_yoy",
                "stock_period_change",
                "stock_target",
                "year_to_date_new_count",
                "last_year_to_date_new_count",
                "new_yoy",
                "new_target",
                "period_new_count",
                "period_closed_count",
            }
            metrics.extend(
                dict.fromkeys(
                    str(claim.get("metric_key") or "")
                    for claim_id in selected_ids
                    if isinstance(
                        (claim := catalog.get(claim_id)),
                        dict,
                    )
                    and str(claim.get("metric_key") or "")
                    in allowed_team_metrics
                )
            )
        metric_order = {
            value: index
            for index, value in enumerate(metrics)
        }
        team_claims = [
            (claim_id, claim)
            for claim_id, claim in catalog.items()
            if claim_id.startswith("team.")
            and isinstance(claim, dict)
            and str(claim.get("metric_key") or "")
            in metric_order
        ]
        team_claims.sort(
            key=lambda item: (
                int(item[0].split(".")[1]),
                metric_order[
                    str(item[1].get("metric_key") or "")
                ],
            )
        )
        for claim_id, _ in team_claims:
            add(claim_id)
        return chosen

    explanation_words = (
        "什么意思",
        "是什么",
        "怎么理解",
        "怎么算",
        "口径",
        "怎么统计",
        "如何统计",
    )
    target_words = ("目标", "达标", "完成了吗", "完成没有")
    rate_words = (
        "下降率",
        "同比",
        "下降多少",
        "下降了多少",
        "降了多少",
        "降幅",
    )
    quantity_words = ("多少", "几件", "数量")

    if "综合减损率" in query:
        recognized = True
        add("scope.comprehensive_loss_rate")
    if "实质减损" in query:
        recognized = True
        add("scope.substantial_loss_amount")

    stock_period_change = (
        "存量" in query
        and any(
            phrase in query
            for phrase in (
                "较上月",
                "比上月",
                "环比",
                "较上周",
                "比上周",
                "较上期",
                "比上期",
            )
        )
    )
    if stock_period_change:
        recognized = True
        add("scope.stock_period_change")

    stock_rate = (
        "存量" in query
        and any(phrase in query for phrase in rate_words)
        and (
            not stock_period_change
            or "同比" in query
            or "存量下降率" in query
        )
    )
    if stock_rate:
        recognized = True
        if any(
            phrase in query for phrase in explanation_words
        ):
            add(
                _definition_claim_id(
                    catalog,
                    term="同比下降率",
                )
            )
        add("scope.stock_yoy")
        if any(phrase in query for phrase in target_words):
            add("scope.stock_target")

    new_rate = "新增" in query and any(
        phrase in query for phrase in rate_words
    )
    if new_rate:
        recognized = True
        if any(
            phrase in query for phrase in explanation_words
        ):
            add(
                _definition_claim_id(
                    catalog,
                    term="同比下降率",
                )
            )
        add("scope.new_yoy")
        if any(phrase in query for phrase in target_words):
            add("scope.new_target")

    if "存量" in query and any(
        phrase in query for phrase in target_words
    ) and not stock_rate:
        recognized = True
        add("scope.stock_yoy")
        add("scope.stock_target")
    if "新增" in query and any(
        phrase in query for phrase in target_words
    ) and not new_rate:
        recognized = True
        add("scope.new_yoy")
        add("scope.new_target")

    if (
        "存量" in query
        and any(
            phrase in query for phrase in explanation_words
        )
        and not stock_rate
        and not stock_period_change
    ):
        recognized = True
        add(_definition_claim_id(catalog, term="存量"))
    if (
        "新增" in query
        and any(
            phrase in query for phrase in explanation_words
        )
        and not new_rate
    ):
        recognized = True
        add(_definition_claim_id(catalog, term="新增"))

    stock_sources = (
        "存量" in query
        and any(
            phrase in query
            for phrase in (
                "哪些分公司",
                "哪个分公司",
                "构成",
                "来自哪里",
                "分布",
            )
        )
    )
    if stock_sources:
        recognized = True
        add("scope.stock_count")
        for claim_id in _sorted_positive_branch_claims(
            catalog,
            metric_key="stock_count",
        ):
            add(claim_id)

    new_sources = (
        "新增" in query
        and any(
            phrase in query
            for phrase in (
                "从哪里",
                "哪些分公司",
                "哪个分公司",
                "为什么",
                "构成",
                "来源",
            )
        )
    )
    if new_sources:
        recognized = True
        metric_key = _new_count_metric_key(
            query,
            selected_ids=selected_ids,
            catalog=catalog,
        )
        add(f"scope.{metric_key}")
        for claim_id in _sorted_positive_branch_claims(
            catalog,
            metric_key=metric_key,
        ):
            add(claim_id)

    if (
        "存量" in query
        and _asks_explicit_metric_quantity(
            query,
            metric="存量",
        )
        and not stock_sources
    ):
        recognized = True
        add(f"scope.{_stock_count_metric_key(query)}")
    if (
        "新增" in query
        and _asks_explicit_metric_quantity(
            query,
            metric="新增",
        )
        and not new_sources
    ):
        recognized = True
        add(
            "scope."
            + _new_count_metric_key(
                query,
                selected_ids=selected_ids,
                catalog=catalog,
            )
        )
    if any(
        phrase in query
        for phrase in (
            "结案",
            "已结",
            "结了",
            "办结",
        )
    ) and any(
        phrase in query for phrase in quantity_words
    ):
        recognized = True
        add("scope.period_closed_count")

    overview = (
        "绩效" in query
        and any(
            phrase in query
            for phrase in (
                "怎么样",
                "怎样",
                "情况",
                "完成",
                "概览",
                "整体",
            )
        )
    )
    if overview:
        recognized = True
        for claim_id in (
            "scope.stock_count",
            "scope.stock_yoy",
            "scope.stock_period_change",
            "scope.stock_target",
            "scope.year_to_date_new_count",
            "scope.new_yoy",
            "scope.new_target",
            "scope.period_new_count",
            "scope.period_closed_count",
        ):
            add(claim_id)

    return chosen if recognized else None


def _new_count_metric_key(
    query: str,
    *,
    selected_ids: list[str] | None = None,
    catalog: dict[str, Any] | None = None,
) -> str:
    """Use the selected report period unless the user explicitly asks for YTD."""

    if any(
        phrase in query
        for phrase in (
            "去年同期累计新增",
            "去年同期新增",
            "去年累计新增",
            "去年新增",
        )
    ):
        return "last_year_to_date_new_count"
    if any(
        phrase in query
        for phrase in (
            "年度累计",
            "年初至今",
            "今年累计",
            "本年累计",
            "当年累计",
            "今年新增",
            "本年新增",
            "全年累计",
        )
    ):
        return "year_to_date_new_count"
    if _has_explicit_period_reference(query):
        return "period_new_count"
    if selected_ids and isinstance(catalog, dict):
        selected_metrics = {
            str(claim.get("metric_key") or "")
            for claim_id in selected_ids
            if isinstance(
                (claim := catalog.get(claim_id)),
                dict,
            )
            and str(claim.get("metric_key") or "")
            in {
                "period_new_count",
                "year_to_date_new_count",
                "last_year_to_date_new_count",
            }
        }
        if len(selected_metrics) == 1:
            return next(iter(selected_metrics))
    return "period_new_count"


def _has_explicit_period_reference(query: str) -> bool:
    return any(
        phrase in query
        for phrase in (
            "本周",
            "这周",
            "当周",
            "本月",
            "这个月",
            "当月",
            "上周",
            "上月",
            "周报",
            "月报",
        )
    ) or bool(
        re.search(
            r"(?:\d{4}年)?\d{1,2}月(?:第[一二三四五六七八九十\d]+周)?",
            query,
        )
    )


def _stock_count_metric_key(query: str) -> str:
    if any(
        phrase in query
        for phrase in (
            "去年同期存量",
            "去年存量",
        )
    ):
        return "last_year_stock_count"
    if any(
        phrase in query
        for phrase in (
            "上期末存量",
            "上月末存量",
            "上周末存量",
        )
    ):
        return "previous_stock_count"
    return "stock_count"


def _asks_explicit_metric_quantity(
    query: str,
    *,
    metric: str,
) -> bool:
    if metric in query and any(
        phrase in query
        for phrase in ("分别多少", "各多少", "分别有多少")
    ):
        return True
    direct_phrases = (
        f"{metric}多少",
        f"{metric}有多少",
        f"{metric}几件",
        f"{metric}数量",
        f"{metric}的数量",
    )
    if any(phrase in query for phrase in direct_phrases):
        return True
    if metric == "存量":
        return bool(
            re.search(
                r"存量(?:被告案件|被告)?(?:有|是|为)?多少(?:件)?",
                query,
            )
        )
    if metric == "新增":
        return bool(
            re.search(
                r"新增(?:案件)?(?:有|是|为)?多少(?:件)?",
                query,
            )
        )
    return False


def _sorted_positive_branch_claims(
    catalog: dict[str, Any],
    *,
    metric_key: str,
) -> list[str]:
    branch_ids = [
        claim_id
        for claim_id, claim in catalog.items()
        if claim_id.startswith("branch.")
        and isinstance(claim, dict)
        and str(claim.get("metric_key") or "")
        == metric_key
        and _positive_performance_metric(claim)
    ]
    branch_ids.sort(
        key=lambda claim_id: (
            -_performance_metric_number(catalog[claim_id]),
            str(catalog[claim_id].get("subject") or ""),
        )
    )
    return branch_ids


def _requested_case_groups(
    query: str,
    *,
    selected_ids: list[str],
    catalog: dict[str, Any],
) -> tuple[str, ...]:
    asks_for_cases = any(
        phrase in query
        for phrase in (
            "哪几件",
            "哪些案件",
            "哪几起",
            "案件明细",
            "逐件",
            "逐条",
            "分别是什么",
            "有哪些",
            "具体案件",
            "列出来",
        )
    )
    if not asks_for_cases:
        return ()
    has_closed = any(
        phrase in query
        for phrase in (
            "结案",
            "已结",
            "结了",
            "办结",
            "结掉",
        )
    )
    has_new = "新增" in query
    if has_new or has_closed:
        return tuple(
            group
            for group, requested in (
                ("period_new", has_new),
                ("period_closed", has_closed),
            )
            if requested
        )
    selected_groups = {
        str(claim.get("case_group") or "")
        for claim_id in selected_ids
        if isinstance(
            (claim := catalog.get(claim_id)),
            dict,
        )
        and str(claim.get("case_group") or "")
        in {"period_new", "period_closed"}
    }
    for claim_id in selected_ids:
        claim = catalog.get(claim_id)
        if not isinstance(claim, dict):
            continue
        metric_key = str(claim.get("metric_key") or "")
        if metric_key == "period_new_count":
            selected_groups.add("period_new")
        elif metric_key == "period_closed_count":
            selected_groups.add("period_closed")
    return tuple(
        group
        for group in ("period_new", "period_closed")
        if group in selected_groups
    )


def _definition_claim_id(
    catalog: dict[str, Any],
    *,
    term: str,
) -> str:
    return next(
        (
            claim_id
            for claim_id, claim in catalog.items()
            if isinstance(claim, dict)
            and str(claim.get("kind") or "")
            == "definition"
            and str(claim.get("term") or "") == term
        ),
        "",
    )


def _positive_performance_metric(
    claim: dict[str, Any],
) -> bool:
    return _performance_metric_number(claim) > 0


def _performance_metric_number(
    claim: dict[str, Any],
) -> float:
    try:
        return float(claim.get("value") or 0)
    except (TypeError, ValueError):
        return 0.0


def _expand_requested_case_list(
    claim_ids: list[str],
    *,
    catalog: dict[str, Any],
    user_query: str,
) -> list[str]:
    query = str(user_query or "")
    groups = _requested_case_groups(
        query,
        selected_ids=claim_ids,
        catalog=catalog,
    )
    if not groups:
        return claim_ids
    expanded = list(claim_ids)
    for group in groups:
        expected = [
            claim_id
            for claim_id, claim in catalog.items()
            if isinstance(claim, dict)
            and str(claim.get("kind") or "") == "case"
            and str(claim.get("case_group") or "") == group
        ]
        if not expected:
            continue
        selected_group = any(
            claim_id in expected
            or str(
                (catalog.get(claim_id) or {}).get(
                    "metric_key"
                )
            )
            == (
                "period_closed_count"
                if group == "period_closed"
                else "period_new_count"
            )
            for claim_id in expanded
        )
        if selected_group:
            expanded.extend(expected)
    return list(dict.fromkeys(expanded))


def _render_selected_performance_facts(
    claim_ids: list[str],
    *,
    facts: dict[str, Any],
    catalog: dict[str, Any],
    user_query: str = "",
) -> str | None:
    selected = [
        (claim_id, catalog[claim_id])
        for claim_id in claim_ids
        if isinstance(catalog.get(claim_id), dict)
    ]
    business_selected = [
        item
        for item in selected
        if str(item[1].get("kind") or "")
        not in {"context", "period"}
    ]
    if not business_selected:
        return None

    case_items = [
        item
        for item in business_selected
        if str(item[1].get("kind") or "") == "case"
    ]
    other_items = [
        item
        for item in business_selected
        if str(item[1].get("kind") or "") != "case"
    ]
    rendered: list[str] = []
    for _, claim in other_items:
        line = _render_performance_claim(
            claim,
            facts=facts,
            user_query=user_query,
        )
        if line and line not in rendered:
            rendered.append(line)

    if case_items:
        for case_group in ("period_new", "period_closed"):
            same_group = [
                claim
                for _, claim in case_items
                if str(claim.get("case_group") or "")
                == case_group
            ]
            if not same_group:
                continue
            if rendered:
                rendered.append("")
            rendered.append(
                _case_list_heading(
                    case_group,
                    count=len(same_group),
                    facts=facts,
                )
            )
            rendered.extend(
                _render_case_claim(claim, index=index)
                for index, claim in enumerate(
                    same_group,
                    start=1,
                )
            )
            truncation = _case_truncation_notice(
                case_group,
                facts=facts,
            )
            if truncation:
                rendered.append(truncation)
    return "\n".join(rendered).strip() or None


def _render_performance_claim(
    claim: dict[str, Any],
    *,
    facts: dict[str, Any],
    user_query: str = "",
) -> str:
    kind = str(claim.get("kind") or "")
    if kind == "definition":
        term = str(claim.get("term") or "").strip()
        definition = str(
            claim.get("canonical_text") or ""
        ).strip()
        return (
            f"{term}是指：{definition}"
            if term and definition
            else definition
        )

    subject = str(claim.get("subject") or "").strip()
    label = str(claim.get("label") or "").strip()
    metric_key = str(
        claim.get("metric_key") or ""
    ).strip()
    value = claim.get("value")
    period = (
        facts.get("period")
        if isinstance(facts.get("period"), dict)
        else {}
    )
    cutoff = str(period.get("cutoff_date") or "").strip()
    comparison = str(
        period.get("comparison_label") or ""
    ).strip()
    if metric_key in {
        "period_new_count",
        "period_closed_count",
    }:
        label = _period_metric_label(
            label,
            metric_key=metric_key,
            period=period,
            user_query=user_query,
        )

    if kind == "metric":
        number = _safe_render_number(value)
        if not number:
            return ""
        if metric_key == "stock_count":
            suffix = f"（截至{cutoff}）" if cutoff else ""
            return (
                f"{subject}当前被告案件存量为{number}件"
                f"{suffix}。"
            )
        if metric_key == "previous_stock_count":
            return (
                f"{subject}{comparison or '上期末'}存量为"
                f"{number}件。"
            )
        if metric_key == "last_year_stock_count":
            return f"{subject}去年同期存量为{number}件。"
        if metric_key == "last_year_to_date_new_count":
            return (
                f"{subject}去年同期累计新增为{number}件。"
            )
        return f"{subject}{label}为{number}件。"

    if kind == "rate" and isinstance(value, dict):
        display = str(value.get("display") or "").strip()
        if not display:
            return ""
        if str(claim.get("direction") or "") == "flat":
            display = "持平"
        if metric_key == "stock_period_change":
            return (
                f"{subject}存量较{comparison or '上期'}"
                f"{display}。"
            )
        return f"{subject}{label}{display}。"

    if kind == "target" and isinstance(value, dict):
        target = str(
            value.get("target_display") or ""
        ).strip()
        status = str(
            value.get("status_label")
            or claim.get("status_label")
            or ""
        ).strip()
        if not target and not status:
            return ""
        target_text = f"为{target}" if target else ""
        status_text = f"当前{status}" if status else ""
        joined = "，".join(
            value
            for value in (target_text, status_text)
            if value
        )
        return f"{subject}{label}{joined}。"

    if kind == "loss" and isinstance(value, dict):
        status = str(value.get("status_label") or "").strip()
        display = str(value.get("display") or "").strip()
        if not display:
            return (
                f"{subject}{label}：{status or '当前数据不足，暂不可计算'}。"
            )
        if metric_key == "comprehensive_loss_rate":
            count = _safe_render_number(
                value.get("eligible_case_count")
            )
            claim_amount = _safe_render_number(
                value.get("claim_amount")
            )
            payable_amount = _safe_render_number(
                value.get("payable_amount")
            )
            details = [
                f"{subject}综合减损率为{display}。",
                (
                    "计算口径为：统计期内已结案且至少一项应付款有数据的"
                    f"案件共{count}件。"
                    if count
                    else ""
                ),
                (
                    "公式为（标的额及利息合计－应付款合计）÷"
                    "标的额及利息合计×100%；"
                    f"本次标的额及利息合计{claim_amount}元，"
                    f"应付款合计{payable_amount}元。"
                    if claim_amount and payable_amount
                    else ""
                ),
            ]
            return "\n".join(item for item in details if item)
        return f"{subject}{label}为{display}。"
    return ""


def _period_metric_label(
    default_label: str,
    *,
    metric_key: str,
    period: dict[str, Any],
    user_query: str,
) -> str:
    query = str(user_query or "")
    historical_reference = any(
        phrase in query
        for phrase in (
            "上周",
            "上一周",
            "前一周",
            "上月",
            "上个月",
            "上一月",
        )
    ) or bool(
        re.search(
            r"(?:\d{4}年)?\d{1,2}月(?:第[一二三四五六七八九十\d]+周)?",
            query,
        )
    )
    period_label = str(period.get("label") or "").strip()
    if not historical_reference or not period_label:
        return default_label
    suffix = (
        "结案"
        if metric_key == "period_closed_count"
        else "新增"
    )
    return f"{period_label}{suffix}"


def _asks_for_unavailable_historical_period(
    user_query: str,
    *,
    facts: dict[str, Any],
) -> bool:
    query = str(user_query or "")
    if not query:
        return False
    has_relative_historical_period = any(
        phrase in query
        for phrase in (
            "上周",
            "上一周",
            "前一周",
            "上月",
            "上个月",
            "上一月",
        )
    )
    is_period_comparison = any(
        phrase in query
        for phrase in (
            "较上周",
            "比上周",
            "较上月",
            "比上月",
            "环比",
            "上期末存量",
            "上月末存量",
            "上周末存量",
        )
    )
    if has_relative_historical_period and not is_period_comparison:
        return True
    period = (
        facts.get("period")
        if isinstance(facts.get("period"), dict)
        else {}
    )
    period_label = str(period.get("label") or "").strip()
    if not period_label:
        return False
    explicit_year = re.search(r"(\d{4})年", query)
    if explicit_year and explicit_year.group(0) not in period_label:
        return True
    explicit_month = re.search(r"(\d{1,2})月", query)
    if (
        explicit_month
        and explicit_month.group(0) not in period_label
    ):
        return True
    explicit_week = re.search(
        r"第[一二三四五六七八九十\d]+周",
        query,
    )
    return bool(
        explicit_week
        and explicit_week.group(0) not in period_label
    )


def _render_case_claim(
    claim: dict[str, Any],
    *,
    index: int,
) -> str:
    case_name = str(claim.get("case_name") or "").strip()
    branch = str(claim.get("branch_name") or "").strip()
    lawyer = str(claim.get("lawyer_name") or "").strip()
    is_closed = (
        str(claim.get("case_group") or "")
        == "period_closed"
    )
    date_label = "结案日期" if is_closed else "新增日期"
    date_value = str(
        claim.get("close_date" if is_closed else "register_date")
        or ""
    ).strip()
    return (
        f"{index}. {case_name or '未提供案件名称'}｜"
        f"分公司：{branch or '未提供'}｜"
        f"承办法务：{lawyer or '未提供'}｜"
        f"{date_label}：{date_value or '未提供'}"
    )


def _case_list_heading(
    case_group: str,
    *,
    count: int,
    facts: dict[str, Any],
) -> str:
    period = (
        facts.get("period")
        if isinstance(facts.get("period"), dict)
        else {}
    )
    label = str(period.get("label") or "").strip()
    scope_name = str(facts.get("scope_name") or "").strip()
    group_label = "结案" if case_group == "period_closed" else "新增"
    prefix = " · ".join(
        value for value in (scope_name, label) if value
    )
    return (
        f"{prefix + '，' if prefix else ''}"
        f"{group_label}案件明细（{count}件）："
    )


def _case_truncation_notice(
    case_group: str,
    *,
    facts: dict[str, Any],
) -> str:
    status = (
        facts.get("case_detail_status")
        if isinstance(
            facts.get("case_detail_status"),
            dict,
        )
        else {}
    )
    prefix = (
        "period_closed"
        if case_group == "period_closed"
        else "period_new"
    )
    if not bool(status.get(f"{prefix}_truncated")):
        return ""
    total = _safe_render_number(
        status.get(f"{prefix}_total")
    )
    returned = _safe_render_number(
        status.get(f"{prefix}_returned")
    )
    return (
        f"当前对话已列出前{returned or '若干'}件，"
        f"完整数据共{total or '若干'}件；请在绩效页面导出完整明细。"
    )


def _safe_render_number(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, Decimal):
        rendered = format(value.normalize(), "f")
    elif isinstance(value, (int, float)):
        rendered = str(value)
    else:
        rendered = str(value).strip()
    return rendered


def _performance_line_matches_claims(
    text: str,
    *,
    claims: tuple[dict[str, Any], ...],
    catalog: dict[str, Any],
) -> bool:
    if not claims:
        return False
    plain_text = _plain_performance_text(text)
    if "其余" in plain_text:
        return False
    used_numbers = _numeric_values(plain_text)
    allowed_numbers = {
        normalized
        for claim in claims
        for value in _walk_fact_values(claim)
        for normalized in _numeric_values(str(value))
    }
    if not used_numbers.issubset(allowed_numbers):
        return False
    if re.search(
        r"[零一二三四五六七八九十百千万亿两]+(?:件|%|成)",
        text,
    ):
        return False

    kinds = {
        str(claim.get("kind") or "")
        for claim in claims
    }
    metric_keys = {
        str(claim.get("metric_key") or "")
        for claim in claims
        if claim.get("metric_key")
    }
    mentioned_metrics = _mentioned_performance_metrics(plain_text)
    definition_claims = tuple(
        claim
        for claim in claims
        if str(claim.get("kind") or "") == "definition"
    )
    if (
        not definition_claims
        and not mentioned_metrics.issubset(metric_keys)
    ):
        return False
    if definition_claims:
        return any(
            _definition_text_matches_claim(
                plain_text,
                claim=claim,
            )
            for claim in definition_claims
        )

    metric_claims = tuple(
        claim
        for claim in claims
        if str(claim.get("kind") or "")
        in {"metric", "rate"}
    )
    if (
        metric_claims
        and not used_numbers
        and not all(
            _zero_metric_assertion(
                plain_text,
                claim=claim,
            )
            for claim in metric_claims
        )
    ):
        return False
    target_claims = tuple(
        claim
        for claim in claims
        if str(claim.get("kind") or "") == "target"
    )
    if target_claims and not (
        used_numbers
        or any(
            _target_status_matches(
                plain_text,
                claim=claim,
            )
            for claim in target_claims
        )
    ):
        return False

    directions = {
        str(claim.get("direction") or "")
        for claim in claims
        if str(claim.get("direction") or "")
    }
    says_decline = any(
        word in plain_text for word in _DECLINE_WORDS
    )
    says_growth = any(
        word in plain_text for word in _GROWTH_WORDS
    )
    says_flat = "持平" in plain_text
    if says_decline and "decline" not in directions:
        return False
    if says_growth and "growth" not in directions:
        return False
    if says_flat and "flat" not in directions:
        return False
    if "rate" in kinds and not (
        says_decline or says_growth or says_flat
    ):
        return False

    allowed_entities = {
        str(entity).strip()
        for claim in claims
        for entity in claim.get("entities") or ()
        if str(entity or "").strip()
    }
    allowed_normalized = {
        _normalized_business_text(entity)
        for entity in allowed_entities
    }
    mentioned_entities = _mentioned_business_entities(
        catalog,
        text=plain_text,
    )
    if any(
        _normalized_business_text(entity)
        not in allowed_normalized
        for entity in mentioned_entities
    ):
        return False
    if any(
        team not in allowed_entities
        for team in _TEAM_NAME_RE.findall(plain_text)
    ):
        return False

    case_claims = tuple(
        claim
        for claim in claims
        if str(claim.get("kind") or "") == "case"
    )
    if case_claims:
        for claim in case_claims:
            case_name = str(claim.get("case_name") or "").strip()
            if (
                not case_name
                or _normalized_business_text(case_name)
                not in _normalized_business_text(plain_text)
            ):
                return False
        if "承办法务" in plain_text and not any(
            str(claim.get("lawyer_name") or "").strip()
            and _normalized_business_text(
                str(claim.get("lawyer_name"))
            )
            in _normalized_business_text(plain_text)
            for claim in case_claims
        ):
            return False
        if "分公司" in plain_text and not any(
            str(claim.get("branch_name") or "").strip()
            and _normalized_business_text(
                str(claim.get("branch_name"))
            )
            in _normalized_business_text(plain_text)
            for claim in case_claims
        ):
            return False
        if "登记日期" in plain_text and not any(
            str(claim.get("register_date") or "").strip()
            and str(claim.get("register_date")) in plain_text
            for claim in case_claims
        ):
            return False
        if "结案日期" in plain_text and not any(
            str(claim.get("close_date") or "").strip()
            and str(claim.get("close_date")) in plain_text
            for claim in case_claims
        ):
            return False
    elif (
        ("包括" in plain_text or "分别为" in plain_text)
        and ("案件" in plain_text or "纠纷" in plain_text)
    ):
        return False

    period_claims = tuple(
        claim
        for claim in claims
        if str(claim.get("kind") or "") == "period"
    )
    if period_claims and not any(
        value
        and (
            value in plain_text
            or bool(_numeric_values(value))
            and _numeric_values(value).issubset(used_numbers)
        )
        for claim in period_claims
        for value in (
            str(claim.get("label") or "").strip(),
            str(claim.get("cutoff_date") or "").strip(),
            str(claim.get("comparison_label") or "").strip(),
        )
    ):
        return False
    return True


def _infer_performance_line_claims(
    text: str,
    *,
    catalog: dict[str, Any],
) -> tuple[dict[str, Any], ...] | None:
    """Bind natural prose to typed claims without trusting a global number set."""

    plain_text = _plain_performance_text(text)
    claims = tuple(
        claim
        for claim in catalog.values()
        if isinstance(claim, dict)
    )
    used_numbers = _numeric_values(plain_text)
    mentioned_metrics = _mentioned_performance_metrics(
        plain_text
    )
    mentioned_entities = _mentioned_business_entities(
        catalog,
        text=plain_text,
    )
    team_mentions = set(_TEAM_NAME_RE.findall(plain_text))

    exact_definitions = tuple(
        claim
        for claim in claims
        if str(claim.get("kind") or "") == "definition"
        and _definition_text_matches_claim(
            plain_text,
            claim=claim,
        )
    )
    if exact_definitions:
        return exact_definitions

    exact_cases = tuple(
        claim
        for claim in claims
        if str(claim.get("kind") or "") == "case"
        and str(claim.get("case_name") or "").strip()
        and _normalized_business_text(
            str(claim.get("case_name"))
        )
        in _normalized_business_text(plain_text)
    )
    if (
        ("包括" in plain_text or "分别为" in plain_text)
        and ("案件" in plain_text or "纠纷" in plain_text)
        and not exact_cases
    ):
        return None
    if exact_cases:
        if "承办法务" in plain_text and not any(
            str(claim.get("lawyer_name") or "").strip()
            and _normalized_business_text(
                str(claim.get("lawyer_name"))
            )
            in _normalized_business_text(plain_text)
            for claim in exact_cases
        ):
            return None
        if "分公司" in plain_text and not any(
            str(claim.get("branch_name") or "").strip()
            and _normalized_business_text(
                str(claim.get("branch_name"))
            )
            in _normalized_business_text(plain_text)
            for claim in exact_cases
        ):
            return None
    if _presentation_only_performance_line(plain_text):
        return ()

    candidates: list[dict[str, Any]] = list(exact_cases)
    for claim in claims:
        kind = str(claim.get("kind") or "")
        metric_key = str(claim.get("metric_key") or "")
        claim_entities = {
            str(value).strip()
            for value in claim.get("entities") or ()
            if str(value or "").strip()
        }
        if (
            mentioned_metrics
            and kind not in {"period", "context", "case"}
            and metric_key not in mentioned_metrics
        ):
            continue
        if mentioned_entities and not (
            mentioned_entities & claim_entities
        ):
            continue
        if team_mentions and not (
            team_mentions & claim_entities
        ):
            continue
        claim_numbers = {
            normalized
            for value in _walk_fact_values(claim)
            for normalized in _numeric_values(str(value))
        }
        status_label = str(
            claim.get("status_label") or ""
        ).strip()
        if not used_numbers and not mentioned_metrics:
            if kind == "target":
                if not _target_status_matches(
                    plain_text,
                    claim=claim,
                ):
                    continue
            elif kind == "period":
                if not any(
                    value and value in plain_text
                    for value in (
                        str(claim.get("label") or "").strip(),
                        str(
                            claim.get("cutoff_date") or ""
                        ).strip(),
                        str(
                            claim.get("comparison_label") or ""
                        ).strip(),
                    )
                ):
                    continue
            elif kind == "context":
                if not any(
                    entity in plain_text
                    for entity in claim_entities
                ):
                    continue
            else:
                continue
        if (
            used_numbers
            and not (used_numbers & claim_numbers)
            and not (
                kind == "rate"
                and str(claim.get("direction") or "") == "flat"
                and "持平" in plain_text
            )
        ):
            continue
        if (
            not used_numbers
            and kind == "target"
            and status_label
            and not _target_status_matches(
                plain_text,
                claim=claim,
            )
        ):
            continue
        if kind in {
            "metric",
            "rate",
            "target",
            "period",
            "context",
        } and claim not in candidates:
            candidates.append(claim)

    if not candidates:
        return (
            ()
            if _presentation_only_performance_line(plain_text)
            else None
        )

    covered_numbers = {
        normalized
        for claim in candidates
        for value in _walk_fact_values(claim)
        for normalized in _numeric_values(str(value))
    }
    if not used_numbers.issubset(covered_numbers):
        return None
    covered_metrics = {
        str(claim.get("metric_key") or "")
        for claim in candidates
        if claim.get("metric_key")
    }
    if not mentioned_metrics.issubset(covered_metrics):
        return None
    covered_entities = {
        str(value).strip()
        for claim in candidates
        for value in claim.get("entities") or ()
        if str(value or "").strip()
    }
    if not mentioned_entities.issubset(covered_entities):
        return None
    if not team_mentions.issubset(covered_entities):
        return None
    return tuple(candidates)


def _presentation_only_performance_line(text: str) -> bool:
    heading = text.strip().strip("：:")
    if text.count("|") >= 2 and not _numeric_values(text):
        return True
    if (
        len(heading) <= 30
        and heading.endswith(("?", "？"))
        and any(
            value in heading
            for value in (
                "存量下降率",
                "存量同比下降率",
                "新增下降率",
                "新增同比下降率",
                "目标完成",
                "绩效完成",
            )
        )
    ):
        return True
    if heading in {
        "存量下降率",
        "存量同比下降率",
        "新增下降率",
        "新增同比下降率",
        "目标完成情况",
        "绩效完成情况",
        "案件名称",
        "分公司",
        "承办法务",
        "登记日期",
        "结案日期",
    }:
        return True
    if _numeric_values(text):
        return False
    if any(word in text for word in (*_DECLINE_WORDS, *_GROWTH_WORDS)):
        return False
    if any(
        value in text
        for value in (
            "目标",
            "没有新增",
            "无新增",
        )
    ):
        return False
    if (
        ("案件" in text or "纠纷" in text)
        and ("包括" in text or "分别为" in text)
    ):
        return False
    return False


def _performance_reply_segments(text: str) -> tuple[str, ...]:
    segments = tuple(
        segment.strip()
        for segment in re.split(r"(?<=[。！？；])", text)
        if segment.strip()
    )
    return segments or (text,)


def _claims_complete_case_listing(text: str) -> bool:
    return (
        ("明细如下" in text or "具体如下" in text)
        and ("案件名称" in text or "哪几件" in text or "|" in text)
        and ("新增" in text or "结案" in text)
    )


def _plain_performance_text(text: str) -> str:
    plain = re.sub(r"^\s*[#>]+\s*", "", str(text or ""))
    plain = plain.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(
        r"^\s*\|?\s*\d+\s*(?:[.、)]|\|)\s*",
        "",
        plain,
    )


def _definition_required_terms_match(
    text: str,
    *,
    claim: dict[str, Any],
) -> bool:
    groups = tuple(
        tuple(
            str(option).strip()
            for option in group
            if str(option or "").strip()
        )
        for group in claim.get("required_term_groups") or ()
        if isinstance(group, (list, tuple))
    )
    if groups:
        return all(
            any(option in text for option in group)
            for group in groups
        )
    terms = tuple(
        str(value).strip()
        for value in claim.get("required_terms") or ()
        if str(value or "").strip()
    )
    return bool(terms) and all(term in text for term in terms)


def _definition_text_matches_claim(
    text: str,
    *,
    claim: dict[str, Any],
) -> bool:
    canonical = str(claim.get("canonical_text") or "").strip()
    if not canonical:
        return False
    if claim.get("validation_policy") == "exact_canonical_text":
        candidate = re.sub(
            r"^\s*[-*+]\s+",
            "",
            _plain_performance_text(text),
            count=1,
        ).strip()
        return candidate.translate(
            _CANONICAL_QUOTE_TRANSLATION
        ) == canonical.translate(_CANONICAL_QUOTE_TRANSLATION)
    return canonical in text or _definition_required_terms_match(
        text,
        claim=claim,
    )


def _target_status_matches(
    text: str,
    *,
    claim: dict[str, Any],
) -> bool:
    status_label = str(
        claim.get("status_label") or ""
    ).strip()
    if not status_label:
        return False
    if status_label in text:
        return True
    if status_label == "达到目标":
        return (
            (
                ("目标" in text and "达到" in text)
                or "达标" in text
            )
            and not any(
                value in text
                for value in (
                    "未达到",
                    "没达到",
                    "未完成",
                    "未达标",
                )
            )
        )
    if status_label == "未达到目标":
        return any(
            value in text
            for value in ("未达到目标", "没达到目标", "未完成目标")
        )
    return False


def _mentioned_performance_metrics(text: str) -> set[str]:
    mentioned: set[str] = set()
    has_target = "目标" in text
    has_rate_direction = any(
        word in text
        for word in (*_DECLINE_WORDS, *_GROWTH_WORDS)
    )
    if "存量" in text:
        if has_target:
            mentioned.add("stock_target")
        if (
            re.search(r"存量[^。；\n]{0,16}\d[\d,.]*\s*件", text)
            or any(
                value in text
                for value in ("当前存量", "目前存量", "存量是", "存量为")
            )
        ):
            mentioned.add("stock_count")
        if any(
            value in text
            for value in (
                "存量同比",
                "存量下降率",
                "同比",
                "较去年同期",
                "去年同期",
            )
        ) or ("同比" in text and has_rate_direction):
            mentioned.add("stock_yoy")
        if has_target and "实际" in text and has_rate_direction:
            mentioned.add("stock_yoy")
        if has_target and text.count("%") >= 2:
            mentioned.add("stock_yoy")
        if "去年同期" in text:
            if re.search(
                r"去年同期[^。；\n]{0,16}\d[\d,.]*\s*件",
                text,
            ):
                mentioned.add("last_year_stock_count")
        if any(
            value in text
            for value in (
                "存量较上期",
                "存量环比",
                "较上月",
                "较上周",
                "上月末",
                "上周五",
            )
        ):
            mentioned.add("stock_period_change")
            if re.search(
                r"(?:上月末|上周五|上期末)"
                r"[^。；\n]{0,16}\d[\d,.]*\s*件",
                text,
            ):
                mentioned.add("previous_stock_count")
    elif any(
        value in text
        for value in ("存量同比", "存量下降率")
    ):
        mentioned.add("stock_yoy")

    if "新增" in text:
        if has_target:
            mentioned.add("new_target")
        if has_target and "实际" in text and has_rate_direction:
            mentioned.add("new_yoy")
        if has_target and text.count("%") >= 2:
            mentioned.add("new_yoy")
        if any(
            value in text
            for value in ("新增同比", "新增下降率")
        ):
            mentioned.add("new_yoy")
        if any(
            value in text
            for value in ("去年同期累计新增", "去年同期新增")
        ):
            mentioned.add("last_year_to_date_new_count")
        if any(
            value in text
            for value in ("年度累计新增", "年初至今新增", "本年新增")
        ):
            mentioned.add("year_to_date_new_count")
            if "同比" in text:
                mentioned.add("new_yoy")
        if any(
            value in text
            for value in (
                "本月新增",
                "本周新增",
                "当月新增",
                "当周新增",
                "本月没有新增",
                "本周没有新增",
                "本月暂无新增",
                "本周暂无新增",
            )
        ):
            mentioned.add("period_new_count")
    if any(
        value in text
        for value in ("新增同比", "新增下降率")
    ):
        mentioned.add("new_yoy")
    if any(
        value in text
        for value in (
            "本月结案",
            "本周结案",
            "当月结案",
            "当周结案",
            "本月没有结案",
            "本周没有结案",
            "本月暂无结案",
            "本周暂无结案",
        )
    ):
        mentioned.add("period_closed_count")
    return mentioned


def _zero_metric_assertion(
    text: str,
    *,
    claim: dict[str, Any],
) -> bool:
    value_numbers = {
        normalized
        for value in _walk_fact_values(claim.get("value"))
        for normalized in _numeric_values(str(value))
    }
    if "0" not in value_numbers:
        return False
    metric_key = str(claim.get("metric_key") or "")
    says_none = any(
        value in text
        for value in ("没有", "暂无", "为零", "0件")
    )
    if not says_none:
        return False
    if metric_key == "period_new_count":
        return "新增" in text
    if metric_key == "period_closed_count":
        return "结案" in text
    return False


def _known_business_entities(
    catalog: dict[str, Any],
) -> set[str]:
    entities: set[str] = set()
    for claim in catalog.values():
        if not isinstance(claim, dict):
            continue
        if str(claim.get("kind") or "") == "definition":
            continue
        for entity in claim.get("entities") or ():
            value = str(entity or "").strip()
            if len(value) >= 2:
                entities.add(value)
    return entities


def _mentioned_business_entities(
    catalog: dict[str, Any],
    *,
    text: str,
) -> set[str]:
    normalized_text = _normalized_business_text(text)
    matched = {
        entity
        for entity in _known_business_entities(catalog)
        if _normalized_business_text(entity) in normalized_text
    }
    return {
        entity
        for entity in matched
        if not any(
            entity != other
            and _normalized_business_text(entity)
            in _normalized_business_text(other)
            for other in matched
        )
    }


def _normalized_business_text(value: str) -> str:
    return (
        re.sub(r"\s+", "", str(value or ""))
        .replace("（", "(")
        .replace("）", ")")
    )


def _walk_fact_values(value: Any) -> tuple[Any, ...]:
    values: list[Any] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif item is not None:
            values.append(item)

    visit(value)
    return tuple(values)


def _numeric_values(text: str) -> set[str]:
    values: set[str] = set()
    for raw in re.findall(
        r"(?<![0-9A-Za-z_])[-+]?\d[\d,]*(?:\.\d+)?",
        text,
    ):
        try:
            number = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        normalized = format(abs(number).normalize(), "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        values.add(normalized or "0")
    return values


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


def _ordered_message_times(
    *,
    message_count: int,
    message_occurred_at: Any | None,
    message_occurred_ats: tuple[Any, ...],
) -> tuple[Any, ...] | None:
    """Keep provider-ingress times aligned with the exact message fragments.

    A missing timestamp is tolerated for existing non-weekly capabilities.  A
    weekly operation with a relative date will later fail closed instead of
    silently using its possibly delayed processing time.
    """

    values = tuple(message_occurred_ats)
    if values:
        if message_occurred_at is not None or len(values) != message_count:
            raise ValueError(
                "message occurrence times must match current user messages"
            )
        return values
    if message_occurred_at is None:
        return None
    if message_count != 1:
        raise ValueError(
            "one message occurrence time cannot bind multiple user messages"
        )
    return (message_occurred_at,)


def _runtime_attestation(settings: object) -> CanaryRuntimeAttestation:
    allowed_tool_names = frozenset(
        runtime_registry_tool_names(settings)
    )
    return CanaryRuntimeAttestation(
        runtime_ready=True,
        runtime_mode="canary_execute",
        registry_digest=runtime_registry_contract_digest(settings),
        prompt_sha256=canary_prompt_sha256(
            allowed_tool_names=allowed_tool_names
        ),
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


def _attach_performance_glossary(
    context: Any,
    *,
    settings: object,
) -> Any:
    if (
        "query_defendant_performance"
        not in context.allowed_tool_names
        or not bool(
            getattr(
                settings,
                "agent2_performance_knowledge_enabled",
                False,
            )
        )
    ):
        return context
    glossary = dict(context.business_glossary)
    glossary.update(_DEFENDANT_PERFORMANCE_GLOSSARY)
    return context.model_copy(update={"business_glossary": glossary})


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
