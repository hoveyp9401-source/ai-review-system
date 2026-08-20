from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import BaseModel

from app.agent2.tool_calling.production_handlers import (
    ProductionHandlerRequest,
    execute_apply_next_weekly_plan,
    execute_query_next_weekly_plan,
    execute_submit_next_weekly_plan,
)
from app.agent2.tool_calling.registry import (
    TOOL_REGISTRY,
    ToolArgumentsValidationError,
    runtime_registry_tool_names,
    validate_tool_arguments,
)


def _settings(**overrides):
    values = {
        "legal_daily_dashboard_enabled": False,
        "agent2_cross_user_daily_read_enabled": False,
        "agent2_performance_tool_enabled": False,
        "legal_ops_data_intake_enabled": False,
        "agent2_performance_knowledge_enabled": False,
        "legal_ops_live_tenant_id": "",
        "agent2_weekly_plan_enabled": False,
        "agent2_weekly_plan_write_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_weekly_plan_tools_are_absent_until_their_independent_flags_open():
    closed = runtime_registry_tool_names(_settings())
    read_only = runtime_registry_tool_names(
        _settings(agent2_weekly_plan_enabled=True)
    )
    writable = runtime_registry_tool_names(
        _settings(
            agent2_weekly_plan_enabled=True,
            agent2_weekly_plan_write_enabled=True,
        )
    )

    weekly_names = {
        "query_next_weekly_plan",
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
        "record_weekly_plan_items_as_today_work",
    }
    assert weekly_names.isdisjoint(closed)
    assert "query_next_weekly_plan" in read_only
    assert {
        "apply_next_weekly_plan",
        "submit_next_weekly_plan",
        "record_weekly_plan_items_as_today_work",
    }.isdisjoint(read_only)
    assert weekly_names <= set(writable)


def test_apply_weekly_plan_accepts_one_atomic_six_day_operation_batch():
    plan_id = UUID("11111111-1111-4111-8111-111111111111")
    arguments = {
        "plan_id": str(plan_id),
        "expected_version": 7,
        "operations": [
            {
                "operation_id": f"add-{day}",
                "operation": "add",
                "plan_date": date(2026, 8, 17 + day).isoformat(),
                "content": f"第{day + 1}天的事项",
                "source_evidence": {
                    "source_message_index": 1,
                    "exact_clause_quote": f"周{'一二三四五六'[day]}第{day + 1}天的事项",
                },
            }
            for day in range(6)
        ],
    }

    validated = validate_tool_arguments(
        "apply_next_weekly_plan",
        arguments,
    )

    assert validated["plan_id"] == str(plan_id)
    assert validated["expected_version"] == 7
    assert [item["plan_date"] for item in validated["operations"]] == [
        "2026-08-17",
        "2026-08-18",
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
        "2026-08-22",
    ]
    definition = TOOL_REGISTRY["apply_next_weekly_plan"]
    assert definition.transaction_policy == "same_weekly_plan_atomic"
    assert definition.transaction_target_policy == "weekly_plan"
    assert definition.permission_policy == (
        "authenticated_owner_weekly_plan_write"
    )


def test_submit_weekly_plan_requires_current_message_confirmation_evidence():
    arguments = {
        "plan_id": "11111111-1111-4111-8111-111111111111",
        "expected_version": 7,
        "confirmation_evidence": {"source_message_index": 1},
    }

    validated = validate_tool_arguments(
        "submit_next_weekly_plan",
        arguments,
    )

    assert validated["confirmation_evidence"] == {
        "source_message_index": 1
    }
    with pytest.raises(ToolArgumentsValidationError):
        validate_tool_arguments(
            "submit_next_weekly_plan",
            {
                "plan_id": arguments["plan_id"],
                "expected_version": 7,
            },
        )


def test_weekly_plan_edit_accepts_complete_replacement_evidence():
    validated = validate_tool_arguments(
        "apply_next_weekly_plan",
        {
            "plan_id": "11111111-1111-4111-8111-111111111111",
            "expected_version": 7,
            "operations": [
                {
                    "operation_id": "edit-1",
                    "operation": "edit",
                    "item_id": "item-1",
                    "content": "准备星河案证据清单",
                    "source_evidence": {
                        "source_message_index": 1,
                        "exact_clause_quote": (
                            "把周三那条改成准备星河案证据清单"
                        ),
                    },
                }
            ],
        },
    )

    assert validated["operations"][0]["source_evidence"] == {
        "source_message_index": 1,
        "exact_clause_quote": "把周三那条改成准备星河案证据清单",
    }


@pytest.mark.parametrize(
    "operation",
    (
        {
            "operation_id": "add-1",
            "operation": "add",
            "plan_date": "2026-08-17",
            "content": "整理材料",
        },
        {
            "operation_id": "edit-1",
            "operation": "edit",
            "item_id": "item-1",
            "content": "整理补充材料",
        },
        {
            "operation_id": "move-1",
            "operation": "move",
            "item_id": "item-1",
            "target_plan_date": "2026-08-18",
        },
        {
            "operation_id": "delete-1",
            "operation": "delete",
            "item_id": "item-1",
        },
        {
            "operation_id": "empty-1",
            "operation": "set_day_empty",
            "plan_date": "2026-08-22",
        },
        {
            "operation_id": "accept-1",
            "operation": "accept_suggestion",
            "suggestion_id": "suggestion-1",
            "plan_date": "2026-08-20",
        },
        {
            "operation_id": "reject-1",
            "operation": "reject_suggestion",
            "suggestion_id": "suggestion-1",
        },
        {
            "operation_id": "capture-1",
            "operation": "capture_suggestion",
            "content": "下周继续整理合同评审规则",
        },
    ),
)
def test_every_weekly_plan_operation_requires_current_message_evidence(
    operation,
):
    with pytest.raises(ToolArgumentsValidationError) as exc_info:
        validate_tool_arguments(
            "apply_next_weekly_plan",
            {
                "plan_id": "11111111-1111-4111-8111-111111111111",
                "expected_version": 1,
                "operations": [operation],
            },
        )

    assert any(
        "source_evidence" in error
        for error in exc_info.value.errors
    )


def test_undated_next_week_matter_has_a_separate_suggestion_operation():
    validated = validate_tool_arguments(
        "apply_next_weekly_plan",
        {
            "plan_id": "11111111-1111-4111-8111-111111111111",
            "expected_version": 0,
            "operations": [
                {
                    "operation_id": "suggest-1",
                    "operation": "capture_suggestion",
                    "content": "下周继续整理合同评审规则",
                    "source_evidence": {"source_message_index": 1},
                }
            ],
        },
    )

    operation = validated["operations"][0]
    assert operation == {
        "operation_id": "suggest-1",
        "operation": "capture_suggestion",
        "content": "下周继续整理合同评审规则",
        "source_evidence": {"source_message_index": 1},
    }
    assert "plan_date" not in operation


class _EmptyArguments(BaseModel):
    pass


class _WeeklyExecutorSpy:
    def __init__(self):
        self.calls = []

    async def query_next_weekly_plan(self, request):
        self.calls.append(("query", request))
        return {"kind": "query"}

    async def apply_next_weekly_plan(self, request):
        self.calls.append(("apply", request))
        return {"kind": "apply"}

    async def submit_next_weekly_plan(self, request):
        self.calls.append(("submit", request))
        return {"kind": "submit"}


@pytest.mark.asyncio
async def test_weekly_handlers_forward_only_to_the_independent_weekly_executor():
    weekly_executor = _WeeklyExecutorSpy()
    request = ProductionHandlerRequest(
        tool_call_id="call-1",
        tool_name="query_next_weekly_plan",
        arguments=_EmptyArguments(),
        executor=object(),
        memory_executor=object(),
        weekly_plan_executor=weekly_executor,
    )

    assert await execute_query_next_weekly_plan(request) == {
        "kind": "query"
    }
    assert await execute_apply_next_weekly_plan(request) == {
        "kind": "apply"
    }
    assert await execute_submit_next_weekly_plan(request) == {
        "kind": "submit"
    }
    assert [name for name, _ in weekly_executor.calls] == [
        "query",
        "apply",
        "submit",
    ]


@pytest.mark.asyncio
async def test_weekly_handlers_fail_closed_when_the_executor_is_not_wired():
    request = ProductionHandlerRequest(
        tool_call_id="call-1",
        tool_name="query_next_weekly_plan",
        arguments=_EmptyArguments(),
        executor=object(),
        memory_executor=object(),
    )

    with pytest.raises(RuntimeError, match="weekly plan executor unavailable"):
        await execute_query_next_weekly_plan(request)
