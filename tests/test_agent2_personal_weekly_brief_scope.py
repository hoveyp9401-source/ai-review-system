from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from app.agent2.personal_weekly_brief_scope import (
    revalidate_personal_weekly_brief_targets,
    validate_personal_weekly_brief_targets,
)
from app.agent2.tool_calling.canary_config import CANARY_MODEL_NAME
from app.legal_daily_roster import (
    FormalLegalDailyRoster,
    FormalRosterMember,
)


def _members(count: int = 74) -> tuple[FormalRosterMember, ...]:
    return tuple(
        FormalRosterMember(
            user_id=f"00000000-0000-4000-8000-{index:012d}",
            user_name=f"脱敏用户{index:02d}",
            dingtalk_user_id=f"ding-{index:02d}",
            team_id=f"team-{index % 7}",
            team_code=f"legal-{index % 7}",
            team_name=f"脱敏部门{index % 7}",
            department_name="法务合约中心",
            team_active=True,
            data_complete=True,
        )
        for index in range(count)
    )


def _scope(count: int = 74):
    members = _members(count)
    roster = FormalLegalDailyRoster(
        tenant_id="tenant-a",
        on_date=date(2026, 8, 22),
        members=members,
    )
    bindings = tuple(
        SimpleNamespace(
            tenant_id="tenant-a",
            user_id=member.user_id,
            dingtalk_user_id=member.dingtalk_user_id,
            display_name=member.user_name,
            active=True,
        )
        for member in members
    )
    controls = tuple(
        SimpleNamespace(
            tenant_id="tenant-a",
            user_id=member.user_id,
            enabled=True,
            messages_enabled=True,
            runtime="canary_execute",
            model_name=CANARY_MODEL_NAME,
        )
        for member in members
    )
    states = tuple(
        SimpleNamespace(
            user_key=f"tenant-a:{member.user_id}",
            conversation_id=f"conversation-{index:02d}",
        )
        for index, member in enumerate(members)
    )
    return roster, bindings, controls, states


def test_exact_74_agent2_users_resolve_to_private_self_only_targets() -> None:
    roster, bindings, controls, states = _scope()

    targets = validate_personal_weekly_brief_targets(
        roster=roster,
        bindings=bindings,
        controls=controls,
        conversation_states=states,
        expected_model_name=CANARY_MODEL_NAME,
    )

    assert len(targets) == 74
    assert {target.internal_user_id for target in targets} == set(roster.user_ids)
    assert len({target.dingtalk_user_id for target in targets}) == 74
    assert all(target.tenant_id == "tenant-a" for target in targets)
    assert all(
        target.conversation_id == f"agent2-direct:{target.internal_user_id}"
        for target in targets
    )


def test_formal_roster_and_agent2_runtime_can_use_separate_tenants() -> None:
    roster, bindings, controls, states = _scope()
    roster = FormalLegalDailyRoster(
        tenant_id="formal-roster-tenant",
        on_date=roster.on_date,
        members=roster.members,
    )
    runtime_tenant = "agent2-runtime-tenant"
    bindings = tuple(
        SimpleNamespace(**{**row.__dict__, "tenant_id": runtime_tenant})
        for row in bindings
    )
    controls = tuple(
        SimpleNamespace(**{**row.__dict__, "tenant_id": runtime_tenant})
        for row in controls
    )
    states = tuple(
        SimpleNamespace(
            user_key=f"{runtime_tenant}:{member.user_id}",
            conversation_id=f"runtime-conversation-{index:02d}",
        )
        for index, member in enumerate(roster.members)
    )

    targets = validate_personal_weekly_brief_targets(
        roster=roster,
        bindings=bindings,
        controls=controls,
        conversation_states=states,
        expected_model_name=CANARY_MODEL_NAME,
        runtime_tenant_id=runtime_tenant,
    )
    revalidated = revalidate_personal_weekly_brief_targets(
        roster=roster,
        frozen_targets=targets,
        bindings=bindings,
        controls=controls,
        conversation_states=states,
        expected_model_name=CANARY_MODEL_NAME,
        runtime_tenant_id=runtime_tenant,
    )

    assert len(targets) == 74
    assert all(target.tenant_id == runtime_tenant for target in targets)
    assert len(revalidated.valid_targets) == 74
    assert revalidated.blocked_reasons == {}


def test_scope_rejects_73_members_instead_of_silently_sending_partial_batch() -> None:
    roster, bindings, controls, states = _scope(73)

    with pytest.raises(RuntimeError, match="exactly 74"):
        validate_personal_weekly_brief_targets(
            roster=roster,
            bindings=bindings,
            controls=controls,
            conversation_states=states,
            expected_model_name=CANARY_MODEL_NAME,
        )


def test_scope_rejects_mismatched_dingtalk_identity_before_any_target_is_returned() -> None:
    roster, bindings, controls, states = _scope()
    bindings = (
        SimpleNamespace(
            **{
                **bindings[0].__dict__,
                "dingtalk_user_id": "ding-other-person",
            }
        ),
        *bindings[1:],
    )

    with pytest.raises(RuntimeError, match="identity binding"):
        validate_personal_weekly_brief_targets(
            roster=roster,
            bindings=bindings,
            controls=controls,
            conversation_states=states,
            expected_model_name=CANARY_MODEL_NAME,
        )


@pytest.mark.parametrize(
    "change",
    (
        {"enabled": False},
        {"messages_enabled": False},
        {"runtime": "shadow"},
        {"model_name": "other-model"},
    ),
)
def test_scope_rejects_any_user_not_confirmed_on_current_agent2(change) -> None:
    roster, bindings, controls, states = _scope()
    controls = (
        SimpleNamespace(**{**controls[0].__dict__, **change}),
        *controls[1:],
    )

    with pytest.raises(RuntimeError, match="Agent2 control"):
        validate_personal_weekly_brief_targets(
            roster=roster,
            bindings=bindings,
            controls=controls,
            conversation_states=states,
            expected_model_name=CANARY_MODEL_NAME,
        )


def test_scope_uses_private_direct_context_without_requiring_prior_chat() -> None:
    roster, bindings, controls, states = _scope()
    states = (*states, states[0])

    targets = validate_personal_weekly_brief_targets(
        roster=roster,
        bindings=bindings,
        controls=controls,
        conversation_states=states,
        expected_model_name=CANARY_MODEL_NAME,
    )

    assert len(targets) == 74
    assert targets[0].conversation_id == f"agent2-direct:{roster.members[0].user_id}"


def test_pre_send_revalidation_blocks_only_changed_control_owner() -> None:
    roster, bindings, controls, states = _scope()
    frozen = validate_personal_weekly_brief_targets(
        roster=roster,
        bindings=bindings,
        controls=controls,
        conversation_states=states,
        expected_model_name=CANARY_MODEL_NAME,
    )
    controls = (
        SimpleNamespace(**{**controls[0].__dict__, "messages_enabled": False}),
        *controls[1:],
    )

    result = revalidate_personal_weekly_brief_targets(
        roster=roster,
        frozen_targets=frozen,
        bindings=bindings,
        controls=controls,
        conversation_states=states,
        expected_model_name=CANARY_MODEL_NAME,
    )

    assert result.blocked_reasons == {
        roster.members[0].user_id: "agent2_control_changed"
    }
    assert len(result.valid_targets) == 73


def test_pre_send_revalidation_uses_stable_private_direct_context() -> None:
    roster, bindings, controls, states = _scope()
    frozen = validate_personal_weekly_brief_targets(
        roster=roster,
        bindings=bindings,
        controls=controls,
        conversation_states=states,
        expected_model_name=CANARY_MODEL_NAME,
    )
    states = (
        SimpleNamespace(
            user_key=states[0].user_key,
            conversation_id="conversation-changed-after-0900",
        ),
        *states[1:],
    )

    result = revalidate_personal_weekly_brief_targets(
        roster=roster,
        frozen_targets=frozen,
        bindings=bindings,
        controls=controls,
        conversation_states=states,
        expected_model_name=CANARY_MODEL_NAME,
    )

    assert result.blocked_reasons == {}
    assert len(result.valid_targets) == 74
    assert (
        result.valid_targets[roster.members[0].user_id].conversation_id
        == f"agent2-direct:{roster.members[0].user_id}"
    )


def test_pre_send_revalidation_stops_when_formal_roster_set_changes() -> None:
    roster, bindings, controls, states = _scope()
    frozen = validate_personal_weekly_brief_targets(
        roster=roster,
        bindings=bindings,
        controls=controls,
        conversation_states=states,
        expected_model_name=CANARY_MODEL_NAME,
    )
    changed_member = replace(
        roster.members[0],
        user_id="99999999-9999-4999-8999-999999999999",
        dingtalk_user_id="ding-new-formal-member",
    )
    changed_roster = FormalLegalDailyRoster(
        tenant_id=roster.tenant_id,
        on_date=roster.on_date,
        members=(changed_member, *roster.members[1:]),
    )

    with pytest.raises(RuntimeError, match="formal roster set changed"):
        revalidate_personal_weekly_brief_targets(
            roster=changed_roster,
            frozen_targets=frozen,
            bindings=bindings,
            controls=controls,
            conversation_states=states,
            expected_model_name=CANARY_MODEL_NAME,
        )
