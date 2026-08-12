from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class SandboxHandlerRequest:
    """Server-built request passed only after Registry and capability validation."""

    tool_call_id: str
    tool_name: str
    arguments: BaseModel
    executor: "SandboxDailyExecutorPort"
    memory_executor: "SandboxMemoryExecutorPort"
    pending_ttl_seconds: int | None = None


class SandboxDailyExecutorPort(Protocol):
    async def query_today_report(self, request: SandboxHandlerRequest) -> Any: ...

    async def query_report_by_date(self, request: SandboxHandlerRequest) -> Any: ...

    async def add_daily_items(self, request: SandboxHandlerRequest) -> Any: ...

    async def edit_daily_items(self, request: SandboxHandlerRequest) -> Any: ...

    async def delete_daily_items(self, request: SandboxHandlerRequest) -> Any: ...

    async def move_daily_items(self, request: SandboxHandlerRequest) -> Any: ...

    async def copy_previous_to_today(self, request: SandboxHandlerRequest) -> Any: ...

    async def correct_daily_report_date(
        self,
        request: SandboxHandlerRequest,
    ) -> Any: ...

    async def complete_previous_plan(self, request: SandboxHandlerRequest) -> Any: ...

    async def confirm_report(self, request: SandboxHandlerRequest) -> Any: ...

    async def request_clear_report(self, request: SandboxHandlerRequest) -> Any: ...

    async def confirm_clear_report(self, request: SandboxHandlerRequest) -> Any: ...


class SandboxMemoryExecutorPort(Protocol):
    async def query_personal_memory(self, request: SandboxHandlerRequest) -> Any: ...

    async def remember_personal_memory(self, request: SandboxHandlerRequest) -> Any: ...

    async def forget_personal_memory(self, request: SandboxHandlerRequest) -> Any: ...


async def execute_query_today_report(request: SandboxHandlerRequest) -> Any:
    return await request.executor.query_today_report(request)


async def execute_query_report_by_date(request: SandboxHandlerRequest) -> Any:
    return await request.executor.query_report_by_date(request)


async def execute_add_daily_items(request: SandboxHandlerRequest) -> Any:
    return await request.executor.add_daily_items(request)


async def execute_edit_daily_items(request: SandboxHandlerRequest) -> Any:
    return await request.executor.edit_daily_items(request)


async def execute_delete_daily_items(request: SandboxHandlerRequest) -> Any:
    return await request.executor.delete_daily_items(request)


async def execute_move_daily_items(request: SandboxHandlerRequest) -> Any:
    return await request.executor.move_daily_items(request)


async def execute_copy_previous_to_today(request: SandboxHandlerRequest) -> Any:
    return await request.executor.copy_previous_to_today(request)


async def execute_correct_daily_report_date(
    request: SandboxHandlerRequest,
) -> Any:
    return await request.executor.correct_daily_report_date(request)


async def execute_complete_previous_plan(request: SandboxHandlerRequest) -> Any:
    return await request.executor.complete_previous_plan(request)


async def execute_confirm_report(request: SandboxHandlerRequest) -> Any:
    return await request.executor.confirm_report(request)


async def execute_request_clear_report(request: SandboxHandlerRequest) -> Any:
    return await request.executor.request_clear_report(request)


async def execute_confirm_clear_report(request: SandboxHandlerRequest) -> Any:
    return await request.executor.confirm_clear_report(request)


async def execute_query_personal_memory(request: SandboxHandlerRequest) -> Any:
    return await request.memory_executor.query_personal_memory(request)


async def execute_remember_personal_memory(request: SandboxHandlerRequest) -> Any:
    return await request.memory_executor.remember_personal_memory(request)


async def execute_forget_personal_memory(request: SandboxHandlerRequest) -> Any:
    return await request.memory_executor.forget_personal_memory(request)
