import pytest

from scripts.run_agent2_personal_weekly_brief_74_scope_simulation import (
    run_simulation,
)


@pytest.mark.asyncio
async def test_all_74_formal_targets_are_owner_isolated_without_send() -> None:
    result = await run_simulation()

    assert result["status"] == "PASS"
    assert result["formal_target_count"] == 74
    assert result["child_member_count"] == 70
    assert result["center_member_count"] == 4
    assert result["child_department_count"] == 7
    assert result["own_scope_passed"] == 74
    assert result["cross_scope_rejected"] == 74
    assert result["source_isolation_passed"] == 74
    assert result["outsider_rejected"] is True
    assert result["ambiguous_identity_rejected"] is True
    assert result["ambiguous_context_rejected"] is True
    assert result["send_switch_enabled"] is False
    assert result["transport_calls"] == 0
    assert result["database_accessed"] is False
