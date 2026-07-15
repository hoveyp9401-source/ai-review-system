from app.config import Settings
from app.agent2.case_followup_gate import CaseFollowupEffectGate


def test_case_followup_effect_switches_default_closed_and_have_independent_allowlists():
    settings = Settings(
        database_url="postgresql+asyncpg://user:pass@localhost/test"
    )

    assert settings.case_followup_enabled is False
    assert settings.case_followup_send_enabled is False
    assert settings.case_followup_report_projection_enabled is False
    assert settings.case_followup_user_allowlist == ""
    assert settings.case_followup_tenant_allowlist == ""
    assert settings.case_followup_trigger_allowlist == ""
    assert settings.case_followup_daily_limit > 0


def test_effect_gate_requires_every_scope_and_limit_before_send_or_projection():
    gate = CaseFollowupEffectGate(
        enabled=True,
        send_enabled=True,
        projection_enabled=True,
        tenant_allowlist=("tenant-a",),
        user_allowlist=("user-1",),
        trigger_allowlist=("fixed_cadence",),
        user_daily_limit=3,
        case_daily_limit=1,
    )

    allowed = gate.decide(
        tenant_id="tenant-a", user_id="user-1", case_id="case-1",
        trigger_type="fixed_cadence", case_is_assigned=True,
        user_messages_today=2, case_messages_today=0,
    )
    blocked = gate.decide(
        tenant_id="tenant-a", user_id="user-1", case_id="case-unassigned",
        trigger_type="fixed_cadence", case_is_assigned=False,
        user_messages_today=0, case_messages_today=0,
    )

    assert allowed.create_task is True
    assert allowed.send_message is True
    assert allowed.allow_projection is True
    assert blocked.create_task is False
    assert blocked.reason_code == "case_not_assigned"
