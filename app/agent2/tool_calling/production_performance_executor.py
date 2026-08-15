from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from app.agent2.performance_knowledge import (
    load_live_performance_evidence,
)
from app.agent2.performance_tool_service import (
    PerformanceToolResult,
    query_live_defendant_performance,
)
from app.agent2.tool_calling.context import TrustedContext
from app.agent2.tool_calling.contracts import (
    QueryDefendantPerformanceArgs,
    ReceiptStatus,
)
from app.agent2.tool_calling.production_daily_executor import (
    ProductionExecutionError,
    ProductionHandlerOutcome,
)
from app.agent2.tool_calling.production_handlers import (
    ProductionHandlerRequest,
)


class ProductionPerformanceExecutor:
    """Adapt published performance reads to the Canary receipt contract."""

    def __init__(
        self,
        *,
        session: Any,
        user: Any,
        context: TrustedContext,
        settings: object,
        performance_loader: Callable[..., Any] | None = None,
    ) -> None:
        self._session = session
        self._user = user
        self._context = context
        self._settings = settings
        self._performance_loader = (
            performance_loader
            or load_live_performance_evidence
        )

    async def query_defendant_performance(
        self,
        request: ProductionHandlerRequest,
    ) -> ProductionHandlerOutcome:
        if not isinstance(
            request.arguments,
            QueryDefendantPerformanceArgs,
        ):
            raise ProductionExecutionError("INVALID_TOOL_ARGUMENTS")
        arguments = request.arguments
        anchor_date = self._today()
        result = await query_live_defendant_performance(
            session=self._session,
            user=self._user,
            settings=self._settings,
            anchor_date=anchor_date,
            tenant_id=self._context.principal.tenant_id,
            view=arguments.view,
            scope_type=arguments.scope_type,
            team_name=arguments.team_name,
            mode=arguments.mode,
            performance_loader=self._performance_loader,
        )
        return self._outcome(
            request=request,
            anchor_date=anchor_date,
            result=result,
        )

    def _today(self) -> date:
        return self._context.now.astimezone(
            ZoneInfo(self._context.principal.timezone)
        ).date()

    def _outcome(
        self,
        *,
        request: ProductionHandlerRequest,
        anchor_date: date,
        result: PerformanceToolResult,
    ) -> ProductionHandlerOutcome:
        target_material = json.dumps(
            {
                "tenant_id": self._context.principal.tenant_id,
                "user_id": str(self._context.principal.user_id),
                "anchor_date": anchor_date.isoformat(),
                "arguments": request.arguments.model_dump(
                    mode="json"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        status = ReceiptStatus(result.status)
        facts: dict[str, Any] = {
            "actual_write": False,
            "authoritative_read_response": True,
            "response_text": result.response_text,
        }
        if result.available_team_names:
            facts["available_team_names"] = list(
                result.available_team_names
            )
        if result.status == "success":
            arguments = request.arguments
            facts["model_composition_allowed"] = True
            facts["回复要求"] = (
                "直接回答用户这一轮的问题，不复述整份报表；只有用户明确"
                "要求概览时才展开多个指标。当用户只询问某个指标的定义或"
                "计算口径时，只回答对应定义或口径，不主动追加该指标的当前"
                "数值、件数、截止日期或其他报表事实；只有用户同时明确询问"
                "当前结果时，才引用独立事实回答。只能使用 performance_facts "
                "中的当前事实，不自行计算，不展示英文键、事实编号或内部"
                "说明。连续追问时不要重复称呼、标题和无关指标。若用户问"
                "“哪几件”，按案件事实逐条列出案件名称、分公司、承办法务"
                "和日期；不得自行分组、计数、合并或写“其余”。最终回复"
                "必须把每个准备采用的事实单独放一行，并在行末附"
                "[依据:<claim_id>]；一行只能引用一个事实编号。<claim_id> "
                "必须逐字复制 claim_catalog 对象的键原文；例如键为"
                "definition.7 就写[依据:definition.7]。不得在编号中加入"
                "“claim_catalog中的”、中文类别名或翻译。系统会按编号核验"
                "事实并隐藏编号，不会重写用户可见文字；没有编号的结论、"
                "评价或补充内容不会发送给用户。若某条事实含"
                "validation_policy=exact_canonical_text，引用该事实的一整行"
                "只能是它的 canonical_text 后紧接行末事实编号；不得在该行"
                "增加标题、前缀、后缀或同一行其他事实。可直接输出该行，"
                "不必另写引导。"
            )
            facts["performance_facts"] = dict(
                result.fact_packet
            )
            facts["performance_query"] = {
                "view": arguments.view,
                "scope_type": arguments.scope_type,
                "scope_name": result.scope_name,
                "mode": arguments.mode,
            }
        return ProductionHandlerOutcome(
            target_type="performance_report",
            target_id=hashlib.sha256(
                target_material.encode("utf-8")
            ).hexdigest(),
            before_report=None,
            after_report=None,
            idempotency_key=None,
            safe_user_facts=facts,
            status_if_unchanged=status,
            error_code=result.error_code,
        )
