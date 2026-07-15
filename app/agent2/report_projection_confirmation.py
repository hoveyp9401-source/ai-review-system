from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Literal


ConfirmationAction = Literal["confirm", "decline", "unknown"]


@dataclass(frozen=True)
class ProjectionConfirmationPendingSnapshot:
    pending_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    request_id: str
    case_id: str
    case_progress_id: str
    expected_state_version: int
    expires_at: datetime
    status: str
    version: int


@dataclass(frozen=True)
class ProjectionConfirmationContext:
    tenant_id: str
    user_id: str
    conversation_id: str
    conversation_state_version: int
    now: datetime
    source_message_already_processed: bool


@dataclass(frozen=True)
class ProjectionConfirmationResolution:
    status: str
    reason_code: str
    action: ConfirmationAction
    pending: ProjectionConfirmationPendingSnapshot | None
    actual_write: bool = False


def parse_projection_confirmation_answer(raw_text: str) -> ConfirmationAction:
    compact = re.sub(
        r"[\s\u3000，,。.!！?？:：;；、（）()【】\[\]\"'“”‘’]+",
        "",
        str(raw_text or ""),
    ).lower()
    if compact in {
        "需要", "确认", "确认加入", "确认加入日报", "加入日报", "也放日报",
        "放日报", "是", "要", "可以", "好", "好的", "yes", "ok",
    }:
        return "confirm"
    if compact in {
        "不需要", "取消", "只记案件", "只记录案件", "别放日报",
        "这条别放日报", "不要放日报", "不加入日报", "否", "不要", "no",
    }:
        return "decline"
    return "unknown"


class ProjectionConfirmationResolver:
    def resolve(
        self,
        pendings: tuple[ProjectionConfirmationPendingSnapshot, ...],
        *,
        answer: str,
        context: ProjectionConfirmationContext,
    ) -> ProjectionConfirmationResolution:
        action = parse_projection_confirmation_answer(answer)
        if action == "unknown":
            return ProjectionConfirmationResolution(
                "not_applicable", "answer_not_accepted", action, None
            )
        local = tuple(
            item
            for item in pendings
            if item.tenant_id == context.tenant_id
            and item.user_id == context.user_id
            and item.conversation_id == context.conversation_id
            and item.status in {"active", "awaiting_input"}
        )
        if len(local) != 1:
            return ProjectionConfirmationResolution(
                "clarification_required", "pending_not_unique", action, None
            )
        pending = local[0]
        if context.source_message_already_processed:
            return ProjectionConfirmationResolution(
                "duplicate", "source_message_already_processed", action, pending
            )
        if context.now >= pending.expires_at:
            return ProjectionConfirmationResolution(
                "expired", "pending_expired", action, pending
            )
        if context.conversation_state_version != pending.expected_state_version:
            return ProjectionConfirmationResolution(
                "conflicted", "conversation_state_version_changed", action, pending
            )
        return ProjectionConfirmationResolution(
            "bound", "pending_revalidated", action, pending
        )
