from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import PerformanceSubmission, PerformanceTask, User
from app.repositories import get_active_user_by_dingtalk_id
from app.services.department_performance_service import (
    REQUIRED_DEPARTMENT_MONTHLY_UNITS,
    build_department_monthly_report,
    build_test_department_monthly_sources,
    department_monthly_coverage,
    source_from_performance_submission,
    split_markdown_message,
)
from app.services.performance_service import PerformanceTaskService, build_performance_message_pair
from app.utils.time import now_in_timezone

router = APIRouter(prefix="/performance", tags=["performance"])


class PerformanceMetricInput(BaseModel):
    metric_no: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1)
    unit: str = ""
    display_lines: list[str] = Field(default_factory=list)


class CreateBlankPerformanceTaskRequest(BaseModel):
    title: str = Field(default="", max_length=256)
    period_label: str = Field(min_length=1, max_length=64)
    metrics: list[PerformanceMetricInput] = Field(min_length=1)
    recipient_dingtalk_user_ids: list[str] = Field(min_length=1)
    created_by: str = ""
    send_messages: bool = False


class ManualPerformanceReplyRequest(BaseModel):
    dingtalk_user_id: str = Field(min_length=1)
    raw_input: str = Field(min_length=1)
    source: str = "performance_manual"


class DepartmentMonthlyReportRequest(BaseModel):
    period_label: str = Field(default="2026-06", min_length=1, max_length=64)
    use_test_data: bool = False
    test_seed: int = 20260630
    expected_units: list[str] = Field(default_factory=lambda: list(REQUIRED_DEPARTMENT_MONTHLY_UNITS))
    generated_for: str = "赵卫中"
    send_messages: bool = False
    recipient_dingtalk_user_ids: list[str] = Field(default_factory=list)


@router.post("/tasks/blank")
async def create_blank_performance_task(
    request: Request,
    body: CreateBlankPerformanceTaskRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    users = await _load_recipient_users(session, body.recipient_dingtalk_user_ids)
    missing_ids = sorted(set(body.recipient_dingtalk_user_ids) - {user.dingtalk_user_id for user in users})
    if missing_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"missing_dingtalk_user_ids": missing_ids})

    service: PerformanceTaskService = request.app.state.performance_service
    task = await service.create_blank_task(
        session,
        title=body.title or f"{body.period_label}团队绩效填报",
        period_label=body.period_label,
        metrics=[metric.model_dump() for metric in body.metrics],
        recipients=users,
        created_by=body.created_by,
    )
    submissions = (
        await session.execute(
            select(PerformanceSubmission).where(PerformanceSubmission.task_id == task.id).order_by(PerformanceSubmission.created_at)
        )
    ).scalars().all()
    send_results: list[dict[str, Any]] = []
    if body.send_messages:
        robot = request.app.state.dingtalk_robot
        for submission in submissions:
            user = next((item for item in users if item.id == submission.user_id), None)
            if user is None:
                continue
            overview_markdown, reply_prompt = build_performance_message_pair(task, submission)
            send_overview = _should_send_performance_overview(task, submission, user)
            snapshot = dict(submission.sent_snapshot_json or {})
            snapshot["messages"] = {
                "overview_markdown": overview_markdown if send_overview else "",
                "reply_prompt": reply_prompt,
                "overview_skipped": not send_overview,
            }
            submission.sent_snapshot_json = snapshot
            message_results: list[dict[str, Any]] = []
            try:
                if send_overview:
                    overview_result = await robot.send_robot_direct_markdown(
                        user_ids=[user.dingtalk_user_id],
                        title="指标完成情况概览",
                        text=overview_markdown,
                    )
                    message_results.append({"type": "overview_markdown", "sent": True, "result": overview_result})
                else:
                    message_results.append({"type": "overview_markdown", "sent": False, "skipped": True, "reason": "zhu_jiajia_reply_prompt_only"})
                reply_result = await robot.send_robot_direct_text(user_ids=[user.dingtalk_user_id], text=reply_prompt)
                message_results.append({"type": "reply_prompt", "sent": True, "result": reply_result})
                submission.last_prompted_at = now_in_timezone(getattr(user, "timezone", "Asia/Shanghai"))
                send_results.append({"dingtalk_user_id": user.dingtalk_user_id, "sent": True, "messages": message_results})
            except Exception as exc:
                send_results.append(
                    {
                        "dingtalk_user_id": user.dingtalk_user_id,
                        "sent": False,
                        "messages": message_results,
                        "error": str(exc)[:500],
                    }
                )
    await session.commit()
    return {
        "task_id": str(task.id),
        "title": task.title,
        "period_label": task.period_label,
        "metric_count": len(task.metrics_json or []),
        "recipient_count": len(submissions),
        "send_results": send_results,
        "submissions": [_submission_payload(item) for item in submissions],
    }


@router.post("/department/monthly-report")
async def build_department_monthly_performance_report(
    request: Request,
    body: DepartmentMonthlyReportRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if body.use_test_data:
        sources = build_test_department_monthly_sources(body.period_label, seed=body.test_seed)
    else:
        sources = await _load_completed_department_sources(session, period_label=body.period_label, expected_units=body.expected_units)
    coverage = department_monthly_coverage(sources, expected_units=body.expected_units)
    report_text = build_department_monthly_report(
        sources,
        period_label=body.period_label,
        expected_units=body.expected_units,
        generated_for=body.generated_for,
        test_mode=body.use_test_data,
    )
    send_results: list[dict[str, Any]] = []
    if body.send_messages:
        if not body.recipient_dingtalk_user_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="recipient_dingtalk_user_ids is required when send_messages is true.")
        robot = request.app.state.dingtalk_robot
        chunks = split_markdown_message(report_text)
        for index, chunk in enumerate(chunks, start=1):
            title = f"部门绩效月报{'测试版' if body.use_test_data else ''}" + (f"（{index}/{len(chunks)}）" if len(chunks) > 1 else "")
            try:
                result = await robot.send_robot_direct_markdown(
                    user_ids=body.recipient_dingtalk_user_ids,
                    title=title,
                    text=chunk,
                )
                send_results.append({"chunk": index, "sent": True, "result": result})
            except Exception as exc:
                send_results.append({"chunk": index, "sent": False, "error": str(exc)[:500]})
    return {
        "period_label": body.period_label,
        "ready": coverage.ready,
        "expected_units": coverage.expected_units,
        "completed_units": coverage.completed_units,
        "missing_units": coverage.missing_units,
        "source_count": len(sources),
        "report_text": report_text,
        "message_chunks": len(split_markdown_message(report_text)),
        "send_results": send_results,
    }


@router.get("/tasks/{task_id}")
async def get_performance_task(task_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    task = await session.get(PerformanceTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    submissions = (
        await session.execute(
            select(PerformanceSubmission).where(PerformanceSubmission.task_id == task.id).order_by(PerformanceSubmission.created_at)
        )
    ).scalars().all()
    return {
        "task_id": str(task.id),
        "title": task.title,
        "period_label": task.period_label,
        "status": task.status,
        "metrics": task.metrics_json,
        "submissions": [_submission_payload(item) for item in submissions],
    }


@router.post("/manual")
async def submit_manual_performance_reply(
    request: Request,
    body: ManualPerformanceReplyRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    user = await get_active_user_by_dingtalk_id(session, body.dingtalk_user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    service: PerformanceTaskService = request.app.state.performance_service
    result = await service.submit_text(session, user=user, raw_input=body.raw_input, source=body.source)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active performance task for this user.")
    await session.commit()
    return {
        "submission_id": result.submission_id,
        "task_id": result.task_id,
        "status": result.status,
        "message": result.message,
        "touched_metrics": result.touched_metrics,
        "missing": result.missing,
        "responses": result.responses,
        "confirmed_by_user": result.confirmed_by_user,
    }


async def _load_recipient_users(session: AsyncSession, dingtalk_user_ids: list[str]) -> list[User]:
    unique_ids = sorted({item.strip() for item in dingtalk_user_ids if item.strip()})
    if not unique_ids:
        return []
    result = await session.execute(select(User).where(User.dingtalk_user_id.in_(unique_ids), User.active.is_(True)))
    return list(result.scalars().all())


async def _load_completed_department_sources(
    session: AsyncSession,
    *,
    period_label: str,
    expected_units: list[str],
) -> list[dict[str, Any]]:
    result = await session.execute(
        select(PerformanceSubmission, PerformanceTask, User)
        .join(PerformanceTask, PerformanceSubmission.task_id == PerformanceTask.id)
        .join(User, PerformanceSubmission.user_id == User.id)
        .where(
            PerformanceTask.period_label == period_label,
            PerformanceTask.status == "active",
            PerformanceSubmission.status == "completed",
            PerformanceSubmission.confirmed_by_user.is_(True),
        )
        .order_by(PerformanceSubmission.updated_at.desc())
    )
    seen_units: set[str] = set()
    sources: list[dict[str, Any]] = []
    for submission, task, user in result.all():
        unit_name = _match_department_unit(task.title, user.name, expected_units)
        if not unit_name or unit_name in seen_units:
            continue
        snapshot = submission.sent_snapshot_json if isinstance(submission.sent_snapshot_json, dict) else {}
        metrics = snapshot.get("metrics") or task.metrics_json or []
        sources.append(
            source_from_performance_submission(
                unit_name=unit_name,
                owner_name=submission.recipient_name or user.name,
                period_label=task.period_label,
                task_title=task.title,
                metrics=metrics,
                responses=submission.responses_json or [],
                status=submission.status,
                confirmed_by_user=submission.confirmed_by_user,
                source_id=str(submission.id),
            )
        )
        seen_units.add(unit_name)
    return sources


def _match_department_unit(task_title: str, user_name: str, expected_units: list[str]) -> str:
    text = f"{task_title} {user_name}"
    for unit in expected_units:
        if unit in text:
            return unit
    return ""


def _should_send_performance_overview(task: PerformanceTask, submission: PerformanceSubmission, user: User) -> bool:
    text = f"{task.title} {submission.recipient_name} {user.name}"
    return "朱佳佳" not in text


def _submission_payload(submission: PerformanceSubmission) -> dict[str, Any]:
    return {
        "submission_id": str(submission.id),
        "task_id": str(submission.task_id),
        "user_id": str(submission.user_id),
        "team_id": str(submission.team_id),
        "recipient_name": submission.recipient_name,
        "status": submission.status,
        "confirmed_by_user": submission.confirmed_by_user,
        "sent_snapshot": submission.sent_snapshot_json,
        "responses": submission.responses_json,
        "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else None,
    }
