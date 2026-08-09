from __future__ import annotations

import asyncio
import io
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.legal_ops.api import LegalOpsRuntime, get_runtime
from app.legal_ops.auth import SandboxPrincipal
from app.legal_ops_data_intake.case_table_releases import (
    CaseTableReleaseError,
    CaseTableReleaseService,
)
from app.legal_ops_data_intake.permissions import IntakeAccess, resolve_intake_access
from app.legal_ops_data_intake.service import DataIntakeService

STATIC_DIR = Path(__file__).with_name("static")
router = APIRouter(prefix="/legal-ops/data-intake", tags=["legal-ops-data-intake"])


@dataclass(frozen=True)
class IntakeContext:
    service: DataIntakeService
    case_tables: CaseTableReleaseService
    access: IntakeAccess


def _runtime_enabled(
    runtime: Annotated[LegalOpsRuntime, Depends(get_runtime)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LegalOpsRuntime:
    if not settings.legal_ops_data_intake_enabled or runtime.mode != "sandbox_live":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return runtime


async def _principal(
    runtime: Annotated[LegalOpsRuntime, Depends(_runtime_enabled)],
    token: Annotated[str | None, Header(alias="X-Legal-Ops-Token")] = None,
) -> SandboxPrincipal:
    try:
        return runtime.principals.authenticate(token or "")
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "login_required", "message": "登录凭证无效或已失效"},
        ) from exc


async def _context(
    request: Request,
    runtime: Annotated[LegalOpsRuntime, Depends(_runtime_enabled)],
    principal: Annotated[SandboxPrincipal, Depends(_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> IntakeContext:
    if (
        not settings.legal_ops_live_tenant_id
        or principal.tenant_id != settings.legal_ops_live_tenant_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "tenant_not_enabled",
                "message": "当前租户尚未启用数据接入中心",
            },
        )
    access = await resolve_intake_access(
        session,
        principal,
        live_mode=runtime.mode == "sandbox_live",
    )
    # Authorization reads must not leak an implicit transaction into a later
    # atomic publish operation.
    await session.rollback()
    llm_client = getattr(request.app.state, "llm_client", None)
    service = DataIntakeService(
        session,
        settings,
        tenant_id=access.tenant_id,
        actor_user_id=access.user_id,
        llm_client=llm_client,
        code_version=os.getenv("APP_COMMIT_SHA", "workspace"),
    )
    case_tables = CaseTableReleaseService(settings, actor_user_id=access.user_id)
    return IntakeContext(service, case_tables, access)


Context = Annotated[IntakeContext, Depends(_context)]


class PeriodBody(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    period_type: Literal["month", "quarter", "half_year", "year", "custom"]
    starts_on: date
    ends_on: date


class RuleConfirmationBody(BaseModel):
    understanding_hash: str = Field(min_length=64, max_length=64)
    confirmed: bool
    assignment_confirmed: bool = False


class RuleScopeEditBody(BaseModel):
    scope_name: str = Field(min_length=1, max_length=256)
    scope_key: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=1000)


class RuleTargetEditBody(BaseModel):
    metric_name: str = Field(min_length=1, max_length=256)
    scope_name: str = Field(default="", max_length=256)
    target_value: str = Field(default="", max_length=128)
    unit: str = Field(default="", max_length=64)
    effective_period: str = Field(default="", max_length=256)
    comparison: Literal["", "at_least", "at_most", "equal"] = ""


class RuleIssueResolutionBody(BaseModel):
    issue: str = Field(min_length=1, max_length=2000)
    response: str = Field(min_length=1, max_length=4000)


class RuleDraftEditBody(BaseModel):
    expected_understanding_hash: str = Field(min_length=64, max_length=64)
    business_scope_name: str = Field(default="", max_length=256)
    rule_summary: str = Field(default="", max_length=4000)
    applicable_period: str = Field(default="", max_length=256)
    applicability_scopes: list[RuleScopeEditBody] = Field(
        default_factory=list,
        max_length=64,
    )
    target_versions: list[RuleTargetEditBody] = Field(
        default_factory=list,
        max_length=512,
    )
    issue_resolutions: list[RuleIssueResolutionBody] = Field(
        default_factory=list,
        max_length=128,
    )


class RuleAmendmentProposalBody(BaseModel):
    expected_understanding_hash: str = Field(min_length=64, max_length=64)
    instruction: str = Field(min_length=1, max_length=8000)
    issue_resolutions: list[RuleIssueResolutionBody] = Field(
        default_factory=list,
        max_length=128,
    )


class RuleAmendmentApplyBody(BaseModel):
    proposal_hash: str = Field(min_length=64, max_length=64)
    confirmed: bool


class TrialBody(BaseModel):
    period_ref: str
    rule_ref: str = ""


class PerformanceReportBody(BaseModel):
    period_ref: str = Field(min_length=1, max_length=128)
    view: Literal["week", "month"]
    anchor_date: date
    scope_key: str = Field(default="", max_length=256)


class RuleSourceValidationBody(BaseModel):
    period_ref: str = Field(min_length=1, max_length=128)
    table_key: str = Field(min_length=1, max_length=128)
    column_mapping: dict[
        Annotated[str, Field(min_length=1, max_length=128)],
        Annotated[str, Field(min_length=1, max_length=512)],
    ] = Field(default_factory=dict, max_length=512)
    confirm_mapping: bool = False


class CaseProgressErrorResolutionBody(BaseModel):
    case_ref: str = Field(default="", max_length=64)
    procedure_node: str = Field(default="", max_length=128)
    reporter_ref: str = Field(default="", max_length=128)
    progress_date: str = Field(default="", max_length=32)
    content: str = Field(default="", max_length=20_000)
    next_plan: str = Field(default="", max_length=10_000)
    apply_same_node: bool = True
    apply_same_reporter: bool = True


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def intake_app(
    _runtime: Annotated[LegalOpsRuntime, Depends(_runtime_enabled)],
) -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@router.get("/assets/{asset}", include_in_schema=False)
def intake_asset(
    asset: Literal["app.js", "styles.css"],
    _runtime: Annotated[LegalOpsRuntime, Depends(_runtime_enabled)],
):
    media_type = "application/javascript" if asset.endswith(".js") else "text/css"
    return FileResponse(STATIC_DIR / asset, media_type=media_type)


@router.get("/api/shell")
async def shell(context: Context) -> dict:
    context.access.require("view")
    return {
        "title": "数据接入中心",
        "identity": context.access.payload(),
        "navigation": [
            {"code": "performance", "label": "绩效数据维护"},
            {"code": "case-daily", "label": "案件每日更新"},
            {"code": "batches", "label": "导入批次记录"},
            {"code": "errors", "label": "导入错误处理"},
        ],
        "safety_notice": "所有正式写入均经过暂存、预览和发布确认；不会写回ERP或发送消息。",
    }


@router.get("/api/dashboard")
async def dashboard(context: Context) -> dict:
    context.access.require("view")
    return await context.service.dashboard()


@router.get("/api/periods")
async def list_periods(context: Context) -> list[dict]:
    context.access.require("view")
    return await context.service.list_periods()


@router.post("/api/periods", status_code=201)
async def create_period(body: PeriodBody, context: Context) -> dict:
    context.access.require("performance_upload")
    return await context.service.create_period(
        label=body.label,
        period_type=body.period_type,
        starts_on=body.starts_on,
        ends_on=body.ends_on,
    )


@router.get("/api/rules")
async def list_rules(context: Context) -> list[dict]:
    context.access.require("view")
    return await context.service.list_rules()


@router.get("/api/rules/{rule_ref}")
async def rule_detail(rule_ref: str, context: Context) -> dict:
    context.access.require("view")
    return await context.service.rule_detail(rule_ref)


@router.post("/api/rules/upload", status_code=201)
async def upload_rule(
    context: Context,
    file: Annotated[UploadFile, File()],
    period_ref: Annotated[str, Form()] = "",
    business_scope_name: Annotated[str, Form(max_length=256)] = "",
) -> dict:
    context.access.require("performance_upload")
    content = await _read_upload(file, context.service.settings)
    return await context.service.upload_rule_package(
        content=content,
        filename=file.filename or "",
        period_ref=period_ref or None,
        business_scope_name=business_scope_name,
    )


@router.post("/api/rules/{rule_ref}/interpret")
async def interpret_rule(rule_ref: str, context: Context) -> dict:
    context.access.require("performance_upload")
    return await context.service.interpret_rule(rule_ref)


@router.post("/api/rules/{rule_ref}/working-copy", status_code=201)
async def create_rule_working_copy(rule_ref: str, context: Context) -> dict:
    context.access.require("performance_upload")
    return await context.service.create_rule_working_copy(rule_ref)


@router.get("/api/rules/{rule_ref}/amendments")
async def list_rule_amendments(rule_ref: str, context: Context) -> list[dict]:
    context.access.require("view")
    return await context.service.list_rule_amendments(rule_ref)


@router.post("/api/rules/{rule_ref}/amendments/propose", status_code=201)
async def propose_rule_amendment(
    rule_ref: str,
    body: RuleAmendmentProposalBody,
    context: Context,
) -> dict:
    context.access.require("performance_upload")
    return await context.service.propose_rule_amendment(
        rule_ref,
        expected_understanding_hash=body.expected_understanding_hash,
        instruction=body.instruction,
        issue_resolutions=[
            item.model_dump() for item in body.issue_resolutions
        ],
    )


@router.post(
    "/api/rules/{rule_ref}/amendments/{amendment_ref}/apply",
)
async def apply_rule_amendment(
    rule_ref: str,
    amendment_ref: str,
    body: RuleAmendmentApplyBody,
    context: Context,
) -> dict:
    context.access.require("performance_upload")
    return await context.service.apply_rule_amendment(
        rule_ref,
        amendment_ref,
        proposal_hash=body.proposal_hash,
        confirmed=body.confirmed,
    )


@router.put("/api/rules/{rule_ref}/draft")
async def save_rule_draft(
    rule_ref: str,
    body: RuleDraftEditBody,
    context: Context,
) -> dict:
    context.access.require("performance_upload")
    return await context.service.save_rule_draft(
        rule_ref,
        expected_understanding_hash=body.expected_understanding_hash,
        business_scope_name=body.business_scope_name,
        rule_summary=body.rule_summary,
        applicable_period=body.applicable_period,
        applicability_scopes=[
            item.model_dump() for item in body.applicability_scopes
        ],
        target_versions=[item.model_dump() for item in body.target_versions],
        issue_resolutions=[
            item.model_dump() for item in body.issue_resolutions
        ],
    )


@router.get("/api/rules/{rule_ref}/source-files")
async def list_rule_source_files(
    rule_ref: str,
    context: Context,
    period_ref: Annotated[str, Query(max_length=64)] = "",
) -> list[dict]:
    context.access.require("view")
    return await context.service.list_rule_source_files(
        rule_ref,
        period_ref=period_ref or None,
    )


@router.post("/api/rules/{rule_ref}/source-files/upload", status_code=201)
async def upload_rule_source_file(
    rule_ref: str,
    context: Context,
    file: Annotated[UploadFile, File()],
    business_label: Annotated[str, Form(max_length=256)] = "",
) -> dict:
    context.access.require("performance_upload")
    content = await _read_upload(file, context.service.settings)
    return await context.service.upload_rule_source_file(
        rule_ref,
        content=content,
        filename=file.filename or "",
        business_label=business_label,
    )


@router.post(
    "/api/rules/{rule_ref}/source-files/{source_file_ref}/abandon",
)
async def abandon_rule_source_file(
    rule_ref: str,
    source_file_ref: str,
    context: Context,
) -> dict:
    context.access.require("performance_upload")
    return await context.service.abandon_rule_source_file(
        rule_ref,
        source_file_ref,
    )


@router.post(
    "/api/rules/{rule_ref}/source-files/{source_file_ref}/validate",
)
async def validate_rule_source_file(
    rule_ref: str,
    source_file_ref: str,
    body: RuleSourceValidationBody,
    context: Context,
) -> dict:
    context.access.require("performance_upload")
    return await context.service.validate_rule_source_file(
        rule_ref,
        source_file_ref,
        period_ref=body.period_ref,
        table_key=body.table_key,
        column_mapping=body.column_mapping,
        confirm_mapping=body.confirm_mapping,
    )


@router.post("/api/rules/{rule_ref}/preview-calculation")
async def preview_rule_calculation(
    rule_ref: str,
    body: TrialBody,
    context: Context,
) -> dict:
    context.access.require("view")
    return await context.service.preview_rule_calculation(
        rule_ref,
        body.period_ref,
    )


@router.post("/api/rules/{rule_ref}/report-preview")
async def preview_performance_report(
    rule_ref: str,
    body: PerformanceReportBody,
    context: Context,
) -> dict:
    context.access.require("view")
    return await context.service.performance_report(
        rule_ref,
        body.period_ref,
        view=body.view,
        anchor_date=body.anchor_date,
        scope_key=body.scope_key,
    )


@router.get("/api/rules/{rule_ref}/report.xlsx")
async def export_performance_report_xlsx(
    rule_ref: str,
    context: Context,
    period_ref: Annotated[str, Query(min_length=1, max_length=128)],
    view: Annotated[Literal["week", "month"], Query()],
    anchor_date: Annotated[date, Query()],
    scope_key: Annotated[str, Query(max_length=256)] = "",
) -> StreamingResponse:
    context.access.require("view")
    content, filename = await context.service.export_performance_report(
        rule_ref,
        period_ref,
        view=view,
        anchor_date=anchor_date,
        scope_key=scope_key,
        file_type="xlsx",
    )
    return _download_response(
        content,
        filename,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/api/rules/{rule_ref}/report.docx")
async def export_performance_report_docx(
    rule_ref: str,
    context: Context,
    period_ref: Annotated[str, Query(min_length=1, max_length=128)],
    view: Annotated[Literal["week", "month"], Query()],
    anchor_date: Annotated[date, Query()],
    scope_key: Annotated[str, Query(max_length=256)] = "",
) -> StreamingResponse:
    context.access.require("view")
    content, filename = await context.service.export_performance_report(
        rule_ref,
        period_ref,
        view=view,
        anchor_date=anchor_date,
        scope_key=scope_key,
        file_type="docx",
    )
    return _download_response(
        content,
        filename,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.post("/api/rules/{rule_ref}/confirm")
async def confirm_rule(
    rule_ref: str,
    body: RuleConfirmationBody,
    context: Context,
) -> dict:
    context.access.require("publish")
    return await context.service.confirm_rule(
        rule_ref,
        understanding_hash=body.understanding_hash,
        confirmed=body.confirmed,
        assignment_confirmed=body.assignment_confirmed,
    )


@router.post("/api/performance/tables/upload", status_code=201)
async def upload_performance_table(
    context: Context,
    file: Annotated[UploadFile, File()],
    period_ref: Annotated[str, Form()],
    table_key: Annotated[str, Form()],
    rule_ref: Annotated[str, Form()] = "",
) -> dict:
    context.access.require("performance_upload")
    content = await _read_upload(file, context.service.settings)
    return await context.service.upload_performance_table(
        period_ref=period_ref,
        table_key=table_key,
        content=content,
        filename=file.filename or "",
        rule_ref=rule_ref or None,
    )


@router.post("/api/performance/tables/{batch_no}/publish")
async def publish_performance_table(batch_no: str, context: Context) -> dict:
    context.access.require("publish")
    return await context.service.publish_performance_table(batch_no)


@router.post("/api/calculations/trial", status_code=201)
async def trial_calculation(body: TrialBody, context: Context) -> dict:
    context.access.require("performance_upload")
    return await context.service.trial_calculate(
        body.period_ref,
        rule_ref=body.rule_ref,
    )


@router.get("/api/calculations/{calculation_ref}")
async def calculation_detail(calculation_ref: str, context: Context) -> dict:
    context.access.require("view")
    return await context.service.calculation_detail(calculation_ref)


@router.post("/api/calculations/{calculation_ref}/publish")
async def publish_calculation(calculation_ref: str, context: Context) -> dict:
    context.access.require("publish")
    return await context.service.publish_calculation(calculation_ref)


@router.post("/api/calculations/{calculation_ref}/abandon")
async def abandon_calculation(calculation_ref: str, context: Context) -> dict:
    context.access.require("performance_upload")
    return await context.service.abandon_calculation(calculation_ref)


@router.get("/api/calculations/{calculation_ref}/export")
async def export_calculation(
    calculation_ref: str, context: Context
) -> StreamingResponse:
    context.access.require("view")
    content = await context.service.export_calculation(calculation_ref)
    return _xlsx_response(content, f"绩效结果-{calculation_ref[:8]}.xlsx")


@router.get("/api/calculations/{calculation_ref}/errors.xlsx")
async def export_calculation_errors(
    calculation_ref: str, context: Context
) -> StreamingResponse:
    context.access.require("view")
    content = await context.service.export_calculation_errors(calculation_ref)
    return _xlsx_response(content, f"绩效异常-{calculation_ref[:8]}.xlsx")


@router.post("/api/case-master/upload", status_code=201)
async def upload_case_master(
    context: Context,
    file: Annotated[UploadFile, File()],
    import_mode: Annotated[Literal["full", "incremental"], Form()],
    source_system: Annotated[str, Form()] = "ERP",
    profile_key: Annotated[str, Form()] = "auto",
) -> dict:
    context.access.require("case_upload")
    content = await _read_upload(file, context.service.settings)
    return await context.service.upload_case_master(
        content=content,
        filename=file.filename or "",
        source_system=source_system,
        import_mode=import_mode,
        profile_key=profile_key,
    )


@router.post("/api/case-master/{batch_no}/publish")
async def publish_case_master(batch_no: str, context: Context) -> dict:
    context.access.require("publish")
    return await context.service.publish_case_master(batch_no)


@router.post("/api/case-daily-snapshot/upload", status_code=201)
async def upload_case_daily_snapshot(
    context: Context,
    file: Annotated[UploadFile, File()],
    snapshot_date: Annotated[str, Form()],
    profile_key: Annotated[str, Form()] = "auto",
) -> dict:
    context.access.require("case_upload")
    content = await _read_upload(file, context.service.settings)
    return await context.service.upload_case_daily_snapshot(
        content=content,
        filename=file.filename or "",
        snapshot_date=snapshot_date,
        profile_key=profile_key,
    )


@router.post("/api/case-daily-snapshot/{batch_no}/publish")
async def publish_case_daily_snapshot(batch_no: str, context: Context) -> dict:
    context.access.require("publish")
    return await context.service.publish_case_daily_snapshot(batch_no)
@router.get("/api/case-tables/status")
async def case_table_status(context: Context) -> dict:
    context.access.require("view")
    return await _case_table_call(context.case_tables.status)


@router.get("/api/case-tables/history")
async def case_table_history(
    context: Context,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict]:
    context.access.require("view")
    return await _case_table_call(context.case_tables.history, limit=limit)


@router.get("/api/case-tables/pending")
async def pending_case_tables(
    context: Context,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict]:
    context.access.require("view")
    return await _case_table_call(context.case_tables.pending, limit=limit)


@router.post("/api/case-tables/upload", status_code=201)
async def upload_case_table(
    context: Context,
    file: Annotated[UploadFile, File()],
    table_kind: Annotated[Literal["defendant", "plaintiff"], Form()],
) -> dict:
    context.access.require("case_upload")
    content = await _read_upload(file, context.service.settings)
    return await _case_table_call(
        context.case_tables.preview,
        content=content,
        filename=file.filename or "",
        table_kind=table_kind,
    )


@router.post("/api/case-tables/{batch_no}/publish")
async def publish_case_table(batch_no: str, context: Context) -> dict:
    context.access.require("publish")
    return await _case_table_call(context.case_tables.publish, batch_no)


@router.post("/api/case-tables/versions/{version_id}/restore")
async def restore_case_table_version(version_id: str, context: Context) -> dict:
    context.access.require("publish")
    return await _case_table_call(context.case_tables.restore, version_id)
@router.post("/api/case-progress/upload", status_code=201)
async def upload_case_progress(
    context: Context,
    file: Annotated[UploadFile, File()],
    source_system: Annotated[str, Form()] = "ERP",
    profile_key: Annotated[str, Form()] = "auto",
    snapshot_date: Annotated[str, Form()] = "",
    reporter_id: Annotated[str, Form()] = "",
) -> dict:
    context.access.require("case_upload")
    content = await _read_upload(file, context.service.settings)
    return await context.service.upload_case_progress(
        content=content,
        filename=file.filename or "",
        source_system=source_system,
        profile_key=profile_key,
        snapshot_date=snapshot_date,
        reporter_id=reporter_id,
    )


@router.post("/api/case-progress/{batch_no}/publish")
async def publish_case_progress(batch_no: str, context: Context) -> dict:
    context.access.require("publish")
    return await context.service.publish_case_progress(batch_no)


@router.get("/api/batches")
async def list_batches(
    context: Context,
    business_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    context.access.require("view")
    return await context.service.list_batches(
        business_type=business_type,
        limit=limit,
    )


@router.get("/api/batches/{batch_no}")
async def batch_detail(
    batch_no: str,
    context: Context,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    row_status: Annotated[
        Literal["valid", "warning", "error", "skipped"] | None,
        Query(),
    ] = None,
) -> dict:
    context.access.require("view")
    return await context.service.batch_detail(
        batch_no,
        offset=offset,
        limit=limit,
        row_status=row_status,
    )


@router.post("/api/batches/{batch_no}/abandon")
async def abandon_batch(batch_no: str, context: Context) -> dict:
    batch = await context.service.batch_detail(batch_no)
    capability = (
        "performance_upload"
        if batch["business_type"]
        in {"performance_rule_package", "performance_source_table"}
        else "case_upload"
    )
    capabilities = context.access.capabilities
    if not (capabilities.get(capability, False) or capabilities.get("publish", False)):
        context.access.require(capability)
    return await context.service.abandon_batch(batch_no)


@router.get("/api/batches/{batch_no}/errors.xlsx")
async def download_errors(batch_no: str, context: Context) -> StreamingResponse:
    context.access.require("view")
    content = await context.service.download_batch_errors(batch_no)
    return _xlsx_response(content, f"导入错误-{batch_no}.xlsx")


@router.get("/api/errors")
async def list_errors(
    context: Context,
    error_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    context.access.require("view")
    return await context.service.list_errors(error_type=error_type, limit=limit)


@router.get("/api/errors/{row_ref}/options")
async def error_resolution_options(row_ref: str, context: Context) -> dict:
    context.access.require("case_upload")
    return await context.service.error_resolution_options(row_ref)


@router.post("/api/errors/{row_ref}/resolve")
async def resolve_case_progress_error(
    row_ref: str,
    body: CaseProgressErrorResolutionBody,
    context: Context,
) -> dict:
    context.access.require("case_upload")
    return await context.service.resolve_case_progress_error(
        row_ref,
        case_ref=body.case_ref,
        procedure_node=body.procedure_node,
        reporter_ref=body.reporter_ref,
        progress_date=body.progress_date,
        content=body.content,
        next_plan=body.next_plan,
        apply_same_node=body.apply_same_node,
        apply_same_reporter=body.apply_same_reporter,
    )


@router.get("/api/templates/{template_type}")
async def download_template(
    template_type: Literal["performance", "case_master", "case_progress"],
    context: Context,
    table_key: Annotated[str, Query()] = "",
    period_ref: Annotated[str, Query()] = "",
    rule_ref: Annotated[str, Query()] = "",
) -> StreamingResponse:
    context.access.require("view")
    content = await context.service.template(
        template_type,
        table_key=table_key,
        period_ref=period_ref,
        rule_ref=rule_ref,
    )
    name = {
        "performance": f"绩效源表模板-{table_key or '未指定'}",
        "case_master": "案件主表模板",
        "case_progress": "案件进展模板",
    }[template_type]
    return _xlsx_response(content, f"{name}.xlsx")


async def _case_table_call(operation, /, *args, **kwargs):
    try:
        return await asyncio.to_thread(operation, *args, **kwargs)
    except CaseTableReleaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


async def _read_upload(file: UploadFile, settings: Settings) -> bytes:
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".xlsx", ".csv", ".zip", ".md"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unsupported_file_type",
                "message": (
                    "业务数据只支持 xlsx、csv；绩效规则支持 zip 技能包"
                    "或单个 SKILL.md。禁止 xlsm 宏文件"
                ),
            },
        )
    maximum = settings.legal_ops_data_intake_max_file_mb * 1024 * 1024
    content = await file.read(maximum + 1)
    await file.close()
    if len(content) > maximum:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "file_too_large",
                "message": f"文件不能超过{settings.legal_ops_data_intake_max_file_mb}MB",
            },
        )
    if not content:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_file", "message": "上传文件不能为空"},
        )
    return content


def _xlsx_response(content: bytes, filename: str) -> StreamingResponse:
    return _download_response(
        content,
        filename,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _download_response(
    content: bytes,
    filename: str,
    media_type: str,
) -> StreamingResponse:
    encoded = quote(filename)
    return StreamingResponse(
        io.BytesIO(content),
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )
