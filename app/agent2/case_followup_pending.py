from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol


@dataclass(frozen=True)
class CaseFollowupPendingSnapshot:
    pending_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    task_id: str
    case_id: str
    followup_id: str
    case_version: int
    expected_state_version: int
    source_message_id: str
    expires_at: datetime
    status: str
    version: int


@dataclass(frozen=True)
class CaseFollowupReplyIntent:
    reply_type: Literal[
        "case_fact", "snooze", "cancel", "report_preference", "correction",
        "selection", "confirmation", "information",
    ]
    source_message_id: str
    raw_text: str


@dataclass(frozen=True)
class CaseFollowupPendingContext:
    tenant_id: str
    user_id: str
    conversation_id: str
    conversation_state_version: int
    now: datetime
    source_message_already_processed: bool


@dataclass(frozen=True)
class CaseFollowupValidation:
    status: Literal["valid", "not_found", "version_conflict", "forbidden", "illegal"]
    reason: str = ""


class CaseFollowupPendingValidator(Protocol):
    async def validate(
        self,
        pending: CaseFollowupPendingSnapshot,
        context: CaseFollowupPendingContext,
    ) -> CaseFollowupValidation: ...


@dataclass(frozen=True)
class CaseFollowupPendingResolution:
    status: str
    reason_code: str
    pending_id: str
    case_id: str
    followup_id: str
    intent: CaseFollowupReplyIntent
    actual_write: bool
    pending_after: CaseFollowupPendingSnapshot


class CaseFollowupPendingResolver:
    """Bind one typed reply intent to one revalidated Follow-up Pending."""

    async def resolve(
        self,
        pendings: tuple[CaseFollowupPendingSnapshot, ...],
        *,
        intent: CaseFollowupReplyIntent,
        context: CaseFollowupPendingContext,
        validator: CaseFollowupPendingValidator,
    ) -> CaseFollowupPendingResolution:
        local = tuple(
            item for item in pendings
            if item.tenant_id == context.tenant_id
            and item.user_id == context.user_id
            and item.conversation_id == context.conversation_id
            and item.status in {"active", "awaiting_input"}
        )
        fallback = local[0] if local else pendings[0] if pendings else _empty_pending(context)
        if len(local) != 1:
            return self._result("clarification_required", "pending_not_unique", fallback, intent)
        pending = local[0]
        if context.source_message_already_processed:
            return self._result("duplicate", "source_message_already_processed", pending, intent)
        if context.now >= pending.expires_at:
            return self._result(
                "expired", "pending_expired", replace(pending, status="expired", version=pending.version + 1), intent
            )
        if context.conversation_state_version != pending.expected_state_version:
            return self._result(
                "conflicted", "conversation_state_version_changed",
                replace(pending, status="conflicted", version=pending.version + 1), intent,
            )
        validation = await validator.validate(pending, context)
        if validation.status != "valid":
            status = "permission_revoked" if validation.status == "forbidden" else "conflicted"
            return self._result(
                status,
                validation.reason or validation.status,
                replace(pending, status=status, version=pending.version + 1),
                intent,
            )
        return CaseFollowupPendingResolution(
            status="bound",
            reason_code="pending_revalidated",
            pending_id=pending.pending_id,
            case_id=pending.case_id,
            followup_id=pending.followup_id,
            intent=intent,
            actual_write=False,
            pending_after=pending,
        )

    @staticmethod
    def _result(status, reason, pending, intent):
        return CaseFollowupPendingResolution(
            status=status,
            reason_code=reason,
            pending_id=pending.pending_id,
            case_id="",
            followup_id="",
            intent=intent,
            actual_write=False,
            pending_after=pending,
        )


def _empty_pending(context: CaseFollowupPendingContext) -> CaseFollowupPendingSnapshot:
    return CaseFollowupPendingSnapshot(
        pending_id="none", tenant_id=context.tenant_id, user_id=context.user_id,
        conversation_id=context.conversation_id, task_id="none", case_id="none",
        followup_id="none", case_version=0,
        expected_state_version=context.conversation_state_version,
        source_message_id="", expires_at=context.now, status="conflicted", version=1,
    )
