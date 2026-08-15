from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.agent2.daily_briefing_fact_query import (
    DailyBriefingFactAmbiguous,
    DailyBriefingFactNotFound,
    DailyBriefingFactQuery,
    DailyBriefingFactQueryRequest,
    SqlDailyBriefingFactRepository,
)
from app.agent2.report_insight_query import StructuredReportInsightQuery
from app.agent2.report_insights import (
    ReportInsightModule,
    SqlReportInsightRepository,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedReportSnapshot,
)
from app.agent2.tool_calling.contracts import (
    AddDailyItemsArgs,
    CompletePreviousPlanArgs,
    ConfirmReportArgs,
    CopyPreviousToTodayArgs,
    CorrectDailyReportDateArgs,
    DeleteDailyItemsArgs,
    EditDailyItemsArgs,
    MoveDailyItemsArgs,
    QueryDailyBriefingFactsArgs,
    QueryManagedDailyReportsArgs,
    QueryReportByDateArgs,
    QueryReportInsightsArgs,
    ReceiptStatus,
    RecordWeeklyPlanItemsAsTodayWorkArgs,
    RequestClearReportArgs,
)
from app.agent2.tool_calling.daily_report_date_correction import (
    SqlDailyReportDateCorrection,
)
from app.agent2.tool_calling.idempotency import build_write_idempotency_key
from app.agent2.tool_calling.production_handlers import ProductionHandlerRequest
from app.agent2.tool_calling.production_store import (
    ProductionContextStore,
    ProductionDateResolver,
    ToolCallCanaryClearPending,
    report_state_hash,
    trusted_snapshot_from_report,
)
from app.agent2.tool_calling.validation import BoundCall
from app.agent2.typed_daily_commands import TypedDailyCommand
from app.agent2.typed_daily_executor import (
    TypedDailyExecutionContext,
    build_typed_daily_snapshot,
    execute_typed_agent2_daily_commands,
    pending_confirmation_next_step,
)
from app.agent2.weekly_plan_models import WeeklyPlan
from app.agent2.weekly_plan_store import SqlWeeklyPlanStore
from app.legal_daily_dashboard.chat_query import (
    ManagedDailyQuery,
    ManagedDailyQueryAmbiguous,
    ManagedDailyQueryRequest,
)
from app.legal_daily_dashboard.domain import DashboardActor
from app.legal_daily_dashboard.service import DashboardNotFound
from app.legal_daily_dashboard.sql_repository import (
    SqlDashboardRepository,
)
from app.models import DailyReport, User
from app.repositories import acquire_daily_report_advisory_lock
from app.services.state_machine import assess_daily_report_completeness


class ProductionExecutionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WeeklyPlanReadPort(Protocol):
    async def load_plan(
        self,
        *,
        tenant_id: str,
        plan_id: str,
        owner_user_id: str,
        for_update: bool = False,
    ) -> WeeklyPlan | None: ...


@dataclass(frozen=True)
class ProductionHandlerOutcome:
    target_type: str
    target_id: str
    before_report: TrustedReportSnapshot | None
    after_report: TrustedReportSnapshot | None
    idempotency_key: str | None
    typed_receipt_ids: tuple[str, ...] = ()
    affected_item_ids: tuple[str, ...] = ()
    safe_user_facts: dict[str, Any] | None = None
    status_if_unchanged: ReceiptStatus = ReceiptStatus.NO_OP
    before_version: int | None = None
    after_version: int | None = None
    error_code: str | None = None


class ProductionDailyExecutor:
    """Translate validated Tool Calls into the existing typed daily executor."""

    def __init__(
        self,
        *,
        session: Any,
        user: User,
        context: TrustedContext,
        settings: object,
        bound_calls: dict[str, BoundCall],
        source_channel: str,
        source_text_hash: str,
        date_resolver: ProductionDateResolver,
        managed_daily_query: ManagedDailyQuery | None = None,
        daily_briefing_fact_query: DailyBriefingFactQuery | None = None,
        weekly_plan_store: WeeklyPlanReadPort | None = None,
    ) -> None:
        self._session = session
        self._user = user
        self._context = context
        self._settings = settings
        self._bound_calls = bound_calls
        self._source_channel = source_channel
        self._source_text_hash = source_text_hash
        self._date_resolver = date_resolver
        self._managed_daily_query = managed_daily_query or ManagedDailyQuery(
            SqlDashboardRepository(session)
        )
        self._daily_briefing_fact_query = (
            daily_briefing_fact_query
            or DailyBriefingFactQuery(
                SqlDailyBriefingFactRepository(session)
            )
        )
        self._context_store = ProductionContextStore(
            session,
            user=user,
            tenant_id=context.principal.tenant_id,
            settings=settings,
        )
        self._date_correction = SqlDailyReportDateCorrection(session)
        self._weekly_plan_store = weekly_plan_store or SqlWeeklyPlanStore(
            session
        )

    async def query_today_report(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        report_date = self._context.now.astimezone(
            ZoneInfo(self._context.principal.timezone)
        ).date()
        return await self._query(request, report_date)

    async def query_report_by_date(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, QueryReportByDateArgs)
        bound = self._bound(request)
        report_date = self._resolved_date(bound, "resolved_date")
        del arguments
        return await self._query(request, report_date)

    async def query_managed_daily_reports(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(
            request,
            QueryManagedDailyReportsArgs,
        )
        report_date = self._today()
        candidate_matches: bool | None = None
        if arguments.report_date_expression is not None:
            resolution = self._date_resolver.resolve(
                expression=arguments.report_date_expression,
                proposed_date=arguments.proposed_report_date,
                now=self._context.now,
                timezone=self._context.principal.timezone,
            )
            if resolution.resolved_date is None:
                return self._managed_daily_read_outcome(
                    request=request,
                    report_date=report_date,
                    status=ReceiptStatus.CLARIFICATION_REQUIRED,
                    error_code=(resolution.error_code or "DATE_EXPRESSION_UNRESOLVED"),
                    facts={
                        "clarification": {
                            "reason": "date_unresolved",
                        },
                    },
                )
            report_date = resolution.resolved_date
            candidate_matches = resolution.candidate_matches
        try:
            result = await self._managed_daily_query.execute(
                actor=DashboardActor(
                    tenant_id=self._managed_daily_data_tenant_id(),
                    user_id=str(self._context.principal.user_id),
                ),
                request=ManagedDailyQueryRequest(
                    view=arguments.view,
                    report_date=report_date,
                    member_name=arguments.member_name,
                    team_name=arguments.team_name,
                ),
                now=self._context.now,
            )
        except ManagedDailyQueryAmbiguous as exc:
            return self._managed_daily_read_outcome(
                request=request,
                report_date=report_date,
                status=ReceiptStatus.CLARIFICATION_REQUIRED,
                error_code="MANAGED_DAILY_TARGET_AMBIGUOUS",
                facts={
                    "clarification": {
                        "reason": "ambiguous_target",
                        "candidates": list(exc.candidates),
                    },
                },
            )
        except DashboardNotFound:
            return self._managed_daily_read_outcome(
                request=request,
                report_date=report_date,
                status=ReceiptStatus.BLOCKED,
                error_code="MANAGED_DAILY_TARGET_NOT_FOUND",
                facts={
                    "access": "not_found",
                },
            )
        facts: dict[str, Any] = {
            "managed_daily_query": result,
        }
        if candidate_matches is not None:
            facts["date_candidate_matches"] = candidate_matches
        return self._managed_daily_read_outcome(
            request=request,
            report_date=report_date,
            status=ReceiptStatus.SUCCESS,
            facts=facts,
        )

    async def query_daily_briefing_facts(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(
            request,
            QueryDailyBriefingFactsArgs,
        )
        report_date: date | None = None
        candidate_matches: bool | None = None
        if arguments.report_date_expression is not None:
            resolution = self._date_resolver.resolve(
                expression=arguments.report_date_expression,
                proposed_date=arguments.proposed_report_date,
                now=self._context.now,
                timezone=self._context.principal.timezone,
            )
            if resolution.resolved_date is None:
                return self._daily_briefing_fact_read_outcome(
                    request=request,
                    report_date=self._today(),
                    status=ReceiptStatus.CLARIFICATION_REQUIRED,
                    error_code=(
                        resolution.error_code
                        or "DATE_EXPRESSION_UNRESOLVED"
                    ),
                    facts={
                        "clarification": {
                            "reason": "briefing_date_unresolved",
                        },
                    },
                )
            report_date = resolution.resolved_date
            candidate_matches = resolution.candidate_matches
        if report_date is None:
            return self._daily_briefing_fact_read_outcome(
                request=request,
                report_date=self._today(),
                status=ReceiptStatus.CLARIFICATION_REQUIRED,
                error_code="DAILY_BRIEFING_DATE_REQUIRED",
                facts={
                    "clarification": {
                        "reason": "briefing_date_required",
                    },
                },
            )
        try:
            result = await self._daily_briefing_fact_query.execute(
                tenant_id=self._managed_daily_data_tenant_id(),
                actor_user_id=str(self._context.principal.user_id),
                request=DailyBriefingFactQueryRequest(
                    report_date=report_date,
                    view=arguments.view,
                    member_name=arguments.member_name,
                    recipient_name=arguments.recipient_name,
                    team_name=arguments.team_name,
                ),
                now=self._context.now,
                timezone=self._context.principal.timezone,
            )
        except DailyBriefingFactAmbiguous as exc:
            return self._daily_briefing_fact_read_outcome(
                request=request,
                report_date=report_date,
                status=ReceiptStatus.CLARIFICATION_REQUIRED,
                error_code="DAILY_BRIEFING_TARGET_AMBIGUOUS",
                facts={
                    "clarification": {
                        "reason": "ambiguous_target",
                        "candidates": list(exc.candidates),
                    },
                },
            )
        except DailyBriefingFactNotFound:
            return self._daily_briefing_fact_read_outcome(
                request=request,
                report_date=report_date,
                status=ReceiptStatus.BLOCKED,
                error_code="DAILY_BRIEFING_TARGET_NOT_FOUND",
                facts={
                    "access": "not_found",
                },
            )
        facts: dict[str, Any] = {
            "daily_briefing_facts": result,
        }
        if candidate_matches is not None:
            facts["date_candidate_matches"] = candidate_matches
        return self._daily_briefing_fact_read_outcome(
            request=request,
            report_date=report_date,
            status=ReceiptStatus.SUCCESS,
            facts=facts,
        )

    async def query_report_insights(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, QueryReportInsightsArgs)
        current_date = self._today()
        roster_tenant_id = str(
            getattr(
                self._settings,
                "legal_daily_dashboard_tenant_id",
                "",
            )
            or self._context.principal.tenant_id
        ).strip()
        query = StructuredReportInsightQuery(
            query=(
                f"{arguments.query_kind}:{arguments.scope_type}:"
                f"{arguments.scope_name}:{arguments.period_type}"
            ),
            query_kind=arguments.query_kind,
            scope_type=arguments.scope_type,
            scope_name=arguments.scope_name,
            period_type=arguments.period_type,
            status_filter=arguments.status_filter,
        )
        if query.query_kind == "submission_coverage":
            from app.agent2.context_pack import KnowledgeEvidenceFrame
            from app.agent2.report_insights import ReportInsightAnswer
            from app.agent2.submission_coverage_query import (
                SubmissionCoverageAmbiguous,
                SubmissionCoverageDataError,
                SubmissionCoverageNotFound,
                SubmissionCoverageQuery,
                SubmissionCoverageRequest,
            )

            try:
                coverage = await SubmissionCoverageQuery(
                    SqlDashboardRepository(self._session)
                ).execute(
                    SubmissionCoverageRequest(
                        tenant_id=roster_tenant_id,
                        scope_name=query.scope_name,
                        period_type=query.period_type,
                        current_date=current_date,
                        now=self._context.now,
                    )
                )
            except SubmissionCoverageAmbiguous:
                return self._report_insight_read_outcome(
                    request=request,
                    current_date=current_date,
                    status=ReceiptStatus.CLARIFICATION_REQUIRED,
                    error_code="SUBMISSION_COVERAGE_SCOPE_AMBIGUOUS",
                    facts={
                        "model_composition_allowed": False,
                        "response_text": "提交情况查询的部门范围不唯一，请明确具体部门。",
                    },
                )
            except SubmissionCoverageNotFound:
                return self._report_insight_read_outcome(
                    request=request,
                    current_date=current_date,
                    status=ReceiptStatus.BLOCKED,
                    error_code="SUBMISSION_COVERAGE_SCOPE_NOT_FOUND",
                    facts={
                        "model_composition_allowed": False,
                        "response_text": "没有找到对应的正式法务部门。",
                    },
                )
            except SubmissionCoverageDataError:
                return self._report_insight_read_outcome(
                    request=request,
                    current_date=current_date,
                    status=ReceiptStatus.BLOCKED,
                    error_code="SUBMISSION_COVERAGE_DATA_INVALID",
                    facts={
                        "model_composition_allowed": False,
                        "response_text": "提交责任数据存在冲突，暂时无法可靠统计。",
                    },
                )
            answer = ReportInsightAnswer(
                text="",
                evidence=KnowledgeEvidenceFrame(
                    source_type="daily_report_insight",
                    source_id=(
                        "daily_submission_coverage:"
                        f"{coverage.facts['scope_label']}:"
                        f"{coverage.facts['period_start']}:"
                        f"{coverage.facts['period_end']}"
                    ),
                    title=coverage.title,
                    summary=(
                        f"{coverage.facts['scope_label']} "
                        f"{coverage.facts['period_start']} 至 "
                        f"{coverage.facts['period_end']} 提交覆盖事实"
                    ),
                    facts=coverage.facts,
                    confidence=1.0,
                    freshness=coverage.freshness,
                ),
            )
        else:
            answer = await ReportInsightModule(
                SqlReportInsightRepository(
                    self._session,
                    roster_date=current_date,
                    roster_tenant_id=roster_tenant_id,
                )
            ).answer_query(
                query,
                requester=self._user,
                current_date=current_date,
            )
        if answer is None:
            return self._report_insight_read_outcome(
                request=request,
                current_date=current_date,
                status=ReceiptStatus.BLOCKED,
                error_code="REPORT_INSIGHT_QUERY_NOT_SUPPORTED",
                facts={
                    "model_composition_allowed": False,
                    "response_text": "这项日报查询暂时无法完成。",
                },
            )
        return self._report_insight_read_outcome(
            request=request,
            current_date=current_date,
            status=ReceiptStatus.SUCCESS,
            facts={
                "model_composition_allowed": True,
                "report_insight": {
                    "answer_text": answer.text,
                    "title": answer.evidence.title,
                    "facts": dict(answer.evidence.facts),
                    "freshness": answer.evidence.freshness,
                },
                "回复要求": (
                    "直接回答用户这一轮的问题；只使用 report_insight 中的事实，"
                    "保留人数、份数、日期、事项和闭环规则，不补造信息。若同一轮"
                    "查询了两个周期，应分别说明两个周期，不能遗漏其中一个。事项列表"
                    "只能使用一层连续编号，不要给事项再添加内部编号或嵌套编号；不要把"
                    "编号问题解释为用户偏好或记忆。若 facts.needs_time_scope=true，"
                    "只说明 unclosed_count、起止日期和三个范围选项，再自然询问用户要看"
                    "最近7天、最近30天还是全部历史；不要提日报份数或其他分类数量，"
                    "不得猜测或列出已暂缓返回的明细。若 unclosed_preview_truncated=true，"
                    "必须说清总数、当前仅展示前多少项、另有多少项未展开；不得把展示数"
                    "说成全部数量。若 facts.query_kind 是 team_work_summary 或"
                    " recent_work，必须说明 work_item_count，并覆盖 work_date_counts 中"
                    "每个有工作记录的日期及"
                    "对应数量；若 work_preview_truncated=true，还必须说明"
                    "work_preview_count、work_unshown_count 和未展开口径，不得把预览"
                    "说成全部工作。若 facts.query_kind=submission_coverage，所有分类"
                    "计数单位都是人次（一个人一天算一次），必须说明起止日期和"
                    "responsibility_data_complete；本周结果还必须说明 queried_at"
                    "所给的查询时点。责任待核不能说成未交，"
                    "not_yet_due 不能说成逾期，pending_confirmation 不能说成"
                    "已提交；expected_count、report_count、completed_count 和"
                    "pending_confirmation_count 必须分别说明，即使其中一项为 0；"
                    "report_count 为 0 也不能把 expected_count 说成 0。"
                    "并按 daily_breakdown 和"
                    "member_breakdown 保留逐日、逐人事实。"
                ),
            },
        )

    def _managed_daily_data_tenant_id(self) -> str:
        """Use the server-owned dashboard data scope, never a model argument."""

        configured = str(
            getattr(
                self._settings,
                "legal_daily_dashboard_tenant_id",
                "",
            )
            or ""
        ).strip()
        return configured or self._context.principal.tenant_id

    async def add_daily_items(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, AddDailyItemsArgs)
        bound = self._bound(request)
        report_date = self._resolved_date(bound, "resolved_date")
        is_trusted_retry = (
            arguments.date_selection == "trusted_failed_write"
        )
        if is_trusted_retry:
            # Use the same report-level lock as every normal Daily writer.
            # This also serializes the important "report did not exist"
            # state, for which a row lock alone cannot protect anything.
            await acquire_daily_report_advisory_lock(
                self._session,
                self._user.id,
                report_date,
            )
        before = (
            await self._snapshot(report_date, for_update=True)
            if is_trusted_retry
            else await self._snapshot(report_date)
        )
        if is_trusted_retry and report_state_hash(before) != str(
            bound.date_facts.get("retry_target_state_sha256") or ""
        ):
            raise ProductionExecutionError("DAILY_RETRY_TARGET_STALE")
        typed_before = await self._typed_snapshot(report_date)
        existing = {
            field: set(getattr(typed_before, field))
            for field in ("today_work", "problems", "tomorrow_plan")
        }
        additions: dict[str, list[str]] = {
            "today_work": [],
            "problems": [],
            "tomorrow_plan": [],
        }
        for item in arguments.items:
            if (
                item.content in existing[item.field]
                or item.content in additions[item.field]
            ):
                continue
            additions[item.field].append(item.content)
        acknowledged_empty_fields = set(arguments.acknowledged_empty_fields)
        conflicting_empty_fields = {
            field_name
            for field_name in acknowledged_empty_fields
            if existing[field_name]
        }
        if conflicting_empty_fields:
            raise ProductionExecutionError(
                "EXPLICIT_EMPTY_FIELD_CONTAINS_ITEMS"
            )
        commands: list[TypedDailyCommand] = []
        next_version = typed_before.version
        for field in ("today_work", "problems", "tomorrow_plan"):
            if (
                field in acknowledged_empty_fields
                and field not in typed_before.acknowledged_empty_fields
            ):
                commands.append(
                    self._command(
                        request,
                        ordinal=len(commands),
                        command_type="acknowledge_empty_section",
                        report_id=typed_before.report_id,
                        report_version=next_version,
                        patch={"field": field},
                    )
                )
                next_version += 1
            values = additions[field]
            if not values:
                continue
            commands.append(
                self._command(
                    request,
                    ordinal=len(commands),
                    command_type="append_item",
                    report_id=typed_before.report_id,
                    report_version=next_version,
                    patch={"field": field, "items": values},
                )
            )
            next_version += 1
        if arguments.submit_after_write and typed_before.status != "completed":
            commands.append(
                self._command(
                    request,
                    ordinal=len(commands),
                    command_type="submit_report",
                    report_id=typed_before.report_id,
                    report_version=next_version,
                    patch={},
                )
            )
        typed_receipts = await self._execute_typed(
            report_date,
            commands,
            allow_completed_content_mutation=True,
        )
        after = await self._snapshot(report_date)
        return self._outcome(
            request,
            before=before,
            after=after,
            typed_receipt_ids=typed_receipts,
        )

    async def edit_daily_items(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, EditDailyItemsArgs)
        bound = self._bound(request)
        report = self._required_bound_report(bound)
        before = await self._snapshot(report.report_date)
        live = await self._typed_snapshot(report.report_date)
        self._require_same_report(report, live.report_id)
        all_contents = {item.content for item in report.items}
        target_contents = {
            report.item(item_id).content
            for item_id in arguments.target_item_ids
            if report.item(item_id) is not None
        }
        if (
            arguments.replacement in all_contents
            and arguments.replacement not in target_contents
        ):
            raise ProductionExecutionError("DUPLICATE_ITEM_CONTENT")
        commands = tuple(
            self._command(
                request,
                ordinal=index,
                command_type="edit_item",
                report_id=report.report_id,
                report_version=live.version + index,
                target_item_ids=(item_id,),
                patch={"replacement": arguments.replacement},
            )
            for index, item_id in enumerate(arguments.target_item_ids)
        )
        typed_receipts = await self._execute_typed(
            report.report_date,
            commands,
            allow_completed_content_mutation=True,
        )
        after = await self._snapshot(report.report_date)
        return self._outcome(
            request,
            before=before,
            after=after,
            typed_receipt_ids=typed_receipts,
        )

    async def delete_daily_items(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, DeleteDailyItemsArgs)
        bound = self._bound(request)
        report = self._required_bound_report(bound)
        before = await self._snapshot(report.report_date)
        live = await self._typed_snapshot(report.report_date)
        self._require_same_report(report, live.report_id)
        commands = tuple(
            self._command(
                request,
                ordinal=index,
                command_type="delete_item",
                report_id=report.report_id,
                report_version=live.version + index,
                target_item_ids=(item_id,),
                patch={},
            )
            for index, item_id in enumerate(arguments.target_item_ids)
        )
        typed_receipts = await self._execute_typed(
            report.report_date,
            commands,
            allow_completed_content_mutation=True,
        )
        after = await self._snapshot(report.report_date)
        return self._outcome(
            request,
            before=before,
            after=after,
            typed_receipt_ids=typed_receipts,
        )

    async def move_daily_items(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, MoveDailyItemsArgs)
        bound = self._bound(request)
        report = self._required_bound_report(bound)
        before = await self._snapshot(report.report_date)
        live = await self._typed_snapshot(report.report_date)
        self._require_same_report(report, live.report_id)
        command = self._command(
            request,
            ordinal=0,
            command_type="move_item",
            report_id=report.report_id,
            report_version=live.version,
            target_item_ids=arguments.target_item_ids,
            patch={
                "source_field": arguments.source_field,
                "target_field": arguments.target_field,
            },
        )
        typed_receipts = await self._execute_typed(
            report.report_date,
            (command,),
            allow_completed_content_mutation=True,
        )
        after = await self._snapshot(report.report_date)
        return self._outcome(
            request,
            before=before,
            after=after,
            typed_receipt_ids=typed_receipts,
        )

    async def copy_previous_to_today(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        self._arguments(request, CopyPreviousToTodayArgs)
        bound = self._bound(request)
        source = self._required_source_report(bound)
        target_date = self._today()
        before = await self._snapshot(target_date)
        target = await self._typed_snapshot(target_date)
        sections = self._sections(source)
        command = self._command(
            request,
            ordinal=0,
            command_type="copy_report",
            report_id=target.report_id,
            report_version=target.version,
            patch={
                "sections": sections,
                "source_report_date": source.report_date.isoformat(),
                "source_report_id": str(source.report_id),
            },
        )
        typed_receipts = await self._execute_typed(target_date, (command,))
        after = await self._snapshot(target_date)
        return self._outcome(
            request,
            before=before,
            after=after,
            typed_receipt_ids=typed_receipts,
        )

    async def correct_daily_report_date(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(
            request,
            CorrectDailyReportDateArgs,
        )
        bound = self._bound(request)
        source = self._required_source_report(bound)
        source_date = self._resolved_date(bound, "resolved_source_date")
        target_date = self._resolved_date(bound, "resolved_target_date")
        if source.report_date not in {source_date, target_date}:
            raise ProductionExecutionError("SOURCE_REPORT_DATE_MISMATCH")
        before = (
            source
            if source.report_date == source_date
            else await self._snapshot(source_date)
        )
        result = await self._date_correction.execute(
            user=self._user,
            tenant_id=self._context.principal.tenant_id,
            source_report_id=source.report_id,
            expected_version=source.version,
            source_date=source_date,
            target_date=target_date,
            acknowledged_empty_fields=(
                arguments.acknowledged_empty_fields
            ),
            submit_after_correction=(
                arguments.submit_after_correction
            ),
            idempotency_key=self._tool_idempotency_key(request),
            source_message_id=(
                self._context.principal.source_message_id
            ),
            now=self._context.now,
        )
        after = await self._snapshot(target_date)
        if result.status in {"blocked", "clarification_required"}:
            return ProductionHandlerOutcome(
                target_type="daily_report",
                target_id=str(source.report_id),
                before_report=before or source,
                after_report=before or source,
                idempotency_key=self._tool_idempotency_key(request),
                safe_user_facts={
                    "actual_write": False,
                    "source_report_date": source_date.isoformat(),
                    "target_report_date": target_date.isoformat(),
                    "clarification": {
                        "reason": result.error_code,
                    },
                },
                status_if_unchanged=(
                    ReceiptStatus.CLARIFICATION_REQUIRED
                    if result.status == "clarification_required"
                    else ReceiptStatus.BLOCKED
                ),
                before_version=result.before_version,
                after_version=result.after_version,
                error_code=result.error_code,
            )
        effective = after or source
        return ProductionHandlerOutcome(
            target_type="daily_report",
            target_id=str(effective.report_id),
            before_report=before,
            after_report=effective,
            idempotency_key=self._tool_idempotency_key(request),
            typed_receipt_ids=result.typed_receipt_ids,
            safe_user_facts={
                "actual_write": result.status == "success",
                "source_report_date": source_date.isoformat(),
                "target_report_date": target_date.isoformat(),
                "report_date": target_date.isoformat(),
                "report_status": effective.status,
                "acknowledged_empty_fields": sorted(
                    effective.acknowledged_empty_fields
                ),
            },
            status_if_unchanged=ReceiptStatus.NO_OP,
            before_version=result.before_version,
            after_version=result.after_version,
        )

    async def complete_previous_plan(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(request, CompletePreviousPlanArgs)
        bound = self._bound(request)
        source = self._required_source_report(bound)
        target_date = self._today()
        before = await self._snapshot(target_date)
        target = await self._typed_snapshot(target_date)
        existing = set(target.today_work)
        values = [
            source.item(item_id).content
            for item_id in arguments.target_item_ids
            if source.item(item_id) is not None
            and source.item(item_id).content not in existing
        ]
        commands = (
            (
                self._command(
                    request,
                    ordinal=0,
                    command_type="append_item",
                    report_id=target.report_id,
                    report_version=target.version,
                    patch={"field": "today_work", "items": values},
                ),
            )
            if values
            else ()
        )
        typed_receipts = await self._execute_typed(target_date, commands)
        after = await self._snapshot(target_date)
        return self._outcome(
            request,
            before=before,
            after=after,
            typed_receipt_ids=typed_receipts,
        )

    async def record_weekly_plan_items_as_today_work(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        arguments = self._arguments(
            request,
            RecordWeeklyPlanItemsAsTodayWorkArgs,
        )
        bound = self._bound(request)
        plan = bound.weekly_plan
        if plan is None:
            raise ProductionExecutionError("WEEKLY_PLAN_CONTEXT_REQUIRED")
        principal = self._context.principal
        if (
            principal.conversation_kind != "direct"
            or str(getattr(self._user, "id", ""))
            != str(principal.user_id)
            or plan.tenant_id != principal.tenant_id
            or plan.owner_user_id != str(principal.user_id)
            or str(arguments.plan_id) != plan.plan_id
        ):
            raise ProductionExecutionError("WEEKLY_PLAN_SCOPE_MISMATCH")
        try:
            live_plan = await self._weekly_plan_store.load_plan(
                tenant_id=principal.tenant_id,
                plan_id=plan.plan_id,
                owner_user_id=str(principal.user_id),
                for_update=True,
            )
        except (TypeError, ValueError) as exc:
            raise ProductionExecutionError(
                "WEEKLY_PLAN_LIVE_READ_FAILED"
            ) from exc
        if live_plan is None:
            raise ProductionExecutionError("WEEKLY_PLAN_NOT_FOUND")
        if (
            live_plan.plan_id != plan.plan_id
            or live_plan.tenant_id != principal.tenant_id
            or live_plan.owner_user_id != str(principal.user_id)
            or live_plan.target_week_start != plan.target_week_start
        ):
            raise ProductionExecutionError("WEEKLY_PLAN_SCOPE_MISMATCH")
        if (
            live_plan.version != arguments.expected_version
            or live_plan.version != plan.version
        ):
            raise ProductionExecutionError("STALE_WEEKLY_PLAN_VERSION")

        trusted_items = {
            item.item_id: item
            for day in plan.days
            for item in day.items
        }
        live_items = {
            item.item_id: item
            for day in live_plan.days
            for item in day.items
        }
        if any(
            item_id not in trusted_items or item_id not in live_items
            for item_id in arguments.target_item_ids
        ):
            raise ProductionExecutionError(
                "UNTRUSTED_WEEKLY_PLAN_ITEM_ID"
            )
        if any(
            live_items[item_id].original_text
            != trusted_items[item_id].original_text
            for item_id in arguments.target_item_ids
        ):
            raise ProductionExecutionError("WEEKLY_PLAN_CONTEXT_MISMATCH")

        target_date = self._today()
        before = await self._snapshot(target_date)
        target = await self._typed_snapshot(target_date)
        seen = set(target.today_work)
        values: list[str] = []
        for item_id in arguments.target_item_ids:
            original_text = live_items[item_id].original_text
            if original_text in seen:
                continue
            seen.add(original_text)
            values.append(original_text)
        commands = (
            (
                self._command(
                    request,
                    ordinal=0,
                    command_type="append_item",
                    report_id=target.report_id,
                    report_version=target.version,
                    patch={"field": "today_work", "items": values},
                ),
            )
            if values
            else ()
        )
        typed_receipts = await self._execute_typed(
            target_date,
            commands,
            allow_completed_content_mutation=True,
        )
        after = await self._snapshot(target_date)
        return self._outcome(
            request,
            before=before,
            after=after,
            typed_receipt_ids=typed_receipts,
        )

    async def confirm_report(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        self._arguments(request, ConfirmReportArgs)
        bound = self._bound(request)
        report = self._required_bound_report(bound)
        before = await self._snapshot(report.report_date)
        live = await self._typed_snapshot(report.report_date)
        self._require_same_report(report, live.report_id)
        completion_facts = _daily_completion_facts(
            today_work=live.today_work,
            problems=live.problems,
            tomorrow_plan=live.tomorrow_plan,
            acknowledged_empty_fields=live.acknowledged_empty_fields,
            report_status=live.status,
            persisted_report_available=before is not None,
        )
        if completion_facts["missing_sections"]:
            return ProductionHandlerOutcome(
                target_type="daily_report",
                target_id=str(report.report_id),
                before_report=before,
                after_report=before,
                idempotency_key=self._tool_idempotency_key(request),
                safe_user_facts={
                    "actual_write": False,
                    "report_date": report.report_date.isoformat(),
                    "report_status": live.status,
                    **completion_facts,
                },
                status_if_unchanged=ReceiptStatus.CLARIFICATION_REQUIRED,
                error_code="REPORT_INCOMPLETE",
            )
        command = self._command(
            request,
            ordinal=0,
            command_type="submit_report",
            report_id=report.report_id,
            report_version=live.version,
            patch={},
        )
        typed_receipts = await self._execute_typed(
            report.report_date,
            (command,),
        )
        after = await self._snapshot(report.report_date)
        return self._outcome(
            request,
            before=before,
            after=after,
            typed_receipt_ids=typed_receipts,
        )

    async def request_clear_report(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        self._arguments(request, RequestClearReportArgs)
        bound = self._bound(request)
        report = self._required_bound_report(bound)
        ttl = request.pending_ttl_seconds
        if ttl is None or ttl <= 0:
            raise ProductionExecutionError("CLEAR_PENDING_TTL_REQUIRED")
        active = list(
            (
                await self._session.scalars(
                    select(ToolCallCanaryClearPending).where(
                        ToolCallCanaryClearPending.namespace == CANARY_STATE_NAMESPACE,
                        ToolCallCanaryClearPending.tenant_id
                        == self._context.principal.tenant_id,
                        ToolCallCanaryClearPending.user_id
                        == str(self._context.principal.user_id),
                        ToolCallCanaryClearPending.conversation_id
                        == self._context.principal.conversation_id,
                        ToolCallCanaryClearPending.consumed_at.is_(None),
                        ToolCallCanaryClearPending.expires_at > self._context.now,
                    )
                )
            ).all()
        )
        if len(active) > 1:
            raise ProductionExecutionError("MULTIPLE_CLEAR_PENDINGS")
        key = self._tool_idempotency_key(request)
        if active:
            pending = active[0]
            if (
                pending.report_id != report.report_id
                or pending.report_version != report.version
                or pending.report_state_hash != report_state_hash(report)
            ):
                raise ProductionExecutionError("CLEAR_PENDING_CONFLICT")
        else:
            pending = ToolCallCanaryClearPending(
                pending_id=uuid5(
                    NAMESPACE_URL,
                    f"agent2-tool-call-clear-pending:{key}",
                ),
                namespace=CANARY_STATE_NAMESPACE,
                tenant_id=self._context.principal.tenant_id,
                user_id=str(self._context.principal.user_id),
                conversation_id=self._context.principal.conversation_id,
                report_id=report.report_id,
                report_version=report.version,
                report_state_hash=report_state_hash(report),
                target_date=report.report_date,
                expires_at=self._context.now + timedelta(seconds=ttl),
                source_message_id=self._context.principal.source_message_id,
            )
            self._session.add(pending)
            await self._session.flush()
        facts = {
            "actual_write": True,
            "pending_created": True,
            "target_date": report.report_date.isoformat(),
            "confirmation_required": True,
        }
        return ProductionHandlerOutcome(
            target_type="clear_pending",
            target_id=str(pending.pending_id),
            before_report=report,
            after_report=report,
            idempotency_key=key,
            safe_user_facts=facts,
            status_if_unchanged=ReceiptStatus.SUCCESS,
        )

    async def confirm_clear_report(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        bound = self._bound(request)
        report = self._required_bound_report(bound)
        rows = list(
            (
                await self._session.scalars(
                    select(ToolCallCanaryClearPending)
                    .where(
                        ToolCallCanaryClearPending.namespace == CANARY_STATE_NAMESPACE,
                        ToolCallCanaryClearPending.tenant_id
                        == self._context.principal.tenant_id,
                        ToolCallCanaryClearPending.user_id
                        == str(self._context.principal.user_id),
                        ToolCallCanaryClearPending.conversation_id
                        == self._context.principal.conversation_id,
                        ToolCallCanaryClearPending.consumed_at.is_(None),
                    )
                    .with_for_update()
                )
            ).all()
        )
        if len(rows) != 1:
            raise ProductionExecutionError("CLEAR_PENDING_REQUIRED")
        pending = rows[0]
        if (
            pending.expires_at <= self._context.now
            or pending.source_message_id == self._context.principal.source_message_id
        ):
            raise ProductionExecutionError("CLEAR_PENDING_EXPIRED")
        if (
            pending.report_id != report.report_id
            or pending.report_version != report.version
            or pending.target_date != report.report_date
            or pending.report_state_hash != report_state_hash(report)
        ):
            raise ProductionExecutionError("CLEAR_PENDING_STALE")
        before = await self._snapshot(report.report_date)
        live = await self._typed_snapshot(report.report_date)
        self._require_same_report(report, live.report_id)
        commands: list[TypedDailyCommand] = []
        next_version = live.version
        if live.status == "completed":
            commands.append(
                self._command(
                    request,
                    ordinal=0,
                    command_type="reopen_report",
                    report_id=report.report_id,
                    report_version=next_version,
                    patch={},
                )
            )
            next_version += 1
        commands.append(
            self._command(
                request,
                ordinal=len(commands),
                command_type="clear_report",
                report_id=report.report_id,
                report_version=next_version,
                patch={"field": "all"},
            )
        )
        typed_receipts = await self._execute_typed(
            report.report_date,
            tuple(commands),
        )
        pending.consumed_at = self._context.now
        await self._session.flush()
        after = await self._snapshot(report.report_date)
        outcome = self._outcome(
            request,
            before=before,
            after=after,
            typed_receipt_ids=typed_receipts,
        )
        return ProductionHandlerOutcome(
            **{
                **outcome.__dict__,
                "safe_user_facts": {
                    **(outcome.safe_user_facts or {}),
                    "pending_consumed": True,
                },
            }
        )

    async def _query(
        self,
        request: ProductionHandlerRequest,
        report_date,
    ) -> ProductionHandlerOutcome:
        before = await self._snapshot(report_date)
        typed = await self._typed_snapshot(report_date)
        command = self._command(
            request,
            ordinal=0,
            command_type="query_report",
            report_id=typed.report_id,
            report_version=typed.version,
            patch={"report_date": report_date.isoformat()},
        )
        typed_receipts = await self._execute_typed(report_date, (command,))
        after = await self._snapshot(report_date)
        facts = {
            "actual_write": False,
            "report_found": after is not None,
            "report_snapshot": after.safe_snapshot() if after is not None else None,
            "report_date": report_date.isoformat(),
        }
        return ProductionHandlerOutcome(
            target_type="daily_report",
            target_id=str(typed.report_id),
            before_report=before,
            after_report=after,
            idempotency_key=None,
            typed_receipt_ids=typed_receipts,
            safe_user_facts=facts,
            status_if_unchanged=(
                ReceiptStatus.SUCCESS if after is not None else ReceiptStatus.NO_OP
            ),
        )

    def _managed_daily_read_outcome(
        self,
        *,
        request: ProductionHandlerRequest,
        report_date,
        status: ReceiptStatus,
        facts: dict[str, Any],
        error_code: str | None = None,
    ) -> ProductionHandlerOutcome:
        target_material = json.dumps(
            {
                "tenant_id": (self._context.principal.tenant_id),
                "user_id": str(self._context.principal.user_id),
                "report_date": report_date.isoformat(),
                "arguments": request.arguments.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ProductionHandlerOutcome(
            target_type="managed_daily_report",
            target_id=hashlib.sha256(
                target_material.encode("utf-8")
            ).hexdigest(),
            before_report=None,
            after_report=None,
            idempotency_key=None,
            safe_user_facts={
                "actual_write": False,
                **facts,
            },
            status_if_unchanged=status,
            error_code=error_code,
        )

    def _daily_briefing_fact_read_outcome(
        self,
        *,
        request: ProductionHandlerRequest,
        report_date: date,
        status: ReceiptStatus,
        facts: dict[str, Any],
        error_code: str | None = None,
    ) -> ProductionHandlerOutcome:
        target_material = json.dumps(
            {
                "tenant_id": self._context.principal.tenant_id,
                "user_id": str(self._context.principal.user_id),
                "report_date": report_date.isoformat(),
                "arguments": request.arguments.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ProductionHandlerOutcome(
            target_type="daily_briefing_fact",
            target_id=hashlib.sha256(
                target_material.encode("utf-8")
            ).hexdigest(),
            before_report=None,
            after_report=None,
            idempotency_key=None,
            safe_user_facts={
                "actual_write": False,
                **facts,
            },
            status_if_unchanged=status,
            error_code=error_code,
        )

    def _report_insight_read_outcome(
        self,
        *,
        request: ProductionHandlerRequest,
        current_date: date,
        status: ReceiptStatus,
        facts: dict[str, Any],
        error_code: str | None = None,
    ) -> ProductionHandlerOutcome:
        target_material = json.dumps(
            {
                "tenant_id": self._context.principal.tenant_id,
                "user_id": str(self._context.principal.user_id),
                "current_date": current_date.isoformat(),
                "arguments": request.arguments.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ProductionHandlerOutcome(
            target_type="daily_report_insight",
            target_id=hashlib.sha256(target_material.encode("utf-8")).hexdigest(),
            before_report=None,
            after_report=None,
            idempotency_key=None,
            safe_user_facts={
                "actual_write": False,
                **facts,
            },
            status_if_unchanged=status,
            error_code=error_code,
        )

    async def _execute_typed(
        self,
        report_date,
        commands: Iterable[TypedDailyCommand],
        *,
        allow_completed_append: bool = False,
        allow_completed_content_mutation: bool = False,
    ) -> tuple[str, ...]:
        command_tuple = tuple(commands)
        if not command_tuple:
            return ()
        result = await execute_typed_agent2_daily_commands(
            self._session,
            user=self._user,
            commands=command_tuple,
            execution_context=TypedDailyExecutionContext(
                report_date=report_date,
                source="agent2_tool_call_canary",
                source_text_hash=self._source_text_hash,
                tenant_id=self._context.principal.tenant_id,
                conversation_id="",
                source_turn_id=self._context.principal.source_message_id,
                occurred_at=self._context.now,
                execution_started_at=self._context.now,
                runtime_label="agent2_tool_call_core",
                contract_version="tool_call_registry.v1",
                allow_completed_append=allow_completed_append,
                allow_completed_content_mutation=(
                    allow_completed_content_mutation
                ),
            ),
            settings=self._settings,
            execution_authority="tool_call_core_registry",
        )
        blocked = next(
            (
                item
                for item in result.command_results
                if item.get("validation_status") == "blocked"
                or item.get("status") == "blocked"
            ),
            None,
        )
        if blocked is not None:
            if (
                str(blocked.get("reason") or "") == "invalid_report_state"
                and str(getattr(result, "status", ""))
                in {"collecting", "pending_confirmation"}
                and any(
                    command.command_type == "submit_report" for command in command_tuple
                )
                and not all(
                    (
                        getattr(result, "today_work", ()),
                        getattr(result, "problems", ()),
                        getattr(result, "tomorrow_plan", ()),
                    )
                )
            ):
                raise ProductionExecutionError("REPORT_INCOMPLETE")
            raise ProductionExecutionError(
                str(blocked.get("reason") or "TYPED_EXECUTOR_BLOCKED")
            )
        await self._session.flush()
        return tuple(
            str(item["receipt_id"])
            for item in result.command_results
            if item.get("receipt_id")
        )

    async def _snapshot(
        self,
        report_date,
        *,
        for_update: bool = False,
    ) -> TrustedReportSnapshot | None:
        if for_update:
            report = await self._session.scalar(
                select(DailyReport)
                .where(
                    DailyReport.user_id == self._user.id,
                    DailyReport.report_date == report_date,
                )
                .with_for_update()
            )
            if report is None:
                return None
            return trusted_snapshot_from_report(
                user=self._user,
                tenant_id=self._context.principal.tenant_id,
                report_date=report_date,
                report=report,
                provenance="trusted_context",
            )
        return await self._context_store.load_report(
            self._context_request(),
            report_date,
        )

    async def _typed_snapshot(self, report_date):
        report = await self._session.scalar(
            select(DailyReport).where(
                DailyReport.user_id == self._user.id,
                DailyReport.report_date == report_date,
            )
        )
        return build_typed_daily_snapshot(
            user=self._user,
            report_date=report_date,
            report=report,
        )

    def _context_request(self):
        from app.agent2.tool_calling.assembly import TrustedContextRequest

        return TrustedContextRequest(
            tenant_id=self._context.principal.tenant_id,
            user_id=self._context.principal.user_id,
            conversation_id=self._context.principal.conversation_id,
            source_message_id=self._context.principal.source_message_id,
            timezone=self._context.principal.timezone,
            server_now=self._context.now,
            display_name=self._context.principal.display_name,
        )

    def _bound(self, request: ProductionHandlerRequest) -> BoundCall:
        bound = self._bound_calls.get(request.tool_call_id)
        if bound is None or bound.call.tool_name != request.tool_name:
            raise ProductionExecutionError("BOUND_TOOL_CALL_REQUIRED")
        return bound

    @staticmethod
    def _arguments(request: ProductionHandlerRequest, model_type):
        if not isinstance(request.arguments, model_type):
            raise ProductionExecutionError("TYPED_ARGUMENTS_REQUIRED")
        return request.arguments

    @staticmethod
    def _required_bound_report(bound: BoundCall) -> TrustedReportSnapshot:
        if bound.report is None:
            raise ProductionExecutionError("REPORT_NOT_FOUND")
        return bound.report

    @staticmethod
    def _required_source_report(bound: BoundCall) -> TrustedReportSnapshot:
        if bound.source_report is None:
            raise ProductionExecutionError("SOURCE_REPORT_NOT_FOUND")
        return bound.source_report

    @staticmethod
    def _require_same_report(
        trusted: TrustedReportSnapshot,
        live_report_id: UUID,
    ) -> None:
        if live_report_id != trusted.report_id:
            raise ProductionExecutionError("REPORT_BINDING_CHANGED")

    @staticmethod
    def _resolved_date(bound: BoundCall, key: str):
        value = bound.date_facts.get(key)
        if not isinstance(value, str) or not value:
            raise ProductionExecutionError("DATE_BINDING_REQUIRED")
        from datetime import date

        return date.fromisoformat(value)

    def _today(self):
        return self._context.now.astimezone(
            ZoneInfo(self._context.principal.timezone)
        ).date()

    @staticmethod
    def _sections(report: TrustedReportSnapshot) -> dict[str, list[str]]:
        sections = {
            "today_work": [],
            "problems": [],
            "tomorrow_plan": [],
        }
        for item in report.items:
            sections[item.field].append(item.content)
        return sections

    def _command(
        self,
        request: ProductionHandlerRequest,
        *,
        ordinal: int,
        command_type: str,
        report_id: UUID,
        report_version: int,
        target_item_ids: tuple[str, ...] = (),
        patch: dict[str, Any],
    ) -> TypedDailyCommand:
        tool_key = self._tool_idempotency_key(request)
        identity = (
            f"{self._context.principal.tenant_id}:"
            f"{self._context.principal.source_message_id}:"
            f"{request.tool_call_id}:{ordinal}"
        )
        return TypedDailyCommand(
            command_id=uuid5(NAMESPACE_URL, f"{identity}:command"),
            decision_id=uuid5(
                NAMESPACE_URL,
                f"{self._context.principal.source_message_id}:decision",
            ),
            sub_decision_id=uuid5(NAMESPACE_URL, f"{identity}:subdecision"),
            command_type=command_type,
            report_id=report_id,
            report_version=report_version,
            target_item_ids=target_item_ids,
            patch=patch,
            idempotency_key=f"{tool_key}:daily:{ordinal}",
        )

    def _tool_idempotency_key(
        self,
        request: ProductionHandlerRequest,
    ) -> str:
        bound = self._bound(request)
        report = bound.source_report or bound.report
        target = (
            str(report.report_id)
            if report is not None
            else f"daily_report:{self._context.principal.user_id}"
        )
        expected_version = report.version if report is not None else None
        return build_write_idempotency_key(
            tenant_id=self._context.principal.tenant_id,
            user_id=str(self._context.principal.user_id),
            conversation_id=self._context.principal.conversation_id,
            source_message_id=self._context.principal.source_message_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            canonical_arguments={
                "tool_arguments": bound.arguments,
                "date_facts": bound.date_facts,
                "report_id": (
                    str(bound.report.report_id) if bound.report is not None else None
                ),
                "source_report_id": (
                    str(bound.source_report.report_id)
                    if bound.source_report is not None
                    else None
                ),
            },
            target_object=target,
            expected_version=expected_version,
        )

    def _outcome(
        self,
        request: ProductionHandlerRequest,
        *,
        before: TrustedReportSnapshot | None,
        after: TrustedReportSnapshot | None,
        typed_receipt_ids: tuple[str, ...],
    ) -> ProductionHandlerOutcome:
        affected = _affected_item_ids(before, after)
        changed = report_state_hash(before) != report_state_hash(after)
        report = after or before
        facts = {
            "actual_write": changed,
            "report_date": (
                report.report_date.isoformat() if report is not None else None
            ),
            "report_status": report.status if report is not None else None,
            "affected_item_ids": list(affected),
        }
        if report is not None:
            fields = {
                field_name: tuple(
                    item.content
                    for item in report.items
                    if item.field == field_name
                )
                for field_name in ("today_work", "problems", "tomorrow_plan")
            }
            facts.update(
                _daily_completion_facts(
                    today_work=fields["today_work"],
                    problems=fields["problems"],
                    tomorrow_plan=fields["tomorrow_plan"],
                    acknowledged_empty_fields=(
                        report.acknowledged_empty_fields
                    ),
                    report_status=report.status,
                    persisted_report_available=True,
                )
            )
        if report is not None and report.status == "pending_confirmation":
            facts["next_step"] = pending_confirmation_next_step(
                report_date=report.report_date,
                occurred_at=self._context.now.astimezone(
                    ZoneInfo(self._context.principal.timezone)
                ),
                settings=self._settings,
            )
        return ProductionHandlerOutcome(
            target_type="daily_report",
            target_id=str(report.report_id) if report is not None else "",
            before_report=before,
            after_report=after,
            idempotency_key=self._tool_idempotency_key(request),
            typed_receipt_ids=typed_receipt_ids,
            affected_item_ids=affected,
            safe_user_facts=facts,
            status_if_unchanged=(
                ReceiptStatus.SUCCESS if changed else ReceiptStatus.NO_OP
            ),
        )


def _affected_item_ids(
    before: TrustedReportSnapshot | None,
    after: TrustedReportSnapshot | None,
) -> tuple[str, ...]:
    before_items = (
        {item.item_id: (item.field, item.content) for item in before.items}
        if before is not None
        else {}
    )
    after_items = (
        {item.item_id: (item.field, item.content) for item in after.items}
        if after is not None
        else {}
    )
    return tuple(
        sorted(
            item_id
            for item_id in set(before_items) | set(after_items)
            if before_items.get(item_id) != after_items.get(item_id)
        )
    )


def source_text_hash(user_text: str) -> str:
    return hashlib.sha256(user_text.encode("utf-8")).hexdigest()


def _daily_completion_facts(
    *,
    today_work: object,
    problems: object,
    tomorrow_plan: object,
    acknowledged_empty_fields: set[str] | frozenset[str],
    report_status: str,
    persisted_report_available: bool,
) -> dict[str, object]:
    assessment = assess_daily_report_completeness(
        today_work=today_work,
        problems=problems,
        tomorrow_plan=tomorrow_plan,
        acknowledged_empty_fields=acknowledged_empty_fields,
    )
    facts = assessment.safe_facts()
    draft_available = (
        persisted_report_available
        and report_status in {"collecting", "pending_confirmation"}
    )
    facts.update(
        {
            "confirmation_available": (
                draft_available and assessment.ready_for_confirmation
            ),
            "persisted_draft_available": draft_available,
        }
    )
    return facts
