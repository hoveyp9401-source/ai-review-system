from __future__ import annotations

import hashlib
import logging
from typing import Any
from dataclasses import replace
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import AsyncSessionLocal, get_session
from app.llm.extractor import LLMOutputError
from app.repositories import (
    create_webhook_event_once,
    get_active_user_by_dingtalk_id,
    mark_webhook_event_failed,
    mark_webhook_event_processed,
)
from app.services.dingtalk import (
    DingTalkPayloadError,
    UNSUPPORTED_FILE_REPLY_TEXT,
    VOICE_CONTENT_UNAVAILABLE_REPLY_TEXT,
    VOICE_TRANSCRIPTION_UNAVAILABLE_REPLY_TEXT,
    build_idempotency_key,
    dingtalk_text_response,
    extract_voice_download_code,
    parse_incoming_message,
    verify_incoming_token,
)
from app.services.dingtalk_crypto import DingTalkCallbackCrypto
from app.services.performance_service import (
    NO_ACTIVE_PERFORMANCE_TASK_MESSAGE,
    looks_like_performance_reply_template,
)
from app.services.report_service import DailyReportService
from app.utils.dingtalk_text import format_dingtalk_plain_text
from app.utils.time import now_in_timezone
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
from app.agent2.context_pack import build_agent2_context_pack
from app.agent2.performance_knowledge import attach_live_performance_catalog
from app.agent2.tool_calling.canary_service import (
    CanaryIngressExecutionError,
    build_canary_persisted_response_payload,
    build_canary_response_payload,
    canary_provider_response_payload,
    canary_route_suppresses_delivery,
    deliver_canary_message_if_enabled,
    is_canary_message_delivery_suppressed,
    process_tool_call_canary_ingress,
    resolve_tool_call_canary_route,
)
from app.agent2.tool_calling.turn_batching import (
    prepare_recoverable_ingress_payload,
)
from app.agent2.case_report_projection_runtime import (
    project_committed_case_followup_facts,
)
from app.agent2.report_projection_confirmation_runtime import (
    execute_report_projection_confirmation_turn,
)
from app.agent2.report_projection_correction_runtime import (
    execute_report_projection_correction_turn,
)
from app.agent2.business.composition import Phase2BusinessComposer
from app.agent2.business.entrypoint import (
    build_business_command_context,
    decide_runtime_owner,
    persist_runtime_owner_claim,
    resolve_agent2_entrypoint,
)
from app.agent2.business.repositories import CaseFollowupPolicySqlRepository, CaseProgressSqlRepository, CaseSqlRepository, PartySqlRepository
from app.agent2.business.sql_executor import SqlBusinessExecutor
from app.agent2.business.policy import BusinessEffectPolicy
from app.agent2.daily_shadow import evaluate_daily_shadow
from app.agent2.daily_execution import (
    Agent2DailyExecutionResult,
    agent2_daily_report_version,
    execute_agent2_daily_commands,
)
from app.agent2.typed_daily_executor import execute_typed_agent2_daily_commands
from app.agent2.report_sql_executor import (
    execute_periodic_report_commands,
)
from app.agent2.operation_outcomes import OutcomeReplyComposer
from app.agent2.operation_outcome_store import persist_operation_outcomes
from app.agent2.outcome_adapters import (
    business_composition_outcomes,
    daily_execution_outcomes,
    periodic_execution_outcomes,
    text_outcome,
)
from app.workflows.intake import IncomingMessageEnvelope
from app.workflows.daily_context import (
    build_live_daily_active_task,
    daily_active_task_from_report,
    load_live_daily_context,
    load_live_daily_report,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


def _log_callback_metadata(
    event: str,
    params: dict[str, Any] | None = None,
    *,
    payload: str | bytes | None = None,
) -> None:
    """Log only bounded callback metadata; never credentials or message text."""

    params = params or {}
    payload_bytes = (
        payload
        if isinstance(payload, bytes)
        else str(payload or "").encode("utf-8")
    )
    logger.info(
        "%s parameter_count=%d signature_present=%s nonce_present=%s "
        "timestamp_present=%s token_present=%s payload_chars=%d payload_sha256=%s",
        event,
        len(params),
        bool(params.get("msg_signature") or params.get("signature")),
        bool(params.get("nonce")),
        bool(params.get("timestamp")),
        bool(params.get("token")),
        len(payload_bytes),
        hashlib.sha256(payload_bytes).hexdigest(),
    )


def _log_runtime_failure(event: str, error: BaseException) -> None:
    logger.warning("%s error_type=%s", event, type(error).__name__)


def _get_crypto(settings: Settings) -> DingTalkCallbackCrypto:
    return DingTalkCallbackCrypto(
        token=settings.dingtalk_callback_token,
        aes_key=settings.dingtalk_callback_aes_key,
        app_key=settings.dingtalk_app_key,
    )


def _encrypt_response(crypto: DingTalkCallbackCrypto, payload: dict[str, Any]) -> JSONResponse:
    import json
    plain = json.dumps(payload, ensure_ascii=False)
    encrypted, sig, ts, nc = crypto.encrypt(plain)
    return JSONResponse({
        "msg_signature": sig,
        "timeStamp": ts,
        "nonce": nc,
        "encrypt": encrypted,
    })


def _encrypted_success(crypto: DingTalkCallbackCrypto) -> JSONResponse:
    encrypted, sig, ts, nc = crypto.encrypt("success")
    return JSONResponse({
        "msg_signature": sig,
        "timeStamp": ts,
        "nonce": nc,
        "encrypt": encrypted,
    })


async def _handle_dingtalk_event_subscription(request: Request, settings: Settings):
    params = dict(request.query_params)
    _log_callback_metadata("dingtalk_event_subscription_request", params)

    if request.method == "GET":
        sig = params.get("msg_signature") or params.get("signature") or ""
        timestamp = params.get("timestamp", "")
        nonce = params.get("nonce", "")
        echostr = params.get("echostr", "")
        if sig and timestamp and nonce and echostr:
            crypto = _get_crypto(settings)
            if crypto.verify_signature(sig, timestamp, nonce, echostr):
                return PlainTextResponse(crypto.decrypt(echostr))
            raise HTTPException(status_code=403, detail="Invalid signature")
        return PlainTextResponse("ok")

    body = await request.json()
    msg_signature = params.get("msg_signature") or params.get("signature") or ""
    timestamp = params.get("timestamp", "")
    nonce = params.get("nonce", "")
    if not (msg_signature and timestamp and nonce and "encrypt" in body):
        return PlainTextResponse("ok")

    crypto = _get_crypto(settings)
    if not crypto.verify_signature(msg_signature, timestamp, nonce, body["encrypt"]):
        raise HTTPException(status_code=403, detail="Invalid callback signature")

    raw = crypto.decrypt(body["encrypt"])
    _log_callback_metadata(
        "dingtalk_event_subscription_payload",
        params,
        payload=raw,
    )
    return _encrypted_success(crypto)


@router.get("/dingtalk")
async def dingtalk_webhook_get(
    request: Request,
    signature: str = Query(default=""),
    msg_signature: str = Query(default=""),
    timestamp: str = Query(default=""),
    nonce: str = Query(default=""),
    echostr: str = Query(default=""),
    settings: Settings = Depends(get_settings),
):
    _log_callback_metadata("dingtalk_webhook_get", dict(request.query_params))
    sig = msg_signature or signature
    if sig and timestamp and nonce and echostr:
        crypto = _get_crypto(settings)
        if crypto.verify_signature(sig, timestamp, nonce, echostr):
            raw = crypto.decrypt(echostr)
            return PlainTextResponse(raw)
        raise HTTPException(status_code=403, detail="Invalid signature")
    return PlainTextResponse("ok")


@router.get("/dingtalk/events")
async def dingtalk_event_subscription_get(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    return await _handle_dingtalk_event_subscription(request, settings)


@router.post("/dingtalk/events")
async def dingtalk_event_subscription_post(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    return await _handle_dingtalk_event_subscription(request, settings)


@router.post("/dingtalk")
async def dingtalk_webhook(
    request: Request,
    token: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Any:
    params = dict(request.query_params)
    body = await request.json()
    _log_callback_metadata("dingtalk_webhook_post", params)

    msg_signature = params.get("msg_signature", "")
    timestamp = params.get("timestamp", "")
    nonce = params.get("nonce", "")
    is_encrypted = bool(msg_signature and timestamp and nonce and "encrypt" in body)

    if is_encrypted:
        if not settings.dingtalk_callback_token or not settings.dingtalk_callback_aes_key:
            raise HTTPException(status_code=500, detail="Callback crypto not configured")

        crypto = _get_crypto(settings)
        if not crypto.verify_signature(msg_signature, timestamp, nonce, body["encrypt"]):
            raise HTTPException(status_code=403, detail="Invalid callback signature")

        raw = crypto.decrypt(body["encrypt"])
        _log_callback_metadata("dingtalk_webhook_decrypted", params, payload=raw)

        if raw == "success" or '"EventType":"check_url"' in raw:
            return _encrypted_success(crypto)

        payload = crypto.decrypt_json(body["encrypt"])
    else:
        payload = body
        crypto = None

    try:
        verify_incoming_token(settings, token)
        incoming = parse_incoming_message(payload)
    except DingTalkPayloadError as exc:
        error_resp = dingtalk_text_response(str(exc))
        if is_encrypted and crypto:
            return _encrypt_response(crypto, error_resp)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    message_type = str(
        payload.get("msgtype") or payload.get("msgType") or "text"
    )
    voice_transcribe_error = ""
    voice_content_error = ""

    # If audio/voice message without auto-recognition text, try ASR
    if not incoming.text:
        if message_type in ("audio", "voice"):
            download_code = extract_voice_download_code(payload)
            if download_code:
                robot = request.app.state.dingtalk_robot
                try:
                    recognized = await robot.recognize_audio(str(download_code))
                    from dataclasses import replace
                    incoming = replace(incoming, text=str(recognized).strip())
                except Exception as exc:
                    voice_transcribe_error = (
                        f"voice_transcribe_failed: {exc.__class__.__name__}: {exc}"
                    )
                    logger.warning(
                        "dingtalk_asr_failed download_code_sha256=%s error_type=%s",
                        hashlib.sha256(str(download_code).encode("utf-8")).hexdigest(),
                        type(exc).__name__,
                    )
                else:
                    if not incoming.text:
                        voice_content_error = "voice_transcribe_empty"
            else:
                voice_content_error = "voice_without_download_code"

    persisted_payload = prepare_recoverable_ingress_payload(
        payload,
        text=incoming.text,
        message_type=message_type,
        voice_download_seconds=0.0,
        voice_transcribe_seconds=0.0,
    )
    idempotency_key = build_idempotency_key(payload, incoming)
    event, inserted = await create_webhook_event_once(
        session,
        idempotency_key=idempotency_key,
        external_message_id=incoming.message_id,
        dingtalk_user_id=incoming.dingtalk_user_id,
        payload=persisted_payload,
    )
    await session.commit()

    immediate_failure_reply = ""
    immediate_failure_error = ""
    if message_type == "file":
        immediate_failure_reply = UNSUPPORTED_FILE_REPLY_TEXT
        immediate_failure_error = "unsupported_file_message"
    elif voice_transcribe_error:
        immediate_failure_reply = VOICE_TRANSCRIPTION_UNAVAILABLE_REPLY_TEXT
        immediate_failure_error = voice_transcribe_error
    elif voice_content_error:
        immediate_failure_reply = VOICE_CONTENT_UNAVAILABLE_REPLY_TEXT
        immediate_failure_error = voice_content_error

    if immediate_failure_reply:
        response_payload = dingtalk_text_response(immediate_failure_reply)
        if inserted or event.status == "processing":
            await mark_webhook_event_failed(
                session,
                event,
                error_message=immediate_failure_error,
                response_payload=response_payload,
                now=now_in_timezone(settings.timezone),
            )
            await session.commit()
        else:
            response_payload = (
                canary_provider_response_payload(event.response_payload)
                or response_payload
            )
        if is_encrypted and crypto:
            return _encrypt_response(crypto, response_payload)
        return response_payload

    if not inserted:
        resp = canary_provider_response_payload(
            event.response_payload
        )
        if not resp:
            duplicate_user = await get_active_user_by_dingtalk_id(
                session,
                incoming.dingtalk_user_id,
            )
            if duplicate_user is not None:
                duplicate_resolution = (
                    await resolve_tool_call_canary_route(
                        session,
                        user=duplicate_user,
                        dingtalk_user_id=incoming.dingtalk_user_id,
                        settings=settings,
                        conversation_id=str(
                            getattr(incoming, "conversation_id", "")
                            or (
                                f"dingtalk:{incoming.source}:"
                                f"{incoming.dingtalk_user_id}"
                            )
                        ),
                        source_message_id=idempotency_key,
                        now=now_in_timezone(
                            duplicate_user.timezone
                            or settings.timezone
                        ),
                    )
                )
                if canary_route_suppresses_delivery(
                    duplicate_resolution
                ):
                    if is_encrypted and crypto:
                        return _encrypted_success(crypto)
                    return PlainTextResponse("ok")
            resp = dingtalk_text_response(
                "这条复盘已收到，请等待处理结果。"
            )
        if is_canary_message_delivery_suppressed(resp):
            if is_encrypted and crypto:
                return _encrypted_success(crypto)
            return PlainTextResponse("ok")
        if is_encrypted and crypto:
            return _encrypt_response(crypto, resp)
        return resp

    try:
        user = await get_active_user_by_dingtalk_id(session, incoming.dingtalk_user_id)
        if user is None:
            response_payload = dingtalk_text_response("Unknown user, please contact admin.")
            await mark_webhook_event_failed(
                session, event,
                error_message=f"Unknown DingTalk user: {incoming.dingtalk_user_id}",
                response_payload=response_payload, now=now_in_timezone(settings.timezone),
            )
            await session.commit()
            if is_encrypted and crypto:
                return _encrypt_response(crypto, response_payload)
            return response_payload

        performance_service = getattr(request.app.state, "performance_service", None)
        if performance_service is not None:
            performance_result = await performance_service.submit_text(
                session,
                user=user,
                raw_input=incoming.text,
                source=incoming.source,
                require_performance_signal=True,
            )
            if performance_result is not None:
                response_payload = dingtalk_text_response(performance_result.message)
                await mark_webhook_event_processed(
                    session,
                    event,
                    report_id=None,
                    response_payload=response_payload,
                    now=now_in_timezone(settings.timezone),
                )
                await session.commit()
                if is_encrypted and crypto:
                    robot = request.app.state.dingtalk_robot
                    try:
                        if incoming.session_webhook:
                            await robot.send_session_webhook_text(session_webhook=incoming.session_webhook, text=performance_result.message)
                        else:
                            await robot.send_robot_direct_text(user_ids=[incoming.dingtalk_user_id], text=performance_result.message)
                    except Exception as send_exc:
                        _log_runtime_failure(
                            "dingtalk_async_performance_reply_failed", send_exc
                        )
                    return _encrypted_success(crypto)
                return response_payload

            if looks_like_performance_reply_template(incoming.text):
                response_payload = dingtalk_text_response(NO_ACTIVE_PERFORMANCE_TASK_MESSAGE)
                await mark_webhook_event_processed(
                    session,
                    event,
                    report_id=None,
                    response_payload=response_payload,
                    now=now_in_timezone(settings.timezone),
                )
                await session.commit()
                if is_encrypted and crypto:
                    robot = request.app.state.dingtalk_robot
                    try:
                        if incoming.session_webhook:
                            await robot.send_session_webhook_text(session_webhook=incoming.session_webhook, text=NO_ACTIVE_PERFORMANCE_TASK_MESSAGE)
                        else:
                            await robot.send_robot_direct_text(user_ids=[incoming.dingtalk_user_id], text=NO_ACTIVE_PERFORMANCE_TASK_MESSAGE)
                    except Exception as send_exc:
                        _log_runtime_failure(
                            "dingtalk_async_performance_no_task_reply_failed",
                            send_exc,
                        )
                    return _encrypted_success(crypto)
                return response_payload

        ingress_llm_client = (
            getattr(request.app.state, "llm_client", None)
            or getattr(
                getattr(
                    getattr(request.app.state, "report_service", None),
                    "extractor",
                    None,
                ),
                "client",
                None,
            )
        )
        tool_call_canary = await process_tool_call_canary_ingress(
            session,
            user=user,
            dingtalk_user_id=incoming.dingtalk_user_id,
            user_text=incoming.text,
            source_channel=incoming.source,
            conversation_id=str(
                getattr(incoming, "conversation_id", "") or ""
            ),
            source_message_id=idempotency_key,
            settings=settings,
            llm_client=ingress_llm_client,
            now=now_in_timezone(user.timezone or settings.timezone),
            conversation_kind=incoming.conversation_kind,
            message_occurred_at=event.received_at,
        )
        if tool_call_canary.handled:
            agent2_result = Agent2DailyExecutionResult(
                report_id=tool_call_canary.report_id,
                report_date=now_in_timezone(
                    user.timezone or settings.timezone
                ).date(),
                status="collecting",
                message=tool_call_canary.message,
                report_saved=tool_call_canary.actual_write,
                read_only=not tool_call_canary.actual_write,
                command_results=[],
            )
        else:
            raise RuntimeError(
                "Agent2 Tool-Call Core did not handle a production webhook turn"
            )
        if agent2_result is not None:
            response_payload = (
                build_canary_response_payload(tool_call_canary)
                if tool_call_canary.handled
                else dingtalk_text_response(agent2_result.message)
            )
            persisted_response_payload = (
                build_canary_persisted_response_payload(
                    tool_call_canary
                )
                if tool_call_canary.handled
                else response_payload
            )
            await mark_webhook_event_processed(
                session,
                event,
                report_id=uuid.UUID(agent2_result.report_id) if agent2_result.report_id else None,
                response_payload=persisted_response_payload,
                now=now_in_timezone(settings.timezone),
            )
            await session.commit()
            if is_encrypted and crypto:
                robot = request.app.state.dingtalk_robot

                async def send_agent2_reply() -> None:
                    if incoming.session_webhook:
                        await robot.send_session_webhook_text(
                            session_webhook=incoming.session_webhook,
                            text=agent2_result.message,
                        )
                    else:
                        await robot.send_robot_direct_text(
                            user_ids=[incoming.dingtalk_user_id],
                            text=agent2_result.message,
                        )

                try:
                    if tool_call_canary.handled:
                        await deliver_canary_message_if_enabled(
                            tool_call_canary,
                            send_agent2_reply,
                        )
                    else:
                        await send_agent2_reply()
                except Exception as send_exc:
                    _log_runtime_failure("dingtalk_async_agent2_reply_failed", send_exc)
                return _encrypted_success(crypto)
            if (
                tool_call_canary.handled
                and is_canary_message_delivery_suppressed(response_payload)
            ):
                return PlainTextResponse("ok")
            return response_payload

        raise RuntimeError(
            "Agent2 Tool-Call Core response was not returned by webhook ingress"
        )
    except CanaryIngressExecutionError as exc:
        await session.rollback()
        failure_outcome = exc.outcome()
        response_payload = build_canary_response_payload(
            failure_outcome
        )
        persisted_response_payload = (
            build_canary_persisted_response_payload(failure_outcome)
        )
        async with session.begin():
            event = await session.merge(event)
            await mark_webhook_event_failed(
                session,
                event,
                error_message=exc.error_type,
                response_payload=persisted_response_payload,
                now=now_in_timezone(settings.timezone),
            )
        if is_canary_message_delivery_suppressed(response_payload):
            if is_encrypted and crypto:
                return _encrypted_success(crypto)
            return PlainTextResponse("ok")
        if is_encrypted and crypto:
            return _encrypt_response(crypto, response_payload)
        return response_payload
    except LLMOutputError as exc:
        await session.rollback()
        response_payload = dingtalk_text_response("LLM parse failed, report not saved.")
        async with session.begin():
            event = await session.merge(event)
            await mark_webhook_event_failed(
                session, event, error_message=str(exc),
                response_payload=response_payload, now=now_in_timezone(settings.timezone),
            )
        if is_encrypted and crypto:
            return _encrypt_response(crypto, response_payload)
        return response_payload
    except Exception as exc:
        await session.rollback()
        response_payload = dingtalk_text_response("Processing failed, report not saved.")
        async with session.begin():
            event = await session.merge(event)
            await mark_webhook_event_failed(
                session, event, error_message=str(exc),
                response_payload=response_payload, now=now_in_timezone(settings.timezone),
            )
        if is_encrypted and crypto:
            return _encrypt_response(crypto, response_payload)
        return response_payload


async def _submit_webhook_agent2_if_enabled(
    *,
    session: AsyncSession,
    user: Any,
    incoming: Any,
    settings: Settings,
    message_id: str,
    llm_client: Any | None = None,
):
    entrypoint = await resolve_agent2_entrypoint(
        session,
        settings=settings,
        dingtalk_user_id=str(getattr(incoming, "dingtalk_user_id", "") or ""),
        source_message_id=message_id,
    )
    await persist_runtime_owner_claim(session, entrypoint)
    runtime_owner = decide_runtime_owner(entrypoint.decision)
    phase2_primary = runtime_owner == "agent2_primary"
    phase2_business_context = (
        build_business_command_context(
            entrypoint.binding,
            source_message_id=message_id,
            source_channel=str(getattr(incoming, "source", "") or "dingtalk_webhook"),
            occurred_at=now_in_timezone(settings.timezone),
            conversation_id=str(getattr(incoming, "conversation_id", "") or ""),
        )
        if phase2_primary and entrypoint.binding is not None
        else None
    )
    if runtime_owner == "blocked":
        current_date = now_in_timezone(settings.timezone).date()
        return Agent2DailyExecutionResult(
            report_id=None,
            report_date=current_date,
            status="collecting",
            message="当前账号或所属组织信息无法唯一确认，本次没有执行任何业务操作。",
            report_saved=False,
            read_only=True,
            command_results=[],
        )
    daily_context = await load_live_daily_context(session, user, settings)
    daily_report = daily_context.report
    report_date = daily_context.report_date
    daily_task = daily_context.active_task
    active_tasks = (daily_task,) if daily_task is not None else ()
    envelope = IncomingMessageEnvelope(
        sender_id=str(getattr(user, "id", "") or ""),
        sender_name=str(getattr(user, "name", "") or ""),
        dingtalk_user_id=str(getattr(incoming, "dingtalk_user_id", "") or ""),
        source=str(getattr(incoming, "source", "") or "dingtalk_webhook"),
        raw_text=str(getattr(incoming, "text", "") or ""),
        message_id=message_id,
        conversation_id=str(getattr(incoming, "conversation_id", "") or ""),
        active_tasks=active_tasks,
    )
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
            return Agent2DailyExecutionResult(
                report_id=str(getattr(daily_report, "id", "") or "") or None,
                report_date=(
                    report_date
                ),
                status=str(getattr(daily_report, "status", "") or "collecting"),
                message=projection_turn.reply,
                report_saved=any(item.actual_write for item in projection_turn.outcomes),
                read_only=not any(item.actual_write for item in projection_turn.outcomes),
                today_work=list(getattr(daily_report, "today_work", []) or []),
                problems=list(getattr(daily_report, "problems", []) or []),
                tomorrow_plan=list(getattr(daily_report, "tomorrow_plan", []) or []),
                command_results=[item.as_dict() for item in projection_turn.outcomes],
            )
        correction_turn = await execute_report_projection_correction_turn(
            session_factory=AsyncSessionLocal,
            envelope=envelope,
            business_context=phase2_business_context,
            settings=settings,
        )
        if correction_turn is not None and correction_turn.handled:
            return Agent2DailyExecutionResult(
                report_id=str(getattr(daily_report, "id", "") or "") or None,
                report_date=(
                    report_date
                ),
                status=str(getattr(daily_report, "status", "") or "collecting"),
                message=correction_turn.reply,
                report_saved=any(item.actual_write for item in correction_turn.outcomes),
                read_only=not any(item.actual_write for item in correction_turn.outcomes),
                today_work=list(getattr(daily_report, "today_work", []) or []),
                problems=list(getattr(daily_report, "problems", []) or []),
                tomorrow_plan=list(getattr(daily_report, "tomorrow_plan", []) or []),
                command_results=[item.as_dict() for item in correction_turn.outcomes],
            )
        selection_turn = await execute_selection_pending_turn(
            session=session,
            user=user,
            envelope=envelope,
            business_context=phase2_business_context,
            settings=settings,
        )
        if selection_turn is not None and selection_turn.handled:
            return Agent2DailyExecutionResult(
                report_id=str(getattr(daily_report, "id", "") or "") or None,
                report_date=(
                    report_date
                ),
                status=str(getattr(daily_report, "status", "") or "collecting"),
                message=selection_turn.reply,
                report_saved=False,
                read_only=not selection_turn.actual_write,
                today_work=list(getattr(daily_report, "today_work", []) or []),
                problems=list(getattr(daily_report, "problems", []) or []),
                tomorrow_plan=list(getattr(daily_report, "tomorrow_plan", []) or []),
                command_results=(
                    [selection_turn.outcome.as_dict()]
                    if selection_turn.outcome is not None
                    else []
                ),
            )
    shadow = None if phase2_primary else evaluate_daily_shadow(envelope, mode="protective_gate")
    if phase2_primary and not cognitive_core_v3_enabled(settings):
        return Agent2DailyExecutionResult(
            report_id=str(getattr(daily_report, "id", "") or "") or None,
            report_date=report_date,
            status=str(getattr(daily_report, "status", "") or "collecting"),
            message="当前服务暂时无法处理这条消息，本次没有执行任何业务操作。",
            report_saved=False,
            read_only=True,
            today_work=list(getattr(daily_report, "today_work", []) or []),
            problems=list(getattr(daily_report, "problems", []) or []),
            tomorrow_plan=list(getattr(daily_report, "tomorrow_plan", []) or []),
            command_results=[],
        )
    if cognitive_core_v3_enabled(settings):
        try:
            if llm_client is None:
                raise RuntimeError("cognitive core v3 requires an LLM client")
            turn_runtime_result = await production_agent2_turn_runtime().handle(
                VerifiedTurnRequest(
                    session=session,
                    user=user,
                    envelope=envelope,
                    llm_client=llm_client,
                    daily_report=daily_report,
                    report_date=report_date,
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
                "webhook_cognitive_v3_blocked reply_kind=%s",
                safe_reply.reply_kind,
            )
            return Agent2DailyExecutionResult(
                report_id=str(getattr(daily_report, "id", "") or "") or None,
                report_date=report_date,
                status=str(getattr(daily_report, "status", "") or "collecting"),
                message=safe_reply.message,
                report_saved=False,
                read_only=True,
                today_work=list(getattr(daily_report, "today_work", []) or []),
                problems=list(getattr(daily_report, "problems", []) or []),
                tomorrow_plan=list(getattr(daily_report, "tomorrow_plan", []) or []),
                command_results=[],
            )
        except Exception as exc:
            _log_runtime_failure("webhook_cognitive_v3_fail_closed", exc)
            return Agent2DailyExecutionResult(
                report_id=str(getattr(daily_report, "id", "") or "") or None,
                report_date=report_date,
                status=str(getattr(daily_report, "status", "") or "collecting"),
                message="这条消息暂时无法完成处理，本次没有写入任何业务内容，请稍后重试。",
                report_saved=False,
                read_only=True,
                today_work=list(getattr(daily_report, "today_work", []) or []),
                problems=list(getattr(daily_report, "problems", []) or []),
                tomorrow_plan=list(getattr(daily_report, "tomorrow_plan", []) or []),
                command_results=[],
            )
        else:
            phase2_business_result = None
            periodic_report_results = []
            verified_execution_context = (
                turn_runtime_result.business_execution_context
            )
            context_pack = await attach_live_performance_catalog(
                context_pack=build_agent2_context_pack(
                    envelope,
                    daily_report=daily_report,
                ),
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
                if phase2_business_context is None:
                    raise RuntimeError("periodic Report commands require a verified Agent2 identity")
                periodic_report_results = await execute_periodic_report_commands(
                    session,
                    commands=cognitive_v3.command_plan.report_commands,
                    context=verified_execution_context,
                    timezone_name=settings.timezone,
                    execution_authority=(
                        turn_runtime_result.mutation_execution_authority
                    ),
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
                    # Admission issuance and its domain effect must remain in one
                    # transaction.  A second session cannot see an uncommitted
                    # authoritative Ticket and would either fail spuriously or
                    # tempt the caller to weaken the authority check.
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
            if cognitive_v3.command_plan.daily_commands:
                daily_result = await execute_typed_agent2_daily_commands(
                    session,
                    user=user,
                    commands=cognitive_v3.command_plan.daily_commands,
                    execution_context=turn_runtime_result.daily_execution_context(),
                    settings=settings,
                    execution_authority=(
                        turn_runtime_result.mutation_execution_authority
                    ),
                )
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=daily_result.command_results,
                    business_result=phase2_business_result,
                    report_results=[item.as_dict() for item in periodic_report_results],
                    business_context=verified_execution_context,
                    selection_continuation=(
                        turn_runtime_result.selection_continuation
                    ),
                )
                outcomes = daily_execution_outcomes(
                    daily_result, source_turn_id=message_id
                )
                if phase2_business_result is not None:
                    outcomes += business_composition_outcomes(phase2_business_result)
                    if verified_execution_context is not None:
                        outcomes += await project_committed_case_followup_facts(
                            business_result=phase2_business_result,
                            business_context=verified_execution_context,
                            source_session=session,
                            session_factory=AsyncSessionLocal,
                            settings=settings,
                            report_date=report_date,
                        )
                outcomes += periodic_execution_outcomes(
                    periodic_report_results, source_turn_id=message_id
                )
                if phase2_primary:
                    side_reply = await build_cognitive_side_reply_v3(
                        decision=cognitive_v3.decision,
                        llm_client=llm_client,
                        context_pack=context_pack,
                    )
                    if side_reply:
                        outcomes += (
                            text_outcome(side_reply, source_turn_id=message_id),
                        )
                if verified_execution_context is not None:
                    await persist_operation_outcomes(
                        session, outcomes,
                        tenant_id=verified_execution_context.tenant_id,
                        user_id=verified_execution_context.actor_user_id,
                        conversation_id=verified_execution_context.conversation_id,
                        source_turn_id=message_id,
                        now=verified_execution_context.occurred_at,
                    )
                daily_result = replace(
                    daily_result,
                    message=append_cognitive_clarification(
                        OutcomeReplyComposer().compose(outcomes),
                        cognitive_v3.decision,
                    ),
                )
                return daily_result
            if phase2_business_result is not None:
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=[],
                    business_result=phase2_business_result,
                    report_results=[item.as_dict() for item in periodic_report_results],
                    business_context=verified_execution_context,
                    selection_continuation=(
                        turn_runtime_result.selection_continuation
                    ),
                )
                outcomes = business_composition_outcomes(phase2_business_result)
                if verified_execution_context is not None:
                    outcomes += await project_committed_case_followup_facts(
                        business_result=phase2_business_result,
                        business_context=verified_execution_context,
                        source_session=session,
                        session_factory=AsyncSessionLocal,
                        settings=settings,
                        report_date=report_date,
                    )
                outcomes += periodic_execution_outcomes(
                    periodic_report_results, source_turn_id=message_id
                )
                side_reply = await build_cognitive_side_reply_v3(
                    decision=cognitive_v3.decision,
                    llm_client=llm_client,
                    context_pack=context_pack,
                )
                if side_reply:
                    outcomes += (text_outcome(side_reply, source_turn_id=message_id),)
                if verified_execution_context is not None:
                    await persist_operation_outcomes(
                        session, outcomes,
                        tenant_id=verified_execution_context.tenant_id,
                        user_id=verified_execution_context.actor_user_id,
                        conversation_id=verified_execution_context.conversation_id,
                        source_turn_id=message_id,
                        now=verified_execution_context.occurred_at,
                    )
                business_message = append_cognitive_clarification(
                    OutcomeReplyComposer().compose(outcomes),
                    cognitive_v3.decision,
                )
                return Agent2DailyExecutionResult(
                    report_id=str(getattr(daily_report, "id", "") or "") or None,
                    report_date=report_date,
                    status=str(getattr(daily_report, "status", "") or "collecting"),
                    message=business_message,
                    report_saved=False,
                    read_only=not any(
                        action.receipt is not None and action.receipt.actual_write
                        for action in phase2_business_result.actions
                    ),
                    today_work=list(getattr(daily_report, "today_work", []) or []),
                    problems=list(getattr(daily_report, "problems", []) or []),
                    tomorrow_plan=list(getattr(daily_report, "tomorrow_plan", []) or []),
                    command_results=[
                        {
                            "semantic_command_id": action.semantic_command_id,
                            "semantic_command_type": action.semantic_command_type,
                            "compiled_command_type": action.compiled_command_type,
                            "status": action.status,
                            "receipt_id": action.receipt.receipt_id if action.receipt else "",
                            "block_reason": action.block.reason_code if action.block else "",
                        }
                        for action in phase2_business_result.actions
                    ] + [item.as_dict() for item in periodic_report_results],
                )
            if periodic_report_results:
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=[],
                    business_result=None,
                    report_results=[item.as_dict() for item in periodic_report_results],
                    business_context=verified_execution_context,
                    selection_continuation=(
                        turn_runtime_result.selection_continuation
                    ),
                )
                latest = periodic_report_results[-1]
                outcomes = periodic_execution_outcomes(
                    periodic_report_results,
                    source_turn_id=message_id,
                )
                if verified_execution_context is not None:
                    await persist_operation_outcomes(
                        session,
                        outcomes,
                        tenant_id=verified_execution_context.tenant_id,
                        user_id=verified_execution_context.actor_user_id,
                        conversation_id=verified_execution_context.conversation_id,
                        source_turn_id=message_id,
                        now=verified_execution_context.occurred_at,
                    )
                return Agent2DailyExecutionResult(
                    report_id=str(latest.execution.after.report_id),
                    report_date=report_date,
                    status=latest.execution.after.status,
                    message=append_cognitive_clarification(
                        OutcomeReplyComposer().compose(outcomes),
                        cognitive_v3.decision,
                    ),
                    report_saved=any(item.actual_write for item in periodic_report_results),
                    read_only=not any(item.actual_write for item in periodic_report_results),
                    command_results=[item.as_dict() for item in periodic_report_results],
                )
            lifecycle_message = pending_lifecycle_reply(cognitive_v3.decision)
            selection_message = selection_request_reply(cognitive_v3.decision)
            information_message = information_pending_reply(cognitive_v3.decision)
            admission_message = admission_block_reply(cognitive_v3.decision)
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
                message = lifecycle_message
            elif selection_message:
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=[],
                    business_result=None,
                    report_results=[],
                    business_context=verified_execution_context,
                )
                message = selection_message
            elif information_message:
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=[],
                    business_result=None,
                    report_results=[],
                    business_context=verified_execution_context,
                )
                message = information_message
            elif has_bound_confirmation_pending(cognitive_v3.decision):
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=[],
                    business_result=None,
                    report_results=[],
                    business_context=verified_execution_context,
                )
                message = cognitive_v3.decision.clarification_need.question
            elif admission_message:
                await finalize_cognitive_core_v3_execution(
                    session=session,
                    result=cognitive_v3,
                    command_results=[],
                    business_result=None,
                    report_results=[],
                    business_context=verified_execution_context,
                )
                message = admission_message
            elif cognitive_v3.decision.clarification_need is not None:
                message = cognitive_v3.decision.clarification_need.question
            elif phase2_primary and (
                message := await build_cognitive_side_reply_v3(
                    decision=cognitive_v3.decision,
                    llm_client=llm_client,
                    context_pack=context_pack,
                )
            ):
                pass
            elif phase2_primary:
                message = "这条消息暂时无法形成明确可执行的操作，本次没有写入任何内容。"
            else:
                message = (
                    getattr(getattr(shadow, "assistant_reply", None), "text", "")
                    or shadow.gate_decision.reply_text
                    or "这条消息已按业务对话处理，没有写入日报。"
                )
            if phase2_primary:
                if verified_execution_context is None:
                    raise RuntimeError(
                        "Agent2 read-only outcome requires verified execution context"
                    )
                message = format_dingtalk_plain_text(message)
                outcomes = (text_outcome(message, source_turn_id=message_id),)
                await persist_operation_outcomes(
                    session,
                    outcomes,
                    tenant_id=verified_execution_context.tenant_id,
                    user_id=verified_execution_context.actor_user_id,
                    conversation_id=verified_execution_context.conversation_id,
                    source_turn_id=message_id,
                    now=verified_execution_context.occurred_at,
                )
                message = OutcomeReplyComposer().compose(outcomes)
            return Agent2DailyExecutionResult(
                report_id=str(getattr(daily_report, "id", "") or "") or None,
                report_date=report_date,
                status=str(getattr(daily_report, "status", "") or "collecting"),
                message=message,
                report_saved=False,
                read_only=True,
                today_work=list(getattr(daily_report, "today_work", []) or []),
                problems=list(getattr(daily_report, "problems", []) or []),
                tomorrow_plan=list(getattr(daily_report, "tomorrow_plan", []) or []),
                command_results=[],
            )
    if shadow is None or shadow.gate_decision.block_legacy_daily or not shadow.commands:
        return None
    return await execute_agent2_daily_commands(
        session,
        user=user,
        raw_input=envelope.raw_text,
        source="agent2_dingtalk_webhook_text",
        commands=list(shadow.commands),
        settings=settings,
        message_id=message_id,
        expected_report_version=agent2_daily_report_version(daily_report),
    )
