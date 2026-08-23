from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select

from app.agent2.business.models import Agent2IdentityBinding
from app.agent2.tool_calling.canary_store import ToolCallCanaryControl
from app.legal_daily_roster import (
    FORMAL_ROSTER_MEMBER_COUNT,
    FormalLegalDailyRoster,
    load_formal_legal_daily_roster,
)
from app.models import WebhookEvent
from app.services.dingtalk import (
    DingTalkPayloadError,
    normalize_dingtalk_conversation_kind,
)


@dataclass(frozen=True)
class PersonalWeeklyBriefTarget:
    tenant_id: str
    internal_user_id: str
    dingtalk_user_id: str
    display_name: str
    conversation_id: str


@dataclass(frozen=True)
class PersonalWeeklyBriefTargetRevalidation:
    valid_targets: dict[str, PersonalWeeklyBriefTarget]
    blocked_reasons: dict[str, str]
    roster_user_ids: tuple[str, ...]


class PersonalWeeklyBriefOverallScopeError(RuntimeError):
    """The latest formal identity set is unsafe for every outbound send."""


def validate_personal_weekly_brief_targets(
    *,
    roster: FormalLegalDailyRoster,
    bindings: tuple[Any, ...] | list[Any],
    controls: tuple[Any, ...] | list[Any],
    conversation_states: tuple[Any, ...] | list[Any],
    expected_model_name: str,
    runtime_tenant_id: str | None = None,
) -> tuple[PersonalWeeklyBriefTarget, ...]:
    """Freeze each formal owner against Xiaolv's observed private chat.

    Owners without one unambiguous provider conversation remain in the formal
    74-person snapshot with an empty conversation id.  Pre-send revalidation
    then blocks only those owners instead of inventing a synthetic chat id.
    """
    if roster.member_count != FORMAL_ROSTER_MEMBER_COUNT:
        raise RuntimeError("personal weekly brief scope must contain exactly 74 members")
    if not expected_model_name.strip():
        raise RuntimeError("personal weekly brief Agent2 model is missing")
    runtime_tenant = str(runtime_tenant_id or roster.tenant_id).strip()
    if not runtime_tenant:
        raise RuntimeError("personal weekly brief runtime tenant is missing")

    binding_by_user = _unique_by(
        bindings,
        key=lambda row: str(getattr(row, "user_id", "")),
        label="identity binding",
    )
    control_by_user = _unique_by(
        controls,
        key=lambda row: str(getattr(row, "user_id", "")),
        label="Agent2 control",
    )
    roster_ids = set(roster.user_ids)
    if set(binding_by_user) != roster_ids:
        raise RuntimeError("personal weekly brief identity binding scope is incomplete")
    if set(control_by_user) != roster_ids:
        raise RuntimeError("personal weekly brief Agent2 control scope is incomplete")
    conversation_ids_by_dingtalk_user = _conversation_ids_by_dingtalk_user(
        conversation_states
    )

    targets: list[PersonalWeeklyBriefTarget] = []
    for member in roster.members:
        binding = binding_by_user[member.user_id]
        if not (
            getattr(binding, "active", False) is True
            and str(getattr(binding, "tenant_id", "")) == runtime_tenant
            and str(getattr(binding, "user_id", "")) == member.user_id
            and str(getattr(binding, "dingtalk_user_id", ""))
            == member.dingtalk_user_id
        ):
            raise RuntimeError("personal weekly brief identity binding mismatch")
        control = control_by_user[member.user_id]
        if not (
            str(getattr(control, "tenant_id", "")) == runtime_tenant
            and getattr(control, "enabled", False) is True
            and getattr(control, "messages_enabled", False) is True
            and str(getattr(control, "runtime", "")) == "canary_execute"
            and str(getattr(control, "model_name", "")) == expected_model_name
        ):
            raise RuntimeError("personal weekly brief Agent2 control mismatch")
        targets.append(
            PersonalWeeklyBriefTarget(
                tenant_id=runtime_tenant,
                internal_user_id=member.user_id,
                dingtalk_user_id=member.dingtalk_user_id,
                display_name=member.user_name,
                conversation_id=_only_conversation_id(
                    conversation_ids_by_dingtalk_user.get(
                        member.dingtalk_user_id,
                        set(),
                    )
                ),
            )
        )
    dingtalk_ids = [target.dingtalk_user_id for target in targets]
    conversations = [
        target.conversation_id for target in targets if target.conversation_id
    ]
    if len(dingtalk_ids) != len(set(dingtalk_ids)) or len(conversations) != len(
        set(conversations)
    ):
        raise RuntimeError("personal weekly brief direct conversation scope is ambiguous")
    return tuple(targets)


def revalidate_personal_weekly_brief_targets(
    *,
    roster: FormalLegalDailyRoster,
    frozen_targets: tuple[PersonalWeeklyBriefTarget, ...],
    bindings: tuple[Any, ...] | list[Any],
    controls: tuple[Any, ...] | list[Any],
    conversation_states: tuple[Any, ...] | list[Any],
    expected_model_name: str,
    runtime_tenant_id: str | None = None,
) -> PersonalWeeklyBriefTargetRevalidation:
    """Recheck frozen identity and private-chat facts immediately before send."""
    runtime_tenant = str(runtime_tenant_id or roster.tenant_id).strip()
    if not runtime_tenant:
        raise RuntimeError("personal weekly brief runtime tenant is missing")
    frozen_by_user = {target.internal_user_id: target for target in frozen_targets}
    if len(frozen_by_user) != FORMAL_ROSTER_MEMBER_COUNT:
        raise PersonalWeeklyBriefOverallScopeError(
            "personal weekly brief frozen target set is not exactly 74"
        )
    roster_ids = set(roster.user_ids)
    if roster.member_count != FORMAL_ROSTER_MEMBER_COUNT or roster_ids != set(
        frozen_by_user
    ):
        raise PersonalWeeklyBriefOverallScopeError(
            "personal weekly brief formal roster set changed after snapshot"
        )

    bindings_by_user = _group_by(bindings, key=lambda row: str(getattr(row, "user_id", "")))
    controls_by_user = _group_by(controls, key=lambda row: str(getattr(row, "user_id", "")))
    conversation_ids_by_dingtalk_user = _conversation_ids_by_dingtalk_user(
        conversation_states
    )
    valid: dict[str, PersonalWeeklyBriefTarget] = {}
    blocked: dict[str, str] = {}
    for member in roster.members:
        user_id = member.user_id
        binding_rows = bindings_by_user.get(user_id, ())
        if len(binding_rows) != 1:
            blocked[user_id] = "identity_binding_not_unique"
            continue
        binding = binding_rows[0]
        if not (
            getattr(binding, "active", False) is True
            and str(getattr(binding, "tenant_id", "")) == runtime_tenant
            and str(getattr(binding, "dingtalk_user_id", ""))
            == member.dingtalk_user_id
        ):
            blocked[user_id] = "identity_binding_changed"
            continue
        control_rows = controls_by_user.get(user_id, ())
        if len(control_rows) != 1:
            blocked[user_id] = "agent2_control_not_unique"
            continue
        control = control_rows[0]
        if not (
            str(getattr(control, "tenant_id", "")) == runtime_tenant
            and getattr(control, "enabled", False) is True
            and getattr(control, "messages_enabled", False) is True
            and str(getattr(control, "runtime", "")) == "canary_execute"
            and str(getattr(control, "model_name", "")) == expected_model_name
        ):
            blocked[user_id] = "agent2_control_changed"
            continue
        conversation_ids = conversation_ids_by_dingtalk_user.get(
            member.dingtalk_user_id,
            set(),
        )
        if not conversation_ids:
            blocked[user_id] = "direct_conversation_unavailable"
            continue
        if len(conversation_ids) != 1:
            blocked[user_id] = "direct_conversation_ambiguous"
            continue
        latest = PersonalWeeklyBriefTarget(
            tenant_id=runtime_tenant,
            internal_user_id=user_id,
            dingtalk_user_id=member.dingtalk_user_id,
            display_name=member.user_name,
            conversation_id=next(iter(conversation_ids)),
        )
        if latest != frozen_by_user[user_id]:
            blocked[user_id] = "target_changed_after_snapshot"
            continue
        valid[user_id] = latest

    dingtalk_ids = [target.dingtalk_user_id for target in valid.values()]
    conversations = [target.conversation_id for target in valid.values()]
    if len(dingtalk_ids) != len(set(dingtalk_ids)) or len(conversations) != len(
        set(conversations)
    ):
        raise PersonalWeeklyBriefOverallScopeError(
            "personal weekly brief latest identity set is ambiguous"
        )
    return PersonalWeeklyBriefTargetRevalidation(
        valid_targets=valid,
        blocked_reasons=blocked,
        roster_user_ids=roster.user_ids,
    )


async def load_personal_weekly_brief_targets(
    session: Any,
    *,
    tenant_id: str,
    roster_tenant_id: str | None = None,
    on_date: date,
    expected_model_name: str,
    robot_code: str,
) -> tuple[PersonalWeeklyBriefTarget, ...]:
    roster = await load_formal_legal_daily_roster(
        session,
        tenant_id=roster_tenant_id or tenant_id,
        on_date=on_date,
    )
    user_ids = roster.user_ids
    bindings = tuple(
        (
            await session.scalars(
                select(Agent2IdentityBinding).where(
                    Agent2IdentityBinding.tenant_id == tenant_id,
                    Agent2IdentityBinding.user_id.in_(user_ids),
                    Agent2IdentityBinding.active.is_(True),
                )
            )
        ).all()
    )
    controls = tuple(
        (
            await session.scalars(
                select(ToolCallCanaryControl).where(
                    ToolCallCanaryControl.tenant_id == tenant_id,
                    ToolCallCanaryControl.user_id.in_(user_ids),
                )
            )
        ).all()
    )
    direct_conversations = await _load_direct_conversation_observations(
        session,
        dingtalk_user_ids=tuple(member.dingtalk_user_id for member in roster.members),
        robot_code=robot_code,
    )
    return validate_personal_weekly_brief_targets(
        roster=roster,
        bindings=bindings,
        controls=controls,
        conversation_states=direct_conversations,
        expected_model_name=expected_model_name,
        runtime_tenant_id=tenant_id,
    )


async def load_personal_weekly_brief_target_revalidation(
    session: Any,
    *,
    tenant_id: str,
    roster_tenant_id: str | None = None,
    on_date: date,
    expected_model_name: str,
    frozen_targets: tuple[PersonalWeeklyBriefTarget, ...],
    robot_code: str,
) -> PersonalWeeklyBriefTargetRevalidation:
    roster = await load_formal_legal_daily_roster(
        session,
        tenant_id=roster_tenant_id or tenant_id,
        on_date=on_date,
    )
    user_ids = roster.user_ids
    bindings = tuple(
        (
            await session.scalars(
                select(Agent2IdentityBinding).where(
                    Agent2IdentityBinding.tenant_id == tenant_id,
                    Agent2IdentityBinding.user_id.in_(user_ids),
                )
            )
        ).all()
    )
    controls = tuple(
        (
            await session.scalars(
                select(ToolCallCanaryControl).where(
                    ToolCallCanaryControl.tenant_id == tenant_id,
                    ToolCallCanaryControl.user_id.in_(user_ids),
                )
            )
        ).all()
    )
    direct_conversations = await _load_direct_conversation_observations(
        session,
        dingtalk_user_ids=tuple(member.dingtalk_user_id for member in roster.members),
        robot_code=robot_code,
    )
    return revalidate_personal_weekly_brief_targets(
        roster=roster,
        frozen_targets=frozen_targets,
        bindings=bindings,
        controls=controls,
        conversation_states=direct_conversations,
        expected_model_name=expected_model_name,
        runtime_tenant_id=tenant_id,
    )


def _unique_by(rows, *, key, label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for row in rows:
        identifier = key(row)
        if not identifier or identifier in result:
            raise RuntimeError(f"personal weekly brief {label} is ambiguous")
        result[identifier] = row
    return result


def _group_by(rows, *, key) -> dict[str, tuple[Any, ...]]:
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(key(row), []).append(row)
    return {identifier: tuple(values) for identifier, values in grouped.items()}


def _conversation_ids_by_dingtalk_user(
    rows: tuple[Any, ...] | list[Any],
) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for row in rows:
        dingtalk_user_id = str(
            getattr(row, "dingtalk_user_id", "") or ""
        ).strip()
        conversation_id = str(
            getattr(row, "conversation_id", "") or ""
        ).strip()
        if not dingtalk_user_id or not conversation_id:
            continue
        if len(dingtalk_user_id) > 128 or len(conversation_id) > 256:
            continue
        grouped.setdefault(dingtalk_user_id, set()).add(conversation_id)
    return grouped


def _only_conversation_id(conversation_ids: set[str]) -> str:
    if len(conversation_ids) != 1:
        return ""
    return next(iter(conversation_ids))


@dataclass(frozen=True)
class _DirectConversationObservation:
    dingtalk_user_id: str
    conversation_id: str


async def _load_direct_conversation_observations(
    session: Any,
    *,
    dingtalk_user_ids: tuple[str, ...],
    robot_code: str,
) -> tuple[_DirectConversationObservation, ...]:
    expected_robot_code = str(robot_code or "").strip()
    if not expected_robot_code:
        raise RuntimeError("personal weekly brief Xiaolv robot identity is missing")
    events = tuple(
        (
            await session.scalars(
                select(WebhookEvent).where(
                    WebhookEvent.platform == "dingtalk",
                    WebhookEvent.dingtalk_user_id.in_(dingtalk_user_ids),
                )
            )
        ).all()
    )
    observations: list[_DirectConversationObservation] = []
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if str(payload.get("robotCode") or "").strip() != expected_robot_code:
            continue
        try:
            conversation_kind = normalize_dingtalk_conversation_kind(
                payload.get("conversationType")
                or payload.get("conversation_type")
            )
        except DingTalkPayloadError:
            continue
        if conversation_kind != "direct":
            continue
        dingtalk_user_id = str(event.dingtalk_user_id or "").strip()
        conversation_id = str(
            payload.get("conversationId")
            or payload.get("conversation_id")
            or payload.get("openConversationId")
            or ""
        ).strip()
        if (
            not dingtalk_user_id
            or len(dingtalk_user_id) > 128
            or not conversation_id
            or len(conversation_id) > 256
        ):
            continue
        observations.append(
            _DirectConversationObservation(
                dingtalk_user_id=dingtalk_user_id,
                conversation_id=conversation_id,
            )
        )
    return tuple(observations)


__all__ = [
    "PersonalWeeklyBriefTarget",
    "PersonalWeeklyBriefTargetRevalidation",
    "PersonalWeeklyBriefOverallScopeError",
    "load_personal_weekly_brief_targets",
    "load_personal_weekly_brief_target_revalidation",
    "revalidate_personal_weekly_brief_targets",
    "validate_personal_weekly_brief_targets",
]
