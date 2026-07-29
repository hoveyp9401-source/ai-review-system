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
            facts["performance_query"] = {
                "view": arguments.view,
                "scope_type": arguments.scope_type,
                "scope_name": result.scope_name,
                "mode": arguments.mode,
                "rule_version": result.rule_version,
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
