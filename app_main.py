from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import admin, debug, reports, tasks, webhook
from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.extractor import DailyReportExtractor, TeamSummaryGenerator
from app.services.dingtalk import DingTalkRobotClient
from app.services.report_service import DailyReportService
from app.services.summary_service import SummaryService


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    llm_client = LLMClient(settings)
    robot = DingTalkRobotClient(settings)
    app.state.llm_client = llm_client
    app.state.dingtalk_robot = robot
    app.state.report_service = DailyReportService(settings, DailyReportExtractor(llm_client))
    app.state.summary_service = SummaryService(settings, TeamSummaryGenerator(llm_client))
    yield
    await robot.close()
    await llm_client.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.include_router(webhook.router)
    app.include_router(admin.router)
    app.include_router(reports.router)
    app.include_router(tasks.router)
    app.include_router(debug.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
