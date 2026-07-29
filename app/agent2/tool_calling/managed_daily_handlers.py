from __future__ import annotations

from app.agent2.tool_calling.contracts import (
    ExecutionMode,
    ReceiptStatus,
    ToolReceipt,
)
from app.agent2.tool_calling.handlers import ShadowHandlerRequest


def simulate_managed_daily_query(
    request: ShadowHandlerRequest,
) -> ToolReceipt:
    """Fail closed when offline Shadow has no organization read projection."""

    return ToolReceipt(
        status=ReceiptStatus.BLOCKED,
        tool_name=request.tool_name,
        changed=False,
        target_type="managed_daily_report",
        target_id="",
        error_code="MANAGED_DAILY_SHADOW_READ_UNAVAILABLE",
        safe_user_facts={
            "actual_write": False,
            "shadow_read_available": False,
        },
        execution_mode=ExecutionMode.SHADOW_PROPOSAL,
    )
