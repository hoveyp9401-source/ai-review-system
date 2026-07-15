from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from app.agent2.case_followup_pending import CaseFollowupPendingSnapshot


@dataclass(frozen=True)
class FollowupDispatchState:
    followup_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    case_id: str
    case_name: str
    case_version: int
    question_text: str
    task_status: str
    message_status: str
    response_status: str
    expires_at: datetime
    version: int
    provider_message_id: str = ""


@dataclass(frozen=True)
class FollowupOutboxPlan:
    notification_id: str
    idempotency_key: str
    payload: dict[str, str]
    state_after: FollowupDispatchState


def build_followup_reminder_outbox_plan(
    state: FollowupDispatchState,
    *,
    reminder_number: int,
    now: datetime,
) -> FollowupOutboxPlan:
    if (
        state.task_status != "waiting_for_reply"
        or state.message_status not in {"accepted_by_provider", "delivery_confirmed"}
        or state.response_status != "awaiting_input"
        or not state.provider_message_id
    ):
        raise ValueError("only a provider-accepted unanswered follow-up can be reminded")
    if reminder_number < 1:
        raise ValueError("reminder number must be positive")
    if now >= state.expires_at:
        raise ValueError("expired follow-up cannot be reminded")
    idempotency_key = (
        f"case-lifecycle-followup:{state.tenant_id}:{state.followup_id}:"
        f"reminder:{reminder_number}"
    )
    return FollowupOutboxPlan(
        str(uuid5(NAMESPACE_URL, idempotency_key)),
        idempotency_key,
        {
            "text": f"提醒一下：{state.question_text}",
            "case_id": state.case_id,
            "case_name": state.case_name,
            "case_version": str(state.case_version),
            "followup_id": state.followup_id,
            "expires_at": state.expires_at.isoformat(),
            "reminder_number": str(reminder_number),
            "is_reminder": "true",
        },
        state,
    )


@dataclass(frozen=True)
class FollowupProviderAcceptance:
    state_after: FollowupDispatchState
    pending: CaseFollowupPendingSnapshot
    delivery_confirmed: bool


@dataclass(frozen=True)
class FollowupAnswerSettlement:
    state_after: FollowupDispatchState
    pending_after: CaseFollowupPendingSnapshot
    completed: bool


def build_followup_outbox_plan(
    state: FollowupDispatchState,
    *,
    now: datetime,
) -> FollowupOutboxPlan:
    if state.task_status != "scheduled" or state.message_status != "scheduled":
        raise ValueError("only a scheduled follow-up can enter the outbox")
    if now >= state.expires_at:
        raise ValueError("expired follow-up cannot enter the outbox")
    if not state.question_text.strip():
        raise ValueError("follow-up question text is required")
    idempotency_key = (
        f"case-lifecycle-followup:{state.tenant_id}:{state.followup_id}:send"
    )
    notification_id = str(uuid5(NAMESPACE_URL, idempotency_key))
    payload = {
        "text": state.question_text,
        "case_id": state.case_id,
        "case_name": state.case_name,
        "case_version": str(state.case_version),
        "followup_id": state.followup_id,
        "expires_at": state.expires_at.isoformat(),
    }
    return FollowupOutboxPlan(
        notification_id,
        idempotency_key,
        payload,
        replace(
            state,
            task_status="queued",
            message_status="queued",
            version=state.version + 1,
        ),
    )


def accept_provider_receipt(
    state: FollowupDispatchState,
    *,
    external_message_id: str,
    now: datetime,
    expected_state_version: int,
) -> FollowupProviderAcceptance:
    message_id = external_message_id.strip()
    if not message_id:
        raise ValueError("provider message id is required")
    if state.message_status not in {"queued", "sending"}:
        raise ValueError("provider receipt does not match a sendable follow-up")
    if now >= state.expires_at:
        raise ValueError("expired follow-up cannot await a reply")
    pending_id = str(
        uuid5(
            NAMESPACE_URL,
            f"case-followup-pending:{state.tenant_id}:{state.followup_id}:{message_id}",
        )
    )
    pending = CaseFollowupPendingSnapshot(
        pending_id=pending_id,
        tenant_id=state.tenant_id,
        user_id=state.user_id,
        conversation_id=state.conversation_id,
        task_id=state.followup_id,
        case_id=state.case_id,
        followup_id=state.followup_id,
        case_version=state.case_version,
        expected_state_version=expected_state_version,
        source_message_id=message_id,
        expires_at=state.expires_at,
        status="awaiting_input",
        version=1,
    )
    return FollowupProviderAcceptance(
        state_after=replace(
            state,
            task_status="waiting_for_reply",
            message_status="accepted_by_provider",
            response_status="awaiting_input",
            provider_message_id=message_id,
            version=state.version + 1,
        ),
        pending=pending,
        delivery_confirmed=False,
    )


def settle_followup_answer(
    state: FollowupDispatchState,
    pending: CaseFollowupPendingSnapshot,
    *,
    now: datetime,
    case_receipt_status: str,
    case_actual_write: bool,
) -> FollowupAnswerSettlement:
    committed = case_receipt_status == "executed" and case_actual_write
    if not committed:
        return FollowupAnswerSettlement(state, pending, False)
    if pending.status not in {"active", "awaiting_input"}:
        raise ValueError("follow-up pending is not consumable")
    return FollowupAnswerSettlement(
        replace(
            state,
            task_status="answered",
            response_status="answered",
            version=state.version + 1,
        ),
        replace(
            pending,
            status="consumed",
            version=pending.version + 1,
        ),
        True,
    )
