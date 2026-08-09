from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import dingtalk_stream
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import AsyncSessionLocal, engine
from app.llm.client import LLMClient
from app.llm.extractor import DailyReportExtractor, LLMOutputError
from app.message_identity import canonical_dingtalk_idempotency_key
from app.models import WebhookEvent
from app.progress.outbox import enqueue_daily_report_outbox_best_effort
from app.repositories import (
    create_webhook_event_once,
    get_active_user_by_dingtalk_id,
    list_active_user_habits,
    mark_webhook_event_failed,
    mark_webhook_event_processed,
    maybe_create_report_interaction_event,
)
from app.services.dingtalk import DingTalkRobotClient, extract_voice_download_code, extract_voice_text
from app.services.performance_service import (
    NO_ACTIVE_PERFORMANCE_TASK_MESSAGE,
    PERFORMANCE_PENDING_CONFIRMATION,
    PerformanceTaskService,
    is_performance_reply_candidate,
    looks_like_performance_reply_template,
    submission_metrics,
)
from app.services.report_service import DailyReportService
from app.utils.dingtalk_text import format_dingtalk_plain_text
from app.utils.time import now_in_timezone
from app.agent2.assistant_tools import build_tool_assisted_reply
from app.agent2.case_table_rag import DEFAULT_CASE_RAG_INDEX, CaseTableRagAdapter, find_case_location_hint
from app.agent2.context_pack import Agent2ContextPack, build_agent2_context_pack
from app.agent2.cognitive_runtime_v3 import (
    admission_block_reply,
    cognitive_core_v3_enabled,
    execute_selection_pending_turn,
    finalize_cognitive_core_v3_execution,
    information_pending_reply,
    selection_request_reply,
    semantic_admission_mode,
)
from app.agent2.turn_runtime import (
    InformationContinuationBlocked,
    SelectionContinuationBlocked,
    VerifiedTurnRejected,
    VerifiedTurnRequest,
    production_agent2_turn_runtime,
    verified_turn_rejection_reply,
)
from app.agent2.cognitive_reply_v3 import (
    append_cognitive_clarification,
    build_cognitive_side_reply_v3,
    has_bound_confirmation_pending,
    has_pending_lifecycle_update,
    pending_lifecycle_reply,
)
from app.agent2.tool_calling.canary_service import (
    CanaryIngressExecutionError,
    build_canary_persisted_response_payload,
    deliver_cached_canary_message_if_enabled,
    deliver_canary_message_if_enabled,
    process_tool_call_canary_ingress,
    resolve_tool_call_canary_route,
)
from app.agent2.tool_calling.turn_batching import (
    CanaryTurnBatchCoordinator,
    SealedTurnBatch,
    TurnFragment,
    build_batched_follower_payload,
    is_recoverable_ingress_payload,
    prepare_recoverable_ingress_payload,
    provider_payload_from_ingress,
)
from app.agent2.case_report_projection_runtime import (
    project_committed_case_followup_facts,
)
from app.agent2.case_travel_clarification_sql import (
    create_case_travel_offers_from_business_result,
    execute_case_travel_clarification_turn,
)
from app.agent2.report_projection_confirmation_runtime import (
    execute_report_projection_confirmation_turn,
)
from app.agent2.report_projection_correction_runtime import (
    execute_report_projection_correction_turn,
)
from app.agent2.business.composition import (
    BusinessCompositionResult,
    Phase2BusinessComposer,
)
from app.agent2.business.entrypoint import (
    build_business_command_context,
    decide_runtime_owner,
    persist_runtime_owner_claim,
    resolve_agent2_entrypoint,
)
from app.agent2.business.repositories import CaseFollowupPolicySqlRepository, CaseProgressSqlRepository, CaseSqlRepository, PartySqlRepository
from app.agent2.business.sql_executor import SqlBusinessExecutor
from app.agent2.business.policy import BusinessEffectPolicy
from app.agent2.knowledge_resolver import (
    KnowledgeQuery,
    load_live_daily_history_adapter,
    load_live_org_directory_adapter,
    resolve_knowledge,
)
from app.agent2.personal_memory import build_personal_memory_profile
from app.agent2.performance_knowledge import attach_live_performance_catalog
from app.agent2.recent_context import load_recent_case_context_messages
from app.agent2.daily_clarification import (
    DailyCandidateClarification,
    build_daily_candidate_clarification,
    build_daily_candidate_clarification_reply,
    build_pending_daily_candidate_focus_reply,
)
from app.agent2.daily_state import set_pending_daily_candidate
from app.agent2.daily_execution import (
    agent2_daily_report_version,
    agent2_daily_should_fallback_to_legacy,
    execute_agent2_daily_commands,
)
from app.agent2.daily_shadow import evaluate_daily_shadow
from app.agent2.typed_daily_executor import execute_typed_agent2_daily_commands
from app.agent2.report_sql_executor import execute_periodic_report_commands
from app.agent2.coordination_sandbox import CANDIDATE_CASE_PROGRESS, CANDIDATE_TRAVEL_COORDINATION
from app.agent2.workflow_audit import create_agent2_workflow_audit_event
from app.agent2.operation_outcomes import OutcomeReplyComposer
from app.agent2.operation_outcome_store import persist_operation_outcomes
from app.agent2.outcome_adapters import (
    business_composition_outcomes,
    daily_execution_outcomes,
    periodic_execution_outcomes,
    text_outcome,
)
from app.workflows.gate import GateDecision
from app.workflows.daily_context import (
    daily_active_task_from_report,
    load_live_daily_context,
    load_live_daily_report,
)
from app.workflows.intake import (
    ActiveWorkflowTask,
    IncomingMessageEnvelope,
    WORKFLOW_MONTHLY_REPORT,
)

logger = logging.getLogger("ai_review_stream")

TEXT_TEXT_ONLY = "\u6ca1\u6709\u8bc6\u522b\u5230\u8bed\u97f3\u6587\u5b57\uff0c\u8bf7\u518d\u53d1\u4e00\u6b21\u6216\u6539\u53d1\u6587\u5b57\u3002"
TEXT_QUEUE_FULL = "\u5f53\u524d\u590d\u76d8\u6d88\u606f\u8f83\u591a\uff0c\u7cfb\u7edf\u5df2\u6ee1\u8f7d\uff0c\u8bf7\u7a0d\u540e\u518d\u53d1\u4e00\u6b21\u3002"
TEXT_UNKNOWN_USER = "\u672a\u8bc6\u522b\u5230\u4f60\u7684\u5458\u5de5\u4fe1\u606f\uff0c\u8bf7\u8054\u7cfb\u7ba1\u7406\u5458\u5148\u7ef4\u62a4 users \u8868\u3002"
TEXT_LLM_FAILED = "\u590d\u76d8\u89e3\u6790\u5931\u8d25\uff0c\u7cfb\u7edf\u6ca1\u6709\u5165\u5e93\u3002\u8bf7\u7a0d\u540e\u91cd\u8bd5\u6216\u8054\u7cfb\u7ba1\u7406\u5458\u67e5\u770b LLM \u8f93\u51fa\u3002"
TEXT_PROCESS_FAILED = "\u590d\u76d8\u5904\u7406\u5931\u8d25\uff0c\u7cfb\u7edf\u6ca1\u6709\u5165\u5e93\u3002\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"


@dataclass(frozen=True)
class StreamJob:
    message: dingtalk_stream.ChatbotMessage
    text: str
    payload: dict[str, Any]
    received_at_monotonic: float = 0.0
    queued_at_monotonic: float = 0.0
    message_type: str = "text"
    voice_download_seconds: float = 0.0
    voice_transcribe_seconds: float = 0.0
    event_id: uuid.UUID | None = None
    idempotency_key: str = ""
    recovered: bool = False
    persisted_received_at: datetime | None = None
    worker_dequeued_at_monotonic: float = 0.0


@dataclass(frozen=True)
class PersistedStreamIngress:
    event_id: uuid.UUID
    idempotency_key: str
    inserted: bool
    status: str
    response_payload: dict[str, Any]
    payload: dict[str, Any]
    received_at: datetime


@dataclass(frozen=True)
class StreamReplyObservation:
    elapsed_seconds: float
    transport_status: str
    provider_accepted: bool
    delivery_verified: bool
    error_type: str = ""


class StreamBatchRequiresSerialProcessing(RuntimeError):
    """The sealed time batch is valid, but its members need original serial handling."""


@dataclass(frozen=True)
class SealedStreamJobBatch:
    turn_batch: SealedTurnBatch
    jobs: tuple[StreamJob, ...]
    wait_seconds: float
    execution_done: asyncio.Future[str]

    def is_leader(self, job: StreamJob) -> bool:
        return (
            job.event_id is not None
            and self.turn_batch.is_leader(job.event_id)
        )


STREAM_TIMING_FIELDS = (
    "queue_wait_seconds",
    "voice_download_seconds",
    "voice_transcribe_seconds",
    "load_user_seconds",
    "turn_batch_wait_seconds",
    "acquire_report_lock_seconds",
    "llm_intent_seconds",
    "llm_extract_seconds",
    "agent2_model_elapsed_seconds",
    "report_merge_seconds",
    "db_commit_seconds",
    "dingtalk_send_seconds",
    "total_seconds",
)

STREAM_LLM_META_FIELDS = (
    "llm_intent_model",
    "llm_extract_model",
    "llm_summary_model",
    "llm_intent_thinking",
    "llm_extract_thinking",
    "llm_intent_timeout",
    "llm_extract_timeout",
    "llm_fallback_to_pro",
    "llm_fallback_reason",
)

STREAM_AGENT2_MODEL_META_FIELDS = (
    "agent2_model_call_count",
    "agent2_model_request_attempt_count",
    "agent2_model_transport_retry_count",
)

STREAM_AGENT2_TOOL_COUNT_FIELDS = (
    "agent2_tool_success_count",
    "agent2_tool_no_op_count",
    "agent2_tool_clarification_count",
    "agent2_tool_blocked_count",
    "agent2_tool_failure_count",
)

STREAM_AGENT_META_FIELDS = (
    "entered_report_agent",
    "has_pending_before",
    "pending_action_before",
    "pending_section_before",
    "state_resolver_decision",
    "state_resolver_reason",
    "report_agent_seconds",
    "report_agent_model",
    "report_agent_thinking",
    "report_agent_timeout",
    "report_agent_intent",
    "report_agent_confidence",
    "report_agent_should_write",
    "report_agent_output_action",
    "pending_created",
    "pending_action_saved",
    "pending_action_after",
    "pending_cleared",
    "pending_kept_after_failure",
    "executor_action",
    "executor_result",
    "executor_error",
    "reference_report_lookup_date",
    "reference_report_found",
    "reference_report_tomorrow_plan_count",
    "reference_report_loaded",
    "reference_report_source",
    "rollover_completed_items",
    "rollover_unfinished_items",
    "deduped_items",
    "post_action_preview",
    "turn_batch_id",
    "turn_batch_size",
)


def _elapsed_seconds(start: float) -> float:
    return round(time.perf_counter() - start, 4)


def _safe_seconds(value: Any) -> float:
    try:
        return round(float(value or 0.0), 4)
    except (TypeError, ValueError):
        return 0.0


def _safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _add_timing(timings: dict[str, Any], key: str, value: float) -> None:
    timings[key] = round(_safe_seconds(timings.get(key)) + _safe_seconds(value), 4)


def _apply_canary_observability(
    timings: dict[str, Any],
    outcome: Any,
) -> None:
    timings["agent2_model_call_count"] = _safe_nonnegative_int(
        getattr(outcome, "model_call_count", 0)
    )
    timings["agent2_model_request_attempt_count"] = _safe_nonnegative_int(
        getattr(outcome, "model_request_attempt_count", 0)
    )
    timings["agent2_model_transport_retry_count"] = _safe_nonnegative_int(
        getattr(outcome, "model_transport_retry_count", 0)
    )
    timings["agent2_model_elapsed_seconds"] = _safe_seconds(
        getattr(outcome, "model_elapsed_seconds", 0.0)
    )
    timings["agent2_model_result_status"] = str(
        getattr(outcome, "model_result_status", "not_called")
        or "not_called"
    )
    for timing_field, outcome_field in (
        ("agent2_tool_success_count", "tool_success_count"),
        ("agent2_tool_no_op_count", "tool_no_op_count"),
        (
            "agent2_tool_clarification_count",
            "tool_clarification_count",
        ),
        ("agent2_tool_blocked_count", "tool_blocked_count"),
        ("agent2_tool_failure_count", "tool_failure_count"),
    ):
        timings[timing_field] = _safe_nonnegative_int(
            getattr(outcome, outcome_field, 0)
        )
    timings["agent2_user_visible_result"] = str(
        getattr(outcome, "user_visible_result", "unknown")
        or "unknown"
    )
    timings["agent2_reply_formed"] = bool(
        getattr(outcome, "reply_formed", False)
    )
    timings["agent2_message_processing_status"] = "consumed"
    timings["agent2_business_result_status"] = str(
        getattr(outcome, "user_visible_result", "unknown")
        or "unknown"
    )
    timings["agent2_business_transaction_status"] = "pending"
    timings["agent2_business_changed"] = bool(
        getattr(outcome, "actual_write", False)
    )
    timings["agent2_reply_status"] = (
        "formed"
        if bool(getattr(outcome, "reply_formed", False))
        else "missing"
    )


def _apply_reply_observability(
    timings: dict[str, Any],
    observation: StreamReplyObservation,
) -> None:
    _add_timing(
        timings,
        "dingtalk_send_seconds",
        observation.elapsed_seconds,
    )
    timings["agent2_transport_status"] = observation.transport_status
    timings["agent2_provider_accepted"] = observation.provider_accepted
    timings["agent2_delivery_verified"] = observation.delivery_verified
    timings["agent2_transport_error_type"] = observation.error_type
    timings["agent2_delivery_status"] = (
        "verified"
        if observation.delivery_verified
        else (
            "suppressed"
            if observation.transport_status == "suppressed"
            else (
                "unverified"
                if observation.provider_accepted
                else "not_delivered"
            )
        )
    )


def _canary_stream_status(
    outcome: Any,
    observation: StreamReplyObservation,
) -> str:
    if observation.transport_status == "failed":
        return "tool_call_canary_reply_failed"
    if observation.transport_status == "suppressed":
        return "tool_call_canary_delivery_suppressed"
    if str(getattr(outcome, "user_visible_result", "")) == "failed":
        return "tool_call_canary_failed"
    if str(getattr(outcome, "owner", "")) == "blocked":
        return "tool_call_canary_blocked"
    return "tool_call_canary_processed"


def _initial_stream_timings(job: StreamJob, started_at: float) -> dict[str, Any]:
    timings = {field: 0.0 for field in STREAM_TIMING_FIELDS}
    for field in STREAM_AGENT2_MODEL_META_FIELDS:
        timings[field] = 0
    for field in STREAM_AGENT2_TOOL_COUNT_FIELDS:
        timings[field] = 0
    timings["agent2_user_visible_result"] = "unknown"
    timings["agent2_reply_formed"] = False
    timings["agent2_model_result_status"] = "not_called"
    timings["agent2_message_processing_status"] = "not_consumed"
    timings["agent2_business_result_status"] = "unknown"
    timings["agent2_business_transaction_status"] = "not_started"
    timings["agent2_business_changed"] = False
    timings["agent2_reply_status"] = "not_formed"
    timings["agent2_transport_status"] = "not_attempted"
    timings["agent2_provider_accepted"] = False
    timings["agent2_delivery_verified"] = False
    timings["agent2_transport_error_type"] = ""
    timings["agent2_delivery_status"] = "not_attempted"
    for field in STREAM_LLM_META_FIELDS:
        timings[field] = False if field.endswith("_timeout") or field == "llm_fallback_to_pro" else None
    for field in STREAM_AGENT_META_FIELDS:
        timings[field] = None
    if job.queued_at_monotonic:
        queue_observed_at = (
            job.worker_dequeued_at_monotonic or started_at
        )
        timings["queue_wait_seconds"] = round(
            max(0.0, queue_observed_at - job.queued_at_monotonic),
            4,
        )
    timings["voice_download_seconds"] = _safe_seconds(job.voice_download_seconds)
    timings["voice_transcribe_seconds"] = _safe_seconds(job.voice_transcribe_seconds)
    return timings


def _apply_report_timings(timings: dict[str, Any], report_timings: dict[str, Any] | None) -> None:
    report_timings = report_timings or {}
    for key in (
        "acquire_report_lock_seconds",
        "llm_intent_seconds",
        "llm_extract_seconds",
        "report_merge_seconds",
    ):
        timings[key] = _safe_seconds(report_timings.get(key))
    for key in STREAM_LLM_META_FIELDS:
        if key in report_timings:
            timings[key] = report_timings.get(key)
    for key in STREAM_AGENT_META_FIELDS:
        if key in report_timings:
            timings[key] = report_timings.get(key)


def _log_stream_timing(
    *,
    job: StreamJob,
    dingtalk_user_id: str,
    user_name: str | None,
    timings: dict[str, Any],
    status: str,
    report_id: str | None = None,
    error: str | None = None,
) -> None:
    entered_report_agent = bool(timings.get("entered_report_agent") or timings.get("report_agent_seconds"))
    agent2_model_call_count = _safe_nonnegative_int(
        timings.get("agent2_model_call_count")
    )
    agent2_model_request_attempt_count = _safe_nonnegative_int(
        timings.get("agent2_model_request_attempt_count")
    )
    if (
        timings.get("agent2_message_processing_status")
        == "consumed"
    ):
        entered_llm = bool(
            agent2_model_call_count
            or agent2_model_request_attempt_count
        )
    else:
        entered_llm = bool(
            agent2_model_call_count
            or agent2_model_request_attempt_count
            or timings.get("llm_intent_seconds")
            or timings.get("llm_extract_seconds")
            or entered_report_agent
        )
    record: dict[str, Any] = {
        "message_id": job.message.message_id,
        "user_id": dingtalk_user_id,
        "user_name": user_name,
        "message_type": job.message_type or ("voice" if job.message.message_type in ("audio", "voice") else "text"),
        "status": status,
        "text_len": len(job.text or ""),
        "entered_llm": entered_llm,
        "entered_report_agent": entered_report_agent,
        "fast_path": not entered_llm,
        "report_id": report_id,
        "error": error,
    }
    for field in STREAM_TIMING_FIELDS:
        record[field] = _safe_seconds(timings.get(field))
    for field in STREAM_AGENT2_MODEL_META_FIELDS:
        record[field] = _safe_nonnegative_int(timings.get(field))
    for field in STREAM_AGENT2_TOOL_COUNT_FIELDS:
        record[field] = _safe_nonnegative_int(timings.get(field))
    record["agent2_user_visible_result"] = str(
        timings.get("agent2_user_visible_result") or "unknown"
    )
    record["agent2_reply_formed"] = bool(
        timings.get("agent2_reply_formed")
    )
    record["agent2_model_result_status"] = str(
        timings.get("agent2_model_result_status") or "not_called"
    )
    record["agent2_message_processing_status"] = str(
        timings.get("agent2_message_processing_status")
        or "not_consumed"
    )
    record["agent2_business_result_status"] = str(
        timings.get("agent2_business_result_status") or "unknown"
    )
    record["agent2_business_transaction_status"] = str(
        timings.get("agent2_business_transaction_status")
        or "not_started"
    )
    record["agent2_business_changed"] = bool(
        timings.get("agent2_business_changed")
    )
    record["agent2_reply_status"] = str(
        timings.get("agent2_reply_status") or "not_formed"
    )
    record["agent2_transport_status"] = str(
        timings.get("agent2_transport_status") or "not_attempted"
    )
    record["agent2_provider_accepted"] = bool(
        timings.get("agent2_provider_accepted")
    )
    record["agent2_delivery_verified"] = bool(
        timings.get("agent2_delivery_verified")
    )
    record["agent2_transport_error_type"] = str(
        timings.get("agent2_transport_error_type") or ""
    )
    record["agent2_delivery_status"] = str(
        timings.get("agent2_delivery_status") or "not_attempted"
    )
    for field in STREAM_LLM_META_FIELDS:
        record[field] = timings.get(field)
    for field in STREAM_AGENT_META_FIELDS:
        record[field] = timings.get(field)
    try:
        logger.info(
            "stream timing %s",
            json.dumps(record, ensure_ascii=False, sort_keys=True),
        )
    except Exception:
        # Monitoring is best-effort and must never change the user-facing turn.
        pass


def _log_immediate_stream_timing(
    *,
    incoming: dingtalk_stream.ChatbotMessage,
    user_id: str,
    message_type: str,
    text_len: int,
    received_at_monotonic: float,
    status: str,
    voice_download_seconds: float = 0.0,
    voice_transcribe_seconds: float = 0.0,
    error: str | None = None,
) -> None:
    timings = {field: 0.0 for field in STREAM_TIMING_FIELDS}
    timings["voice_download_seconds"] = _safe_seconds(voice_download_seconds)
    timings["voice_transcribe_seconds"] = _safe_seconds(voice_transcribe_seconds)
    timings["total_seconds"] = round(max(0.0, time.perf_counter() - received_at_monotonic), 4)
    job = StreamJob(
        message=incoming,
        text="x" * max(0, text_len),
        payload=incoming.to_dict(),
        received_at_monotonic=received_at_monotonic,
        message_type=message_type,
        voice_download_seconds=voice_download_seconds,
        voice_transcribe_seconds=voice_transcribe_seconds,
    )
    _log_stream_timing(
        job=job,
        dingtalk_user_id=user_id,
        user_name=None,
        timings=timings,
        status=status,
        error=error,
    )


async def _record_immediate_stream_failure(
    *,
    incoming: dingtalk_stream.ChatbotMessage,
    user_id: str,
    payload: dict[str, Any],
    reply_text: str,
    error_message: str,
    settings: Settings,
) -> None:
    idempotency_key = _stream_idempotency_key(incoming, "")
    response_payload = {"msgtype": "text", "text": {"content": reply_text}}
    try:
        async with AsyncSessionLocal() as session:
            event, inserted = await create_webhook_event_once(
                session,
                idempotency_key=idempotency_key,
                external_message_id=incoming.message_id,
                dingtalk_user_id=user_id,
                payload=payload,
            )
            if inserted or event.status == "processing":
                await mark_webhook_event_failed(
                    session,
                    event,
                    error_message=error_message,
                    response_payload=response_payload,
                    now=now_in_timezone(settings.timezone),
                )
                await session.commit()
    except Exception:
        logger.exception("failed to persist immediate stream failure")


def _stream_idempotency_key(message: dingtalk_stream.ChatbotMessage, text: str) -> str:
    return canonical_dingtalk_idempotency_key(
        message_id=getattr(message, "message_id", ""),
        user_id=(
            getattr(message, "sender_staff_id", "")
            or getattr(message, "sender_id", "")
        ),
        conversation_id=getattr(message, "conversation_id", ""),
        text=text,
        created_at=getattr(message, "create_at", ""),
    )


def _stream_source_message_id(
    event: Any,
    message: dingtalk_stream.ChatbotMessage,
    text: str,
) -> str:
    persisted_key = str(getattr(event, "idempotency_key", "") or "").strip()
    return persisted_key or _stream_idempotency_key(message, text)


def _stream_user_id(message: dingtalk_stream.ChatbotMessage) -> str:
    return message.sender_staff_id or message.sender_id or ""


class StreamJobBatchCoordinator:
    """Keep runtime jobs attached to a semantics-free, time-bounded turn batch."""

    def __init__(self, turn_coordinator: CanaryTurnBatchCoordinator) -> None:
        self._turn_coordinator = turn_coordinator
        self._jobs: dict[uuid.UUID, StreamJob] = {}
        self._execution_done: dict[str, asyncio.Future[str]] = {}
        self._lock = asyncio.Lock()

    async def collect(self, job: StreamJob) -> SealedStreamJobBatch | None:
        event_id = job.event_id
        received_at = job.persisted_received_at
        dingtalk_user_id = _stream_user_id(job.message).strip()
        conversation_id = str(
            getattr(job.message, "conversation_id", "") or ""
        ).strip()
        source_message_id = str(
            job.idempotency_key
            or _stream_idempotency_key(job.message, job.text)
        ).strip()
        if (
            event_id is None
            or received_at is None
            or not dingtalk_user_id
            or not conversation_id
            or not source_message_id
        ):
            return None

        async with self._lock:
            existing = self._jobs.get(event_id)
            if existing is not None and existing is not job:
                raise RuntimeError("stream batch event is bound to another job")
            self._jobs[event_id] = job

        started = time.perf_counter()
        turn_batch = await self._turn_coordinator.collect(
            fragment=TurnFragment(
                event_id=event_id,
                source_message_id=source_message_id,
                dingtalk_user_id=dingtalk_user_id,
                conversation_id=conversation_id,
                text=job.text,
                received_at=received_at,
            )
        )
        wait_seconds = _elapsed_seconds(started)
        async with self._lock:
            try:
                jobs = tuple(
                    self._jobs[fragment.event_id]
                    for fragment in turn_batch.fragments
                )
            except KeyError as exc:
                raise RuntimeError(
                    "sealed stream batch is missing a runtime job"
                ) from exc
            execution_done = self._execution_done.get(turn_batch.batch_id)
            if execution_done is None:
                execution_done = asyncio.get_running_loop().create_future()
                self._execution_done[turn_batch.batch_id] = execution_done
        return SealedStreamJobBatch(
            turn_batch=turn_batch,
            jobs=jobs,
            wait_seconds=wait_seconds,
            execution_done=execution_done,
        )

    async def complete(
        self,
        batch: SealedStreamJobBatch,
        *,
        mode: str,
    ) -> None:
        async with self._lock:
            execution_done = self._execution_done.pop(
                batch.turn_batch.batch_id,
                batch.execution_done,
            )
            if not execution_done.done():
                execution_done.set_result(mode)
            for event_id in batch.turn_batch.event_ids:
                self._jobs.pop(event_id, None)


async def _send_stream_reply(
    robot: DingTalkRobotClient,
    message: dingtalk_stream.ChatbotMessage,
    text: str,
    timeout_seconds: float,
) -> None:
    user_id = _stream_user_id(message)
    if message.session_webhook:
        await asyncio.wait_for(
            robot.send_session_webhook_text(session_webhook=message.session_webhook, text=text),
            timeout=timeout_seconds,
        )
        return
    if user_id:
        await asyncio.wait_for(
            robot.send_robot_direct_text(user_ids=[user_id], text=text),
            timeout=timeout_seconds,
        )


async def _persist_stream_ingress(
    job: StreamJob,
) -> PersistedStreamIngress:
    idempotency_key = _stream_idempotency_key(job.message, job.text)
    payload = prepare_recoverable_ingress_payload(
        job.payload,
        text=job.text,
        message_type=job.message_type,
        voice_download_seconds=job.voice_download_seconds,
        voice_transcribe_seconds=job.voice_transcribe_seconds,
    )
    async with AsyncSessionLocal() as session:
        event, inserted = await create_webhook_event_once(
            session,
            idempotency_key=idempotency_key,
            external_message_id=job.message.message_id,
            dingtalk_user_id=_stream_user_id(job.message),
            payload=payload,
        )
        await session.commit()
        return PersistedStreamIngress(
            event_id=event.id,
            idempotency_key=event.idempotency_key,
            inserted=inserted,
            status=event.status,
            response_payload=dict(event.response_payload or {}),
            payload=payload if inserted else dict(event.payload or {}),
            received_at=event.received_at,
        )


async def _fail_persisted_stream_ingress(
    *,
    event_id: uuid.UUID,
    error_message: str,
    response_payload: dict[str, Any],
    settings: Settings,
) -> None:
    async with AsyncSessionLocal() as session:
        event = await session.get(WebhookEvent, event_id)
        if event is None or event.status != "processing":
            return
        await mark_webhook_event_failed(
            session,
            event,
            error_message=error_message,
            response_payload=response_payload,
            now=now_in_timezone(settings.timezone),
        )
        await session.commit()


class DailyReviewStreamHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self, queue: asyncio.Queue[StreamJob], robot: DingTalkRobotClient, settings: Settings):
        super().__init__()
        self.queue = queue
        self.robot = robot
        self.settings = settings

    def _reply_soon(self, incoming: dingtalk_stream.ChatbotMessage, text: str) -> None:
        async def _safe_reply() -> None:
            try:
                await _send_stream_reply(self.robot, incoming, text, self.settings.stream_reply_timeout_seconds)
            except Exception:
                logger.exception("stream immediate reply failed")

        asyncio.create_task(_safe_reply())

    def _reply_cached_soon(
        self,
        incoming: dingtalk_stream.ChatbotMessage,
        response_payload: dict[str, Any],
    ) -> None:
        async def _safe_reply() -> None:
            try:
                await deliver_cached_canary_message_if_enabled(
                    response_payload,
                    lambda text: _send_stream_reply(
                        self.robot,
                        incoming,
                        text,
                        self.settings.stream_reply_timeout_seconds,
                    ),
                )
            except Exception:
                logger.exception("stream cached reply failed")

        asyncio.create_task(_safe_reply())

    async def process(self, callback_message: dingtalk_stream.CallbackMessage):
        received_at_monotonic = time.perf_counter()
        incoming = dingtalk_stream.ChatbotMessage.from_dict(callback_message.data)
        user_id = _stream_user_id(incoming)
        message_type = "voice" if incoming.message_type in ("audio", "voice") else "text"
        voice_download_seconds = 0.0
        voice_transcribe_seconds = 0.0

        if incoming.message_type in ('audio', 'voice'):
            payload = incoming.to_dict()
            recognized = extract_voice_text(payload)
            if recognized:
                text = str(recognized).strip()
                logger.info(
                    "received stream voice message id=%s user=%s auto_recognition len=%s",
                    incoming.message_id,
                    user_id,
                    len(text),
                )
            else:
                download_code = extract_voice_download_code(payload)
                if not download_code:
                    logger.info(
                        "received voice message without downloadCode user=%s",
                        user_id,
                    )
                    await _record_immediate_stream_failure(
                        incoming=incoming,
                        user_id=user_id,
                        payload=payload,
                        reply_text=TEXT_TEXT_ONLY,
                        error_message="voice_without_download_code",
                        settings=self.settings,
                    )
                    self._reply_soon(incoming, TEXT_TEXT_ONLY)
                    _log_immediate_stream_timing(
                        incoming=incoming,
                        user_id=user_id,
                        message_type=message_type,
                        text_len=0,
                        received_at_monotonic=received_at_monotonic,
                        status="voice_without_download_code",
                    )
                    return dingtalk_stream.AckMessage.STATUS_OK, "ok"

                transcribe_start = time.perf_counter()
                try:
                    recognized = await asyncio.wait_for(
                        self.robot.recognize_audio(str(download_code)),
                        timeout=self.settings.stream_reply_timeout_seconds,
                    )
                except Exception as exc:
                    voice_transcribe_seconds = _elapsed_seconds(transcribe_start)
                    logger.exception(
                        "ASR recognition failed user=%s download_code=%s",
                        user_id,
                        str(download_code)[:16],
                    )
                    await _record_immediate_stream_failure(
                        incoming=incoming,
                        user_id=user_id,
                        payload=payload,
                        reply_text=TEXT_TEXT_ONLY,
                        error_message=f"voice_transcribe_failed: {exc.__class__.__name__}: {exc}",
                        settings=self.settings,
                    )
                    self._reply_soon(incoming, TEXT_TEXT_ONLY)
                    _log_immediate_stream_timing(
                        incoming=incoming,
                        user_id=user_id,
                        message_type=message_type,
                        text_len=0,
                        received_at_monotonic=received_at_monotonic,
                        status="voice_transcribe_failed",
                        voice_transcribe_seconds=voice_transcribe_seconds,
                        error="asr_failed",
                    )
                    return dingtalk_stream.AckMessage.STATUS_OK, "asr failed"

                voice_transcribe_seconds = _elapsed_seconds(transcribe_start)
                text = recognized.strip()
                logger.info(
                    "received stream voice message id=%s user=%s asr len=%s",
                    incoming.message_id,
                    user_id,
                    len(text),
                )
        else:
            text_parts = self.extract_text_from_incoming_message(incoming) or []
            text = "\n".join(str(part).strip() for part in text_parts if str(part).strip()).strip()
            logger.info(
                "received stream message id=%s user=%s type=%s conversation=%s text_len=%s",
                incoming.message_id,
                user_id,
                incoming.message_type,
                incoming.conversation_id,
                len(text),
            )

        if not text:
            self._reply_soon(incoming, TEXT_TEXT_ONLY)
            _log_immediate_stream_timing(
                incoming=incoming,
                user_id=user_id,
                message_type=message_type,
                text_len=0,
                received_at_monotonic=received_at_monotonic,
                status="empty_text",
                voice_download_seconds=voice_download_seconds,
                voice_transcribe_seconds=voice_transcribe_seconds,
            )
            return dingtalk_stream.AckMessage.STATUS_OK, "ok"

        job = StreamJob(
            message=incoming,
            text=text,
            payload=incoming.to_dict(),
            received_at_monotonic=received_at_monotonic,
            message_type=message_type,
            voice_download_seconds=voice_download_seconds,
            voice_transcribe_seconds=voice_transcribe_seconds,
        )
        try:
            persisted = await _persist_stream_ingress(job)
        except Exception:
            logger.exception("stream ingress persistence failed")
            self._reply_soon(incoming, TEXT_PROCESS_FAILED)
            _log_immediate_stream_timing(
                incoming=incoming,
                user_id=user_id,
                message_type=message_type,
                text_len=len(text),
                received_at_monotonic=received_at_monotonic,
                status="ingress_persistence_failed",
                voice_download_seconds=voice_download_seconds,
                voice_transcribe_seconds=voice_transcribe_seconds,
                error="ingress_persistence_failed",
            )
            return dingtalk_stream.AckMessage.STATUS_OK, "persistence failed"

        if not persisted.inserted:
            if persisted.status in {"processed", "failed"}:
                self._reply_cached_soon(
                    incoming,
                    persisted.response_payload,
                )
            return dingtalk_stream.AckMessage.STATUS_OK, "duplicate"

        job = replace(
            job,
            payload=persisted.payload,
            queued_at_monotonic=time.perf_counter(),
            event_id=persisted.event_id,
            idempotency_key=persisted.idempotency_key,
            persisted_received_at=persisted.received_at,
        )
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            response_payload = {
                "msgtype": "text",
                "text": {"content": TEXT_QUEUE_FULL},
            }
            await _fail_persisted_stream_ingress(
                event_id=persisted.event_id,
                error_message="stream_queue_full",
                response_payload=response_payload,
                settings=self.settings,
            )
            self._reply_soon(incoming, TEXT_QUEUE_FULL)
            _log_immediate_stream_timing(
                incoming=incoming,
                user_id=user_id,
                message_type=message_type,
                text_len=len(text),
                received_at_monotonic=received_at_monotonic,
                status="queue_full",
                voice_download_seconds=voice_download_seconds,
                voice_transcribe_seconds=voice_transcribe_seconds,
            )
            return dingtalk_stream.AckMessage.STATUS_OK, "queue full"

        logger.info(
            "queued stream message id=%s user=%s queue_size=%s",
            incoming.message_id,
            user_id,
            self.queue.qsize(),
        )
        return dingtalk_stream.AckMessage.STATUS_OK, "ok"


async def _mark_canary_turn_closed(
    *,
    session: Any,
    event: WebhookEvent,
    turn_batch: SealedTurnBatch | None,
    report_id: uuid.UUID | None,
    response_payload: dict[str, Any],
    now: Any,
    error_message: str | None = None,
) -> None:
    if turn_batch is None:
        rows = [event]
        leader_event_id = event.id
    else:
        rows = list(
            (
                await session.scalars(
                    select(WebhookEvent).where(
                        WebhookEvent.id.in_(turn_batch.event_ids)
                    )
                )
            ).all()
        )
        if len(rows) != len(turn_batch.event_ids):
            raise RuntimeError("turn batch event set is incomplete")
        leader_event_id = turn_batch.leader_event_id
    for row in rows:
        payload = (
            response_payload
            if row.id == leader_event_id
            else build_batched_follower_payload(
                batch_id=turn_batch.batch_id if turn_batch else "",
                leader_event_id=str(leader_event_id),
            )
        )
        if error_message is None:
            await mark_webhook_event_processed(
                session,
                row,
                report_id=report_id,
                response_payload=payload,
                now=now,
            )
        else:
            await mark_webhook_event_failed(
                session,
                row,
                error_message=error_message,
                response_payload=payload,
                now=now,
            )


async def _enqueue_recoverable_stream_jobs(
    queue: asyncio.Queue[StreamJob],
) -> int:
    recovered = 0
    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(WebhookEvent)
                    .where(WebhookEvent.status == "processing")
                    .order_by(WebhookEvent.received_at, WebhookEvent.id)
                    .limit(max(1, queue.maxsize))
                )
            ).all()
        )
        for event in rows:
            payload = dict(event.payload or {})
            if not is_recoverable_ingress_payload(payload):
                continue
            meta = payload.get("_agent2_stream_ingress_v1")
            if not isinstance(meta, dict):
                continue
            try:
                message = dingtalk_stream.ChatbotMessage.from_dict(
                    provider_payload_from_ingress(payload)
                )
                job = StreamJob(
                    message=message,
                    text=str(meta.get("text") or ""),
                    payload=payload,
                    queued_at_monotonic=time.perf_counter(),
                    message_type=str(
                        meta.get("message_type") or "text"
                    ),
                    voice_download_seconds=float(
                        meta.get("voice_download_seconds") or 0.0
                    ),
                    voice_transcribe_seconds=float(
                        meta.get("voice_transcribe_seconds") or 0.0
                    ),
                    event_id=event.id,
                    idempotency_key=event.idempotency_key,
                    recovered=True,
                    persisted_received_at=event.received_at,
                )
            except (TypeError, ValueError):
                logger.exception(
                    "cannot recover stream event=%s",
                    event.id,
                )
                continue
            try:
                queue.put_nowait(job)
            except asyncio.QueueFull:
                logger.warning(
                    "stream recovery queue full recovered=%s",
                    recovered,
                )
                break
            recovered += 1
    if recovered:
        logger.info("recovered unfinished stream jobs=%s", recovered)
    return recovered


async def _reply(
    handler: DailyReviewStreamHandler,
    robot: DingTalkRobotClient,
    job: StreamJob,
    text: str,
) -> float:
    observation = await _reply_with_observability(
        handler,
        robot,
        job,
        text,
    )
    return observation.elapsed_seconds


async def _reply_with_observability(
    handler: DailyReviewStreamHandler,
    robot: DingTalkRobotClient,
    job: StreamJob,
    text: str,
) -> StreamReplyObservation:
    send_start = time.perf_counter()
    try:
        await _send_stream_reply(robot, job.message, text, handler.settings.stream_reply_timeout_seconds)
        send_seconds = _elapsed_seconds(send_start)
        logger.info("stream final reply sent message=%s send_seconds=%s", job.message.message_id, send_seconds)
        return StreamReplyObservation(
            elapsed_seconds=send_seconds,
            transport_status="provider_accepted",
            provider_accepted=True,
            delivery_verified=False,
        )
    except Exception as exc:
        send_seconds = _elapsed_seconds(send_start)
        logger.exception("stream final reply failed: %s", exc)
        return StreamReplyObservation(
            elapsed_seconds=send_seconds,
            transport_status="failed",
            provider_accepted=False,
            delivery_verified=False,
            error_type=type(exc).__name__,
        )


async def _evaluate_stream_legacy_daily_gate(
    *,
    session: Any,
    user: Any,
    job: StreamJob,
    performance_service: PerformanceTaskService,
    settings: Settings,
) -> GateDecision:
    _, shadow, _, _ = await _evaluate_stream_daily_shadow(
        session=session,
        user=user,
        job=job,
        performance_service=performance_service,
        settings=settings,
    )
    return shadow.gate_decision


async def _evaluate_stream_daily_shadow(
    *,
    session: Any,
    user: Any,
    job: StreamJob,
    performance_service: PerformanceTaskService,
    settings: Settings,
    mode_override: str | None = None,
):
    active_tasks: list[ActiveWorkflowTask] = []
    daily_report = None
    try:
        active_submission = await performance_service.get_active_submission(session, user.id)
        if active_submission is not None:
            metrics = submission_metrics(active_submission)
            responses = list(active_submission.responses_json or [])
            reply_candidate = is_performance_reply_candidate(
                metrics=metrics,
                responses=responses,
                raw_input=job.text,
                status=active_submission.status,
            )
            active_tasks.append(
                ActiveWorkflowTask(
                    workflow=WORKFLOW_MONTHLY_REPORT,
                    task_id=str(active_submission.task_id),
                    status=str(active_submission.status or ""),
                    reply_candidate=reply_candidate,
                    awaiting_confirmation=active_submission.status == PERFORMANCE_PENDING_CONFIRMATION,
                    reason="performance submission is active",
                    metadata={"submission_id": str(active_submission.id)},
                )
            )
    except Exception as exc:
        logger.info("stream workflow gate skipped performance task lookup: %s", exc)

    try:
        daily_report = await load_live_daily_report(session, user, settings)
        daily_task = daily_active_task_from_report(daily_report)
        if daily_task is not None:
            active_tasks.append(daily_task)
    except Exception as exc:
        logger.info("stream workflow gate skipped daily task lookup: %s", exc)

    timezone = getattr(user, "timezone", "") or getattr(settings, "timezone", "Asia/Shanghai")
    envelope = IncomingMessageEnvelope(
        sender_id=str(getattr(user, "id", "") or ""),
        sender_name=str(getattr(user, "name", "") or ""),
        dingtalk_user_id=_stream_user_id(job.message),
        source="dingtalk_stream_text",
        raw_text=job.text,
        message_id=str(getattr(job.message, "message_id", "") or ""),
        conversation_id=str(getattr(job.message, "conversation_id", "") or ""),
        received_at=now_in_timezone(timezone),
        active_tasks=tuple(active_tasks),
    )
    mode = mode_override or getattr(settings, "workflow_intake_mode", "observe_only")
    shadow = evaluate_daily_shadow(envelope, mode=mode)
    try:
        user_habits = await list_active_user_habits(session, user.id)
    except Exception as exc:
        logger.info("stream workflow gate skipped personal memory habit lookup: %s", exc)
        user_habits = []
    context_pack = build_agent2_context_pack(
        envelope,
        daily_report=daily_report,
        personal_memory=build_personal_memory_profile(user=user, user_habits=user_habits),
        knowledge=await _resolve_stream_context_knowledge(
            session=session,
            user=user,
            settings=settings,
            envelope=envelope,
            shadow=shadow,
        ),
    )
    logger.info(
        "stream workflow gate observation %s",
        json.dumps(shadow.gate_observation(envelope), ensure_ascii=False, sort_keys=True),
    )
    await create_agent2_workflow_audit_event(
        session=session,
        user=user,
        incoming=SimpleNamespace(dingtalk_user_id=_stream_user_id(job.message), text=job.text),
        settings=settings,
        envelope=envelope,
        shadow=shadow,
        mode=mode,
        observe_only_log=False,
    )
    return envelope, shadow, context_pack, daily_report


async def _build_stream_cognitive_context(
    *,
    session: Any,
    user: Any,
    job: StreamJob,
    performance_service: PerformanceTaskService,
    settings: Settings,
):
    """Build trusted Agent2 resources without invoking the legacy/Shadow semantic chain."""

    active_tasks: list[ActiveWorkflowTask] = []
    daily_report = None
    timezone = getattr(user, "timezone", "") or getattr(settings, "timezone", "Asia/Shanghai")
    target_report_date = now_in_timezone(timezone).date()
    try:
        active_submission = await performance_service.get_active_submission(session, user.id)
        if active_submission is not None:
            metrics = submission_metrics(active_submission)
            responses = list(active_submission.responses_json or [])
            active_tasks.append(
                ActiveWorkflowTask(
                    workflow=WORKFLOW_MONTHLY_REPORT,
                    task_id=str(active_submission.task_id),
                    status=str(active_submission.status or ""),
                    reply_candidate=is_performance_reply_candidate(
                        metrics=metrics,
                        responses=responses,
                        raw_input=job.text,
                        status=active_submission.status,
                    ),
                    awaiting_confirmation=active_submission.status == PERFORMANCE_PENDING_CONFIRMATION,
                    reason="performance submission is active",
                    metadata={"submission_id": str(active_submission.id)},
                )
            )
    except Exception as exc:
        logger.info("stream Agent2 context skipped performance task lookup: %s", exc)
    try:
        daily_context = await load_live_daily_context(session, user, settings)
        daily_report = daily_context.report
        target_report_date = daily_context.report_date
        daily_task = daily_context.active_task
        if daily_task is not None:
            active_tasks.append(daily_task)
    except Exception as exc:
        logger.info("stream Agent2 context skipped daily task lookup: %s", exc)

    envelope = IncomingMessageEnvelope(
        sender_id=str(getattr(user, "id", "") or ""),
        sender_name=str(getattr(user, "name", "") or ""),
        dingtalk_user_id=_stream_user_id(job.message),
        source="dingtalk_stream_text",
        raw_text=job.text,
        message_id=str(getattr(job.message, "message_id", "") or ""),
        conversation_id=str(getattr(job.message, "conversation_id", "") or ""),
        received_at=now_in_timezone(timezone),
        active_tasks=tuple(active_tasks),
    )
    try:
        user_habits = await list_active_user_habits(session, user.id)
    except Exception as exc:
        logger.info("stream Agent2 context skipped personal memory habit lookup: %s", exc)
        user_habits = []
    context_pack = build_agent2_context_pack(
        envelope,
        daily_report=daily_report,
        personal_memory=build_personal_memory_profile(user=user, user_habits=user_habits),
        knowledge=await _resolve_stream_context_knowledge(
            session=session,
            user=user,
            settings=settings,
            envelope=envelope,
            shadow=None,
        ),
    )
    return envelope, context_pack, daily_report, target_report_date


async def _resolve_stream_context_knowledge(
    *,
    session: Any,
    user: Any,
    settings: Settings,
    envelope: IncomingMessageEnvelope,
    shadow: Any | None,
) -> tuple[Any, ...]:
    adapters: list[Any] = []
    try:
        adapters.append(await load_live_org_directory_adapter(session))
    except Exception as exc:
        logger.info("stream context pack skipped org directory knowledge: %s", exc)
    try:
        timezone = getattr(user, "timezone", "") or getattr(settings, "timezone", "Asia/Shanghai")
        adapters.append(
            await load_live_daily_history_adapter(
                session,
                user,
                settings,
                current_date=now_in_timezone(timezone).date(),
                include_current=False,
            )
        )
    except Exception as exc:
        logger.info("stream context pack skipped daily history knowledge: %s", exc)
    if shadow is not None and DEFAULT_CASE_RAG_INDEX.exists():
        adapters.append(CaseTableRagAdapter(DEFAULT_CASE_RAG_INDEX))
    if not adapters:
        return ()

    plan = getattr(shadow, "plan", None) if shadow is not None else None
    intent = str(getattr(plan, "primary_workflow", "") or "")
    timezone = getattr(user, "timezone", "") or getattr(settings, "timezone", "Asia/Shanghai")
    recent_case_messages = await load_recent_case_context_messages(
        session=session,
        dingtalk_user_id=str(envelope.dingtalk_user_id or ""),
        current_message_id=str(envelope.message_id or ""),
        current_text=str(envelope.raw_text or ""),
    )
    resolution = resolve_knowledge(
        KnowledgeQuery(
            text=str(envelope.raw_text or ""),
            user_id=str(getattr(user, "id", "") or envelope.sender_id or ""),
            dingtalk_user_id=str(envelope.dingtalk_user_id or ""),
            intent=intent,
            metadata={
                "current_date": now_in_timezone(timezone).date().isoformat(),
                "recent_case_messages": recent_case_messages,
                "requester": _knowledge_requester_metadata(user),
            },
        ),
        adapters,
    )
    if resolution.warnings and resolution.status != "available":
        logger.info(
            "stream context pack knowledge unavailable %s",
            json.dumps({"warnings": list(resolution.warnings)}, ensure_ascii=False, sort_keys=True),
        )
    return tuple(resolution.evidence)


def _knowledge_requester_metadata(user: Any) -> dict[str, str]:
    team = getattr(user, "team", None)
    return {
        "user_id": str(getattr(user, "id", "") or ""),
        "dingtalk_user_id": str(getattr(user, "dingtalk_user_id", "") or ""),
        "name": str(getattr(user, "name", "") or ""),
        "role": str(getattr(user, "role", "") or "member"),
        "team_id": str(getattr(user, "team_id", "") or getattr(team, "id", "") or ""),
        "team_name": str(getattr(team, "name", "") or ""),
        "department_name": str(getattr(team, "department_name", "") or ""),
    }


async def _process_stream_agent2_daily_if_enabled(
    *,
    session: Any,
    user: Any,
    event: Any,
    job: StreamJob,
    handler: DailyReviewStreamHandler,
    robot: DingTalkRobotClient,
    llm_client: LLMClient,
    settings: Settings,
    performance_service: PerformanceTaskService,
    timings: dict[str, Any],
) -> str | None:
    source_message_id = _stream_source_message_id(event, job.message, job.text)
    entrypoint = await resolve_agent2_entrypoint(
        session,
        settings=settings,
        dingtalk_user_id=str(getattr(user, "dingtalk_user_id", "") or ""),
        source_message_id=source_message_id,
    )
    await persist_runtime_owner_claim(session, entrypoint)
    runtime_owner = decide_runtime_owner(entrypoint.decision)
    phase2_primary = runtime_owner == "agent2_primary"
    phase2_business_context = (
        build_business_command_context(
            entrypoint.binding,
            source_message_id=source_message_id,
            source_channel="dingtalk_stream",
            occurred_at=now_in_timezone(settings.timezone),
            conversation_id=str(
                getattr(job.message, "conversation_id", "")
                or getattr(event, "conversation_id", "") or ""
            ),
        )
        if phase2_primary and entrypoint.binding is not None
        else None
    )
    if runtime_owner == "blocked":
        reply_text = "当前账号或所属组织信息无法唯一确认，本次没有执行任何业务操作。"
        response_payload = {"msgtype": "text", "text": {"content": reply_text}}
        await mark_webhook_event_processed(
            session,
            event,
            report_id=None,
            response_payload=response_payload,
            now=now_in_timezone(settings.timezone),
        )
        await session.commit()
        _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, reply_text))
        return "agent2_phase2_entrypoint_blocked"
    if phase2_primary and not cognitive_core_v3_enabled(settings):
        reply_text = "当前服务暂时无法处理这条消息，本次没有执行任何业务操作。"
        response_payload = {"msgtype": "text", "text": {"content": reply_text}}
        await mark_webhook_event_processed(
            session,
            event,
            report_id=None,
            response_payload=response_payload,
            now=now_in_timezone(settings.timezone),
        )
        await session.commit()
        _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, reply_text))
        return "agent2_phase2_cognitive_core_disabled"

    if phase2_primary:
        envelope, context_pack, daily_report, target_report_date = await _build_stream_cognitive_context(
            session=session,
            user=user,
            job=job,
            performance_service=performance_service,
            settings=settings,
        )
        shadow = None
        gate_decision = None
    else:
        envelope, shadow, context_pack, daily_report = await _evaluate_stream_daily_shadow(
            session=session,
            user=user,
            job=job,
            performance_service=performance_service,
            settings=settings,
            mode_override="protective_gate",
        )
        target_report_date = (
            getattr(daily_report, "report_date", None)
            or now_in_timezone(settings.timezone).date()
        )
        gate_decision = shadow.gate_decision
    # The adapter-resolved id includes the event-id fallback and is the only
    # source identity admitted by the unified runtime and downstream receipts.
    envelope = replace(envelope, message_id=source_message_id)
    v3_execution_result = None
    cognitive_v3 = None
    phase2_business_result: BusinessCompositionResult | None = None
    case_travel_offers: tuple[Any, ...] = ()
    periodic_report_results: list[Any] = []
    processed_command_names: list[str] = []
    pre_runtime_admission_mode = (
        semantic_admission_mode(
            settings,
            tenant_id=phase2_business_context.tenant_id,
            user_id=phase2_business_context.actor_user_id,
        )
        if phase2_primary and phase2_business_context is not None
        else "disabled"
    )
    if (
        phase2_primary
        and phase2_business_context is not None
        and pre_runtime_admission_mode != "enforced"
    ):
        projection_turn = await execute_report_projection_confirmation_turn(
            session_factory=AsyncSessionLocal,
            envelope=envelope,
            business_context=phase2_business_context,
            settings=settings,
        )
        if projection_turn is not None and projection_turn.handled:
            reply_text = projection_turn.reply
            response_payload = {"msgtype": "text", "text": {"content": reply_text}}
            await mark_webhook_event_processed(
                session,
                event,
                report_id=None,
                response_payload=response_payload,
                now=now_in_timezone(settings.timezone),
            )
            await session.commit()
            _add_timing(
                timings,
                "dingtalk_send_seconds",
                await _reply(handler, robot, job, reply_text),
            )
            return "agent2_report_projection_confirmation_processed"
        correction_turn = await execute_report_projection_correction_turn(
            session_factory=AsyncSessionLocal,
            envelope=envelope,
            business_context=phase2_business_context,
            settings=settings,
        )
        if correction_turn is not None and correction_turn.handled:
            reply_text = correction_turn.reply
            response_payload = {"msgtype": "text", "text": {"content": reply_text}}
            await mark_webhook_event_processed(
                session, event, report_id=None, response_payload=response_payload,
                now=now_in_timezone(settings.timezone),
            )
            await session.commit()
            _add_timing(
                timings, "dingtalk_send_seconds",
                await _reply(handler, robot, job, reply_text),
            )
            return "agent2_report_projection_correction_processed"
        case_travel_turn = await execute_case_travel_clarification_turn(
            session,
            business_context=phase2_business_context,
            raw_text=job.text,
            settings=settings,
        )
        if case_travel_turn is not None and case_travel_turn.handled:
            if case_travel_turn.business_result is not None:
                outcomes = business_composition_outcomes(
                    case_travel_turn.business_result
                )
                reply_text = OutcomeReplyComposer().compose(outcomes)
            else:
                outcomes = (
                    text_outcome(
                        case_travel_turn.reply,
                        source_turn_id=source_message_id,
                    ),
                )
                reply_text = case_travel_turn.reply
            await persist_operation_outcomes(
                session,
                outcomes,
                tenant_id=phase2_business_context.tenant_id,
                user_id=phase2_business_context.actor_user_id,
                conversation_id=phase2_business_context.conversation_id,
                source_turn_id=source_message_id,
                now=phase2_business_context.occurred_at,
            )
            response_payload = {"msgtype": "text", "text": {"content": reply_text}}
            await mark_webhook_event_processed(
                session,
                event,
                report_id=None,
                response_payload=response_payload,
                now=now_in_timezone(settings.timezone),
            )
            await session.commit()
            _add_timing(
                timings,
                "dingtalk_send_seconds",
                await _reply(handler, robot, job, reply_text),
            )
            return "agent2_case_travel_clarification_processed"
        selection_turn = await execute_selection_pending_turn(
            session=session,
            user=user,
            envelope=envelope,
            business_context=phase2_business_context,
            settings=settings,
        )
        if selection_turn is not None and selection_turn.handled:
            reply_text = selection_turn.reply
            response_payload = {"msgtype": "text", "text": {"content": reply_text}}
            await mark_webhook_event_processed(
                session,
                event,
                report_id=None,
                response_payload=response_payload,
                now=now_in_timezone(settings.timezone),
            )
            await session.commit()
            _add_timing(
                timings,
                "dingtalk_send_seconds",
                await _reply(handler, robot, job, reply_text),
            )
            return "agent2_selection_pending_processed"
    if cognitive_core_v3_enabled(settings):
        try:
            turn_runtime_result = await production_agent2_turn_runtime().handle(
                VerifiedTurnRequest(
                    session=session,
                    user=user,
                    envelope=envelope,
                    llm_client=llm_client,
                    daily_report=daily_report,
                    report_date=target_report_date,
                    settings=settings,
                    business_context=phase2_business_context,
                )
            )
            cognitive_v3 = turn_runtime_result.orchestration
        except (
            InformationContinuationBlocked,
            SelectionContinuationBlocked,
            VerifiedTurnRejected,
        ) as exc:
            safe_reply = verified_turn_rejection_reply(exc)
            logger.info(
                "stream cognitive core v3 blocked turn kind=%s",
                safe_reply.reply_kind,
            )
            reply_text = safe_reply.message
            response_payload = {"msgtype": "text", "text": {"content": reply_text}}
            await mark_webhook_event_processed(
                session,
                event,
                report_id=None,
                response_payload=response_payload,
                now=now_in_timezone(settings.timezone),
            )
            commit_start = time.perf_counter()
            await session.commit()
            _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
            _add_timing(
                timings,
                "dingtalk_send_seconds",
                await _reply(handler, robot, job, reply_text),
            )
            return f"agent2_{safe_reply.reply_kind}"
        except Exception:
            logger.exception("stream cognitive core v3 failed; write path is fail-closed")
            reply_text = "这条消息暂时没有处理成功，本次没有修改任何业务内容，请稍后重试。"
            response_payload = {"msgtype": "text", "text": {"content": reply_text}}
            await mark_webhook_event_processed(
                session,
                event,
                report_id=None,
                response_payload=response_payload,
                now=now_in_timezone(settings.timezone),
            )
            commit_start = time.perf_counter()
            await session.commit()
            _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
            _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, reply_text))
            return "agent2_cognitive_v3_unavailable"
        else:
            verified_execution_context = (
                turn_runtime_result.business_execution_context
            )
            context_pack = await attach_live_performance_catalog(
                context_pack=context_pack,
                session=session,
                user=user,
                settings=settings,
                decision=cognitive_v3.decision,
                anchor_date=now_in_timezone(settings.timezone).date(),
                tenant_id=(
                    verified_execution_context.tenant_id
                    if verified_execution_context is not None
                    else ""
                ),
                actor_role_ids=(
                    tuple(verified_execution_context.actor_role_ids)
                    if verified_execution_context is not None
                    else ()
                ),
            )
            if cognitive_v3.command_plan.report_commands:
                if verified_execution_context is None:
                    raise RuntimeError(
                        "stream periodic Report commands require verified identity"
                    )
                periodic_report_results = await asyncio.wait_for(
                    execute_periodic_report_commands(
                        session,
                        commands=cognitive_v3.command_plan.report_commands,
                        context=verified_execution_context,
                        timezone_name=settings.timezone,
                        execution_authority=(
                            turn_runtime_result.mutation_execution_authority
                        ),
                    ),
                    timeout=settings.stream_processing_timeout_seconds,
                )
            if phase2_primary and cognitive_v3.command_plan.business_commands:
                binding = entrypoint.binding
                if binding is None:
                    raise RuntimeError("Agent2 primary route requires a verified identity binding")
                business_context = verified_execution_context
                if business_context is None:
                    raise RuntimeError("Agent2 primary route requires business command context")
                executable_candidates = tuple(
                    command
                    for command in cognitive_v3.command_plan.business_commands
                    if command.command_type
                    in {
                        "record_travel_candidate",
                        "update_travel_candidate",
                        "respond_travel_collaboration_candidate",
                        "record_case_progress_candidate",
                        "update_case_progress_candidate",
                        "delete_case_progress_candidate",
                        "query_case_progress_candidate",
                        "link_case_progress_candidate",
                        "list_assigned_cases",
                        "query_operation_status",
                        "query_case_risk",
                        "update_case_followup_policy_candidate",
                        "trigger_case_followup_now_candidate",
                    }
                )
                if executable_candidates:
                    # Keep persisted Admission Tickets, business receipts, and
                    # domain effects on the stream turn's single commit boundary.
                    phase2_business_result = await Phase2BusinessComposer(
                        case_repository=CaseSqlRepository(session),
                        party_repository=PartySqlRepository(session),
                        progress_repository=CaseProgressSqlRepository(session),
                        followup_policy_repository=CaseFollowupPolicySqlRepository(session),
                        executor=SqlBusinessExecutor(
                            session,
                            effect_policy=BusinessEffectPolicy.from_settings(settings),
                            execution_authority=(
                                turn_runtime_result.mutation_execution_authority
                            ),
                        ),
                    ).execute(executable_candidates, business_context)
                    case_travel_offers = await (
                        create_case_travel_offers_from_business_result(
                            session,
                            business_result=phase2_business_result,
                            business_context=business_context,
                            raw_text=job.text,
                        )
                    )
            if cognitive_v3.command_plan.daily_commands:
                processed_command_names = [
                    command.command_type for command in cognitive_v3.command_plan.daily_commands
                ]
                v3_execution_result = await asyncio.wait_for(
                    execute_typed_agent2_daily_commands(
                        session,
                        user=user,
                        commands=cognitive_v3.command_plan.daily_commands,
                        execution_context=turn_runtime_result.daily_execution_context(),
                        settings=settings,
                        execution_authority=(
                            turn_runtime_result.mutation_execution_authority
                        ),
                    ),
                    timeout=settings.stream_processing_timeout_seconds,
                )
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=v3_execution_result.command_results,
                    business_result=phase2_business_result,
                    report_results=[
                        item.as_dict() for item in periodic_report_results
                    ],
                    business_context=verified_execution_context,
                    selection_continuation=(
                        turn_runtime_result.selection_continuation
                    ),
                )
            elif phase2_business_result is None and not periodic_report_results:
                lifecycle_message = pending_lifecycle_reply(cognitive_v3.decision)
                selection_message = selection_request_reply(
                    cognitive_v3.decision
                )
                information_message = information_pending_reply(
                    cognitive_v3.decision
                )
                admission_message = admission_block_reply(
                    cognitive_v3.decision
                )
                if lifecycle_message:
                    if not has_pending_lifecycle_update(cognitive_v3.decision):
                        raise RuntimeError("pending lifecycle reply requires a state update")
                    await finalize_cognitive_core_v3_execution(
                        session=session,
                        result=cognitive_v3,
                        command_results=[],
                        business_result=None,
                        report_results=[],
                        business_context=verified_execution_context,
                    )
                    reply_text = lifecycle_message
                elif selection_message:
                    await finalize_cognitive_core_v3_execution(
                        session=session,
                        result=cognitive_v3,
                        command_results=[],
                        business_result=None,
                        report_results=[],
                        business_context=verified_execution_context,
                    )
                    reply_text = selection_message
                elif information_message:
                    await finalize_cognitive_core_v3_execution(
                        session=session,
                        result=cognitive_v3,
                        command_results=[],
                        business_result=None,
                        report_results=[],
                        business_context=verified_execution_context,
                    )
                    reply_text = information_message
                elif has_bound_confirmation_pending(cognitive_v3.decision):
                    await finalize_cognitive_core_v3_execution(
                        session=session,
                        result=cognitive_v3,
                        command_results=[],
                        business_result=None,
                        report_results=[],
                        business_context=verified_execution_context,
                    )
                    reply_text = cognitive_v3.decision.clarification_need.question
                elif admission_message:
                    await finalize_cognitive_core_v3_execution(
                        session=session,
                        result=cognitive_v3,
                        command_results=[],
                        business_result=None,
                        report_results=[],
                        business_context=verified_execution_context,
                    )
                    reply_text = admission_message
                elif cognitive_v3.decision.clarification_need is not None:
                    reply_text = cognitive_v3.decision.clarification_need.question
                elif phase2_primary and (
                    reply_text := await build_cognitive_side_reply_v3(
                        decision=cognitive_v3.decision,
                        llm_client=llm_client,
                        context_pack=context_pack,
                    )
                ):
                    pass
                elif phase2_primary:
                    reply_text = "这条消息暂时无法形成明确可执行的操作，本次没有写入任何内容。"
                else:
                    reply_text = await _agent2_blocked_reply_text(
                        shadow=shadow,
                        raw_text=job.text,
                        llm_client=llm_client,
                        context_pack=context_pack,
                        daily_candidate_clarification=None,
                    )
                if phase2_primary:
                    if verified_execution_context is None:
                        raise RuntimeError(
                            "Agent2 read-only outcome requires verified execution context"
                        )
                    reply_text = format_dingtalk_plain_text(reply_text)
                    read_only_source_turn_id = str(
                        envelope.message_id or source_message_id
                    )
                    outcomes = (
                        text_outcome(
                            reply_text,
                            source_turn_id=read_only_source_turn_id,
                        ),
                    )
                    await persist_operation_outcomes(
                        session,
                        outcomes,
                        tenant_id=verified_execution_context.tenant_id,
                        user_id=verified_execution_context.actor_user_id,
                        conversation_id=verified_execution_context.conversation_id,
                        source_turn_id=read_only_source_turn_id,
                        now=verified_execution_context.occurred_at,
                    )
                    reply_text = OutcomeReplyComposer().compose(outcomes)
                response_payload = {"msgtype": "text", "text": {"content": reply_text}}
                await mark_webhook_event_processed(
                    session,
                    event,
                    report_id=None,
                    response_payload=response_payload,
                    now=now_in_timezone(settings.timezone),
                )
                commit_start = time.perf_counter()
                await session.commit()
                _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
                _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, reply_text))
                return "agent2_cognitive_v3_read_only"
            elif phase2_business_result is None:
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=[],
                    business_result=None,
                    report_results=[
                        item.as_dict() for item in periodic_report_results
                    ],
                    business_context=verified_execution_context,
                    selection_continuation=(
                        turn_runtime_result.selection_continuation
                    ),
                )
                outcomes = periodic_execution_outcomes(
                    periodic_report_results,
                    source_turn_id=str(envelope.message_id or source_message_id),
                )
                if verified_execution_context is not None:
                    await persist_operation_outcomes(
                        session,
                        outcomes,
                        tenant_id=verified_execution_context.tenant_id,
                        user_id=verified_execution_context.actor_user_id,
                        conversation_id=verified_execution_context.conversation_id,
                        source_turn_id=str(
                            envelope.message_id or source_message_id
                        ),
                        now=verified_execution_context.occurred_at,
                    )
                reply_text = append_cognitive_clarification(
                    OutcomeReplyComposer().compose(outcomes),
                    cognitive_v3.decision,
                )
                latest = periodic_report_results[-1]
                response_payload = {
                    "msgtype": "text",
                    "text": {"content": reply_text},
                }
                await mark_webhook_event_processed(
                    session,
                    event,
                    report_id=None,
                    response_payload=response_payload,
                    now=now_in_timezone(settings.timezone),
                )
                commit_start = time.perf_counter()
                await session.commit()
                _add_timing(
                    timings,
                    "db_commit_seconds",
                    _elapsed_seconds(commit_start),
                )
                _add_timing(
                    timings,
                    "dingtalk_send_seconds",
                    await _reply(handler, robot, job, reply_text),
                )
                return "agent2_periodic_report_processed"
            else:
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=[],
                    business_result=phase2_business_result,
                    report_results=[
                        item.as_dict() for item in periodic_report_results
                    ],
                    business_context=verified_execution_context,
                    selection_continuation=(
                        turn_runtime_result.selection_continuation
                    ),
                )
                outcomes = business_composition_outcomes(phase2_business_result)
                outcomes += tuple(
                    text_outcome(
                        offer.question,
                        source_turn_id=str(envelope.message_id or source_message_id),
                    )
                    for offer in case_travel_offers
                )
                outcomes += periodic_execution_outcomes(
                    periodic_report_results,
                    source_turn_id=str(envelope.message_id or source_message_id),
                )
                if verified_execution_context is not None:
                    outcomes += await project_committed_case_followup_facts(
                        business_result=phase2_business_result,
                        business_context=verified_execution_context,
                        source_session=session,
                        session_factory=AsyncSessionLocal,
                        settings=settings,
                        report_date=target_report_date,
                    )
                side_reply_text = await build_cognitive_side_reply_v3(
                    decision=cognitive_v3.decision,
                    llm_client=llm_client,
                    context_pack=context_pack,
                )
                if side_reply_text:
                    outcomes += (
                        text_outcome(
                            side_reply_text,
                            source_turn_id=str(envelope.message_id or source_message_id),
                        ),
                    )
                if verified_execution_context is not None:
                    await persist_operation_outcomes(
                        session, outcomes,
                        tenant_id=verified_execution_context.tenant_id,
                        user_id=verified_execution_context.actor_user_id,
                        conversation_id=verified_execution_context.conversation_id,
                        source_turn_id=str(envelope.message_id or source_message_id),
                        now=verified_execution_context.occurred_at,
                    )
                reply_text = append_cognitive_clarification(
                    OutcomeReplyComposer().compose(outcomes),
                    cognitive_v3.decision,
                )
                response_payload = {"msgtype": "text", "text": {"content": reply_text}}
                await mark_webhook_event_processed(
                    session,
                    event,
                    report_id=None,
                    response_payload=response_payload,
                    now=now_in_timezone(settings.timezone),
                )
                commit_start = time.perf_counter()
                await session.commit()
                _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
                _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, reply_text))
                return "agent2_phase2_business_processed"
    if (
        not phase2_primary
        and v3_execution_result is None
        and gate_decision is not None
        and gate_decision.block_legacy_daily
    ):
        daily_candidate_clarification = build_daily_candidate_clarification(
            raw_text=job.text,
            context_pack=context_pack,
        )
        reply_text = await _agent2_blocked_reply_text(
            shadow=shadow,
            raw_text=job.text,
            llm_client=llm_client,
            context_pack=context_pack,
            daily_candidate_clarification=daily_candidate_clarification,
        )
        if daily_candidate_clarification is not None:
            _store_pending_daily_candidate(
                daily_report,
                daily_candidate_clarification,
                settings=settings,
            )
        response_payload = {"msgtype": "text", "text": {"content": reply_text}}
        await mark_webhook_event_processed(
            session,
            event,
            report_id=None,
            response_payload=response_payload,
            now=now_in_timezone(settings.timezone),
        )
        commit_start = time.perf_counter()
        await session.commit()
        _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
        _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, reply_text))
        return "agent2_daily_gate_blocked"

    if v3_execution_result is not None:
        result = v3_execution_result
    else:
        if phase2_primary:
            raise RuntimeError("Agent2 Phase 2 primary cannot execute shadow daily commands")
        commands = list(shadow.commands)
        if not commands:
            return None
        processed_command_names = [command.operation for command in commands]

        result = await asyncio.wait_for(
            execute_agent2_daily_commands(
                session,
                user=user,
                raw_input=job.text,
                source="agent2_dingtalk_stream_text",
                commands=commands,
                settings=settings,
                message_id=str(envelope.message_id or getattr(event, "external_message_id", "") or getattr(event, "id", "")),
                expected_report_version=agent2_daily_report_version(daily_report),
            ),
            timeout=settings.stream_processing_timeout_seconds,
        )
        if _agent2_daily_should_fallback_to_legacy(result.command_results):
            logger.info("agent2 daily edit unresolved, falling back to legacy user_id=%s actions=%s", user.id, result.command_results)
            return None
    reply_message = result.message
    if phase2_primary and cognitive_v3 is not None:
        outcomes = daily_execution_outcomes(
            result,
            source_turn_id=str(envelope.message_id or source_message_id),
        )
        if phase2_business_result is not None:
            outcomes += business_composition_outcomes(phase2_business_result)
            outcomes += tuple(
                text_outcome(
                    offer.question,
                    source_turn_id=str(envelope.message_id or source_message_id),
                )
                for offer in case_travel_offers
            )
            if verified_execution_context is not None:
                outcomes += await project_committed_case_followup_facts(
                    business_result=phase2_business_result,
                    business_context=verified_execution_context,
                    source_session=session,
                    session_factory=AsyncSessionLocal,
                    settings=settings,
                    report_date=target_report_date,
                )
        outcomes += periodic_execution_outcomes(
            periodic_report_results,
            source_turn_id=str(envelope.message_id or source_message_id),
        )
        side_reply_text = await build_cognitive_side_reply_v3(
            decision=cognitive_v3.decision,
            llm_client=llm_client,
            context_pack=context_pack,
        )
        if side_reply_text:
            outcomes += (
                text_outcome(
                    side_reply_text,
                    source_turn_id=str(envelope.message_id or source_message_id),
                ),
            )
        if verified_execution_context is not None:
            await persist_operation_outcomes(
                session, outcomes,
                tenant_id=verified_execution_context.tenant_id,
                user_id=verified_execution_context.actor_user_id,
                conversation_id=verified_execution_context.conversation_id,
                source_turn_id=str(envelope.message_id or source_message_id),
                now=verified_execution_context.occurred_at,
            )
        reply_message = append_cognitive_clarification(
            OutcomeReplyComposer().compose(outcomes),
            cognitive_v3.decision,
        )
    elif shadow is not None:
        side_reply_text = await _agent2_side_reply_text(
            shadow=shadow,
            raw_text=job.text,
            llm_client=llm_client,
            context_pack=context_pack,
        )
        if side_reply_text:
            reply_message = f"{reply_message}\n\n{side_reply_text}"
        candidate_feedback = _agent2_candidate_feedback_text(
            shadow,
            raw_text=job.text,
            context_pack=context_pack,
        )
        if candidate_feedback:
            reply_message = f"{reply_message}\n\n{candidate_feedback}"
    response_payload = {"msgtype": "text", "text": {"content": reply_message}}
    await mark_webhook_event_processed(
        session,
        event,
        report_id=uuid.UUID(result.report_id) if result.report_id else None,
        response_payload=response_payload,
        now=now_in_timezone(settings.timezone),
    )
    commit_start = time.perf_counter()
    await session.commit()
    _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
    _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, reply_message))
    logger.info(
        "agent2 daily processed report_id=%s report_saved=%s read_only=%s commands=%s",
        result.report_id,
        result.report_saved,
        result.read_only,
        processed_command_names,
    )
    return "agent2_cognitive_v3_processed" if v3_execution_result is not None else "agent2_daily_processed"


def _agent2_daily_should_fallback_to_legacy(actions: list[dict[str, Any]]) -> bool:
    return agent2_daily_should_fallback_to_legacy(actions)


def _store_pending_daily_candidate(
    daily_report: Any | None,
    clarification: DailyCandidateClarification,
    *,
    settings: Settings,
) -> bool:
    if daily_report is None:
        return False
    daily_report.section_status = set_pending_daily_candidate(
        getattr(daily_report, "section_status", None),
        dict(clarification.pending_payload),
        created_at=now_in_timezone(settings.timezone).isoformat(),
    )
    return True


async def _agent2_blocked_reply_text(
    *,
    shadow: Any,
    raw_text: str,
    llm_client: LLMClient,
    context_pack: Agent2ContextPack | None = None,
    daily_candidate_clarification: DailyCandidateClarification | None = None,
) -> str:
    candidate_feedback = _agent2_candidate_feedback_text(shadow, raw_text=raw_text, context_pack=context_pack)
    daily_candidate_reply = (
        daily_candidate_clarification.reply_text
        if daily_candidate_clarification is not None
        else build_daily_candidate_clarification_reply(
            raw_text=raw_text,
            context_pack=context_pack,
        )
    )
    if daily_candidate_reply:
        if candidate_feedback:
            return f"{daily_candidate_reply}\n\n{candidate_feedback}"
        return daily_candidate_reply
    pending_candidate_focus_reply = build_pending_daily_candidate_focus_reply(
        raw_text=raw_text,
        context_pack=context_pack,
    )
    if pending_candidate_focus_reply:
        if candidate_feedback:
            return f"{pending_candidate_focus_reply}\n\n{candidate_feedback}"
        return pending_candidate_focus_reply
    assistant_reply = getattr(shadow, "assistant_reply", None)
    if assistant_reply is not None and getattr(assistant_reply, "text", ""):
        result = await build_tool_assisted_reply(
            raw_text=raw_text,
            assistant_reply=assistant_reply,
            llm_client=llm_client,
            context_pack=context_pack,
        )
        if result.fallback_used and result.error:
            logger.info("agent2 assistant tool fallback source=%s error=%s", result.source, result.error)
        if candidate_feedback:
            return f"{result.text}\n\n{candidate_feedback}"
        return result.text
    if candidate_feedback:
        return (
            "\u8fd9\u53e5\u6211\u6ca1\u6709\u5199\u5165\u65e5\u62a5\u3002\n\n"
            f"{candidate_feedback}"
        )
    gate_decision = getattr(shadow, "gate_decision", None)
    reply_text = str(getattr(gate_decision, "reply_text", "") or "")
    if reply_text:
        return reply_text
    return "\u8fd9\u53e5\u6211\u5148\u4e0d\u5199\u5165\u65e5\u62a5\uff0c\u8bf7\u8865\u5145\u8bf4\u660e\u3002"


async def _agent2_side_reply_text(
    *,
    shadow: Any,
    raw_text: str,
    llm_client: LLMClient,
    context_pack: Agent2ContextPack | None = None,
) -> str:
    assistant_reply = getattr(shadow, "assistant_reply", None)
    reply_type = str(getattr(assistant_reply, "reply_type", "") or "")
    if reply_type not in {"internal_qa", "legal_research"}:
        return ""
    result = await build_tool_assisted_reply(
        raw_text=raw_text,
        assistant_reply=assistant_reply,
        llm_client=llm_client,
        context_pack=context_pack,
    )
    if result.fallback_used and result.error:
        logger.info("agent2 side assistant tool fallback source=%s error=%s", result.source, result.error)
    return result.text


def _agent2_candidate_feedback_text(
    shadow: Any,
    *,
    raw_text: str = "",
    context_pack: Agent2ContextPack | None = None,
) -> str:
    sandbox = getattr(shadow, "coordination_sandbox", None)
    candidates = list(getattr(sandbox, "candidates", []) or [])
    if not candidates:
        return ""
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        candidate_type = str(getattr(candidate, "candidate_type", "") or "")
        if candidate_type == CANDIDATE_CASE_PROGRESS:
            matter_hint = _candidate_target_value(candidate, "matter_hint") or "\u6848\u4ef6\u540d\u5f85\u786e\u8ba4"
            key = (candidate_type, matter_hint)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- \u3010\u6848\u4ef6\u8fdb\u5c55\u5019\u9009\u3011{matter_hint}")
            location_line = _case_candidate_location_confirmation_line(
                matter_hint=matter_hint,
                raw_text=raw_text,
                context_pack=context_pack,
            )
            if location_line:
                lines.append(location_line)
            continue
        if candidate_type == CANDIDATE_TRAVEL_COORDINATION:
            destination = _candidate_target_value(candidate, "destination") or "\u76ee\u7684\u5730\u5f85\u786e\u8ba4"
            date_hint = _candidate_date_label(_candidate_target_value(candidate, "date_hint"))
            status = _candidate_travel_status_label(_candidate_target_value(candidate, "status"))
            key = (candidate_type, f"{destination}|{date_hint}|{status}")
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- \u3010\u51fa\u5dee\u534f\u540c\u5019\u9009\u3011{destination} | {date_hint} | {status}")
    if not lines:
        return ""
    return (
        "\u6211\u540c\u65f6\u8bc6\u522b\u5230\u4ee5\u4e0b\u534f\u540c\u5019\u9009"
        "\uff08\u4ec5\u8bb0\u5f55\u5019\u9009\uff0c\u4e0d\u4f1a\u81ea\u52a8\u901a\u77e5\u6216\u5199\u5165\u6b63\u5f0f\u53f0\u8d26\uff09\uff1a\n"
        + "\n".join(lines)
    )


def _candidate_target_value(candidate: Any, key: str) -> str:
    target = getattr(candidate, "target", {}) or {}
    if not isinstance(target, dict):
        return ""
    return str(target.get(key) or "").strip()


def _case_candidate_location_confirmation_line(
    *,
    matter_hint: str,
    raw_text: str,
    context_pack: Agent2ContextPack | None = None,
) -> str:
    text = str(raw_text or "")
    if not _looks_like_case_hearing_trip_probe(text):
        return ""
    hint = _case_location_hint_from_context(
        matter_hint=matter_hint,
        raw_text=text,
        context_pack=context_pack,
    )
    if hint is None:
        return ""
    case_name = getattr(hint, "case_name", "") or matter_hint
    assignee = getattr(hint, "assignee_name", "") or ""
    owner_suffix = f"\uff0c\u627f\u529e/\u8d1f\u8d23\u4eba\uff1a{assignee}" if assignee else ""
    location = getattr(hint, "court_or_location", "") or ""
    if location:
        return (
            f"  \u5e95\u8868\u53ef\u80fd\u547d\u4e2d\u3010{case_name}\u3011{owner_suffix}\uff1b"
            f"\u6cd5\u9662/\u5730\u70b9\u662f\u3010{location}\u3011\u3002"
            f"\u4f60\u662f\u53bb\u3010{location}\u3011\u5f00\u5ead\u561b\uff1f\u786e\u8ba4\u540e\u6211\u518d\u7ed9\u4f60\u8bb0\u5f55\u51fa\u5dee\u5019\u9009\u3002"
        )
    return (
        f"  \u5e95\u8868\u53ef\u80fd\u547d\u4e2d\u3010{case_name}\u3011{owner_suffix}\uff0c"
        "\u4f46\u6ca1\u770b\u5230\u660e\u786e\u6cd5\u9662/\u5730\u70b9\uff1b\u6211\u5148\u4e0d\u8bb0\u5f55\u51fa\u5dee\uff0c\u4f60\u8865\u4e2a\u5730\u70b9\u6211\u518d\u5904\u7406\u3002"
    )


def _looks_like_case_hearing_trip_probe(text: str) -> bool:
    value = str(text or "")
    if not any(marker in value for marker in ("\u5f00\u5ead", "\u5ead\u5ba1", "\u51fa\u5ead")):
        return False
    if not any(marker in value for marker in ("\u53bb", "\u8d74", "\u5230", "\u524d\u5f80", "\u51fa\u5dee")):
        return False
    return any(marker in value for marker in ("\u6848", "\u6848\u4ef6", "\u6cd5\u9662"))


def _case_location_hint_from_context(
    *,
    matter_hint: str,
    raw_text: str,
    context_pack: Agent2ContextPack | None,
) -> Any | None:
    hint = _case_location_hint_from_context_pack(matter_hint=matter_hint, context_pack=context_pack)
    if hint is not None:
        return hint
    if not DEFAULT_CASE_RAG_INDEX.exists():
        return None
    return find_case_location_hint(
        DEFAULT_CASE_RAG_INDEX,
        matter_hint=matter_hint,
        raw_text=raw_text,
    )


def _case_location_hint_from_context_pack(
    *,
    matter_hint: str,
    context_pack: Agent2ContextPack | None,
) -> Any | None:
    if context_pack is None:
        return None
    term = _case_feedback_lookup_term(matter_hint)
    if not term:
        return None
    for evidence in getattr(context_pack, "knowledge", ()) or ():
        if str(getattr(evidence, "source_type", "") or "") != "case_table_rag":
            continue
        facts = getattr(evidence, "facts", None)
        if not isinstance(facts, dict) or "case_count" in facts or facts.get("permission_denied"):
            continue
        case_name = str(facts.get("case_name") or getattr(evidence, "title", "") or "").strip()
        summary = str(getattr(evidence, "summary", "") or "")
        if term not in case_name and term not in summary:
            continue
        location = _location_from_case_facts(facts)
        return SimpleNamespace(
            case_name=case_name,
            court_or_location=location,
            department=str(facts.get("department") or ""),
            assignee_name=str(facts.get("assignee_name") or ""),
            source_id=str(getattr(evidence, "source_id", "") or ""),
        )
    return None


def _case_feedback_lookup_term(matter_hint: str) -> str:
    value = str(matter_hint or "").strip(" ：:，,。；;、")
    value = value.replace("\u6848\u4ef6", "").replace("\u6848", "")
    value = value.replace("\u5f00\u5ead", "").replace("\u5ead\u5ba1", "").replace("\u51fa\u5ead", "")
    return value.strip(" ：:，,。；;、")


def _location_from_case_facts(facts: dict[str, Any]) -> str:
    for key in (
        "\u627f\u529e\u6cd5\u9662",
        "\u53d7\u7406\u6cd5\u9662",
        "\u6267\u884c\u6cd5\u9662",
        "\u4e00\u5ba1\u6cd5\u9662",
        "\u4e8c\u5ba1\u6cd5\u9662",
        "\u7ba1\u8f96\u6cd5\u9662",
        "\u6cd5\u9662\u540d\u79f0",
        "\u5f00\u5ead\u6cd5\u9662",
        "\u5f00\u5ead\u5730\u70b9",
        "\u5ead\u5ba1\u5730\u70b9",
        "\u4ef2\u88c1\u59d4",
        "\u4ef2\u88c1\u59d4\u5458\u4f1a",
    ):
        value = str(facts.get(key) or "").strip(" ：:，,。；;、")
        if value and value not in {"/", "-", "\u65e0", "\u6682\u65e0", "\u5426", "\u662f", "0"}:
            return value
    return ""


def _candidate_date_label(value: str) -> str:
    return {
        "today": "\u4eca\u5929",
        "tomorrow": "\u660e\u5929",
        "future_weekday": "\u672c\u5468\u672a\u6765\u65e5\u671f",
        "next_week": "\u4e0b\u5468",
        "past_weekday": "\u5df2\u8fc7\u65e5\u671f",
    }.get(str(value or "").strip(), "\u65f6\u95f4\u5f85\u786e\u8ba4")


def _candidate_travel_status_label(value: str) -> str:
    return {
        "planned": "\u8ba1\u5212\u51fa\u5dee",
        "tentative": "\u53ef\u80fd\u51fa\u5dee",
        "already_traveled": "\u5df2\u51fa\u5dee",
    }.get(str(value or "").strip(), "\u72b6\u6001\u5f85\u786e\u8ba4")


def _performance_workflow_claims_batch(
    active_submissions: list[Any],
    turn_batch: SealedTurnBatch,
) -> bool:
    return any(
        is_performance_reply_candidate(
            metrics=submission_metrics(submission),
            responses=list(submission.responses_json or []),
            raw_input=fragment.text,
            status=submission.status,
        )
        for fragment in turn_batch.fragments
        for submission in active_submissions
    )


async def _require_agent2_batch_scope(
    *,
    session: Any,
    user: Any,
    dingtalk_user_id: str,
    conversation_id: str,
    turn_batch: SealedTurnBatch,
    performance_service: PerformanceTaskService,
    settings: Settings,
    now: Any,
) -> None:
    """Validate trusted scope facts without interpreting the user's text."""

    if len(turn_batch.fragments) <= 1:
        return
    events = list(
        (
            await session.scalars(
                select(WebhookEvent).where(
                    WebhookEvent.id.in_(turn_batch.event_ids)
                )
            )
        ).all()
    )
    if len(events) != len(turn_batch.event_ids):
        raise StreamBatchRequiresSerialProcessing(
            "turn batch event set changed before execution"
        )
    if any(
        event.status != "processing"
        or event.dingtalk_user_id != dingtalk_user_id
        for event in events
    ):
        raise StreamBatchRequiresSerialProcessing(
            "turn batch event scope changed before execution"
        )

    # Reuse the existing performance workflow's own claim decision.  This
    # adds no new wording rules: a claimed fragment stays on the exact serial
    # path it used before turn batching, while unrelated Agent2 messages can
    # still share one model turn.
    active_performance = await performance_service.get_active_submissions(
        session,
        user.id,
    )
    if _performance_workflow_claims_batch(
        active_performance,
        turn_batch,
    ):
        raise StreamBatchRequiresSerialProcessing(
            "performance workflow claimed a turn fragment"
        )
    if any(
        looks_like_performance_reply_template(fragment.text)
        for fragment in turn_batch.fragments
    ):
        raise StreamBatchRequiresSerialProcessing(
            "performance template requires serial processing"
        )

    route = await resolve_tool_call_canary_route(
        session,
        user=user,
        dingtalk_user_id=dingtalk_user_id,
        settings=settings,
        conversation_id=conversation_id,
        source_message_id=turn_batch.source_message_id,
        now=now,
    )
    if route.decision.owner != "tool_call_core":
        raise StreamBatchRequiresSerialProcessing(
            "Agent2 route is not batchable"
        )


async def _handle_job(
    *,
    job: StreamJob,
    handler: DailyReviewStreamHandler,
    settings: Settings,
    performance_service: PerformanceTaskService,
    report_service: DailyReportService,
    robot: DingTalkRobotClient,
    turn_batch: SealedTurnBatch | None = None,
    turn_batch_wait_seconds: float = 0.0,
) -> None:
    started_at = time.perf_counter()
    timings = _initial_stream_timings(job, started_at)
    idempotency_key = (
        job.idempotency_key
        or _stream_idempotency_key(job.message, job.text)
    )
    dingtalk_user_id = _stream_user_id(job.message)
    status = "started"
    user_name: str | None = None
    report_id: str | None = None
    error_message: str | None = None
    should_log_timing = True
    if turn_batch is not None:
        timings["turn_batch_wait_seconds"] = _safe_seconds(
            turn_batch_wait_seconds
        )
        timings["turn_batch_id"] = turn_batch.batch_id
        timings["turn_batch_size"] = len(turn_batch.fragments)
    logger.info(
        "stream job start idempotency_key=%s user=%s message=%s",
        idempotency_key,
        dingtalk_user_id,
        job.message.message_id,
    )

    async with AsyncSessionLocal() as session:
        event = None
        try:
            if job.event_id is not None:
                event = await session.get(WebhookEvent, job.event_id)
                if event is None:
                    raise RuntimeError(
                        "persisted stream event cannot be loaded"
                    )
                if event.idempotency_key != idempotency_key:
                    raise RuntimeError(
                        "persisted stream event identity mismatch"
                    )
                if event.status != "processing":
                    status = "already_closed"
                    return
                inserted = True
            else:
                logger.info(
                    "creating stream event key=%s",
                    idempotency_key,
                )
                event, inserted = await create_webhook_event_once(
                    session,
                    idempotency_key=idempotency_key,
                    external_message_id=job.message.message_id,
                    dingtalk_user_id=dingtalk_user_id,
                    payload=job.payload,
                )
                commit_start = time.perf_counter()
                await session.commit()
                _add_timing(
                    timings,
                    "db_commit_seconds",
                    _elapsed_seconds(commit_start),
                )
                logger.info(
                    "stream event committed key=%s inserted=%s status=%s",
                    idempotency_key,
                    inserted,
                    event.status,
                )

            if not inserted:
                status = "duplicate"
                response_payload = event.response_payload or {}
                send_seconds = 0.0

                async def send_cached_reply(text: str) -> None:
                    nonlocal send_seconds
                    send_seconds = await _reply(
                        handler,
                        robot,
                        job,
                        text,
                    )

                sent = await deliver_cached_canary_message_if_enabled(
                    response_payload,
                    send_cached_reply,
                )
                if sent:
                    _add_timing(
                        timings,
                        "dingtalk_send_seconds",
                        send_seconds,
                    )
                return

            if not dingtalk_user_id:
                raise ValueError("DingTalk stream message missing senderStaffId.")

            logger.info("loading stream user dingtalk_user_id=%s", dingtalk_user_id)
            load_user_start = time.perf_counter()
            user = await get_active_user_by_dingtalk_id(session, dingtalk_user_id)
            timings["load_user_seconds"] = _elapsed_seconds(load_user_start)
            if user is None:
                status = "unknown_user"
                reply_text = TEXT_UNKNOWN_USER
                response_payload = {"msgtype": "text", "text": {"content": reply_text}}
                await mark_webhook_event_failed(
                    session,
                    event,
                    error_message=f"Unknown DingTalk user: {dingtalk_user_id}",
                    response_payload=response_payload,
                    now=now_in_timezone(settings.timezone),
                )
                commit_start = time.perf_counter()
                await session.commit()
                _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
                _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, reply_text))
                return

            user_name = user.name
            conversation_id = str(
                getattr(job.message, "conversation_id", "") or ""
            )
            canary_now = now_in_timezone(
                user.timezone or settings.timezone
            )
            if turn_batch is not None:
                await _require_agent2_batch_scope(
                    session=session,
                    user=user,
                    dingtalk_user_id=dingtalk_user_id,
                    conversation_id=conversation_id,
                    turn_batch=turn_batch,
                    performance_service=performance_service,
                    settings=settings,
                    now=canary_now,
                )
            logger.info("checking stream performance task user_id=%s", user.id)
            performance_result = await asyncio.wait_for(
                performance_service.submit_text(
                    session,
                    user=user,
                    raw_input=job.text,
                    source="dingtalk_stream_text",
                    require_performance_signal=True,
                ),
                timeout=settings.stream_processing_timeout_seconds,
            )
            if performance_result is not None:
                if turn_batch is not None and len(turn_batch.fragments) > 1:
                    raise StreamBatchRequiresSerialProcessing(
                        "performance route changed after batch admission"
                    )
                response_payload = {"msgtype": "text", "text": {"content": performance_result.message}}
                await mark_webhook_event_processed(
                    session,
                    event,
                    report_id=None,
                    response_payload=response_payload,
                    now=now_in_timezone(settings.timezone),
                )
                commit_start = time.perf_counter()
                await session.commit()
                _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
                logger.info(
                    "stream performance submitted task_id=%s submission_id=%s status=%s",
                    performance_result.task_id,
                    performance_result.submission_id,
                    performance_result.status,
                )
                _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, performance_result.message))
                status = "performance_processed"
                return

            if (
                turn_batch is not None
                and len(turn_batch.fragments) > 1
                and _performance_workflow_claims_batch(
                    await performance_service.get_active_submissions(
                        session,
                        user.id,
                    ),
                    turn_batch,
                )
            ):
                raise StreamBatchRequiresSerialProcessing(
                    "performance route changed after batch admission"
                )

            if looks_like_performance_reply_template(job.text):
                if turn_batch is not None and len(turn_batch.fragments) > 1:
                    raise StreamBatchRequiresSerialProcessing(
                        "performance template changed after batch admission"
                    )
                response_payload = {"msgtype": "text", "text": {"content": NO_ACTIVE_PERFORMANCE_TASK_MESSAGE}}
                await mark_webhook_event_processed(
                    session,
                    event,
                    report_id=None,
                    response_payload=response_payload,
                    now=now_in_timezone(settings.timezone),
                )
                commit_start = time.perf_counter()
                await session.commit()
                _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
                _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, NO_ACTIVE_PERFORMANCE_TASK_MESSAGE))
                status = "performance_no_active_task"
                return

            turn_user_messages = (
                turn_batch.user_messages
                if turn_batch is not None
                else (job.text,)
            )
            turn_source_message_id = (
                turn_batch.source_message_id
                if turn_batch is not None
                else idempotency_key
            )
            tool_call_canary = await process_tool_call_canary_ingress(
                session,
                user=user,
                dingtalk_user_id=dingtalk_user_id,
                user_text=(
                    turn_user_messages[0]
                    if len(turn_user_messages) == 1
                    else ""
                ),
                user_messages=(
                    turn_user_messages
                    if len(turn_user_messages) > 1
                    else ()
                ),
                source_channel="dingtalk_stream_text",
                conversation_id=conversation_id,
                source_message_id=turn_source_message_id,
                settings=settings,
                llm_client=report_service.extractor.client,
                now=now_in_timezone(
                    user.timezone or settings.timezone
                ),
            )
            _apply_canary_observability(timings, tool_call_canary)
            if (
                turn_batch is not None
                and len(turn_batch.fragments) > 1
                and tool_call_canary.owner != "tool_call_core"
            ):
                raise StreamBatchRequiresSerialProcessing(
                    "Agent2 route changed after batch admission"
                )
            if tool_call_canary.handled:
                reply_text = tool_call_canary.message
                response_payload = build_canary_persisted_response_payload(
                    tool_call_canary
                )
                await _mark_canary_turn_closed(
                    session=session,
                    event=event,
                    turn_batch=turn_batch,
                    report_id=(
                        uuid.UUID(tool_call_canary.report_id)
                        if tool_call_canary.report_id
                        else None
                    ),
                    response_payload=response_payload,
                    now=now_in_timezone(settings.timezone),
                )
                commit_start = time.perf_counter()
                await session.commit()
                timings[
                    "agent2_business_transaction_status"
                ] = "committed"
                _add_timing(
                    timings,
                    "db_commit_seconds",
                    _elapsed_seconds(commit_start),
                )
                send_observation = StreamReplyObservation(
                    elapsed_seconds=0.0,
                    transport_status=(
                        "not_attempted"
                        if tool_call_canary.messages_enabled
                        else "suppressed"
                    ),
                    provider_accepted=False,
                    delivery_verified=False,
                )

                async def send_canary_reply() -> None:
                    nonlocal send_observation
                    send_observation = await _reply_with_observability(
                        handler,
                        robot,
                        job,
                        reply_text,
                    )

                await deliver_canary_message_if_enabled(
                    tool_call_canary,
                    send_canary_reply,
                )
                _apply_reply_observability(timings, send_observation)
                status = _canary_stream_status(
                    tool_call_canary,
                    send_observation,
                )
                return

            if turn_batch is not None:
                raise RuntimeError(
                    "canary route changed after the turn batch was sealed"
                )
            raise RuntimeError(
                "Agent2 Tool-Call Core did not handle a production stream turn"
            )
        except StreamBatchRequiresSerialProcessing:
            status = "turn_batch_requires_serial_processing"
            should_log_timing = False
            await session.rollback()
            raise
        except CanaryIngressExecutionError as exc:
            status = "tool_call_canary_failed"
            error_message = exc.error_type
            await session.rollback()
            failure_outcome = exc.outcome()
            _apply_canary_observability(timings, failure_outcome)
            timings[
                "agent2_business_transaction_status"
            ] = "rolled_back"
            reply_text = failure_outcome.message
            response_payload = build_canary_persisted_response_payload(
                failure_outcome
            )
            if event is not None:
                commit_start = time.perf_counter()
                async with session.begin():
                    event = await session.get(WebhookEvent, event.id)
                    if event is None:
                        raise RuntimeError(
                            "stream event missing during failure handling"
                        )
                    await _mark_canary_turn_closed(
                        session=session,
                        event=event,
                        turn_batch=turn_batch,
                        report_id=None,
                        error_message=exc.error_type,
                        response_payload=response_payload,
                        now=now_in_timezone(settings.timezone),
                    )
                _add_timing(
                    timings,
                    "db_commit_seconds",
                    _elapsed_seconds(commit_start),
                )
            send_observation = StreamReplyObservation(
                elapsed_seconds=0.0,
                transport_status=(
                    "not_attempted"
                    if failure_outcome.messages_enabled
                    else "suppressed"
                ),
                provider_accepted=False,
                delivery_verified=False,
            )

            async def send_canary_failure() -> None:
                nonlocal send_observation
                send_observation = await _reply_with_observability(
                    handler,
                    robot,
                    job,
                    reply_text,
                )

            await deliver_canary_message_if_enabled(
                failure_outcome,
                send_canary_failure,
            )
            _apply_reply_observability(timings, send_observation)
            status = _canary_stream_status(
                failure_outcome,
                send_observation,
            )
        except LLMOutputError as exc:
            status = "llm_failed"
            error_message = exc.__class__.__name__
            await session.rollback()
            reply_text = TEXT_LLM_FAILED
            if event is not None:
                commit_start = time.perf_counter()
                async with session.begin():
                    event = await session.get(WebhookEvent, event.id)
                    if event is None:
                        raise RuntimeError(
                            "stream event missing during failure handling"
                        )
                    await _mark_canary_turn_closed(
                        session=session,
                        event=event,
                        turn_batch=turn_batch,
                        report_id=None,
                        error_message=str(exc),
                        response_payload={"msgtype": "text", "text": {"content": reply_text}},
                        now=now_in_timezone(settings.timezone),
                    )
                _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
            _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, reply_text))
        except Exception as exc:
            status = "failed"
            error_message = exc.__class__.__name__
            await session.rollback()
            if (
                timings.get("agent2_business_transaction_status")
                == "pending"
            ):
                timings[
                    "agent2_business_transaction_status"
                ] = "rolled_back"
            reply_text = TEXT_PROCESS_FAILED
            logger.exception("stream job failed")
            if event is not None:
                commit_start = time.perf_counter()
                async with session.begin():
                    event = await session.get(WebhookEvent, event.id)
                    if event is None:
                        raise RuntimeError(
                            "stream event missing during failure handling"
                        )
                    await _mark_canary_turn_closed(
                        session=session,
                        event=event,
                        turn_batch=turn_batch,
                        report_id=None,
                        error_message=str(exc),
                        response_payload={"msgtype": "text", "text": {"content": reply_text}},
                        now=now_in_timezone(settings.timezone),
                    )
                _add_timing(timings, "db_commit_seconds", _elapsed_seconds(commit_start))
            _add_timing(timings, "dingtalk_send_seconds", await _reply(handler, robot, job, reply_text))
        finally:
            if should_log_timing:
                total_base = job.received_at_monotonic or started_at
                timings["total_seconds"] = round(max(0.0, time.perf_counter() - total_base), 4)
                _log_stream_timing(
                    job=job,
                    dingtalk_user_id=dingtalk_user_id,
                    user_name=user_name,
                    timings=timings,
                    status=status,
                    report_id=report_id,
                    error=error_message,
                )
            logger.info("stream job done key=%s", idempotency_key)


def _log_batched_stream_follower(
    *,
    job: StreamJob,
    stream_batch: SealedStreamJobBatch,
    worker_started_at: float,
) -> None:
    timings = _initial_stream_timings(job, worker_started_at)
    timings["turn_batch_wait_seconds"] = _safe_seconds(
        stream_batch.wait_seconds
    )
    timings["turn_batch_id"] = stream_batch.turn_batch.batch_id
    timings["turn_batch_size"] = len(
        stream_batch.turn_batch.fragments
    )
    total_base = job.received_at_monotonic or worker_started_at
    timings["total_seconds"] = round(
        max(0.0, time.perf_counter() - total_base),
        4,
    )
    _log_stream_timing(
        job=job,
        dingtalk_user_id=_stream_user_id(job.message),
        user_name=None,
        timings=timings,
        status="tool_call_canary_batched_follower",
    )


async def _observe_batched_stream_follower(
    *,
    job: StreamJob,
    stream_batch: SealedStreamJobBatch,
    worker_started_at: float,
) -> None:
    try:
        mode = await asyncio.shield(stream_batch.execution_done)
    except asyncio.CancelledError:
        return
    if mode == "combined":
        _log_batched_stream_follower(
            job=job,
            stream_batch=stream_batch,
            worker_started_at=worker_started_at,
        )


async def _worker(
    worker_id: int,
    queue: asyncio.Queue[StreamJob],
    handler: DailyReviewStreamHandler,
    settings: Settings,
    performance_service: PerformanceTaskService,
    report_service: DailyReportService,
    robot: DingTalkRobotClient,
    turn_batch_coordinator: StreamJobBatchCoordinator | None,
    conversation_locks: dict[str, asyncio.Lock],
) -> None:
    while True:
        job = await queue.get()
        worker_started_at = time.perf_counter()
        job = replace(
            job,
            worker_dequeued_at_monotonic=worker_started_at,
        )
        try:
            logger.info("worker=%s processing message=%s", worker_id, job.message.message_id)
            stream_batch = (
                await turn_batch_coordinator.collect(job)
                if turn_batch_coordinator is not None
                else None
            )
            if stream_batch is not None and not stream_batch.is_leader(job):
                asyncio.create_task(
                    _observe_batched_stream_follower(
                        job=job,
                        stream_batch=stream_batch,
                        worker_started_at=worker_started_at,
                    )
                )
                continue
            turn_key = _stream_turn_key(job)
            lock = conversation_locks.setdefault(turn_key, asyncio.Lock())
            batch_mode = "aborted"
            try:
                async with lock:
                    if stream_batch is None:
                        await _handle_job(
                            job=job,
                            handler=handler,
                            settings=settings,
                            performance_service=performance_service,
                            report_service=report_service,
                            robot=robot,
                        )
                    else:
                        leader_job = stream_batch.jobs[0]
                        try:
                            await _handle_job(
                                job=leader_job,
                                handler=handler,
                                settings=settings,
                                performance_service=performance_service,
                                report_service=report_service,
                                robot=robot,
                                turn_batch=stream_batch.turn_batch,
                                turn_batch_wait_seconds=(
                                    stream_batch.wait_seconds
                                ),
                            )
                            batch_mode = "combined"
                        except StreamBatchRequiresSerialProcessing:
                            batch_mode = "serial"
                            for member_job in stream_batch.jobs:
                                await _handle_job(
                                    job=member_job,
                                    handler=handler,
                                    settings=settings,
                                    performance_service=performance_service,
                                    report_service=report_service,
                                    robot=robot,
                                )
            finally:
                if (
                    stream_batch is not None
                    and turn_batch_coordinator is not None
                ):
                    await turn_batch_coordinator.complete(
                        stream_batch,
                        mode=batch_mode,
                    )
        finally:
            queue.task_done()


def _stream_turn_key(job: StreamJob) -> str:
    """Serialize one user's messages inside one conversation in arrival order."""

    message = job.message
    user_id = str(
        getattr(message, "sender_staff_id", "")
        or getattr(message, "sender_id", "")
        or ""
    ).strip()
    conversation_id = str(
        getattr(message, "conversation_id", "") or ""
    ).strip()
    if user_id and conversation_id:
        return f"{user_id}:{conversation_id}"
    message_id = str(getattr(message, "message_id", "") or "").strip()
    return f"unbound:{message_id or id(job)}"


async def run_stream() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        raise RuntimeError("DingTalk app key/secret are required for stream mode.")

    llm_client = LLMClient(settings)
    robot = DingTalkRobotClient(settings)
    performance_service = PerformanceTaskService(settings)
    report_service = DailyReportService(settings, DailyReportExtractor(llm_client))
    queue: asyncio.Queue[StreamJob] = asyncio.Queue(maxsize=settings.stream_queue_size)
    conversation_locks: dict[str, asyncio.Lock] = {}
    handler = DailyReviewStreamHandler(queue, robot, settings)
    turn_batch_coordinator = StreamJobBatchCoordinator(
        CanaryTurnBatchCoordinator(
            quiet_seconds=(
                settings.agent2_canary_turn_batch_quiet_seconds
            ),
            max_window_seconds=(
                settings.agent2_canary_turn_batch_max_window_seconds
            ),
        )
    )

    credential = dingtalk_stream.Credential(settings.dingtalk_app_key, settings.dingtalk_app_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, handler)

    workers = [
        asyncio.create_task(
            _worker(
                i + 1,
                queue,
                handler,
                settings,
                performance_service,
                report_service,
                robot,
                turn_batch_coordinator,
                conversation_locks,
            )
        )
        for i in range(settings.stream_worker_count)
    ]

    try:
        await _enqueue_recoverable_stream_jobs(queue)
        logger.info(
            "starting DingTalk stream workers=%s queue_size=%s",
            settings.stream_worker_count,
            settings.stream_queue_size,
        )
        await client.start()
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await robot.close()
        await llm_client.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(run_stream())


if __name__ == "__main__":
    main()
