from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class ProductionHandlerRequest:
    """Server-built request after Registry and production capability checks."""

    tool_call_id: str
    tool_name: str
    arguments: BaseModel
    executor: "ProductionDailyExecutorPort"
    memory_executor: "ProductionPersonalMemoryExecutorPort"
    performance_executor: (
        "ProductionPerformanceExecutorPort | None"
    ) = None
    weekly_plan_executor: (
        "ProductionWeeklyPlanExecutorPort | None"
    ) = None
    periodic_report_executor: (
        "ProductionPeriodicReportExecutorPort | None"
    ) = None
    pending_ttl_seconds: int | None = None


class ProductionDailyExecutorPort(Protocol):
    async def query_today_report(self, request: ProductionHandlerRequest) -> Any: ...

    async def query_report_by_date(self, request: ProductionHandlerRequest) -> Any: ...

    async def query_managed_daily_reports(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...

    async def query_daily_briefing_facts(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...

    async def query_report_insights(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...

    async def add_daily_items(self, request: ProductionHandlerRequest) -> Any: ...

    async def edit_daily_items(self, request: ProductionHandlerRequest) -> Any: ...

    async def delete_daily_items(self, request: ProductionHandlerRequest) -> Any: ...

    async def move_daily_items(self, request: ProductionHandlerRequest) -> Any: ...

    async def copy_previous_to_today(self, request: ProductionHandlerRequest) -> Any: ...

    async def correct_daily_report_date(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...

    async def complete_previous_plan(self, request: ProductionHandlerRequest) -> Any: ...

    async def confirm_report(self, request: ProductionHandlerRequest) -> Any: ...

    async def request_clear_report(self, request: ProductionHandlerRequest) -> Any: ...

    async def confirm_clear_report(self, request: ProductionHandlerRequest) -> Any: ...


class ProductionPersonalMemoryExecutorPort(Protocol):
    async def query_personal_memory(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...

    async def remember_personal_memory(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...

    async def forget_personal_memory(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...


class ProductionPerformanceExecutorPort(Protocol):
    async def query_defendant_performance(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...


class ProductionWeeklyPlanExecutorPort(Protocol):
    async def query_next_weekly_plan(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...

    async def apply_next_weekly_plan(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...

    async def submit_next_weekly_plan(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...


class ProductionPeriodicReportExecutorPort(Protocol):
    async def query_current_weekly_report(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...

    async def apply_current_weekly_report(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...

    async def submit_current_weekly_report(
        self,
        request: ProductionHandlerRequest,
    ) -> Any: ...


async def execute_query_today_report(request: ProductionHandlerRequest) -> Any:
    return await request.executor.query_today_report(request)


async def execute_query_report_by_date(request: ProductionHandlerRequest) -> Any:
    return await request.executor.query_report_by_date(request)


async def execute_query_managed_daily_reports(
    request: ProductionHandlerRequest,
) -> Any:
    return await request.executor.query_managed_daily_reports(
        request
    )


async def execute_query_daily_briefing_facts(
    request: ProductionHandlerRequest,
) -> Any:
    return await request.executor.query_daily_briefing_facts(request)


async def execute_query_report_insights(
    request: ProductionHandlerRequest,
) -> Any:
    return await request.executor.query_report_insights(request)


async def execute_query_defendant_performance(
    request: ProductionHandlerRequest,
) -> Any:
    if request.performance_executor is None:
        raise RuntimeError("performance executor unavailable")
    return await request.performance_executor.query_defendant_performance(
        request
    )


def _required_weekly_plan_executor(
    request: ProductionHandlerRequest,
) -> ProductionWeeklyPlanExecutorPort:
    executor = request.weekly_plan_executor
    if executor is None:
        raise RuntimeError("weekly plan executor unavailable")
    return executor


async def execute_query_next_weekly_plan(
    request: ProductionHandlerRequest,
) -> Any:
    return await _required_weekly_plan_executor(
        request
    ).query_next_weekly_plan(request)


async def execute_apply_next_weekly_plan(
    request: ProductionHandlerRequest,
) -> Any:
    return await _required_weekly_plan_executor(
        request
    ).apply_next_weekly_plan(request)


async def execute_submit_next_weekly_plan(
    request: ProductionHandlerRequest,
) -> Any:
    return await _required_weekly_plan_executor(
        request
    ).submit_next_weekly_plan(request)


def _required_periodic_report_executor(
    request: ProductionHandlerRequest,
) -> ProductionPeriodicReportExecutorPort:
    executor = request.periodic_report_executor
    if executor is None:
        raise RuntimeError("periodic report executor unavailable")
    return executor


async def execute_query_current_weekly_report(
    request: ProductionHandlerRequest,
) -> Any:
    return await _required_periodic_report_executor(
        request
    ).query_current_weekly_report(request)


async def execute_apply_current_weekly_report(
    request: ProductionHandlerRequest,
) -> Any:
    return await _required_periodic_report_executor(
        request
    ).apply_current_weekly_report(request)


async def execute_submit_current_weekly_report(
    request: ProductionHandlerRequest,
) -> Any:
    return await _required_periodic_report_executor(
        request
    ).submit_current_weekly_report(request)


async def execute_add_daily_items(request: ProductionHandlerRequest) -> Any:
    return await request.executor.add_daily_items(request)


async def execute_edit_daily_items(request: ProductionHandlerRequest) -> Any:
    return await request.executor.edit_daily_items(request)


async def execute_delete_daily_items(request: ProductionHandlerRequest) -> Any:
    return await request.executor.delete_daily_items(request)


async def execute_move_daily_items(request: ProductionHandlerRequest) -> Any:
    return await request.executor.move_daily_items(request)


async def execute_copy_previous_to_today(request: ProductionHandlerRequest) -> Any:
    return await request.executor.copy_previous_to_today(request)


async def execute_correct_daily_report_date(
    request: ProductionHandlerRequest,
) -> Any:
    return await request.executor.correct_daily_report_date(request)


async def execute_complete_previous_plan(request: ProductionHandlerRequest) -> Any:
    return await request.executor.complete_previous_plan(request)


async def execute_confirm_report(request: ProductionHandlerRequest) -> Any:
    return await request.executor.confirm_report(request)


async def execute_request_clear_report(request: ProductionHandlerRequest) -> Any:
    return await request.executor.request_clear_report(request)


async def execute_confirm_clear_report(request: ProductionHandlerRequest) -> Any:
    return await request.executor.confirm_clear_report(request)


async def execute_query_personal_memory(
    request: ProductionHandlerRequest,
) -> Any:
    return await request.memory_executor.query_personal_memory(request)


async def execute_remember_personal_memory(
    request: ProductionHandlerRequest,
) -> Any:
    return await request.memory_executor.remember_personal_memory(request)


async def execute_forget_personal_memory(
    request: ProductionHandlerRequest,
) -> Any:
    return await request.memory_executor.forget_personal_memory(request)
