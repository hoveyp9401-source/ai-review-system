from __future__ import annotations

from app.agent2.weekly_plan_access import (
    WeeklyPlanAccessAction,
    WeeklyPlanAccessPolicy,
)


TENANT_ID = "tenant-internal-001"
USER_ID = "user-internal-042"


def test_weekly_plan_access_is_closed_by_default() -> None:
    decision = WeeklyPlanAccessPolicy().decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )

    assert decision.allowed is False
    assert decision.reason == "weekly_plan_disabled"


def test_enabled_read_still_requires_exact_tenant_and_user_allowlists() -> None:
    closed = WeeklyPlanAccessPolicy(enabled=True).decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )
    allowed = WeeklyPlanAccessPolicy(
        enabled=True,
        tenant_allowlist=frozenset({TENANT_ID}),
        user_allowlist=frozenset({USER_ID}),
    ).decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )

    assert closed.allowed is False
    assert closed.reason == "weekly_plan_tenant_not_allowlisted"
    assert allowed.allowed is True
    assert allowed.reason == "allowed"


def test_tenant_and_user_ids_are_exact_and_names_never_grant_access() -> None:
    policy = WeeklyPlanAccessPolicy(
        enabled=True,
        tenant_allowlist=frozenset({TENANT_ID}),
        user_allowlist=frozenset({USER_ID}),
    )

    wrong_tenant_case = policy.decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID.upper(),
        user_id=USER_ID,
        conversation_kind="direct",
    )
    display_name = policy.decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id="测试用户甲",
        conversation_kind="direct",
    )
    padded_user_id = policy.decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=f" {USER_ID} ",
        conversation_kind="direct",
    )

    assert wrong_tenant_case.reason == "weekly_plan_tenant_not_allowlisted"
    assert display_name.reason == "weekly_plan_user_not_allowlisted"
    assert padded_user_id.reason == "weekly_plan_user_not_allowlisted"


def test_single_user_canary_rejects_more_than_one_tenant_or_user() -> None:
    multi_tenant = WeeklyPlanAccessPolicy(
        enabled=True,
        tenant_allowlist=frozenset({TENANT_ID, "tenant-internal-002"}),
        user_allowlist=frozenset({USER_ID}),
    ).decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )
    multi_user = WeeklyPlanAccessPolicy(
        enabled=True,
        tenant_allowlist=frozenset({TENANT_ID}),
        user_allowlist=frozenset({USER_ID, "user-internal-043"}),
    ).decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )

    assert multi_tenant.reason == "weekly_plan_single_canary_scope_required"
    assert multi_user.reason == "weekly_plan_single_canary_scope_required"


def test_only_an_explicit_direct_conversation_is_permitted() -> None:
    policy = WeeklyPlanAccessPolicy(
        enabled=True,
        tenant_allowlist=frozenset({TENANT_ID}),
        user_allowlist=frozenset({USER_ID}),
    )

    group = policy.decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="group",
    )
    unknown = policy.decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="unknown",
    )
    unexpected = policy.decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="DIRECT",
    )

    assert group.reason == "weekly_plan_direct_conversation_required"
    assert unknown.reason == "weekly_plan_direct_conversation_required"
    assert unexpected.reason == "weekly_plan_direct_conversation_required"


def test_write_has_an_independent_switch_after_base_access_checks() -> None:
    base = WeeklyPlanAccessPolicy(
        enabled=True,
        tenant_allowlist=frozenset({TENANT_ID}),
        user_allowlist=frozenset({USER_ID}),
    )
    enabled = WeeklyPlanAccessPolicy(
        enabled=True,
        write_enabled=True,
        tenant_allowlist=frozenset({TENANT_ID}),
        user_allowlist=frozenset({USER_ID}),
    )

    denied = base.decide(
        action=WeeklyPlanAccessAction.WRITE,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )
    allowed = enabled.decide(
        action=WeeklyPlanAccessAction.WRITE,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )

    assert denied.reason == "weekly_plan_write_disabled"
    assert allowed == type(allowed)(allowed=True, reason="allowed")


def test_send_requires_both_send_switch_and_exact_send_user_allowlist() -> None:
    common = {
        "enabled": True,
        "tenant_allowlist": frozenset({TENANT_ID}),
        "user_allowlist": frozenset({USER_ID}),
    }
    disabled = WeeklyPlanAccessPolicy(
        **common,
        send_user_allowlist=frozenset({USER_ID}),
    ).decide(
        action=WeeklyPlanAccessAction.SEND,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )
    not_send_allowlisted = WeeklyPlanAccessPolicy(
        **common,
        send_enabled=True,
    ).decide(
        action=WeeklyPlanAccessAction.SEND,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )
    display_name_only = WeeklyPlanAccessPolicy(
        **common,
        send_enabled=True,
        send_user_allowlist=frozenset({"测试用户甲"}),
    ).decide(
        action=WeeklyPlanAccessAction.SEND,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )
    allowed = WeeklyPlanAccessPolicy(
        **common,
        send_enabled=True,
        send_user_allowlist=frozenset({USER_ID}),
    ).decide(
        action=WeeklyPlanAccessAction.SEND,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )

    assert disabled.reason == "weekly_plan_send_disabled"
    assert not_send_allowlisted.reason == "weekly_plan_send_user_not_allowlisted"
    assert display_name_only.reason == "weekly_plan_allowlist_invalid"
    assert allowed.allowed is True


def test_unknown_action_fails_closed_instead_of_falling_through_to_read() -> None:
    decision = WeeklyPlanAccessPolicy(
        enabled=True,
        write_enabled=True,
        send_enabled=True,
        tenant_allowlist=frozenset({TENANT_ID}),
        user_allowlist=frozenset({USER_ID}),
        send_user_allowlist=frozenset({USER_ID}),
    ).decide(
        action="delete",  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )

    assert decision.allowed is False
    assert decision.reason == "weekly_plan_action_invalid"


def test_missing_stable_internal_identity_fails_closed() -> None:
    policy = WeeklyPlanAccessPolicy(
        enabled=True,
        tenant_allowlist=frozenset({TENANT_ID}),
        user_allowlist=frozenset({USER_ID}),
    )

    no_tenant = policy.decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id="",
        user_id=USER_ID,
        conversation_kind="direct",
    )
    no_user = policy.decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id="",
        conversation_kind="direct",
    )

    assert no_tenant.reason == "weekly_plan_tenant_id_missing"
    assert no_user.reason == "weekly_plan_user_id_missing"


def test_non_boolean_switch_values_do_not_accidentally_enable_access() -> None:
    policy = WeeklyPlanAccessPolicy(
        enabled="false",  # type: ignore[arg-type]
        write_enabled="true",  # type: ignore[arg-type]
        send_enabled="true",  # type: ignore[arg-type]
        tenant_allowlist=frozenset({TENANT_ID}),
        user_allowlist=frozenset({USER_ID}),
        send_user_allowlist=frozenset({USER_ID}),
    )

    decision = policy.decide(
        action=WeeklyPlanAccessAction.SEND,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )

    assert decision.allowed is False
    assert decision.reason == "weekly_plan_disabled"


def test_string_or_mutable_allowlists_fail_closed_instead_of_using_substrings() -> None:
    string_policy = WeeklyPlanAccessPolicy(
        enabled=True,
        tenant_allowlist=TENANT_ID,  # type: ignore[arg-type]
        user_allowlist=USER_ID,  # type: ignore[arg-type]
    )
    mutable_policy = WeeklyPlanAccessPolicy(
        enabled=True,
        tenant_allowlist={TENANT_ID},  # type: ignore[arg-type]
        user_allowlist={USER_ID},  # type: ignore[arg-type]
    )

    string_decision = string_policy.decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )
    mutable_decision = mutable_policy.decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        conversation_kind="direct",
    )

    assert string_decision.reason == "weekly_plan_allowlist_invalid"
    assert mutable_decision.reason == "weekly_plan_allowlist_invalid"


def test_a_display_name_cannot_be_used_as_an_allowlisted_internal_identity() -> None:
    decision = WeeklyPlanAccessPolicy(
        enabled=True,
        tenant_allowlist=frozenset({TENANT_ID}),
        user_allowlist=frozenset({"测试用户甲"}),
    ).decide(
        action=WeeklyPlanAccessAction.READ,
        tenant_id=TENANT_ID,
        user_id="测试用户甲",
        conversation_kind="direct",
    )

    assert decision.allowed is False
    assert decision.reason == "weekly_plan_allowlist_invalid"
