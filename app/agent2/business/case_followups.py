from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent2.business.models import Agent2Case, Agent2IdentityBinding, NotificationOutbox


def build_case_progress_followup_message(
    *,
    case_name: str,
    case_number: str = "",
) -> dict[str, str]:
    """Build the bounded prompt used by a real direct-message follow-up."""

    name = str(case_name or "").strip()
    number = str(case_number or "").strip()
    if not name:
        raise ValueError("case_name is required")
    label = f"{name} {number}" if number else name
    return {
        "text": (
            f"案件进展追问：{label}目前有什么新进展？请直接回复已发生的事实。"
            "你的回复会作为机器人追问记录，不会被视为法院正式事实。"
        ),
        "case_name": name,
        "case_number": number,
    }


async def enqueue_case_progress_followup(
    session: AsyncSession,
    *,
    tenant_id: str,
    case_id: str,
    recipient_user_id: str,
    trigger_id: str,
    now: datetime,
    expires_at: datetime,
    allowed_tenant_ids: tuple[str, ...],
    allowed_user_ids: tuple[str, ...],
) -> NotificationOutbox:
    """Create an idempotent, permission-bound direct-message follow-up task."""

    tenant = str(tenant_id or "").strip()
    recipient = str(recipient_user_id or "").strip()
    trigger = str(trigger_id or "").strip()
    if not tenant or not recipient or not trigger:
        raise ValueError("tenant_id, recipient_user_id, and trigger_id are required")
    tenant_cohort = {str(value).strip() for value in allowed_tenant_ids if str(value).strip()}
    user_cohort = {str(value).strip() for value in allowed_user_ids if str(value).strip()}
    if tenant not in tenant_cohort or recipient not in user_cohort:
        raise PermissionError("recipient is outside the configured case follow-up cohort")
    if expires_at <= now:
        raise ValueError("case follow-up expiry must be in the future")
    try:
        parsed_case_id = UUID(str(case_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("valid case_id is required") from exc

    binding = await session.scalar(
        select(Agent2IdentityBinding).where(
            Agent2IdentityBinding.tenant_id == tenant,
            Agent2IdentityBinding.user_id == recipient,
            Agent2IdentityBinding.active.is_(True),
        )
    )
    case = await session.scalar(
        select(Agent2Case).where(
            Agent2Case.tenant_id == tenant,
            Agent2Case.case_id == parsed_case_id,
        )
    )
    if binding is None or case is None:
        raise PermissionError("active recipient binding and visible case are required")
    allowed_case_ids = {
        str(value)
        for value in (binding.permission_scope_json or {}).get("allowed_case_ids", [])
        if value
    }
    if str(parsed_case_id) not in allowed_case_ids:
        raise PermissionError("recipient is not allowed to access the case")
    if (
        binding.company_id != case.company_id
        or binding.department_id != case.department_id
        or binding.team_id != case.team_id
    ):
        raise PermissionError("recipient and case organization scope do not match")

    followup_id = uuid5(
        NAMESPACE_URL,
        f"case-progress-followup:{tenant}:{parsed_case_id}:{recipient}:{trigger}",
    )
    idempotency_key = f"case-progress-followup:{followup_id}"
    message = {
        **build_case_progress_followup_message(
            case_name=case.case_name,
            case_number=case.case_number,
        ),
        "case_id": str(parsed_case_id),
        "followup_id": str(followup_id),
        "trigger_id": trigger,
        "expires_at": expires_at.isoformat(),
        "content_origin": "robot_followup",
    }
    inserted_id = await session.scalar(
        insert(NotificationOutbox)
        .values(
            notification_id=followup_id,
            tenant_id=tenant,
            candidate_id=None,
            recipient_user_id=recipient,
            channel="dingtalk",
            message_type="case_progress_followup",
            message_json=message,
            idempotency_key=idempotency_key,
            status="pending",
            retry_count=0,
            response_json={},
            dispatch_history_json=[],
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[NotificationOutbox.tenant_id, NotificationOutbox.idempotency_key]
        )
        .returning(NotificationOutbox.notification_id)
    )
    event = await session.get(NotificationOutbox, inserted_id or followup_id)
    if event is None:
        event = await session.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.tenant_id == tenant,
                NotificationOutbox.idempotency_key == idempotency_key,
            )
        )
    if event is None:
        raise RuntimeError("case follow-up outbox idempotency row cannot be loaded")
    await session.flush()
    return event


def active_case_progress_followup_resources(
    events: tuple[NotificationOutbox, ...],
    *,
    allowed_case_ids: tuple[str, ...],
    now: datetime,
) -> tuple[dict[str, str], ...]:
    """Project delivered follow-ups into trusted, permission-filtered cognition context."""

    allowed = {str(value) for value in allowed_case_ids if value}
    resources: list[dict[str, str]] = []
    for event in events:
        payload = dict(event.message_json or {})
        case_id = str(payload.get("case_id") or "").strip()
        if (
            event.message_type != "case_progress_followup"
            or event.status != "sent"
            or not event.external_message_id
            or case_id not in allowed
            or str((event.response_json or {}).get("followup_status") or "") == "completed"
        ):
            continue
        try:
            expires_at = datetime.fromisoformat(str(payload.get("expires_at") or ""))
        except ValueError:
            continue
        if expires_at.tzinfo is None or now >= expires_at:
            continue
        resources.append(
            {
                "notification_id": str(event.notification_id),
                "case_id": case_id,
                "case_name": str(payload.get("case_name") or ""),
                "case_number": str(payload.get("case_number") or ""),
                "expires_at": expires_at.isoformat(),
            }
        )
    return tuple(resources)
