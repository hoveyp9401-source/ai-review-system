from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from app.agent2.weekly_plan_context import (
    build_trusted_weekly_plan_context,
    next_week_start,
)
from app.agent2.weekly_plan_models import (
    WeeklyPlan,
    WeeklyPlanDay,
    WeeklyPlanItem,
)
from app.agent2.weekly_plan_suggestions import (
    TrustedSourceKind,
    build_trusted_evidence,
    create_suggestion,
    reject_suggestion,
)
from app.agent2.tool_calling.context import (
    CANARY_STATE_NAMESPACE,
    TrustedContext,
    TrustedPrincipal,
)
from uuid import UUID


def test_next_week_start_uses_the_users_local_date_across_year_boundary() -> None:
    now = datetime(2026, 12, 31, 18, 30, tzinfo=timezone.utc)

    assert next_week_start(now, "Asia/Shanghai") == date(2027, 1, 4)


def test_next_week_start_maps_2026_08_13_to_2026_08_17() -> None:
    now = datetime(2026, 8, 13, 9, 30, tzinfo=timezone.utc)

    assert next_week_start(now, "Asia/Shanghai") == date(2026, 8, 17)


def _plan(*, tenant_id: str = "tenant-a", owner_user_id: str = "user-a") -> WeeklyPlan:
    week = date(2026, 8, 17)
    moment = datetime(2026, 8, 13, 9, tzinfo=timezone.utc)
    item = WeeklyPlanItem(
        item_id="item-monday-1",
        original_text="整理项目原始材料",
        source="user_original_message:message-1",
        created_at=moment,
        updated_at=moment,
    )
    return WeeklyPlan(
        plan_id="plan-a",
        batch_id="batch-a",
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        target_week_start=week,
        status="collecting",
        version=3,
        days=tuple(
            WeeklyPlanDay(
                day_id=f"day-{offset}",
                plan_date=week + timedelta(days=offset),
                state=(
                    "planned"
                    if offset == 0
                    else "explicitly_empty"
                    if offset == 1
                    else "unfilled"
                ),
                items=(item,) if offset == 0 else (),
            )
            for offset in range(6)
        ),
        created_at=moment,
        updated_at=moment,
    )


def test_context_preserves_stable_plan_version_and_exact_monday_to_saturday() -> None:
    context = build_trusted_weekly_plan_context(
        plan=_plan(),
        authenticated_tenant_id="tenant-a",
        authenticated_owner_user_id="user-a",
        target_week_start=date(2026, 8, 17),
    )

    assert context.plan_id == "plan-a"
    assert context.batch_id == "batch-a"
    assert context.version == 3
    assert context.status == "collecting"
    assert [day.plan_date for day in context.days] == [
        date(2026, 8, 17) + timedelta(days=offset) for offset in range(6)
    ]
    assert [day.state for day in context.days] == [
        "planned",
        "explicitly_empty",
        "unfilled",
        "unfilled",
        "unfilled",
        "unfilled",
    ]
    assert context.days[0].items[0].original_text == "整理项目原始材料"


@pytest.mark.parametrize(
    ("plan", "tenant_id", "owner_user_id"),
    [
        (_plan(tenant_id="tenant-b"), "tenant-a", "user-a"),
        (_plan(owner_user_id="user-b"), "tenant-a", "user-a"),
    ],
)
def test_context_rejects_a_plan_from_another_tenant_or_person(
    plan: WeeklyPlan,
    tenant_id: str,
    owner_user_id: str,
) -> None:
    with pytest.raises(ValueError):
        build_trusted_weekly_plan_context(
            plan=plan,
            authenticated_tenant_id=tenant_id,
            authenticated_owner_user_id=owner_user_id,
            target_week_start=date(2026, 8, 17),
        )


def test_context_rejects_a_plan_without_exact_monday_to_saturday_days() -> None:
    plan = replace(_plan(), days=_plan().days[:-1])

    with pytest.raises(ValueError, match="Monday-to-Saturday"):
        build_trusted_weekly_plan_context(
            plan=plan,
            authenticated_tenant_id="tenant-a",
            authenticated_owner_user_id="user-a",
            target_week_start=date(2026, 8, 17),
        )


def test_context_rejects_a_different_open_batch_target_week() -> None:
    with pytest.raises(ValueError, match="exact target week"):
        build_trusted_weekly_plan_context(
            plan=_plan(),
            authenticated_tenant_id="tenant-a",
            authenticated_owner_user_id="user-a",
            target_week_start=date(2026, 8, 24),
        )


def test_context_rejects_invalid_versions_and_day_state_item_combinations() -> None:
    with pytest.raises(ValueError):
        build_trusted_weekly_plan_context(
            plan=replace(_plan(), version=-1),
            authenticated_tenant_id="tenant-a",
            authenticated_owner_user_id="user-a",
            target_week_start=date(2026, 8, 17),
        )

    invalid_monday = replace(_plan().days[0], state="explicitly_empty")
    with pytest.raises(ValueError, match="cannot contain items"):
        build_trusted_weekly_plan_context(
            plan=replace(_plan(), days=(invalid_monday, *_plan().days[1:])),
            authenticated_tenant_id="tenant-a",
            authenticated_owner_user_id="user-a",
            target_week_start=date(2026, 8, 17),
        )


def _suggestion(*, owner_user_id: str = "user-a", week: date = date(2026, 8, 17)):
    created_at = datetime(2026, 8, 13, 9, tzinfo=timezone.utc)
    evidence = build_trusted_evidence(
        owner_user_id=owner_user_id,
        source_kind=TrustedSourceKind.USER_ORIGINAL_MESSAGE,
        source_ref="message-27",
        source_version="v4",
        evidence_text="跟进项目甲；这是不应进入上下文的整段聊天 SECRET_CHAT",
    )
    return create_suggestion(
        owner_user_id=owner_user_id,
        target_week_start=week,
        evidence=evidence,
        matter_excerpt="跟进项目甲",
        created_at=created_at,
        expires_at=created_at + timedelta(days=9),
    )


def test_available_suggestion_is_a_separate_candidate_with_bounded_evidence() -> None:
    available = _suggestion()
    rejected = reject_suggestion(
        _suggestion(),
        decision_ref="decision-message-1",
        decided_at=datetime(2026, 8, 14, 9, tzinfo=timezone.utc),
    )

    context = build_trusted_weekly_plan_context(
        plan=_plan(),
        authenticated_tenant_id="tenant-a",
        authenticated_owner_user_id="user-a",
        target_week_start=date(2026, 8, 17),
        suggestions=(available, rejected),
    )
    payload = context.model_payload()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert len(context.suggestions) == 1
    assert context.suggestions[0].source_ref == "message-27"
    assert context.suggestions[0].source_version == "v4"
    assert context.suggestions[0].evidence_sha256 == available.evidence_sha256
    assert context.suggestions[0].evidence_excerpt == "跟进项目甲"
    assert context.suggestions[0].is_formal_plan_item is False
    assert "跟进项目甲" in context.suggestions[0].prompt
    assert [item.original_text for item in context.days[0].items] == [
        "整理项目原始材料"
    ]
    assert "SECRET_CHAT" not in serialized


def test_expired_suggestion_is_not_exposed_as_an_available_candidate() -> None:
    available = _suggestion()

    before_expiry = build_trusted_weekly_plan_context(
        plan=_plan(),
        authenticated_tenant_id="tenant-a",
        authenticated_owner_user_id="user-a",
        target_week_start=date(2026, 8, 17),
        suggestions=(available,),
        as_of=available.expires_at - timedelta(seconds=1),
    )
    at_expiry = build_trusted_weekly_plan_context(
        plan=_plan(),
        authenticated_tenant_id="tenant-a",
        authenticated_owner_user_id="user-a",
        target_week_start=date(2026, 8, 17),
        suggestions=(available,),
        as_of=available.expires_at,
    )

    assert [item.suggestion_id for item in before_expiry.suggestions] == [
        available.suggestion_id
    ]
    assert at_expiry.suggestions == ()


@pytest.mark.parametrize(
    "suggestion",
    [
        _suggestion(owner_user_id="user-b"),
        _suggestion(week=date(2026, 8, 24)),
        reject_suggestion(
            _suggestion(owner_user_id="user-b"),
            decision_ref="decision-message-2",
            decided_at=datetime(2026, 8, 14, 9, tzinfo=timezone.utc),
        ),
    ],
)
def test_context_rejects_a_suggestion_from_another_person_or_target_week(
    suggestion,
) -> None:
    with pytest.raises(ValueError):
        build_trusted_weekly_plan_context(
            plan=_plan(),
            authenticated_tenant_id="tenant-a",
            authenticated_owner_user_id="user-a",
            target_week_start=date(2026, 8, 17),
            suggestions=(suggestion,),
        )


def test_explicit_open_batch_target_is_authoritative_on_its_monday() -> None:
    monday_now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    assert next_week_start(monday_now, "Asia/Shanghai") == date(2026, 8, 24)

    context = build_trusted_weekly_plan_context(
        plan=_plan(),
        authenticated_tenant_id="tenant-a",
        authenticated_owner_user_id="user-a",
        target_week_start=date(2026, 8, 17),
    )

    assert context.target_week_start == date(2026, 8, 17)


def test_main_agent2_context_exposes_only_the_bounded_weekly_plan_payload() -> None:
    weekly = build_trusted_weekly_plan_context(
        plan=_plan(owner_user_id="11111111-1111-4111-8111-111111111111"),
        authenticated_tenant_id="tenant-a",
        authenticated_owner_user_id="11111111-1111-4111-8111-111111111111",
        target_week_start=date(2026, 8, 17),
        suggestions=(_suggestion(owner_user_id="11111111-1111-4111-8111-111111111111"),),
    )
    context = TrustedContext(
        namespace=CANARY_STATE_NAMESPACE,
        now=datetime(2026, 8, 13, 9, tzinfo=timezone.utc),
        principal=TrustedPrincipal(
            tenant_id="tenant-a",
            user_id=UUID("11111111-1111-4111-8111-111111111111"),
            conversation_id="cid-a",
            source_message_id="msg-a",
            timezone="Asia/Shanghai",
            conversation_kind="direct",
        ),
        weekly_plan=weekly,
    )

    payload = context.model_payload()

    assert payload["weekly_plan"]["plan_id"] == weekly.plan_id
    assert "SECRET_CHAT" not in json.dumps(payload, ensure_ascii=False)
