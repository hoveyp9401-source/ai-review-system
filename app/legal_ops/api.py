from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID

from app.config import Settings, get_settings
from app.db import get_session
from app.agent2.business.entrypoint import parse_tenant_allowlist
from app.agent2.business.contracts import BusinessCommandContext
from app.agent2.business.models import Agent2Case, Agent2IdentityBinding, CaseProgress
from app.agent2.business.policy import BusinessEffectPolicy
from app.agent2.business.sql_executor import SqlBusinessExecutor
from app.agent2.case_followup_commands import (
    CancelCaseFollowupTask,
    TriggerCaseFollowupNow,
    UpdateCaseFollowupPolicy,
)
from app.agent2.case_followup_admin_sql import cancel_unsent_case_followup_task
from app.agent2.case_followup_metrics import load_case_followup_metrics
from app.agent2.case_followup_policy_sql import apply_case_followup_policy_command
from app.legal_ops.auth import PrincipalDirectory, SandboxPrincipal
from app.legal_ops.access import LivePrincipalScope, load_live_principal_scope
from app.legal_ops.repository import SandboxRepository
from app.legal_ops.seed import build_phase0_seed
from app.legal_ops.service import LegalOpsReadService
from app.legal_ops.phase2_read import (
    load_phase2_case_detail,
    load_case_followup_configuration,
    load_phase2_party_detail,
    load_phase2_read_model,
    search_phase2_parties,
)
from app.legal_ops.followup_bulk import (
    BulkFollowupChange,
    BulkFollowupFilter,
    preview_bulk_followup_change,
)
from app.legal_ops.live_workspace import (
    load_case_detail_workspace,
    load_report_center,
    load_team_center,
    load_travel_center,
    project_case_workspace,
)
from app.legal_ops.write_service import (
    LegalOpsWriteResult,
    build_case_owner_context,
    create_case_progress,
    delete_case_progress,
    mutate_report,
    update_case_progress,
)
from app.models import Agent2ConversationState
from app.scheduler.runner import (
    _configured_followup_conversation_map,
    resolve_followup_conversation_id,
)


STATIC_DIR = Path(__file__).with_name("static")
EXPORT_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "legal-ops-exports"
EXPORT_MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@dataclass(frozen=True)
class LegalOpsRuntime:
    enabled: bool
    repository: SandboxRepository
    service: LegalOpsReadService
    principals: PrincipalDirectory
    mode: Literal["demo", "sandbox_live", "disabled"] = "demo"
    seed_manifest: str = str(Path(__file__).with_name("fixtures") / "phase0_manifest.json")


def build_runtime(settings: Settings) -> LegalOpsRuntime:
    sandbox_enabled = bool(settings.legal_ops_sandbox_enabled)
    live_enabled = bool(settings.legal_ops_live_enabled)
    if sandbox_enabled and live_enabled:
        raise RuntimeError("Legal Ops demo and live modes are mutually exclusive")
    enabled = sandbox_enabled or live_enabled
    mode: Literal["demo", "sandbox_live", "disabled"] = (
        "demo" if sandbox_enabled else "sandbox_live" if live_enabled else "disabled"
    )
    repository = SandboxRepository(
        settings.legal_ops_sandbox_data_path,
        sandbox_enabled=enabled,
    )
    if sandbox_enabled:
        expected_seed = build_phase0_seed(settings.legal_ops_sandbox_seed_manifest)
        if not repository.path.exists():
            repository.reset(expected_seed)
        else:
            try:
                current_metadata = repository.load().get("metadata", {})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "legal ops snapshot is unreadable; refusing startup overwrite, run an explicit confirmed reset"
                ) from exc
            expected_metadata = expected_seed["metadata"]
            if (
                current_metadata.get("seed_id") != expected_metadata["seed_id"]
                or current_metadata.get("generator_revision") != expected_metadata["generator_revision"]
            ):
                raise RuntimeError(
                    "legal ops seed revision mismatch; refusing startup overwrite, run tenant-scoped confirmed resets"
                )
    default_tenant = (
        settings.legal_ops_live_tenant_id.strip()
        if live_enabled
        else settings.legal_ops_sandbox_default_tenant.strip()
    )
    if not default_tenant and sandbox_enabled:
        default_tenant = repository.load()["tenants"][0]["id"]
    has_live_credentials = bool(
        settings.legal_ops_live_principals_json.strip()
        or settings.legal_ops_live_token.strip()
    )
    if live_enabled and (not default_tenant or not has_live_credentials):
        raise RuntimeError("Legal Ops live mode requires tenant id and credentials")
    if live_enabled and settings.legal_ops_live_principals_json.strip():
        principals = PrincipalDirectory.from_json(settings.legal_ops_live_principals_json)
    elif sandbox_enabled and settings.legal_ops_sandbox_principals_json.strip():
        principals = PrincipalDirectory.from_json(settings.legal_ops_sandbox_principals_json)
    else:
        principals = PrincipalDirectory.single_admin(
            token=(settings.legal_ops_live_token if live_enabled else settings.legal_ops_sandbox_token),
            tenant_id=default_tenant,
        )
    return LegalOpsRuntime(
        enabled=enabled,
        repository=repository,
        service=LegalOpsReadService(repository),
        principals=principals,
        mode=mode,
        seed_manifest=settings.legal_ops_sandbox_seed_manifest,
    )


def get_runtime(request: Request, settings: Settings = Depends(get_settings)) -> LegalOpsRuntime:
    runtime = getattr(request.app.state, "legal_ops_runtime", None)
    if runtime is None:
        runtime = build_runtime(settings)
        request.app.state.legal_ops_runtime = runtime
    if not runtime.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return runtime


def require_principal(
    runtime: Annotated[LegalOpsRuntime, Depends(get_runtime)],
    token: Annotated[str | None, Header(alias="X-Legal-Ops-Token")] = None,
) -> SandboxPrincipal:
    try:
        return runtime.principals.authenticate(token or "")
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


Runtime = Annotated[LegalOpsRuntime, Depends(get_runtime)]
Principal = Annotated[SandboxPrincipal, Depends(require_principal)]

router = APIRouter(prefix="/legal-ops", tags=["legal-ops-sandbox"])


class FollowupPolicyUpdateBody(BaseModel):
    assigned_user_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=0)
    cadence_type: Literal[
        "daily", "weekly", "every_15_days", "monthly", "custom_interval",
        "event_only", "manual_only", "paused", "disabled",
    ]
    enabled: bool
    custom_interval_days: int | None = Field(default=None, ge=1, le=365)
    business_days_only: bool | None = None
    snoozed_until: datetime | None = None
    hearing_reminders_enabled: bool | None = None
    stage_transition_enabled: bool | None = None
    node_transition_enabled: bool | None = None
    force_manual_override: bool = False
    source_turn_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=8, max_length=512)


class TriggerFollowupNowBody(BaseModel):
    assigned_user_id: str = Field(min_length=1, max_length=128)
    source_turn_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=8, max_length=512)


class CancelFollowupBody(BaseModel):
    followup_id: str = Field(min_length=1, max_length=128)
    assigned_user_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    source_turn_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=8, max_length=512)


class BulkFollowupFilterBody(BaseModel):
    case_type: str = ""
    stage: str = ""
    node: str = ""
    assigned_user_id: str = ""
    risk_level: str = ""
    has_hearing_date: bool | None = None
    cadence_type: str = ""
    waiting_for_reply: bool | None = None


class BulkFollowupChangeBody(BaseModel):
    cadence_type: Literal[
        "daily", "weekly", "every_15_days", "monthly", "custom_interval",
        "event_only", "manual_only", "paused", "disabled",
    ]
    enabled: bool
    custom_interval_days: int | None = Field(default=None, ge=1, le=365)
    hearing_reminders_enabled: bool | None = None
    stage_transition_enabled: bool | None = None
    node_transition_enabled: bool | None = None
    force_manual_override: bool = False


class BulkFollowupPreviewBody(BaseModel):
    filters: BulkFollowupFilterBody = Field(default_factory=BulkFollowupFilterBody)
    change: BulkFollowupChangeBody


class BulkFollowupApplyBody(BulkFollowupPreviewBody):
    preview_id: str = Field(min_length=64, max_length=64)
    confirm: bool
    source_turn_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=8, max_length=384)


class CaseProgressCreateBody(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    details: str = Field(default="", max_length=10000)
    occurred_at: datetime | None = None
    current_status: str = Field(default="", max_length=2000)
    next_actions: list[str] = Field(default_factory=list, max_length=20)
    operation_id: str = Field(min_length=8, max_length=256)


class CaseProgressUpdateBody(BaseModel):
    expected_version: int = Field(ge=1)
    summary: str | None = Field(default=None, min_length=1, max_length=2000)
    details: str | None = Field(default=None, max_length=10000)
    operation_id: str = Field(min_length=8, max_length=256)


class CaseProgressDeleteBody(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(default="用户从案件工作台删除", min_length=1, max_length=500)
    operation_id: str = Field(min_length=8, max_length=256)


class ReportCommandBody(BaseModel):
    command_type: Literal["append_item", "edit_item", "delete_item", "submit_report"]
    expected_version: int = Field(ge=0)
    field_name: str = Field(default="", max_length=64)
    item_ref: str = Field(default="", max_length=256)
    value: str = Field(default="", max_length=10000)
    operation_id: str = Field(min_length=8, max_length=256)


def _require_demo(runtime: LegalOpsRuntime) -> None:
    if runtime.mode != "demo":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found in live mode")


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def legal_ops_app(_runtime: Runtime) -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@router.get("/assets/{asset}", include_in_schema=False)
def legal_ops_asset(asset: Literal["app.js", "styles.css"], _runtime: Runtime):
    media_type = "application/javascript" if asset.endswith(".js") else "text/css"
    return FileResponse(STATIC_DIR / asset, media_type=media_type)


@router.get("/api/shell")
def shell(runtime: Runtime, principal: Principal) -> dict[str, Any]:
    if runtime.mode == "sandbox_live":
        return {
            "mode": "sandbox_live",
            "tenant": {"id": principal.tenant_id, "name": "Agent2 法务灰测中台"},
            "companies": [],
            "teams": [],
            "users": [],
            "navigation": ["overview", "reports", "cases", "travel", "team", "audit"],
            "fixture_notice": "当前页面直接读取服务器 PostgreSQL 中的灰测真实数据；演示 Fixture 与静态业务记录不进入本视图。",
            "scope": {
                "tenant_id": principal.tenant_id,
                "user_id": principal.user_id,
                "role_ids": list(principal.role_ids),
            },
        }
    return runtime.service.shell(principal)


@router.get("/api/overview")
def overview(runtime: Runtime, principal: Principal) -> dict[str, Any]:
    _require_demo(runtime)
    return runtime.service.overview(principal)


@router.get("/api/reports/{period}")
def reporting_dashboard(
    period: Literal["daily", "weekly", "monthly"], runtime: Runtime, principal: Principal
) -> dict[str, Any]:
    _require_demo(runtime)
    return runtime.service.period_dashboard(principal, period)


@router.get("/api/reports/{period}/{submission_id}")
def reporting_detail(
    period: Literal["daily", "weekly", "monthly"],
    submission_id: str,
    runtime: Runtime,
    principal: Principal,
) -> dict[str, Any]:
    _require_demo(runtime)
    try:
        return runtime.service.report_detail(principal, period, submission_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/api/teams")
def team_workbench(runtime: Runtime, principal: Principal) -> dict[str, Any]:
    _require_demo(runtime)
    return runtime.service.team_workbench(principal)


@router.get("/api/travel")
def travel_dashboard(runtime: Runtime, principal: Principal) -> dict[str, Any]:
    _require_demo(runtime)
    return runtime.service.travel_dashboard(principal)


@router.get("/api/metrics")
def metric_center(runtime: Runtime, principal: Principal) -> dict[str, Any]:
    _require_demo(runtime)
    return runtime.service.metric_center(principal)


@router.get("/api/performance")
def performance_center(runtime: Runtime, principal: Principal) -> dict[str, Any]:
    _require_demo(runtime)
    return runtime.service.performance_center(principal)


@router.get("/api/exports/{period}/{file_format}")
def download_performance_export(
    period: Literal["weekly", "monthly"],
    file_format: Literal["docx", "pdf", "xlsx"],
    runtime: Runtime,
    principal: Principal,
) -> FileResponse:
    _require_demo(runtime)
    del runtime, principal
    filename = f"legal-ops-{period}-report.{file_format}"
    export_path = EXPORT_DIR / filename
    if not export_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="export artifact not generated")
    return FileResponse(
        export_path,
        filename=filename,
        media_type=EXPORT_MEDIA_TYPES[file_format],
    )


@router.get("/api/cases")
def case_list(
    runtime: Runtime,
    principal: Principal,
    tenant_id: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    risk: Annotated[str | None, Query()] = None,
    team_id: Annotated[str | None, Query()] = None,
    query: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    _require_demo(runtime)
    try:
        return runtime.service.case_list(
            principal,
            requested_tenant_id=tenant_id,
            status=status_filter,
            risk=risk,
            team_id=team_id,
            query=query,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/api/forest")
def case_forest(runtime: Runtime, principal: Principal) -> dict[str, Any]:
    _require_demo(runtime)
    return runtime.service.case_forest(principal)


@router.get("/api/cases/{case_id}")
def case_detail(case_id: str, runtime: Runtime, principal: Principal) -> dict[str, Any]:
    _require_demo(runtime)
    try:
        return runtime.service.case_detail(principal, case_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/api/quality")
def quality_center(runtime: Runtime, principal: Principal) -> dict[str, Any]:
    _require_demo(runtime)
    return runtime.service.quality_center(principal)


@router.get("/api/sources")
def source_center(runtime: Runtime, principal: Principal) -> dict[str, Any]:
    _require_demo(runtime)
    return runtime.service.source_center(principal)


@router.get("/api/permissions")
def permission_center(runtime: Runtime, principal: Principal) -> dict[str, Any]:
    _require_demo(runtime)
    return runtime.service.permission_center(principal)


@router.get("/api/phase2")
async def phase2_evidence_center(
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    return await load_phase2_read_model(
        session,
        tenant_id=principal.tenant_id,
        limit=limit,
        allowed_case_ids=scope.allowed_case_ids,
        principal_user_id=(scope.user_id if scope.allowed_case_ids is not None else ""),
    )


@router.get("/api/workspace/cases")
async def live_case_workspace(
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    case_type: Annotated[str, Query(max_length=64)] = "",
    stage: Annotated[str, Query(max_length=64)] = "",
    query: Annotated[str, Query(max_length=256)] = "",
    progress_status: Literal["", "active", "missing"] = "",
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    read_model = await load_phase2_read_model(
        session,
        tenant_id=principal.tenant_id,
        limit=500,
        allowed_case_ids=scope.allowed_case_ids,
        principal_user_id=(scope.user_id if scope.allowed_case_ids is not None else ""),
    )
    return project_case_workspace(
        read_model,
        page=page,
        page_size=page_size,
        case_type=case_type,
        stage=stage,
        query=query,
        progress_status=progress_status,
    )


@router.get("/api/workspace/reports")
async def live_report_center(
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    return await load_report_center(
        session,
        tenant_id=principal.tenant_id,
        principal_user_id=(scope.user_id if scope.allowed_case_ids is not None else ""),
    )


@router.post("/api/workspace/reports/{report_ref}/commands")
async def live_report_command(
    report_ref: str,
    body: ReportCommandBody,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    context = _case_owner_write_context(principal, scope, body.operation_id)
    if body.command_type == "append_item" and (not body.field_name or not body.value.strip()):
        raise HTTPException(status_code=422, detail="新增报告条目需要栏目和正文")
    if body.command_type == "edit_item" and (not body.item_ref or not body.value.strip()):
        raise HTTPException(status_code=422, detail="修改报告条目需要明确条目和正文")
    if body.command_type == "delete_item" and not body.item_ref:
        raise HTTPException(status_code=422, detail="删除报告条目需要明确条目")
    result = await mutate_report(
        session,
        context=context,
        settings=settings,
        report_ref=report_ref,
        command_type=body.command_type,
        expected_version=body.expected_version,
        field_name=body.field_name,
        item_ref=body.item_ref,
        value=body.value,
    )
    if not result.succeeded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": result.reason_code, "actual_write": False},
        )
    return result.user_payload()


@router.get("/api/workspace/cases/{case_id}")
async def live_case_detail_workspace(
    case_id: str,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    try:
        return await load_case_detail_workspace(
            session,
            tenant_id=principal.tenant_id,
            case_id=case_id,
            allowed_case_ids=scope.allowed_case_ids,
            can_manage_followup=principal.has_role("tenant_admin", "system_admin"),
            editable_actor_user_id=(scope.user_id if scope.binding is not None else ""),
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/api/workspace/cases/{case_id}/progress")
async def live_create_case_progress(
    case_id: str,
    body: CaseProgressCreateBody,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    context = _case_owner_write_context(principal, scope, body.operation_id)
    occurred_at = body.occurred_at or context.occurred_at
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    result = await create_case_progress(
        session,
        context=context,
        settings=settings,
        case_id=case_id,
        summary=body.summary,
        details=body.details,
        occurred_at=occurred_at,
        current_status=body.current_status,
        next_actions=tuple(item.strip() for item in body.next_actions if item.strip()),
    )
    return await _case_write_response(
        result=result,
        session=session,
        principal=principal,
        scope=scope,
        case_id=case_id,
    )


@router.put("/api/workspace/cases/{case_id}/progress/{progress_id}")
async def live_update_case_progress(
    case_id: str,
    progress_id: str,
    body: CaseProgressUpdateBody,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    if body.summary is None and body.details is None:
        raise HTTPException(status_code=422, detail="至少提供一项修改内容")
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    await _assert_progress_belongs_to_case(
        session, tenant_id=principal.tenant_id, case_id=case_id, progress_id=progress_id
    )
    context = _case_owner_write_context(principal, scope, body.operation_id)
    result = await update_case_progress(
        session,
        context=context,
        settings=settings,
        progress_id=progress_id,
        expected_version=body.expected_version,
        summary=body.summary,
        details=body.details,
    )
    return await _case_write_response(
        result=result,
        session=session,
        principal=principal,
        scope=scope,
        case_id=case_id,
    )


@router.delete("/api/workspace/cases/{case_id}/progress/{progress_id}")
async def live_delete_case_progress(
    case_id: str,
    progress_id: str,
    body: CaseProgressDeleteBody,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    await _assert_progress_belongs_to_case(
        session, tenant_id=principal.tenant_id, case_id=case_id, progress_id=progress_id
    )
    context = _case_owner_write_context(principal, scope, body.operation_id)
    result = await delete_case_progress(
        session,
        context=context,
        settings=settings,
        progress_id=progress_id,
        expected_version=body.expected_version,
        reason=body.reason,
    )
    return await _case_write_response(
        result=result,
        session=session,
        principal=principal,
        scope=scope,
        case_id=case_id,
    )


@router.get("/api/workspace/team")
async def live_team_center(
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    return await load_team_center(
        session,
        tenant_id=principal.tenant_id,
        team_id=(str(scope.binding.team_id) if scope.binding is not None else ""),
    )


@router.get("/api/workspace/travel")
async def live_travel_center(
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    read_model = await load_phase2_read_model(
        session,
        tenant_id=principal.tenant_id,
        limit=500,
        allowed_case_ids=scope.allowed_case_ids,
        principal_user_id=(scope.user_id if scope.allowed_case_ids is not None else ""),
    )
    return await load_travel_center(
        session, tenant_id=principal.tenant_id, read_model=read_model
    )


@router.get("/api/phase2/cases/{case_id}")
async def phase2_case_lifecycle(
    case_id: str,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    try:
        return await load_phase2_case_detail(
            session,
            tenant_id=principal.tenant_id,
            case_id=case_id,
            allowed_case_ids=scope.allowed_case_ids,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/api/phase2/cases/{case_id}/followup")
async def phase2_case_followup_configuration(
    case_id: str,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    try:
        return await load_case_followup_configuration(
            session, tenant_id=principal.tenant_id, case_id=case_id,
            allowed_case_ids=scope.allowed_case_ids,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put("/api/phase2/cases/{case_id}/followup")
async def phase2_update_case_followup_configuration(
    case_id: str,
    body: FollowupPolicyUpdateBody,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    if not principal.has_role("tenant_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant admin required")
    command = UpdateCaseFollowupPolicy(
        command_id=f"legal-ops-policy:{body.source_turn_id}",
        tenant_id=principal.tenant_id,
        case_id=case_id,
        assigned_user_id=body.assigned_user_id,
        expected_version=body.expected_version,
        policy_source="case_manual_override",
        cadence_type=body.cadence_type,
        enabled=body.enabled,
        force_manual_override=body.force_manual_override,
        source_turn_id=body.source_turn_id,
        idempotency_key=body.idempotency_key,
        custom_interval_days=body.custom_interval_days,
        business_days_only=body.business_days_only,
        snoozed_until=body.snoozed_until,
        hearing_reminders_enabled=body.hearing_reminders_enabled,
        stage_transition_enabled=body.stage_transition_enabled,
        node_transition_enabled=body.node_transition_enabled,
    )
    receipt = await apply_case_followup_policy_command(
        session, command, actor_user_id=principal.user_id,
        actor_is_tenant_admin=True,
    )
    await session.commit()
    if receipt.status != "executed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": receipt.error_code,
                "failed_stage": receipt.failed_stage,
                "receipt_id": str(receipt.receipt_id),
                "actual_write": False,
            },
        )
    current = await load_case_followup_configuration(
        session, tenant_id=principal.tenant_id, case_id=case_id
    )
    return {
        "receipt_id": str(receipt.receipt_id),
        "actual_write": True,
        "configuration": current,
    }


@router.post("/api/phase2/cases/{case_id}/followup/trigger-now")
async def phase2_trigger_case_followup_now(
    case_id: str,
    body: TriggerFollowupNowBody,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    if not principal.has_role("tenant_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant admin required")
    try:
        case_uuid = UUID(case_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found") from exc
    case = await session.scalar(select(Agent2Case).where(
        Agent2Case.tenant_id == principal.tenant_id,
        Agent2Case.case_id == case_uuid,
        Agent2Case.owner_user_id == body.assigned_user_id,
    ))
    binding = await session.scalar(select(Agent2IdentityBinding).where(
        Agent2IdentityBinding.tenant_id == principal.tenant_id,
        Agent2IdentityBinding.user_id == body.assigned_user_id,
        Agent2IdentityBinding.active.is_(True),
    ))
    allowed = {
        str(value) for value in ((binding.permission_scope_json if binding else {}) or {}).get(
            "allowed_case_ids", []
        ) if value
    }
    if case is None or binding is None or case_id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="case permission denied")
    states = tuple((await session.scalars(select(Agent2ConversationState).where(
        Agent2ConversationState.user_key
        == f"{principal.tenant_id}:{body.assigned_user_id}"
    ))).all())
    conversation_id = resolve_followup_conversation_id(
        _configured_followup_conversation_map(
            settings.case_followup_conversation_map_json
        ),
        tenant_id=principal.tenant_id, user_id=body.assigned_user_id,
        observed_conversation_ids=tuple(item.conversation_id for item in states),
    )
    if not conversation_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "conversation_scope_not_unique", "actual_write": False},
        )
    now = datetime.now(timezone.utc)
    context = BusinessCommandContext(
        tenant_id=principal.tenant_id, company_id=binding.company_id,
        department_id=binding.department_id, team_id=binding.team_id,
        actor_user_id=principal.user_id, actor_role_ids=("tenant_admin",),
        allowed_case_ids=(case_id,), source_message_id=body.source_turn_id,
        source_channel="legal_ops", occurred_at=now,
        conversation_id=conversation_id,
    )
    receipt = await SqlBusinessExecutor(
        session,
        effect_policy=BusinessEffectPolicy.from_settings(settings),
        execution_authority="authenticated_admin_command",
    ).execute(
        TriggerCaseFollowupNow(
            command_id=f"legal-ops-trigger:{body.source_turn_id}",
            tenant_id=principal.tenant_id, case_id=case_id,
            assigned_user_id=body.assigned_user_id,
            source_turn_id=body.source_turn_id,
            idempotency_key=body.idempotency_key,
        ),
        context,
    )
    await session.commit()
    if receipt.status not in {"executed", "duplicate"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "reason": receipt.error_code, "receipt_id": receipt.receipt_id,
            "actual_write": False,
        })
    return {
        "receipt_id": receipt.receipt_id,
        "actual_write": receipt.actual_write,
        "configuration": await load_case_followup_configuration(
            session, tenant_id=principal.tenant_id, case_id=case_id
        ),
    }


@router.post("/api/phase2/cases/{case_id}/followup/cancel")
async def phase2_cancel_case_followup(
    case_id: str,
    body: CancelFollowupBody,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    if not principal.has_role("tenant_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant admin required")
    receipt = await cancel_unsent_case_followup_task(
        session,
        CancelCaseFollowupTask(
            command_id=f"legal-ops-cancel:{body.source_turn_id}",
            tenant_id=principal.tenant_id, case_id=case_id,
            followup_id=body.followup_id,
            assigned_user_id=body.assigned_user_id,
            expected_version=body.expected_version,
            source_turn_id=body.source_turn_id,
            idempotency_key=body.idempotency_key,
        ),
        actor_user_id=principal.user_id, actor_is_tenant_admin=True,
    )
    await session.commit()
    if receipt.status != "executed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "reason": receipt.error_code, "receipt_id": str(receipt.receipt_id),
            "actual_write": False,
        })
    return {
        "receipt_id": str(receipt.receipt_id), "actual_write": True,
        "configuration": await load_case_followup_configuration(
            session, tenant_id=principal.tenant_id, case_id=case_id
        ),
    }


@router.post("/api/phase2/followup/bulk-preview")
async def phase2_preview_bulk_followup_configuration(
    body: BulkFollowupPreviewBody,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    if not principal.has_role("tenant_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant admin required")
    return await preview_bulk_followup_change(
        session, tenant_id=principal.tenant_id,
        filters=BulkFollowupFilter(**body.filters.model_dump()),
        change=BulkFollowupChange(**body.change.model_dump()),
    )


@router.get("/api/phase2/followup/metrics")
async def phase2_case_followup_metrics(
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
    user_id: str = "",
    case_id: str = "",
    trigger_type: str = "",
    policy_type: str = "",
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    if not principal.has_role("tenant_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant admin required")
    try:
        return await load_case_followup_metrics(
            session, tenant_id=principal.tenant_id, user_id=user_id,
            case_id=case_id, trigger_type=trigger_type, policy_type=policy_type,
            start_at=start_at, end_at=end_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid filter") from exc


@router.post("/api/phase2/followup/bulk-apply")
async def phase2_apply_bulk_followup_configuration(
    body: BulkFollowupApplyBody,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    if not principal.has_role("tenant_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant admin required")
    if not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "explicit_confirmation_required", "actual_write": False},
        )
    filters = BulkFollowupFilter(**body.filters.model_dump())
    change = BulkFollowupChange(**body.change.model_dump())
    current = await preview_bulk_followup_change(
        session, tenant_id=principal.tenant_id, filters=filters, change=change
    )
    if current["preview_id"] != body.preview_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "bulk_preview_stale", "actual_write": False,
                "current_preview_id": current["preview_id"],
            },
        )
    results: list[dict[str, Any]] = []
    for item in current["items"]:
        if not item["will_change"]:
            results.append({
                "case_id": item["case_id"], "status": "skipped",
                "reason": item["skip_reason"], "actual_write": False,
            })
            continue
        command = UpdateCaseFollowupPolicy(
            command_id=f"bulk-policy:{body.source_turn_id}:{item['case_id']}",
            tenant_id=principal.tenant_id, case_id=item["case_id"],
            assigned_user_id=item["assigned_user_id"],
            expected_version=int(item["before"]["version"]),
            policy_source="bulk_assignment", cadence_type=change.cadence_type,
            enabled=change.enabled,
            force_manual_override=change.force_manual_override,
            source_turn_id=body.source_turn_id,
            idempotency_key=f"{body.idempotency_key}:{item['case_id']}",
            custom_interval_days=change.custom_interval_days,
            hearing_reminders_enabled=change.hearing_reminders_enabled,
            stage_transition_enabled=change.stage_transition_enabled,
            node_transition_enabled=change.node_transition_enabled,
        )
        receipt = await apply_case_followup_policy_command(
            session, command, actor_user_id=principal.user_id,
            actor_is_tenant_admin=True,
        )
        await session.commit()
        results.append({
            "case_id": item["case_id"], "status": receipt.status,
            "reason": receipt.error_code or "", "actual_write": receipt.actual_write,
            "receipt_id": str(receipt.receipt_id),
        })
    return {
        "preview_id": body.preview_id,
        "matched_count": current["matched_count"],
        "executed_count": sum(item["status"] == "executed" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "skipped_count": sum(item["status"] == "skipped" for item in results),
        "results": results,
    }


@router.get("/api/phase2/parties")
async def phase2_party_search(
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
    query: Annotated[str, Query(max_length=256)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    return {
        "tenant_id": principal.tenant_id,
        "items": await search_phase2_parties(
            session,
            tenant_id=principal.tenant_id,
            query=query,
            limit=limit,
            allowed_case_ids=scope.allowed_case_ids,
        ),
    }


@router.get("/api/phase2/parties/{party_id}")
async def phase2_party_detail(
    party_id: str,
    runtime: Runtime,
    principal: Principal,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_phase2_live_read(runtime, principal, settings)
    scope = await _load_phase2_principal_scope(runtime, principal, session)
    try:
        return await load_phase2_party_detail(
            session,
            tenant_id=principal.tenant_id,
            party_id=party_id,
            allowed_case_ids=scope.allowed_case_ids,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _require_phase2_live_read(
    runtime: LegalOpsRuntime,
    principal: SandboxPrincipal,
    settings: Settings,
) -> None:
    allowed_tenants = parse_tenant_allowlist(settings.agent2_business_tenant_ids)
    live_authorized = runtime.mode == "sandbox_live" and principal.tenant_id == settings.legal_ops_live_tenant_id
    if not live_authorized and (
        not settings.agent2_business_phase2_enabled or principal.tenant_id not in allowed_tenants
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def _load_phase2_principal_scope(
    runtime: LegalOpsRuntime,
    principal: SandboxPrincipal,
    session: AsyncSession,
) -> LivePrincipalScope:
    if runtime.mode != "sandbox_live":
        return LivePrincipalScope(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            allowed_case_ids=None,
        )
    try:
        return await load_live_principal_scope(session, principal)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


def _case_owner_write_context(
    principal: SandboxPrincipal,
    scope: LivePrincipalScope,
    operation_id: str,
) -> BusinessCommandContext:
    try:
        return build_case_owner_context(
            principal=principal,
            scope=scope,
            operation_id=operation_id,
            occurred_at=datetime.now(timezone.utc),
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


async def _assert_progress_belongs_to_case(
    session: AsyncSession,
    *,
    tenant_id: str,
    case_id: str,
    progress_id: str,
) -> None:
    try:
        case_uuid = UUID(case_id)
        progress_uuid = UUID(progress_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="案件进展不存在") from exc
    exists = await session.scalar(
        select(CaseProgress.progress_id).where(
            CaseProgress.tenant_id == tenant_id,
            CaseProgress.case_id == case_uuid,
            CaseProgress.progress_id == progress_uuid,
        )
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="案件进展不存在")


async def _case_write_response(
    *,
    result: LegalOpsWriteResult,
    session: AsyncSession,
    principal: SandboxPrincipal,
    scope: LivePrincipalScope,
    case_id: str,
) -> dict[str, Any]:
    if not result.succeeded:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": result.reason_code, "actual_write": False},
        )
    payload = result.user_payload()
    payload["case"] = await load_case_detail_workspace(
        session,
        tenant_id=principal.tenant_id,
        case_id=case_id,
        allowed_case_ids=scope.allowed_case_ids,
        can_manage_followup=principal.has_role("tenant_admin", "system_admin"),
        editable_actor_user_id=(scope.user_id if scope.binding is not None else ""),
    )
    return payload


@router.post("/api/admin/reset")
def reset_sandbox(
    runtime: Runtime,
    principal: Principal,
    confirmation: Annotated[str | None, Header(alias="X-Legal-Ops-Reset-Confirm")] = None,
) -> dict[str, Any]:
    _require_demo(runtime)
    if not principal.has_role("tenant_admin", "sandbox_admin", "system_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="sandbox administrator role required")
    seed = build_phase0_seed(runtime.seed_manifest)
    expected = seed["metadata"]["seed_id"]
    if confirmation != expected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"explicit reset confirmation required: {expected}",
        )
    try:
        result = runtime.repository.reset_tenant(seed, principal.tenant_id)
    except (FileNotFoundError, LookupError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {
        "reset": result,
        "verification": runtime.repository.verify_tenant(principal.tenant_id),
    }
