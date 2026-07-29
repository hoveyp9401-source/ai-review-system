from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import admin, debug, performance, reports, tasks, webhook
from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.extractor import DailyReportExtractor, TeamSummaryGenerator
from app.legal_daily_dashboard.api import (
    build_runtime as build_legal_daily_dashboard_runtime,
)
from app.legal_daily_dashboard.api import (
    router as legal_daily_dashboard_router,
)
from app.legal_ops.api import build_runtime as build_legal_ops_runtime
from app.legal_ops.api import router as legal_ops_router
from app.legal_ops_data_intake.api import router as legal_ops_data_intake_router
from app.services.dingtalk import DingTalkRobotClient
from app.services.performance_service import PerformanceTaskService
from app.services.report_service import DailyReportService
from app.services.summary_service import SummaryService


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    llm_client = LLMClient(settings)
    robot = DingTalkRobotClient(settings)
    app.state.llm_client = llm_client
    app.state.dingtalk_robot = robot
    app.state.report_service = DailyReportService(
        settings, DailyReportExtractor(llm_client)
    )
    app.state.performance_service = PerformanceTaskService(settings)
    app.state.summary_service = SummaryService(
        settings, TeamSummaryGenerator(llm_client)
    )
    app.state.legal_ops_runtime = build_legal_ops_runtime(settings)
    app.state.legal_daily_dashboard_runtime = build_legal_daily_dashboard_runtime(
        settings
    )
    yield
    await robot.close()
    await llm_client.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def admin_auth_cookie_middleware(request: Request, call_next):
        if not request.url.path.startswith("/admin"):
            return await call_next(request)
        settings_provider = request.app.dependency_overrides.get(
            get_settings, get_settings
        )
        current_settings = settings_provider()
        ok, status_code, detail, should_set_cookie = admin.evaluate_admin_auth(
            request, current_settings
        )
        if not ok:
            return JSONResponse({"detail": detail}, status_code=status_code)
        response = await call_next(request)
        if should_set_cookie:
            admin.set_admin_session_cookie(response, current_settings)
        return response

    app.include_router(webhook.router)
    app.include_router(admin.router)
    app.include_router(performance.router)
    app.include_router(reports.router)
    app.include_router(tasks.router)
    app.include_router(debug.router)
    app.include_router(legal_ops_router)
    app.include_router(legal_ops_data_intake_router)
    app.include_router(legal_daily_dashboard_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
