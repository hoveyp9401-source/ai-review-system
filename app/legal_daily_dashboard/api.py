from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.legal_daily_dashboard.analysis import (
    AnalysisValidationError,
    DirectReviewAnalyzer,
)
from app.legal_daily_dashboard.analysis_service import (
    ReviewAnalysisNotFound,
    ReviewAnalysisService,
)
from app.legal_daily_dashboard.domain import DashboardActor
from app.legal_daily_dashboard.service import DashboardNotFound, DashboardService
from app.legal_daily_dashboard.sql_repository import SqlDashboardRepository
from app.legal_ops.auth import PrincipalDirectory
from app.utils.time import now_in_timezone

STATIC_DIR = Path(__file__).with_name("static")


@dataclass(frozen=True)
class DashboardRuntime:
    enabled: bool
    manager_write_enabled: bool
    principals: PrincipalDirectory
    tenant_id: str
    analysis_enabled: bool = False


def build_runtime(settings: Settings) -> DashboardRuntime:
    enabled = bool(getattr(settings, "legal_daily_dashboard_enabled", False))
    write_enabled = bool(
        getattr(
            settings,
            "legal_daily_dashboard_manager_write_enabled",
            False,
        )
    )
    analysis_enabled = bool(
        getattr(settings, "legal_daily_dashboard_analysis_enabled", False)
    )
    principals_json = str(
        getattr(settings, "legal_daily_dashboard_principals_json", "") or ""
    ).strip()
    token = str(getattr(settings, "legal_daily_dashboard_token", "") or "").strip()
    tenant_id = str(
        getattr(settings, "legal_daily_dashboard_tenant_id", "") or ""
    ).strip()
    system_user_id = str(
        getattr(settings, "legal_daily_dashboard_system_user_id", "") or ""
    ).strip()
    if principals_json:
        principals = PrincipalDirectory.from_json(principals_json)
    else:
        principals = PrincipalDirectory.single_admin(
            token=token,
            tenant_id=tenant_id,
            user_id=system_user_id or "system:legal-daily-dashboard",
        )
    if enabled and (not tenant_id or (not principals_json and not token)):
        raise RuntimeError("Legal daily dashboard requires tenant id and credentials")
    return DashboardRuntime(
        enabled=enabled,
        manager_write_enabled=write_enabled,
        principals=principals,
        tenant_id=tenant_id,
        analysis_enabled=analysis_enabled,
    )


def get_runtime(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> DashboardRuntime:
    runtime = getattr(
        request.app.state,
        "legal_daily_dashboard_runtime",
        None,
    )
    if runtime is None:
        runtime = build_runtime(settings)
        request.app.state.legal_daily_dashboard_runtime = runtime
    if not runtime.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )
    return runtime


def require_actor(
    runtime: Annotated[DashboardRuntime, Depends(get_runtime)],
    token: Annotated[
        str | None,
        Header(alias="X-Legal-Daily-Token"),
    ] = None,
) -> DashboardActor:
    try:
        principal = runtime.principals.authenticate(token or "")
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="身份验证失败",
        ) from exc
    if principal.tenant_id != runtime.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该凭证无权访问当前日报驾驶舱",
        )
    return DashboardActor(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
    )


def get_dashboard_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DashboardService:
    return DashboardService(SqlDashboardRepository(session))


def get_review_analysis_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[DashboardRuntime, Depends(get_runtime)],
    settings: Settings = Depends(get_settings),
) -> ReviewAnalysisService:
    if not runtime.analysis_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="日报内容分析刷新尚未启用",
        )
    client = getattr(request.app.state, "llm_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="日报内容分析服务尚未就绪",
        )
    model = (
        settings.legal_daily_dashboard_analysis_model.strip()
        or settings.llm_high_risk_model
    )
    return ReviewAnalysisService(
        repository=SqlDashboardRepository(session),
        analyzer=DirectReviewAnalyzer(
            client=client,
            model=model,
            timeout_seconds=settings.legal_daily_dashboard_analysis_timeout_seconds,
            max_retries=settings.legal_daily_dashboard_analysis_max_retries,
        ),
    )


Runtime = Annotated[DashboardRuntime, Depends(get_runtime)]
Actor = Annotated[DashboardActor, Depends(require_actor)]
Service = Annotated[DashboardService, Depends(get_dashboard_service)]
ReviewService = Annotated[
    ReviewAnalysisService,
    Depends(get_review_analysis_service),
]
SettingsDependency = Annotated[Settings, Depends(get_settings)]

router = APIRouter(
    prefix="/legal-daily-dashboard",
    tags=["legal-daily-dashboard"],
)


class ManagerDecisionBody(BaseModel):
    target_type: Literal["review_suggestion", "work_item"]
    target_ref: str = Field(min_length=1, max_length=128)
    decision: Literal[
        "normal",
        "waiting_external",
        "followup",
        "completed",
        "system_error",
    ]
    note: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=256)


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard_app(_runtime: Runtime) -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@router.get("/assets/{asset}", include_in_schema=False)
def dashboard_asset(
    asset: Literal["app.js", "styles.css"],
    _runtime: Runtime,
) -> FileResponse:
    media_type = "application/javascript" if asset.endswith(".js") else "text/css"
    return FileResponse(STATIC_DIR / asset, media_type=media_type)


@router.get("/api/overview")
async def overview(
    actor: Actor,
    service: Service,
    runtime: Runtime,
    settings: SettingsDependency,
    report_date: date = Query(),
    team: str | None = Query(default=None),
) -> dict[str, object]:
    try:
        result = await service.get_overview(
            actor=actor,
            report_date=report_date,
            team_ref=team,
            now=now_in_timezone(settings.timezone),
        )
        return {
            **result,
            "capabilities": {
                "manager_decision_write": (runtime.manager_write_enabled),
                "analysis_refresh": runtime.analysis_enabled,
                "message_send": False,
                "daily_report_edit": False,
            },
        }
    except DashboardNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到",
        ) from exc


@router.post("/api/manager-decisions")
async def save_manager_decision(
    body: ManagerDecisionBody,
    actor: Actor,
    service: Service,
    runtime: Runtime,
    settings: SettingsDependency,
) -> dict[str, object]:
    if not runtime.manager_write_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="管理者处理记录写入未启用",
        )
    try:
        return await service.record_manager_decision(
            actor=actor,
            target_type=body.target_type,
            target_ref=body.target_ref,
            decision=body.decision,
            note=body.note,
            idempotency_key=body.idempotency_key,
            now=now_in_timezone(settings.timezone),
        )
    except DashboardNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get("/api/members")
async def members(
    actor: Actor,
    service: Service,
    settings: SettingsDependency,
    report_date: date = Query(),
    team: str | None = Query(default=None),
) -> dict[str, object]:
    try:
        return await service.get_members(
            actor=actor,
            report_date=report_date,
            team_ref=team,
            now=now_in_timezone(settings.timezone),
        )
    except DashboardNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到",
        ) from exc


@router.get("/api/members/{member_ref}/timeline")
async def member_timeline(
    member_ref: str,
    actor: Actor,
    service: Service,
    settings: SettingsDependency,
    end_date: date = Query(),
    days: int = Query(default=14),
) -> dict[str, object]:
    try:
        return await service.get_member_timeline(
            actor=actor,
            member_ref=member_ref,
            end_date=end_date,
            days=days,
            now=now_in_timezone(settings.timezone),
        )
    except DashboardNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post("/api/members/{member_ref}/analysis")
async def refresh_member_analysis(
    member_ref: str,
    actor: Actor,
    service: Service,
    analysis_service: ReviewService,
    runtime: Runtime,
    settings: SettingsDependency,
    end_date: date = Query(),
    days: int = Query(default=14),
) -> dict[str, object]:
    if not runtime.analysis_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="日报内容分析刷新尚未启用",
        )
    try:
        team_ref = await service.resolve_member_team(
            actor=actor,
            member_ref=member_ref,
            on_date=end_date,
        )
        return await analysis_service.analyze_member(
            tenant_id=actor.tenant_id,
            team_ref=team_ref,
            member_ref=member_ref,
            end_date=end_date,
            days=days,
        )
    except (DashboardNotFound, ReviewAnalysisNotFound) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到",
        ) from exc
    except AnalysisValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="模型分析结果未通过原文证据校验",
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="日报分析超时，本次没有保存任何分析结果，请稍后重试",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get("/api/items")
async def items(
    actor: Actor,
    service: Service,
    end_date: date = Query(),
    team: str | None = Query(default=None),
) -> dict[str, object]:
    try:
        return await service.get_items(
            actor=actor,
            end_date=end_date,
            team_ref=team,
        )
    except DashboardNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到",
        ) from exc


@router.get("/api/trends")
async def trends(
    actor: Actor,
    service: Service,
    settings: SettingsDependency,
    end_date: date = Query(),
    period: Literal["week", "month"] = Query(default="week"),
    team: str | None = Query(default=None),
) -> dict[str, object]:
    try:
        return await service.get_trends(
            actor=actor,
            end_date=end_date,
            period=period,
            team_ref=team,
            now=now_in_timezone(settings.timezone),
        )
    except DashboardNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到",
        ) from exc


@router.get("/api/briefing", response_class=PlainTextResponse)
async def briefing(
    actor: Actor,
    service: Service,
    settings: SettingsDependency,
    end_date: date = Query(),
    period: Literal["week", "month"] = Query(default="week"),
    team: str | None = Query(default=None),
) -> PlainTextResponse:
    try:
        trend = await service.get_trends(
            actor=actor,
            end_date=end_date,
            period=period,
            team_ref=team,
            now=now_in_timezone(settings.timezone),
        )
    except DashboardNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到",
        ) from exc
    content = service.render_briefing(trend)
    filename = f"legal-daily-briefing-{end_date.isoformat()}.txt"
    return PlainTextResponse(
        content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
