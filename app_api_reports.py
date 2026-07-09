from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.repositories import (
    create_webhook_event_once,
    get_active_user_by_dingtalk_id,
    list_reports_for_date,
    mark_webhook_event_failed,
    mark_webhook_event_processed,
)
from app.services.report_service import DailyReportService
from app.utils.time import now_in_timezone

router = APIRouter(prefix="/reports", tags=["reports"])


class ManualReportRequest(BaseModel):
    dingtalk_user_id: str = Field(min_length=1)
    raw_input: str = Field(min_length=1)
    source: str = "manual_text"
    report_date: date | None = None
    idempotency_key: str | None = None


@router.post("/manual")
async def submit_manual_report(
    request: Request,
    body: ManualReportRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await get_active_user_by_dingtalk_id(session, body.dingtalk_user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if body.idempotency_key:
        event, inserted = await create_webhook_event_once(
            session,
            idempotency_key=f"manual:{body.idempotency_key}",
            external_message_id=body.idempotency_key,
            dingtalk_user_id=body.dingtalk_user_id,
            payload=body.model_dump(),
        )
        await session.commit()
        if not inserted and event.status == "processed":
            return event.response_payload
        if not inserted:
            return {"status": event.status, "message": "This idempotency key is already being processed."}
    else:
        event = None

    service: DailyReportService = request.app.state.report_service
    try:
        result = await service.submit_text(
            session,
            user=user,
            raw_input=body.raw_input,
            source=body.source,
            report_date=body.report_date,
        )
    except Exception as exc:
        await session.rollback()
        if event is not None:
            async with session.begin():
                event = await session.merge(event)
                await mark_webhook_event_failed(
                    session,
                    event,
                    error_message=str(exc),
                    response_payload={"status": "failed", "message": "Report parsing failed; data was not saved."},
                    now=now_in_timezone(user.timezone),
                )
        raise
    response = {
        "report_id": result.report_id,
        "report_date": result.report_date.isoformat(),
        "status": result.status,
        "completeness_score": result.completeness_score,
        "missing_sections": result.missing_sections,
        "message": result.message,
        "confirmation_type": result.confirmation_type,
        "confirmed_by_user": result.confirmed_by_user,
        "quality_warning": result.quality_warning,
        "reply_kind": result.reply_kind,
        "structured": result.structured.model_dump(),
        "merged_report": {
            "today_work": result.today_work,
            "problems": result.problems,
            "tomorrow_plan": result.tomorrow_plan,
            "section_status": result.section_status,
        },
    }
    if event is not None:
        await mark_webhook_event_processed(
            session,
            event,
            report_id=uuid.UUID(result.report_id) if result.report_id else None,
            response_payload=response,
            now=now_in_timezone(user.timezone),
        )
    await session.commit()
    return response


@router.get("")
async def get_reports(report_date: date, session: AsyncSession = Depends(get_session)) -> list[dict]:
    reports = await list_reports_for_date(session, report_date)
    return [
        {
            "id": str(report.id),
            "user_id": str(report.user_id),
            "team_id": str(report.team_id),
            "date": report.report_date.isoformat(),
            "today_work": report.today_work,
            "problems": report.problems,
            "tomorrow_plan": report.tomorrow_plan,
            "raw_input": report.raw_input,
            "completeness_score": float(report.completeness_score),
            "status": report.status,
            "confirmation_type": report.confirmation_type,
            "confirmed_by_user": report.confirmed_by_user,
            "quality_warning": report.quality_warning,
        }
        for report in reports
    ]
