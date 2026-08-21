from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.agent2.personal_weekly_brief_scope import (
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


def test_scope_rejects_ambiguous_conversation_context() -> None:
    roster, bindings, controls, states = _scope()
    states = (*states, states[0])

    with pytest.raises(RuntimeError, match="conversation context"):
        validate_personal_weekly_brief_targets(
            roster=roster,
            bindings=bindings,
            controls=controls,
            conversation_states=states,
            expected_model_name=CANARY_MODEL_NAME,
        )
