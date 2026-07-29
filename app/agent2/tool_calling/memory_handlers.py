from __future__ import annotations

from typing import get_args

from app.agent2.memory import (
    TrustedPersonalMemory,
    model_visible_personal_memory_value,
)
from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    PersonalMemoryKey,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.handlers import ShadowHandlerRequest


_M2_MEMORY_KEYS = frozenset(get_args(PersonalMemoryKey))


def simulate_memory_query(request: ShadowHandlerRequest) -> ToolReceipt:
    entries = (
        tuple(
            entry
            for entry in request.context.personal_memory.entries
            if entry.memory_type == "response_preference"
            and entry.memory_key in _M2_MEMORY_KEYS
        )
        if request.context.personal_memory is not None
        else ()
    )
    return _receipt(
        request,
        status=ReceiptStatus.SUCCESS,
        target_id="current_user",
        would_change=False,
        facts={
            "memories": [entry.model_payload() for entry in entries],
        },
    )


def simulate_memory_remember(request: ShadowHandlerRequest) -> ToolReceipt:
    key = str(request.arguments["memory_key"])
    value = dict(request.arguments["value"])
    existing = _entry(request, key)
    would_change = (
        existing is None
        or existing.value.model_dump(mode="json") != value
    )
    return _receipt(
        request,
        status=(
            ReceiptStatus.SUCCESS
            if would_change
            else ReceiptStatus.NO_OP
        ),
        target_id=key,
        would_change=would_change,
        facts={
            "memory_key": key,
            "memory_type": "response_preference",
            "value": model_visible_personal_memory_value(
                "response_preference",
                key,
                value,
            ),
            "replaces_existing": existing is not None and would_change,
        },
        existing=existing,
    )


def simulate_memory_forget(request: ShadowHandlerRequest) -> ToolReceipt:
    key = str(request.arguments["memory_key"])
    existing = _entry(request, key)
    would_change = existing is not None
    return _receipt(
        request,
        status=(
            ReceiptStatus.SUCCESS
            if would_change
            else ReceiptStatus.NO_OP
        ),
        target_id=key,
        would_change=would_change,
        facts={
            "memory_key": key,
            "memory_type": "response_preference",
            "forgotten": False,
            "would_be_forgotten": would_change,
        },
        existing=existing,
    )


def _entry(
    request: ShadowHandlerRequest,
    key: str,
) -> TrustedPersonalMemory | None:
    memory = request.context.personal_memory
    if memory is None:
        return None
    return next(
        (
            entry
            for entry in memory.entries
            if entry.memory_type == "response_preference"
            and entry.memory_key == key
            and entry.memory_key in _M2_MEMORY_KEYS
        ),
        None,
    )


def _receipt(
    request: ShadowHandlerRequest,
    *,
    status: ReceiptStatus,
    target_id: str,
    would_change: bool,
    facts: dict[str, object],
    existing: TrustedPersonalMemory | None = None,
) -> ToolReceipt:
    version = existing.version if existing is not None else None
    return ToolReceipt(
        status=status,
        tool_name=request.tool_name,
        changed=False,
        target_type="personal_memory",
        target_id=target_id,
        before_version=version,
        after_version=version,
        safe_user_facts={
            "proposal_validated": True,
            "actual_write": False,
            "execution_mode": ExecutionMode.SHADOW_PROPOSAL,
            "would_change": would_change,
            **facts,
        },
        execution_mode=ExecutionMode.SHADOW_PROPOSAL,
        would_change=would_change,
        idempotency_key=request.idempotency_key,
    )
