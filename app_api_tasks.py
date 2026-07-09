from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.scheduler.jobs import remind_missing_reports
from app.services.summary_service import SummaryService

router = APIRouter(prefix="/tasks", tags=["tasks"])


class DingTalkDirectTextRequest(BaseModel):
    user_ids: list[str] = Field(min_length=1)
    text: str = Field(min_length=1)


@router.post("/summaries/{summary_date}")
async def generate_summaries(
    summary_date: date,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    service: SummaryService = request.app.state.summary_service
    result = await service.generate_for_date(session, summary_date)
    await session.commit()
    return result


@router.post("/reminders/{report_date}")
async def send_reminders(
    report_date: date,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    robot = request.app.state.dingtalk_robot
    result = await remind_missing_reports(session, settings, robot, report_date)
    await session.commit()
    return result


@router.post("/dingtalk/credential-check")
async def check_dingtalk_credentials(request: Request) -> dict:
    robot = request.app.state.dingtalk_robot
    token = await robot.get_enterprise_access_token()
    return {
        "ok": True,
        "has_access_token": bool(token),
        "expires_in_seconds_approx": robot.enterprise_access_token_seconds_remaining(),
    }


@router.post("/dingtalk/direct-text-test")
async def send_dingtalk_direct_text_test(body: DingTalkDirectTextRequest, request: Request) -> dict:
    robot = request.app.state.dingtalk_robot
    result = await robot.send_robot_direct_text(user_ids=body.user_ids, text=body.text)
    return {
        "ok": True,
        "target_count": len(body.user_ids),
        "process_query_key_present": bool(result.get("processQueryKey")),
        "invalid_user_count": len(result.get("invalidStaffIdList") or []),
        "filtered_user_count": len(result.get("filteredStaffIdList") or []),
        "flow_controlled_user_count": len(result.get("flowControlledStaffIdList") or []),
    }
