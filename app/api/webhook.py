from __future__ import annotations

from typing import Any
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.llm.extractor import LLMOutputError
from app.repositories import (
    create_webhook_event_once,
    get_active_user_by_dingtalk_id,
    mark_webhook_event_failed,
    mark_webhook_event_processed,
)
from app.services.dingtalk import (
    DingTalkPayloadError,
    build_idempotency_key,
    dingtalk_text_response,
    extract_voice_download_code,
    parse_incoming_message,
    verify_incoming_token,
)
from app.services.dingtalk_crypto import DingTalkCallbackCrypto
from app.services.performance_service import (
    NO_ACTIVE_PERFORMANCE_TASK_MESSAGE,
    PERFORMANCE_PENDING_CONFIRMATION,
    is_performance_reply_candidate,
    looks_like_performance_reply_template,
    submission_metrics,
)
from app.services.report_service import DailyReportService
from app.utils.time import now_in_timezone
from app.agent2.daily_shadow import evaluate_daily_shadow
from app.agent2.workflow_audit import create_agent2_workflow_audit_event
from app.workflows.intake import (
    ActiveWorkflowTask,
    IncomingMessageEnvelope,
    WORKFLOW_MONTHLY_REPORT,
)
from app.workflows.gate import GateDecision
from app.workflows.daily_context import build_live_daily_active_task

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


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
    print(f"DingTalk event subscription params: {params}", flush=True)

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
    print(f"DingTalk event subscription payload: {raw[:1000]}", flush=True)
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
    print(f"DingTalk GET params: {dict(request.query_params)}", flush=True)
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
    print(f"DingTalk POST params: {params}", flush=True)

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

        print(f"DingTalk decrypted payload: {raw[:1000]}", flush=True)

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

    # If audio/voice message without auto-recognition text, try ASR
    if not incoming.text:
        msg_type = payload.get("msgtype") or payload.get("msgType") or ""
        if msg_type in ("audio", "voice"):
            download_code = extract_voice_download_code(payload)
            if download_code:
                robot = request.app.state.dingtalk_robot
                try:
                    recognized = await robot.recognize_audio(str(download_code))
                    from dataclasses import replace
                    incoming = replace(incoming, text=str(recognized).strip())
                except Exception:
                    print(f"DingTalk ASR failed for downloadCode={str(download_code)[:16]}", flush=True)

    idempotency_key = build_idempotency_key(payload, incoming)
    event, inserted = await create_webhook_event_once(
        session,
        idempotency_key=idempotency_key,
        external_message_id=incoming.message_id,
        dingtalk_user_id=incoming.dingtalk_user_id,
        payload=payload,
    )
    await session.commit()

    if not inserted:
        resp = event.response_payload or dingtalk_text_response("这条复盘已收到，请等待处理结果。")
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
        await _observe_workflow_route(
            session=session,
            user=user,
            incoming=incoming,
            performance_service=performance_service,
            settings=settings,
        )
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
                        print(f"DingTalk async performance reply failed: {send_exc}", flush=True)
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
                        print(f"DingTalk async performance no-task reply failed: {send_exc}", flush=True)
                    return _encrypted_success(crypto)
                return response_payload

        gate_decision = await _evaluate_legacy_daily_gate(
            session=session,
            user=user,
            incoming=incoming,
            performance_service=performance_service,
            settings=settings,
        )
        if gate_decision.block_legacy_daily:
            response_payload = dingtalk_text_response(gate_decision.reply_text or "这句我先不写入日报，请补充说明。")
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
                        await robot.send_session_webhook_text(session_webhook=incoming.session_webhook, text=response_payload["text"]["content"])
                    else:
                        await robot.send_robot_direct_text(user_ids=[incoming.dingtalk_user_id], text=response_payload["text"]["content"])
                except Exception as send_exc:
                    print(f"DingTalk async gate reply failed: {send_exc}", flush=True)
                return _encrypted_success(crypto)
            return response_payload

        report_service: DailyReportService = request.app.state.report_service
        result = await report_service.submit_text(
            session, user=user, raw_input=incoming.text, source=incoming.source,
        )
        response_payload = dingtalk_text_response(result.message)
        await mark_webhook_event_processed(
            session, event, report_id=uuid.UUID(result.report_id) if result.report_id else None,
            response_payload=response_payload, now=now_in_timezone(settings.timezone),
        )
        await session.commit()
        if is_encrypted and crypto:
            robot = request.app.state.dingtalk_robot
            try:
                if incoming.session_webhook:
                    await robot.send_session_webhook_text(session_webhook=incoming.session_webhook, text=result.message)
                else:
                    await robot.send_robot_direct_text(user_ids=[incoming.dingtalk_user_id], text=result.message)
            except Exception as send_exc:
                print(f"DingTalk async reply failed: {send_exc}", flush=True)
            return _encrypted_success(crypto)
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


async def _observe_workflow_route(
    *,
    session: AsyncSession,
    user: Any,
    incoming: Any,
    performance_service: Any,
    settings: Settings,
) -> None:
    await _evaluate_legacy_daily_gate(
        session=session,
        user=user,
        incoming=incoming,
        performance_service=performance_service,
        settings=settings,
        observe_only_log=True,
    )


async def _evaluate_legacy_daily_gate(
    *,
    session: AsyncSession,
    user: Any,
    incoming: Any,
    performance_service: Any,
    settings: Settings,
    observe_only_log: bool = False,
) -> GateDecision:
    active_tasks: list[ActiveWorkflowTask] = []
    if performance_service is not None:
        try:
            active_submission = await performance_service.get_active_submission(session, user.id)
            if active_submission is not None:
                metrics = submission_metrics(active_submission)
                responses = list(active_submission.responses_json or [])
                reply_candidate = is_performance_reply_candidate(
                    metrics=metrics,
                    responses=responses,
                    raw_input=incoming.text,
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
            print(f"Workflow route observation skipped performance task lookup: {exc}", flush=True)

    try:
        daily_task = await build_live_daily_active_task(session, user, settings)
        if daily_task is not None:
            active_tasks.append(daily_task)
    except Exception as exc:
        print(f"Workflow route observation skipped daily task lookup: {exc}", flush=True)

    envelope = IncomingMessageEnvelope(
        sender_id=str(getattr(user, "id", "") or ""),
        sender_name=str(getattr(user, "name", "") or ""),
        dingtalk_user_id=str(getattr(incoming, "dingtalk_user_id", "") or ""),
        source=str(getattr(incoming, "source", "") or ""),
        raw_text=str(getattr(incoming, "text", "") or ""),
        message_id=str(getattr(incoming, "message_id", "") or ""),
        conversation_id=str(getattr(incoming, "conversation_id", "") or ""),
        active_tasks=tuple(active_tasks),
    )
    mode = "observe_only" if observe_only_log else getattr(settings, "workflow_intake_mode", "observe_only")
    shadow = evaluate_daily_shadow(envelope, mode=mode)
    gate_decision = shadow.gate_decision
    print(
        "Workflow route observation: "
        + json.dumps(shadow.route_observation(envelope), ensure_ascii=False, sort_keys=True),
        flush=True,
    )
    print(
        "Workflow gate observation: "
        + json.dumps(shadow.gate_observation(envelope), ensure_ascii=False, sort_keys=True),
        flush=True,
    )
    await create_agent2_workflow_audit_event(
        session=session,
        user=user,
        incoming=incoming,
        settings=settings,
        envelope=envelope,
        shadow=shadow,
        mode=mode,
        observe_only_log=observe_only_log,
    )
    return gate_decision
