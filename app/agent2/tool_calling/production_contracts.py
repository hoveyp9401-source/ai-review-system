from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.agent2.tool_calling.contracts import ToolReceipt


@dataclass(frozen=True)
class ProductionExecutionCapability:
    """Server-created authority for one already-routed Canary turn."""

    tenant_id: str
    user_id: str
    conversation_id: str
    source_message_id: str
    control_key: str
    control_version: int
    registry_digest: str
    prompt_sha256: str
    model_name: str
    expires_at: datetime
    enabled: bool
    messages_enabled: bool
    runtime: str = "canary_execute"

    def __post_init__(self) -> None:
        if not all(
            (
                self.tenant_id,
                self.user_id,
                self.conversation_id,
                self.source_message_id,
                self.control_key,
                self.registry_digest,
                self.prompt_sha256,
                self.model_name,
            )
        ):
            raise ValueError("production capability requires complete server scope")
        if self.control_version < 1:
            raise ValueError("production capability version must be positive")
        if self.expires_at.tzinfo is None:
            raise ValueError("production capability expiry must be timezone-aware")


@dataclass(frozen=True)
class ProductionRuntimeResult:
    status: Literal["success", "blocked", "failed"]
    receipts: tuple[ToolReceipt, ...] = ()
    error_code: str | None = None
    transaction_opened: bool = False
    transaction_pending: bool = False
    committed_to_outer_transaction: bool = False
    rolled_back: bool = False
    handler_call_count: int = 0
    business_write_count: int = 0
    pending_write_count: int = 0
    memory_write_count: int = 0
    memory_audit_write_count: int = 0
    receipt_write_count: int = 0

    @property
    def actual_write(self) -> bool:
        return bool(
            self.business_write_count
            or self.pending_write_count
            or self.memory_write_count
            or self.memory_audit_write_count
        )
